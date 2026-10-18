from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_login import login_required, current_user
from app.utils.permissions import has_permission, require_permission, protect_blueprint
import datetime
import math
from app.services.timeclock_hours import summarize as summarize_hours
from app.services import timeclock_hours as tc
from app.services.db import get_db_connection
from app.services.data_manager import load_config
import logging

timeclock_bp = Blueprint('timeclock', __name__, url_prefix='/timeclock')
# Every /timeclock route: TIMECLOCK_ENABLED + timeclock.view; manager pages
# add @require_permission('timeclock.manage').
protect_blueprint(timeclock_bp, 'timeclock.view',
                  module_flag='TIMECLOCK_ENABLED', module_name='Timeclock')
logger = logging.getLogger(__name__)

def get_current_status(user_id):
    """Get the current active shift/break for the user."""
    conn = get_db_connection()
    try:
        # Check for open shift
        shift = conn.execute('''
            SELECT * FROM time_entries 
            WHERE user_id = ? AND clock_out IS NULL
            ORDER BY created_at DESC LIMIT 1
        ''', (user_id,)).fetchone()
        
        if shift:
            return dict(shift)
        return None
    finally:
        conn.close()

# --- Helpers ---

def get_pay_period_config():
    """Get pay period settings from config or defaults."""
    config = load_config()
    start_str = config.get('TIMECLOCK_PAY_PERIOD_START', '2024-01-01')
    freq = config.get('TIMECLOCK_PAY_PERIOD_TYPE', 'biweekly') 
    return start_str, freq

def _workweek_anchor():
    """Workweeks start on the weekday of the configured pay-period start."""
    start_str, _freq = get_pay_period_config()
    try:
        return datetime.datetime.strptime(start_str, '%Y-%m-%d').date()
    except ValueError:
        return datetime.date(2024, 1, 1)


def _paid_breaks():
    return bool(load_config().get('TIMECLOCK_PAID_BREAKS', True))


# --- Form input -------------------------------------------------------------
# Blank or junk form input is answered with a flash + redirect, never a 500.
# These return None for anything unusable.

# Dates outside these years are junk for a timeclock, and near year 1 / 9999
# the UTC conversion overflows.
_MIN_YEAR, _MAX_YEAR = 1900, 2999
PAY_PERIOD_TYPES = ('weekly', 'biweekly', 'monthly')
RULE_FREQUENCIES = ('weekly', 'biweekly')
MAX_GENERATE_DAYS = 366


def _form_date(value):
    """'YYYY-MM-DD' -> date."""
    try:
        d = datetime.datetime.strptime(str(value or '').strip(), '%Y-%m-%d').date()
    except ValueError:
        return None
    return d if _MIN_YEAR <= d.year <= _MAX_YEAR else None


def _form_time(value):
    """'HH:MM' (or 'HH:MM:SS') -> datetime.time."""
    value = str(value or '').strip()
    for fmt in ('%H:%M', '%H:%M:%S'):
        try:
            return datetime.datetime.strptime(value, fmt).time()
        except ValueError:
            pass
    return None


def _form_local_datetime(value):
    """Business-local 'YYYY-MM-DD[T ]HH:MM[:SS]' -> stored UTC string."""
    try:
        utc = tc.local_to_utc(value)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    if not _MIN_YEAR <= utc.year <= _MAX_YEAR:
        return None
    return utc.strftime(tc.FMT)


def _existing_user_id(conn, value):
    """Form user id -> int id of an existing user."""
    try:
        user_id = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    row = conn.execute('SELECT 1 FROM users WHERE id = ?', (user_id,)).fetchone()
    return user_id if row else None


def get_current_pay_period():
    """Calculate current pay period start/end based on config."""
    start_str, freq = get_pay_period_config()
    try:
        base_start = datetime.datetime.strptime(start_str, '%Y-%m-%d').date()
        today = tc.local_today()
        
        if freq == 'weekly':
            days_since = (today - base_start).days
            period_idx = days_since // 7
            start = base_start + datetime.timedelta(days=period_idx * 7)
            end = start + datetime.timedelta(days=6)
            return start, end
            
        elif freq == 'biweekly':
            days_since = (today - base_start).days
            period_idx = days_since // 14
            start = base_start + datetime.timedelta(days=period_idx * 14)
            end = start + datetime.timedelta(days=13)
            return start, end
            
        elif freq == 'monthly':
            # Simplified monthly (1st to last day)
            # This ignores base_start day-of-month potentially, usually monthly implies cal month
            # Or we can treat base_start as the 'anchor' day. 
            # Let's assume Calendar Month for simplicity unless custom start day required.
            # If base is 2024-01-15, then period is 15th to 14th?
            # User request: "Start of a pay period... automatic... 2 weeks... monthly"
            # Let's stick to standard intervals from anchor.
            # LOGIC: Pay periods are variable length in monthly if we follow cal months. 
            # If fixed 30 days, that shifts. 
            # Let's try to stick to "Same Day of Month".
            
            # Find period start
            # If today is 5th, and start is 15th. 
            # Previous period start: 15th of last month.
            # Current period start: 15th of this month (if today >= 15th).
            
            anchor_day = base_start.day
            curr_month_start = datetime.date(today.year, today.month, anchor_day)
            
            # Handle short months (e.g. anchor 31, today Feb) - logic gets complex.
            # Fallback: strict calendar months from 1st if type is monthly?
            # Or simplified 4-weeks?
            # Let's implement Calendar Month logic if frequency is monthly, ignoring anchor DAY?
            # OR simple 30 day blocks? Monthly usually means "Calendar Month".
            
            # Let's assume Calendar Month if monthly.
            start = datetime.date(today.year, today.month, 1)
            # End is last day
            next_month = start.replace(day=28) + datetime.timedelta(days=4)
            end = next_month - datetime.timedelta(days=next_month.day)
            return start, end
            
    except Exception as e:
        logger.error(f"Pay Period Calc Error: {e}")
        # Fallback to current week
        today = tc.local_today()
        start = today - datetime.timedelta(days=today.weekday())
        end = start + datetime.timedelta(days=6)
        return start, end
    return tc.local_today(), tc.local_today()

def get_pay_periods_history(limit=12):
    """Generate list of past N pay periods."""
    start_str, freq = get_pay_period_config()
    current_start, current_end = get_current_pay_period()
    
    periods = []
    curr = current_start
    
    for i in range(limit):
        # Go backwards
        if freq == 'weekly':
            s = curr - datetime.timedelta(days=7)
            e = s + datetime.timedelta(days=6)
        elif freq == 'biweekly':
            s = curr - datetime.timedelta(days=14)
            e = s + datetime.timedelta(days=13)
        else: # Monthly
            # Back 1 month
            # First day of previous month
            s = (curr.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
            # Last day of prev month
            e = curr.replace(day=1) - datetime.timedelta(days=1)
            
        periods.append({'start': s, 'end': e, 'label': f"{s.strftime('%b %d')} - {e.strftime('%b %d')}"})
        curr = s
        
    return periods

@timeclock_bp.route('/', strict_slashes=False)
def index():
    config = load_config()
    status = get_current_status(current_user.id)
    
    # Calculate duration and format time
    duration = None
    clock_in_formatted = None
    alert = None
    
    if status:
        try:
            # Stored UTC; show local time, measure real elapsed time
            clock_in_formatted = tc.to_local(status['clock_in']).strftime('%I:%M %p')
            diff = tc.utc_now() - tc.as_utc(status['clock_in'])
            # Format duration
            hours = int(diff.total_seconds()) // 3600
            minutes = (int(diff.total_seconds()) % 3600) // 60
            duration = f"{hours}h {minutes}m"
        except Exception as e:
            logger.error(f"Date parse error: {e}")
            duration = "Unknown"
            clock_in_formatted = status['clock_in']

            clock_in_formatted = status['clock_in']
    
    
    # Check Schedules
    next_shift = None
    week_shifts = []
    pto_balance = 0.0
    
    conn = get_db_connection()
    try:
        now_dt = tc.local_now()
        now_utc = tc.utc_now_str()
        
        # Next Shift (First one starting after NOW or later today/future)
        # Use datetime string comparison but ensure we get shifts from now onwards
        ns = conn.execute('''
            SELECT * FROM scheduled_shifts 
            WHERE user_id = ? AND datetime(start_time) > datetime(?) 
            ORDER BY start_time ASC LIMIT 1
        ''', (current_user.id, now_utc)).fetchone()
        
        if ns:
            ns_start = tc.to_local(ns['start_time'])
            ns_end = tc.to_local(ns['end_time'])
            
            # Helper for "Today/Tomorrow"
            day_label = ns_start.strftime('%A')
            if ns_start.date() == now_dt.date(): day_label = "Today"
            elif ns_start.date() == (now_dt.date() + datetime.timedelta(days=1)): day_label = "Tomorrow"
            
            next_shift = {
                'start': ns_start.strftime('%I:%M %p'),
                'end': ns_end.strftime('%I:%M %p'),
                'day': day_label,
                'date': ns_start.strftime('%b %d')
            }
            
        # Current Week (Next 7 days)
        week_end = tc.local_dates_to_utc_range(now_dt.date() + datetime.timedelta(days=7))[1]
        ws_rows = conn.execute('''
            SELECT * FROM scheduled_shifts 
            WHERE user_id = ? AND datetime(start_time) > datetime(?) AND datetime(start_time) <= datetime(?)
            ORDER BY start_time ASC
        ''', (current_user.id, now_utc, week_end)).fetchall()
        
        for r in ws_rows:
            s = tc.to_local(r['start_time'])
            e = tc.to_local(r['end_time'])
            week_shifts.append({
                'day': s.strftime('%a'),
                'date': s.strftime('%m/%d'),
                'range': f"{s.strftime('%I:%M%p')} - {e.strftime('%I:%M%p')}"
            })
            
        # PTO Balance
        pto_row = conn.execute('SELECT balance_hours FROM user_pto_balances WHERE user_id = ?', (current_user.id,)).fetchone()
        pto_balance = pto_row['balance_hours'] if pto_row else 0.0
        
        # Period Hours Calculation
        period_start, period_end = get_current_pay_period()
        fulltime_hours = float(config.get('TIMECLOCK_FULLTIME_HOURS', 40))
        
        period_entries = conn.execute('''
            SELECT type, clock_in, clock_out FROM time_entries
            WHERE user_id = ? AND type IN ('shift', 'break')
            AND clock_in >= ? AND clock_in <= ?
        ''', (current_user.id, *tc.local_dates_to_utc_range(period_start, period_end))).fetchall()

        totals = summarize_hours(period_entries, _workweek_anchor(), fulltime_hours, _paid_breaks())
        period_hours = round(totals['regular'] + totals['overtime'], 2)
        ot_hours = totals['overtime']
        reg_hours = totals['regular']
            
    except Exception as e:
        logger.error(f"Schedule Check Error: {e}")
        period_hours = 0.0
        ot_hours = 0.0
        reg_hours = 0.0
        fulltime_hours = 40.0
    finally:
        conn.close()

    return render_template('timeclock/index.html', 
                         status=status, 
                         duration=duration, 
                         clock_in_time=clock_in_formatted, 
                         alert=alert,
                         next_shift=next_shift,
                         week_shifts=week_shifts,
                         pto_balance=pto_balance,
                         period_hours=period_hours,
                         ot_hours=ot_hours,
                         reg_hours=reg_hours,
                         fulltime_hours=fulltime_hours)


@timeclock_bp.app_context_processor
def inject_status():
    if not current_user.is_authenticated:
        return {}
    # Light query for sidebar
    # We might want to cache this or optimize if too heavy
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT type FROM time_entries WHERE user_id = ? AND clock_out IS NULL', (current_user.id,)).fetchone()
        if row:
            return {'timeclock_status': row['type']}
        return {'timeclock_status': 'out'}
    except:
        return {'timeclock_status': 'out'}
    finally:
        conn.close()

@timeclock_bp.route('/manager/shifts', methods=['GET', 'POST'])
@require_permission('timeclock.manage')
def shifts():
    conn = get_db_connection()
    try:
        # Self-healing
        try:
            conn.execute('SELECT 1 FROM recurring_shift_rules LIMIT 1')
        except:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS recurring_shift_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    day_of_week INTEGER NOT NULL, -- 0=Mon, 6=Sun
                    start_time TEXT NOT NULL,     -- "09:00"
                    end_time TEXT NOT NULL,       -- "17:00"
                    frequency TEXT DEFAULT 'weekly',
                    reference_date DATE,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                );
            ''')
            conn.commit()

        users = conn.execute("SELECT id, username FROM users ORDER BY username").fetchall()
        
        if request.method == 'POST':
            # Create Shift (Existing Logic)
            user_id = request.form.get('user_id')
            start_time = request.form.get('start_time')
            end_time = request.form.get('end_time')
            
            user_id = _existing_user_id(conn, user_id)
            if user_id is None:
                flash("Please choose an employee.")
                return redirect(url_for('timeclock.shifts'))

            # datetime-local inputs are business-local; store UTC
            start_clean = _form_local_datetime(start_time)
            end_clean = _form_local_datetime(end_time)
            if not start_clean or not end_clean:
                flash("Please enter a valid start and end time.")
                return redirect(url_for('timeclock.shifts', selected_user=user_id))
            if end_clean <= start_clean:  # same fixed-width UTC format: text order is time order
                flash("The shift must end after it starts.")
                return redirect(url_for('timeclock.shifts', selected_user=user_id))

            conn.execute('INSERT INTO scheduled_shifts (user_id, start_time, end_time) VALUES (?, ?, ?)', 
                        (user_id, start_clean, end_clean))
            conn.commit()
            flash("Shift Assigned")
            return redirect(url_for('timeclock.shifts', selected_user=user_id))
            
        # GET List
        today_start_utc = tc.local_dates_to_utc_range(tc.local_today())[0]
        
        # One-time Shifts
        shift_rows = conn.execute('''
            SELECT s.*, u.username 
            FROM scheduled_shifts s
            JOIN users u ON s.user_id = u.id
            WHERE s.start_time >= ?
            ORDER BY s.start_time ASC
        ''', (today_start_utc,)).fetchall()
        
        shifts = []
        for r in shift_rows:
            s = dict(r)
            dt = tc.to_local(s['start_time'])
            s['date_fmt'] = dt.strftime('%A, %b %d')
            # Manual formatting to prevent platform-specific zero-stripping (Fix "5:1" issue)
            s_hr = dt.strftime('%I')
            s_min = dt.strftime('%M')
            s_ampm = dt.strftime('%p')
            
            e_dt = tc.to_local(s['end_time'])
            e_hr = e_dt.strftime('%I')
            e_min = e_dt.strftime('%M')
            e_ampm = e_dt.strftime('%p')
            
            # Format: 05:15 PM - 09:00 PM
            s['time_range'] = f"{s_hr}:{s_min} {s_ampm} - {e_hr}:{e_min} {e_ampm}"
            shifts.append(s)
            
        # Recurring Rules
        rule_rows = conn.execute('''
            SELECT r.*, u.username 
            FROM recurring_shift_rules r
            JOIN users u ON r.user_id = u.id
            ORDER BY r.day_of_week ASC, r.start_time ASC
        ''').fetchall()
        
        rules = []
        days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        for r in rule_rows:
            rule = dict(r)
            dow = rule['day_of_week']
            rule['day_name'] = days[dow] if isinstance(dow, int) and 0 <= dow < 7 else '?'
            rules.append(rule)
            
        active_tab = request.args.get('tab', 'shifts')
        selected_user = request.args.get('selected_user', '')
            
        return render_template('timeclock/shifts.html', shifts=shifts, rules=rules, users=users, active_tab=active_tab, selected_user=selected_user)
    finally:
        conn.close()

@timeclock_bp.route('/manager/shift/delete/<int:shift_id>', methods=['POST'])
@require_permission('timeclock.manage')
def delete_shift(shift_id):
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM scheduled_shifts WHERE id = ?', (shift_id,))
        conn.commit()
        flash("Shift deleted.")
    except Exception as e:
        logger.error(f"Delete Shift Error: {e}")
        flash("Error deleting shift.")
    finally:
        conn.close()
    return redirect(url_for('timeclock.shifts'))

def _shift_on_local_date(conn, user_id, local_date):
    """First scheduled shift starting on a business-local calendar date."""
    start_utc, end_utc = tc.local_dates_to_utc_range(local_date)
    return conn.execute('''
        SELECT start_time, end_time FROM scheduled_shifts
        WHERE user_id = ? AND start_time >= ? AND start_time <= ?
        ORDER BY start_time LIMIT 1
    ''', (user_id, start_utc, end_utc)).fetchone()


def _open_entry(conn, user_id):
    return conn.execute('SELECT id, type FROM time_entries WHERE user_id = ? AND clock_out IS NULL '
                        'ORDER BY clock_in DESC LIMIT 1', (user_id,)).fetchone()


def _clock_transition(expected_open, close_type, open_type, ok_msg, wrong_state_msg):
    """Run one clock state change atomically.

    expected_open: None (must be clocked out), 'shift' or 'break' (the open
    entry's required type). Closes the open entry if close_type is set and
    opens a new `open_type` entry if set. The check and the writes share one
    write lock (BEGIN IMMEDIATE), so a double-tap cannot create two open
    shifts.
    """
    conn = get_db_connection()
    conn.isolation_level = None
    try:
        conn.execute('BEGIN IMMEDIATE')
        current = _open_entry(conn, current_user.id)
        current_type = current['type'] if current else None
        if current_type != expected_open:
            conn.execute('ROLLBACK')
            flash(wrong_state_msg)
            return redirect(url_for('timeclock.index'))

        now = tc.utc_now_str()
        if close_type:
            conn.execute('UPDATE time_entries SET clock_out = ? WHERE id = ?', (now, current['id']))
        if open_type:
            conn.execute('INSERT INTO time_entries (user_id, type, clock_in) VALUES (?, ?, ?)',
                         (current_user.id, open_type, now))
        conn.execute('COMMIT')
        flash(ok_msg)
    except Exception as e:
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        logger.error(f"Timeclock error ({ok_msg}): {e}")
        flash("Timeclock error, please try again.")
    finally:
        conn.close()
    return redirect(url_for('timeclock.index'))


@timeclock_bp.route('/clock_in', methods=['POST'])
def clock_in():
    return _clock_transition(None, None, 'shift', "Clocked In Successfully",
                             "You are already clocked in!")


@timeclock_bp.route('/clock_out', methods=['POST'])
def clock_out():
    conn = get_db_connection()
    try:
        current = _open_entry(conn, current_user.id)
    finally:
        conn.close()
    if not current:
        flash("You are not clocked in.")
        return redirect(url_for('timeclock.index'))
    # Clocking out ends whatever is open (a shift, or a break)
    return _clock_transition(current['type'], current['type'], None, "Clocked Out Successfully",
                             "Your clock status changed; please try again.")


@timeclock_bp.route('/break_start', methods=['POST'])
def break_start():
    # A break ends the open shift entry and opens a 'break' entry; ending the
    # break closes it and opens a new shift entry.
    return _clock_transition('shift', 'shift', 'break', "Break Started",
                             "You need to be clocked in to start a break.")


@timeclock_bp.route('/break_end', methods=['POST'])
def break_end():
    return _clock_transition('break', 'break', 'shift', "Break Ended - Back to Work",
                             "You are not on a break.")

@timeclock_bp.route('/manager/recurring/add', methods=['POST'])
@require_permission('timeclock.manage')
def add_recurring_rule():
    back = redirect(url_for('timeclock.shifts', tab='recurring'))
    conn = get_db_connection()
    try:
        user_id = _existing_user_id(conn, request.form.get('user_id'))
        try:
            day = int(request.form.get('day_of_week', ''))
        except ValueError:
            day = None
        start_t = _form_time(request.form.get('start_time'))
        end_t = _form_time(request.form.get('end_time'))
        freq = request.form.get('frequency', 'weekly')
        ref_date = (request.form.get('reference_date') or '').strip() # Only for biweekly

        if user_id is None:
            flash("Please choose an employee.")
            return back
        if day not in range(7):
            flash("Please choose a day of the week.")
            return back
        if not start_t or not end_t:
            flash("Please enter a valid start and end time.")
            return back
        if end_t <= start_t:
            flash("The shift must end after it starts.")
            return back
        if freq not in RULE_FREQUENCIES:
            flash("Please choose weekly or bi-weekly.")
            return back
        if ref_date and not _form_date(ref_date):
            flash("Please enter a valid reference date.")
            return back
        start, end = start_t.strftime('%H:%M'), end_t.strftime('%H:%M')

        if not ref_date:
            ref_date = tc.local_today().strftime('%Y-%m-%d')

        conn.execute('''
            INSERT INTO recurring_shift_rules (user_id, day_of_week, start_time, end_time, frequency, reference_date)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (user_id, day, start, end, freq, ref_date))
        conn.commit()
        flash("Recurring rule added.")
    except Exception as e:
        flash(f"Error: {e}")
    finally:
        conn.close()
    return redirect(url_for('timeclock.shifts', tab='recurring'))

@timeclock_bp.route('/manager/recurring/delete/<int:rule_id>', methods=['POST'])
@require_permission('timeclock.manage')
def delete_recurring_rule(rule_id):
    conn = get_db_connection()
    conn.execute('DELETE FROM recurring_shift_rules WHERE id = ?', (rule_id,))
    conn.commit()
    conn.close()
    flash("Rule deleted.")
    return redirect(url_for('timeclock.shifts', tab='recurring'))

@timeclock_bp.route('/manager/generate_schedule', methods=['POST'])
@require_permission('timeclock.manage')
def generate_schedule():
    start_day = _form_date(request.form.get('start_date'))
    end_day = _form_date(request.form.get('end_date'))
    if not start_day or not end_day:
        flash("Please choose a valid start and end date.")
        return redirect(url_for('timeclock.shifts'))
    if end_day < start_day:
        flash("The end date must be on or after the start date.")
        return redirect(url_for('timeclock.shifts'))
    if (end_day - start_day).days >= MAX_GENERATE_DAYS:
        flash(f"Generate at most {MAX_GENERATE_DAYS} days at a time.")
        return redirect(url_for('timeclock.shifts'))

    conn = get_db_connection()
    try:
        start_date = datetime.datetime.combine(start_day, datetime.time())
        end_date = datetime.datetime.combine(end_day, datetime.time())

        rules = conn.execute('SELECT * FROM recurring_shift_rules').fetchall()
        
        generated_count = 0
        current = start_date
        while current <= end_date:
            day_of_week = current.weekday() # 0=Mon
            
            for rule in rules:
                if rule['day_of_week'] == day_of_week:
                    should_gen = False
                    if rule['frequency'] == 'weekly':
                        should_gen = True
                    elif rule['frequency'] == 'biweekly':
                        # Check logic
                        ref = datetime.datetime.strptime(rule['reference_date'], '%Y-%m-%d')
                        # Calculate weeks diff.
                        # Simple approach: same "parity" of week number?
                        # Better: (current - ref).days // 7 is even?
                        diff_days = (current - ref).days
                        weeks = diff_days // 7
                        if weeks % 2 == 0:
                            should_gen = True
                            
                    if should_gen:
                        # Construct Start/End DT
                        # Rule times are local wall-clock; store UTC for that date's offset
                        s_dt = tc.local_to_utc_str(f"{current.strftime('%Y-%m-%d')} {rule['start_time']}")
                        e_dt = tc.local_to_utc_str(f"{current.strftime('%Y-%m-%d')} {rule['end_time']}")
                        
                        # Check duplicate
                        exists = conn.execute('''
                            SELECT 1 FROM scheduled_shifts 
                            WHERE user_id = ? AND start_time = ?
                        ''', (rule['user_id'], s_dt)).fetchone()
                        
                        if not exists:
                            conn.execute('INSERT INTO scheduled_shifts (user_id, start_time, end_time) VALUES (?, ?, ?)',
                                        (rule['user_id'], s_dt, e_dt))
                            generated_count += 1
            
            current += datetime.timedelta(days=1)
            
        conn.commit()
        flash(f"Generated {generated_count} shifts.")
    except Exception as e:
        logger.error(f"Gen Error: {e}")
        flash(f"Error: {e}")
    finally:
        conn.close()
    return redirect(url_for('timeclock.shifts'))


@timeclock_bp.route('/manager')
@require_permission('timeclock.manage')
def manager_dashboard():
    conn = get_db_connection()
    try:
        # Self-healing: Ensure table exists
        try:
            conn.execute('SELECT 1 FROM scheduled_shifts LIMIT 1')
        except:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS scheduled_shifts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    start_time DATETIME NOT NULL,
                    end_time DATETIME NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                );
            ''')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_shifts_user_start ON scheduled_shifts(user_id, start_time);')
            
            # PTO Tables
            conn.execute('''
                CREATE TABLE IF NOT EXISTS user_pto_balances (
                    user_id INTEGER PRIMARY KEY,
                    balance_hours REAL DEFAULT 0.0,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                );
            ''')
            
            conn.execute('''
                CREATE TABLE IF NOT EXISTS pto_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    request_type TEXT NOT NULL, -- 'full', 'partial'
                    date DATE NOT NULL,
                    start_time TIME, -- For partial
                    end_time TIME,   -- For partial
                    hours_requested REAL NOT NULL,
                    status TEXT DEFAULT 'pending', -- 'pending', 'approved', 'denied'
                    admin_notes TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (id)
                );
            ''')
            
            conn.commit()

        # Active Entries (Clocked In or Break)
        rows = conn.execute('''
            SELECT t.*, u.username 
            FROM time_entries t
            JOIN users u ON t.user_id = u.id
            WHERE t.clock_out IS NULL
            ORDER BY t.clock_in DESC
        ''').fetchall()
        
        active_entries = []
        active_cnt = 0
        break_cnt = 0
        
        for r in rows:
            entry = dict(r)
            # Duration
            try:
                diff = tc.utc_now() - tc.as_utc(entry['clock_in'])
                hours = int(diff.total_seconds()) // 3600
                minutes = (int(diff.total_seconds()) % 3600) // 60
                entry['duration'] = f"{hours}h {minutes}m"
                # Format start time (local)
                entry['clock_in'] = tc.to_local(entry['clock_in']).strftime('%I:%M %p')
            except:
                entry['duration'] = "Err"
            
            active_entries.append(entry)
            if entry['type'] == 'shift':
                active_cnt += 1
            else:
                break_cnt += 1
                
        # Total Today (Unique Users worked today)
        today_cnt = conn.execute('''
            SELECT COUNT(DISTINCT user_id) as c 
            FROM time_entries 
            WHERE clock_in >= ? AND clock_in <= ?
        ''', tc.local_dates_to_utc_range(tc.local_today())).fetchone()['c']
        
        # Get all users for Force Clock In dropdown
        all_users = conn.execute('SELECT id, username FROM users ORDER BY username').fetchall()
        
        return render_template('timeclock/manager.html', 
                             active_entries=active_entries,
                             active_count=active_cnt,
                             break_count=break_cnt,
                             today_count=today_cnt,
                             all_users=all_users,
                             config=load_config())
    finally:
        conn.close()

@timeclock_bp.route('/manager/force_clock_out/<int:user_id>', methods=['POST'])
@require_permission('timeclock.manage')
def force_clock_out(user_id):
    conn = get_db_connection()
    try:
        now = tc.utc_now_str()
        cur = conn.execute('''
            UPDATE time_entries 
            SET clock_out = ?, notes = 'Force clock out by Admin'
            WHERE user_id = ? AND clock_out IS NULL
        ''', (now, user_id))
        conn.commit()
        flash("User force clocked out." if cur.rowcount else "User is not clocked in.")
    except Exception as e:
        logger.error(f"Force Clock Out Error: {e}")
        flash("Error performing force clock out.")
    finally:
        conn.close()
    return redirect(url_for('timeclock.manager_dashboard'))

@timeclock_bp.route('/manager/force_clock_in/<int:user_id>', methods=['POST'])
@require_permission('timeclock.manage')
def force_clock_in(user_id):
    conn = get_db_connection()
    try:
        if _existing_user_id(conn, user_id) is None:
            flash("User not found.")
            return redirect(url_for('timeclock.manager_dashboard'))
        # Check if user is already clocked in
        existing = conn.execute('SELECT id FROM time_entries WHERE user_id = ? AND clock_out IS NULL', 
                              (user_id,)).fetchone()
        if existing:
            flash("User is already clocked in!")
            return redirect(url_for('timeclock.manager_dashboard'))
            
        now = tc.utc_now_str()
        conn.execute('''
            INSERT INTO time_entries (user_id, type, clock_in, notes)
            VALUES (?, 'shift', ?, 'Force clock in by Admin')
        ''', (user_id, now))
        conn.commit()
        flash("User force clocked in.")
    except Exception as e:
        logger.error(f"Force Clock In Error: {e}")
        flash("Error performing force clock in.")
    finally:
        conn.close()
    return redirect(url_for('timeclock.manager_dashboard'))

@timeclock_bp.route('/manager/export')
@require_permission('timeclock.manage')
def export_csv():
    import csv
    import io
    from flask import Response
    
    conn = get_db_connection()
    try:
        # Default: Last 30 days
        limit_date = tc.local_dates_to_utc_range(tc.local_today() - datetime.timedelta(days=30))[0]
        
        rows = conn.execute('''
            SELECT t.*, u.username 
            FROM time_entries t
            JOIN users u ON t.user_id = u.id
            WHERE t.clock_in >= ?
            ORDER BY t.clock_in DESC
        ''', (limit_date,)).fetchall()
        
        si = io.StringIO()
        cw = csv.writer(si)
        cw.writerow(['Employee', 'Date', 'Type', 'Time In', 'Time Out', 'Duration', 'Notes'])
        
        for r in rows:
            entry = dict(r)
            
            # Format Dates (stored UTC -> local for people; durations are real elapsed time)
            c_in = tc.to_local(entry['clock_in'])
            date_str = c_in.strftime('%Y-%m-%d')
            time_in = c_in.strftime('%I:%M %p')
            
            time_out = ""
            duration = ""
            
            if entry['clock_out']:
                time_out = tc.to_local(entry['clock_out']).strftime('%I:%M %p')
                
                diff = tc.as_utc(entry['clock_out']) - tc.as_utc(entry['clock_in'])
                hours = int(diff.total_seconds()) // 3600
                minutes = (int(diff.total_seconds()) % 3600) // 60
                duration = f"{hours}h {minutes}m"
            else:
                time_out = "Active"
                diff = tc.utc_now() - tc.as_utc(entry['clock_in'])
                hours = int(diff.total_seconds()) // 3600
                minutes = (int(diff.total_seconds()) % 3600) // 60
                duration = f"{hours}h {minutes}m (Running)"
            
            cw.writerow([
                entry['username'],
                date_str,
                entry['type'].upper(),
                time_in,
                time_out,
                duration,
                entry['notes'] or ''
            ])
            
        output = si.getvalue()
        filename = f"timesheets_{tc.local_today().strftime('%Y%m%d')}.csv"
        
        return Response(
            output,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment;filename={filename}"}
        )
    except Exception as e:
        logger.error(f"Export Error: {e}")
        flash("Error exporting data")
        return redirect(url_for('timeclock.manager_dashboard'))
    finally:
        conn.close()

@timeclock_bp.route('/manager/timesheets')
@require_permission('timeclock.manage')
def timesheets():
    conn = get_db_connection()
    try:
        users = conn.execute("SELECT id, username FROM users ORDER BY username").fetchall()
        
        # Filters
        date_from = request.args.get('date_from')
        date_to = request.args.get('date_to')
        user_filter = request.args.get('user_filter')
        
        query = '''
            SELECT t.*, u.username, e.username as editor_name
            FROM time_entries t
            JOIN users u ON t.user_id = u.id
            LEFT JOIN users e ON t.edited_by = e.id
            WHERE 1=1
        '''
        params = []
        
        day_from = _form_date(date_from[:10]) if date_from else None
        day_to = _form_date(date_to[:10]) if date_to else None
        if (date_from and not day_from) or (date_to and not day_to):
            flash("Invalid date filter.")
            return redirect(url_for('timeclock.timesheets'))
        if day_from and day_to and day_to < day_from:
            flash("The end date must be on or after the start date.")
            return redirect(url_for('timeclock.timesheets'))
        if day_from:
            query += " AND t.clock_in >= ?"
            params.append(tc.local_dates_to_utc_range(day_from)[0])
        if day_to:
            query += " AND t.clock_in <= ?"
            params.append(tc.local_dates_to_utc_range(day_to)[1])
        if user_filter:
            try:
                user_filter = int(user_filter)
            except ValueError:
                flash("Invalid employee filter.")
                return redirect(url_for('timeclock.timesheets'))
            query += " AND t.user_id = ?"
            params.append(user_filter)
            
        query += " ORDER BY t.clock_in DESC LIMIT 100"
        
        rows = conn.execute(query, params).fetchall()
        
        processed_rows = []
        for r in rows:
            entry = dict(r)
            # Duration Calc
            try:
                start = tc.to_local(entry['clock_in'])
                # Manual format to ensure minutes are zero-padded (windows strftime issue).
                # 'MM/DD/YYYY h:mm AM': the template shows [:10] as the date, [11:] as the time.
                h = start.strftime('%I').lstrip('0') or '12' # optional: remove leading zero from hour
                m = start.strftime('%M')
                p = start.strftime('%p')
                entry['clock_in_fmt'] = f"{start.strftime('%m/%d/%Y')} {h}:{m} {p}"
                
                if entry['clock_out']:
                    end = tc.to_local(entry['clock_out'])
                    h_end = end.strftime('%I').lstrip('0') or '12'
                    m_end = end.strftime('%M')
                    p_end = end.strftime('%p')
                    entry['clock_out_fmt'] = f"{end.strftime('%m/%d/%Y')} {h_end}:{m_end} {p_end}"
                    diff = tc.as_utc(entry['clock_out']) - tc.as_utc(entry['clock_in'])
                    hours = int(diff.total_seconds()) // 3600
                    minutes = (int(diff.total_seconds()) % 3600) // 60
                    entry['duration'] = f"{hours}h {minutes}m"
                else:
                    entry['clock_out_fmt'] = "Active"
                    diff = tc.utc_now() - tc.as_utc(entry['clock_in'])
                    hours = int(diff.total_seconds()) // 3600
                    minutes = (int(diff.total_seconds()) % 3600) // 60
                    entry['duration'] = f"{hours}h {minutes}m (Run)"
            except Exception as e:
                entry['duration'] = "Err"
                entry['clock_in_fmt'] = str(entry.get('clock_in', ''))
                entry['clock_out_fmt'] = str(entry.get('clock_out', ''))
            
            processed_rows.append(entry)
            
        return render_template('timeclock/timesheets.html', entries=processed_rows, users=users)
    finally:
        conn.close()

@timeclock_bp.route('/manager/settings', methods=['POST'])
@require_permission('timeclock.manage')
def update_settings():
    # Validated through the settings registry; fields missing from the post
    # are left alone (a partial post used to blank the pay-period settings).
    from app.services import settings as settings_store
    updates = {}
    for field, key in (('pay_period_start', 'TIMECLOCK_PAY_PERIOD_START'),
                       ('pay_period_type', 'TIMECLOCK_PAY_PERIOD_TYPE'),
                       ('fulltime_hours', 'TIMECLOCK_FULLTIME_HOURS')):
        value = (request.form.get(field) or '').strip()
        if value:
            updates[key] = value
    period_type = updates.get('TIMECLOCK_PAY_PERIOD_TYPE')
    if period_type is not None and period_type not in PAY_PERIOD_TYPES:
        flash(f"Not saved: Pay period type must be one of: {', '.join(PAY_PERIOD_TYPES)}")
        return redirect(url_for('timeclock.manager_dashboard'))
    try:
        if updates:
            settings_store.set_many(updates, user=current_user.username)
        flash("Settings updated.")
    except settings_store.SettingsError as e:
        flash(f"Not saved: {e}")
    return redirect(url_for('timeclock.manager_dashboard'))

@timeclock_bp.route('/request_pto', methods=['GET', 'POST'])
def request_pto():
    config = load_config()
    min_shift = float(config.get('TIMECLOCK_MIN_SHIFT_DURATION', 4.0)) # Default 4 hours
    
    conn = get_db_connection()
    try:
        # Self-healing: Ensure PTO tables exist
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_pto_balances (
                user_id INTEGER PRIMARY KEY,
                balance_hours REAL DEFAULT 0.0,
                FOREIGN KEY (user_id) REFERENCES users (id)
            );
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS pto_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                request_type TEXT NOT NULL,
                date DATE NOT NULL,
                start_time TIME,
                end_time TIME,
                hours_requested REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                admin_notes TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            );
        ''')
        conn.commit()
        
        # Get Balance
        row = conn.execute("SELECT balance_hours FROM user_pto_balances WHERE user_id = ?", (current_user.id,)).fetchone()
        balance = row['balance_hours'] if row and row['balance_hours'] is not None else 0.0

        if request.method == 'POST':
            req_type = request.form.get('request_type') # 'full', 'partial'
            date_str = request.form.get('date')
            start_time = request.form.get('start_time')
            end_time = request.form.get('end_time')
            notes = request.form.get('notes')

            if req_type not in ('full', 'partial'):
                flash("Please choose a full or partial shift.")
                return redirect(url_for('timeclock.request_pto'))
            day = _form_date(date_str)
            if not day:
                flash("Please choose a valid date.")
                return redirect(url_for('timeclock.request_pto'))
            date_str = day.isoformat()

            hours_requested = 0.0

            if req_type == 'full':
                # Get Shift Duration for that day
                # Find Shift
                shift = _shift_on_local_date(conn, current_user.id, day)

                if not shift:
                    flash("No scheduled shift found on that date to request off.")
                    return redirect(url_for('timeclock.request_pto'))

                diff = (tc.as_utc(shift['end_time']) - tc.as_utc(shift['start_time'])).total_seconds() / 3600
                hours_requested = round(diff, 2)

            elif req_type == 'partial':
                # Calc requested hours
                s_time, e_time = _form_time(start_time), _form_time(end_time)
                s_utc = s_time and _form_local_datetime(f"{date_str} {s_time.strftime('%H:%M')}")
                e_utc = e_time and _form_local_datetime(f"{date_str} {e_time.strftime('%H:%M')}")
                if not s_utc or not e_utc:
                    flash("Please enter a valid start and end time.")
                    return redirect(url_for('timeclock.request_pto'))
                start_time, end_time = s_time.strftime('%H:%M'), e_time.strftime('%H:%M')
                s_t, e_t = tc.as_utc(s_utc), tc.as_utc(e_utc)

                hours_requested = round((e_t - s_t).total_seconds() / 3600, 2)
                if hours_requested <= 0:
                    flash("The end time must be after the start time.")
                    return redirect(url_for('timeclock.request_pto'))

                # Check Minimum Shift Rule
                # Find original shift
                shift = _shift_on_local_date(conn, current_user.id, date_str)
                
                if shift:
                    orig_s = tc.as_utc(shift['start_time'])
                    orig_e = tc.as_utc(shift['end_time'])
                    total_dur = (orig_e - orig_s).total_seconds() / 3600
                    
                    remaining = total_dur - hours_requested
                    if remaining < min_shift and remaining > 0: # If remaining is 0, it's basically a full shift...
                        flash(f"Partial PTO denied. Remaining shift must be at least {min_shift} hours.")
                        return redirect(url_for('timeclock.request_pto'))
            
            # Check Balance
            if hours_requested > balance:
                flash(f"Insufficient PTO balance. You have {balance} hours.")
                return redirect(url_for('timeclock.request_pto'))
                
            conn.execute('''
                INSERT INTO pto_requests (user_id, request_type, date, start_time, end_time, hours_requested, status, admin_notes)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            ''', (current_user.id, req_type, date_str, start_time, end_time, hours_requested, notes))
            conn.commit()
            flash("PTO Request Sent.")
            return redirect(url_for('timeclock.index'))
            
        today_str = tc.local_today().strftime('%Y-%m-%d')
        return render_template('timeclock/request_pto.html', balance=balance, today_str=today_str)
    finally:
        conn.close()

@timeclock_bp.route('/manager/pto')
@require_permission('timeclock.manage')
def manager_pto():
    conn = get_db_connection()
    try:
        # Self-healing: Ensure PTO tables exist
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_pto_balances (
                user_id INTEGER PRIMARY KEY,
                balance_hours REAL DEFAULT 0.0,
                FOREIGN KEY (user_id) REFERENCES users (id)
            );
        ''')
        
        conn.execute('''
            CREATE TABLE IF NOT EXISTS pto_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                request_type TEXT NOT NULL,
                date DATE NOT NULL,
                start_time TIME,
                end_time TIME,
                hours_requested REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                admin_notes TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users (id)
            );
        ''')
        conn.commit()
        
        pending = conn.execute('''
            SELECT r.*, u.username 
            FROM pto_requests r
            JOIN users u ON r.user_id = u.id
            WHERE r.status = 'pending'
            ORDER BY r.date ASC
        ''').fetchall()
        
        history = conn.execute('''
            SELECT r.*, u.username 
            FROM pto_requests r
            JOIN users u ON r.user_id = u.id
            WHERE r.status != 'pending'
            ORDER BY r.date DESC LIMIT 50
        ''').fetchall()
        
        # Also need logic to set balances? 
        users = conn.execute('''
            SELECT u.id, u.username, 
                   COALESCE((SELECT balance_hours FROM user_pto_balances WHERE user_id = u.id), 0) as balance_hours
            FROM users u
            ORDER BY u.username
        ''').fetchall()
        
        return render_template('timeclock/manager_pto.html', pending=pending, history=history, users=users)
    finally:
        conn.close()

@timeclock_bp.route('/manager/pto/update_balance', methods=['POST'])
@require_permission('timeclock.manage')
def update_pto_balance():
    conn = get_db_connection()
    try:
        user_id = _existing_user_id(conn, request.form.get('user_id'))
        if user_id is None:
            flash("Employee not found.")
            return redirect(url_for('timeclock.manager_pto'))
        try:
            amount = float(request.form.get('amount', ''))
        except ValueError:
            amount = None
        if amount is None or not math.isfinite(amount):
            flash("Please enter the balance as a number of hours.")
            return redirect(url_for('timeclock.manager_pto'))

        # Upsert
        exists = conn.execute('SELECT 1 FROM user_pto_balances WHERE user_id = ?', (user_id,)).fetchone()
        if exists:
            conn.execute('UPDATE user_pto_balances SET balance_hours = ? WHERE user_id = ?', (amount, user_id))
        else:
            conn.execute('INSERT INTO user_pto_balances (user_id, balance_hours) VALUES (?, ?)', (user_id, amount))
        conn.commit()
        flash("Balance updated.")
    finally:
        conn.close()
    return redirect(url_for('timeclock.manager_pto'))

@timeclock_bp.route('/manager/pto/action/<int:req_id>', methods=['POST'])
@require_permission('timeclock.manage')
def pto_action(req_id):
    action = request.form.get('action') # 'approve', 'deny'
    if action not in ('approve', 'deny'):
        flash("Unknown action.")
        return redirect(url_for('timeclock.manager_pto'))
    conn = get_db_connection()
    try:
        req = conn.execute('SELECT * FROM pto_requests WHERE id = ?', (req_id,)).fetchone()
        if not req:
            flash("Request not found")
            return redirect(url_for('timeclock.manager_pto'))
        if req['status'] != 'pending':
            # Approving twice would deduct the balance twice
            flash(f"That request was already {req['status']}.")
            return redirect(url_for('timeclock.manager_pto'))

        if action == 'deny':
            conn.execute("UPDATE pto_requests SET status='denied' WHERE id=?", (req_id,))
            conn.commit()
            flash("Request Denied.")

        elif action == 'approve':
            # 1. Deduct Balance
            hrs = req['hours_requested']
            user_id = req['user_id']

            # Check balance again
            bal_row = conn.execute("SELECT balance_hours FROM user_pto_balances WHERE user_id = ?", (user_id,)).fetchone()
            curr_bal = bal_row['balance_hours'] if bal_row and bal_row['balance_hours'] is not None else 0.0

            if curr_bal < hrs:
                flash(f"Cannot approve: User has {curr_bal} hours, needs {hrs}.")
                return redirect(url_for('timeclock.manager_pto'))

            # 2. Log Time Entry?
            # Design decision: Log a 'pto' type entry so it shows on export?
            # Or just update schedule?
            # User requirement: "Export... included pto?" (Implied)
            # Let's create a 'pto' time_entry for visualization.

            # Format times (checked before anything is written)
            # If partial, use start/end. If full, use shift start/end.
            day = _form_date(req['date'])
            if not day:
                flash("Cannot approve: the request has no valid date. Deny it and ask for a new one.")
                return redirect(url_for('timeclock.manager_pto'))
            entry = None
            if req['request_type'] == 'full':
                # Fetch shift again
                shift = _shift_on_local_date(conn, user_id, day)
                if shift:
                    # already UTC
                    entry = (shift['start_time'], shift['end_time'], 'PTO Approved')
            else:
                # Partial
                # pto_requests keeps the local date and wall-clock times; time_entries is UTC
                s_dt = _form_local_datetime(f"{day} {req['start_time']}")
                e_dt = _form_local_datetime(f"{day} {req['end_time']}")
                if not s_dt or not e_dt or e_dt <= s_dt:
                    flash("Cannot approve: the request has no valid times. Deny it and ask for a new one.")
                    return redirect(url_for('timeclock.manager_pto'))
                entry = (s_dt, e_dt, 'Partial PTO Approved')

            new_bal = curr_bal - hrs
            conn.execute('UPDATE user_pto_balances SET balance_hours = ? WHERE user_id = ?', (new_bal, user_id))
            if entry:
                # Insert Time Entry
                conn.execute('''
                    INSERT INTO time_entries (user_id, type, clock_in, clock_out, notes)
                    VALUES (?, 'pto', ?, ?, ?)
                ''', (user_id, *entry))

            conn.execute("UPDATE pto_requests SET status='approved' WHERE id=?", (req_id,))
            conn.commit()
            flash("PTO Approved & Logged.")

    finally:
        conn.close()
    return redirect(url_for('timeclock.manager_pto'))


@timeclock_bp.route('/manager/timesheet/edit/<int:entry_id>', methods=['GET', 'POST'])
@require_permission('timeclock.manage')
def edit_entry(entry_id):
    conn = get_db_connection()
    try:
        if request.method == 'POST':
            # Handle Update
            clock_in = request.form.get('clock_in') # Expected: YYYY-MM-DDTHH:MM
            clock_out = request.form.get('clock_out') # Expected: YYYY-MM-DDTHH:MM
            notes = request.form.get('notes')

            old = conn.execute('SELECT * FROM time_entries WHERE id = ?', (entry_id,)).fetchone()
            if not old:
                flash("Entry not found")
                return redirect(url_for('timeclock.timesheets'))

            # datetime-local inputs return business-local "2023-11-01T14:30";
            # store UTC 'YYYY-MM-DD HH:MM:SS' (DST offset of that date).
            c_in_clean = _form_local_datetime(clock_in)
            c_out_clean = _form_local_datetime(clock_out) if (clock_out or '').strip() else None
            if not c_in_clean or ((clock_out or '').strip() and not c_out_clean):
                flash("Please enter a valid clock-in (and clock-out) time.")
                return redirect(url_for('timeclock.edit_entry', entry_id=entry_id))
            if c_out_clean and c_out_clean <= c_in_clean:  # fixed-width UTC text: text order is time order
                flash("Clock-out must be after clock-in.")
                return redirect(url_for('timeclock.edit_entry', entry_id=entry_id))

            # Keep a record of what the entry said before the edit
            import json
            conn.execute('''
                INSERT INTO pos_audit_log (action, user_id, user_name, target_type, target_id, details, ip_address)
                VALUES ('timeclock_edit', ?, ?, 'time_entry', ?, ?, ?)
            ''', (current_user.id, current_user.username, str(entry_id), json.dumps({
                'employee_id': old['user_id'],
                'before': {'clock_in': old['clock_in'], 'clock_out': old['clock_out'], 'notes': old['notes']},
                'after': {'clock_in': c_in_clean, 'clock_out': c_out_clean, 'notes': notes},
            }), request.remote_addr))

            conn.execute('''
                UPDATE time_entries 
                SET clock_in = ?, clock_out = ?, notes = ?, edited_by = ?
                WHERE id = ?
            ''', (c_in_clean, c_out_clean, notes, current_user.id, entry_id))
            conn.commit()
            flash("Entry updated successfully.")
            return redirect(url_for('timeclock.timesheets'))
            
        # GET: Show Form
        entry = conn.execute('SELECT * FROM time_entries WHERE id = ?', (entry_id,)).fetchone()
        if not entry:
            flash("Entry not found")
            return redirect(url_for('timeclock.timesheets'))
            
        entry = dict(entry)
        # Format for datetime-local input (YYYY-MM-DDTHH:MM)
        # DB is YYYY-MM-DD HH:MM:SS
        # DB is YYYY-MM-DD HH:MM:SS (or datetime object)
        # Safely convert to string first
        # Stored UTC -> local for the datetime-local inputs
        c_in_local = tc.to_local(entry['clock_in'])
        c_out_local = tc.to_local(entry['clock_out'])
        
        entry['clock_in_val'] = c_in_local.strftime('%Y-%m-%dT%H:%M') if c_in_local else ''
        entry['clock_out_val'] = c_out_local.strftime('%Y-%m-%dT%H:%M') if c_out_local else ''
        
        user = conn.execute('SELECT username FROM users WHERE id = ?', (entry['user_id'],)).fetchone()
        username = user['username'] if user else "Unknown User (Deleted)"
        
        return render_template('timeclock/entry_edit.html', entry=entry, username=username)
    finally:
        conn.close()

@timeclock_bp.route('/manager/timesheet/delete/<int:entry_id>', methods=['POST'])
@require_permission('timeclock.manage')
def delete_entry(entry_id):
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM time_entries WHERE id=?', (entry_id,))
        conn.commit()
        flash("Entry deleted.")
    except Exception as e:
        logger.error(f"Error deleting entry: {e}")
        flash("Error deleting entry.")
    finally:
        conn.close()
    
    return redirect(url_for('timeclock.timesheets'))

@timeclock_bp.route('/manager/export/custom', methods=['GET'])
@require_permission('timeclock.manage')
def export_custom_form():
    conn = get_db_connection()
    try:
        users = conn.execute("SELECT id, username FROM users ORDER BY username").fetchall()
    finally:
        conn.close()
    
    # Get current pay period
    current_start, current_end = get_current_pay_period()
    current_period = {
        'start': current_start,
        'end': current_end,
        'label': f"📌 Current: {current_start.strftime('%b %d')} - {current_end.strftime('%b %d')}"
    }
    
    # Past periods
    past_periods = get_pay_periods_history(limit=24)
    
    # Combine: current first, then past
    periods = [current_period] + past_periods
    return render_template('timeclock/export_custom.html', users=users, periods=periods)

@timeclock_bp.route('/manager/export/report', methods=['POST'])
@require_permission('timeclock.manage')
def export_report():
    selected_users = request.form.getlist('user_ids')
    period_str = request.form.get('period') # "YYYY-MM-DD|YYYY-MM-DD" or "custom"
    
    if period_str == 'custom':
        start_date = _form_date(request.form.get('custom_start'))
        end_date = _form_date(request.form.get('custom_end'))
    else:
        parts = (period_str or '').split('|')
        start_date, end_date = (_form_date(parts[0]), _form_date(parts[1])) if len(parts) == 2 else (None, None)
    if not start_date or not end_date:
        flash("Please choose a valid date range.")
        return redirect(url_for('timeclock.export_custom_form'))
    if end_date < start_date:
        flash("The end date must be on or after the start date.")
        return redirect(url_for('timeclock.export_custom_form'))

    if period_str == 'custom':
        date_label = f"{start_date} to {end_date}"
    else:
        # Format label
        date_label = f"{start_date.strftime('%b %d, %Y')} - {end_date.strftime('%b %d, %Y')}"

    conn = get_db_connection()
    try:
        # Only ids of existing users (also bounds the IN list below)
        known_ids = {r['id'] for r in conn.execute('SELECT id FROM users').fetchall()}
        user_ids = []
        for raw in selected_users:
            try:
                uid = int(raw)
            except ValueError:
                continue
            if uid in known_ids and uid not in user_ids:
                user_ids.append(uid)
        if selected_users and not user_ids:
            flash("Please choose valid employees.")
            return redirect(url_for('timeclock.export_custom_form'))

        # Build Query
        query = '''
            SELECT t.*, u.username, u.id as u_id
            FROM time_entries t
            JOIN users u ON t.user_id = u.id
            WHERE t.clock_in >= ? AND t.clock_in <= ?
        '''
        params = list(tc.local_dates_to_utc_range(start_date, end_date))

        if user_ids:
            placeholders = ','.join(['?'] * len(user_ids))
            query += f" AND t.user_id IN ({placeholders})"
            params.extend(user_ids)
            
        query += " ORDER BY u.username, t.clock_in ASC"
        
        rows = conn.execute(query, params).fetchall()
        
        # Structure Data: Group by User -> List of entries
        # And calculate totals
        report_data = {}
        
        for r in rows:
            entry = dict(r)
            uid = entry['u_id']
            uname = entry['username']
            
            if uid not in report_data:
                report_data[uid] = {
                    'name': uname,
                    'entries': [],
                    'total_hours': 0.0,
                    'reg_hours': 0.0,
                    'pto_hours': 0.0,
                    'ot_hours': 0.0
                }
            
            # Duration
            try:
                start = tc.to_local(entry['clock_in'])
                # Time format (local)
                entry['date_fmt'] = start.strftime('%m/%d/%Y')
                entry['in_fmt'] = start.strftime('%I:%M %p')
                
                hours = 0.0
                
                if entry['clock_out']:
                    entry['out_fmt'] = tc.to_local(entry['clock_out']).strftime('%I:%M %p')
                    diff = (tc.as_utc(entry['clock_out']) - tc.as_utc(entry['clock_in'])).total_seconds()
                    hours = diff / 3600
                else:
                    entry['out_fmt'] = "Active"
                    diff = (tc.utc_now() - tc.as_utc(entry['clock_in'])).total_seconds()
                    hours = diff / 3600
                    
                entry['hours'] = round(hours, 2)
                report_data[uid]['total_hours'] += hours
                report_data[uid]['entries'].append(entry)
                
                if entry['type'] == 'pto':
                     report_data[uid]['pto_hours'] += hours
                else:
                     report_data[uid]['reg_hours'] += hours
                     
            except Exception as e:
                logger.error(f"Report entry error: {e}")
                
        # Totals and overtime per workweek (see app/services/timeclock_hours.py)
        fulltime_hours = float(load_config().get('TIMECLOCK_FULLTIME_HOURS', 40))
        anchor, paid_breaks = _workweek_anchor(), _paid_breaks()
        for uid in report_data:
            totals = summarize_hours(report_data[uid]['entries'], anchor, fulltime_hours, paid_breaks)
            report_data[uid]['reg_hours'] = totals['regular']
            report_data[uid]['ot_hours'] = totals['overtime']
            report_data[uid]['pto_hours'] = totals['pto']
            report_data[uid]['total_hours'] = totals['total']
            
        return render_template('timeclock/report_print.html', 
                             report_data=report_data, 
                             date_label=date_label,
                             generated_at=tc.local_now().strftime('%Y-%m-%d %I:%M %p'))
                             
    finally:
        conn.close()

