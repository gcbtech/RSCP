from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_login import login_required, current_user
from app.utils.permissions import has_permission
import datetime
from app.services.db import get_db_connection
from app.services.data_manager import load_config
import logging

timeclock_bp = Blueprint('timeclock', __name__, url_prefix='/timeclock')
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

def get_current_pay_period():
    """Calculate current pay period start/end based on config."""
    start_str, freq = get_pay_period_config()
    try:
        base_start = datetime.datetime.strptime(start_str, '%Y-%m-%d').date()
        today = datetime.datetime.now().date()
        
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
        today = datetime.datetime.now().date()
        start = today - datetime.timedelta(days=today.weekday())
        end = start + datetime.timedelta(days=6)
        return start, end
    return datetime.datetime.now().date(), datetime.datetime.now().date()

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
@login_required
def index():
    config = load_config()
    if not config.get('TIMECLOCK_ENABLED', False):
        flash("Timeclock module is disabled.")
        return redirect(url_for('main.index'))
        
    status = get_current_status(current_user.id)
    
    # Calculate duration and format time
    duration = None
    clock_in_formatted = None
    alert = None
    
    if status:
        try:
            # Handle string format from DB
            clock_in_dt = datetime.datetime.strptime(status['clock_in'], '%Y-%m-%d %H:%M:%S')
            clock_in_formatted = clock_in_dt.strftime('%I:%M %p')
            
            now = datetime.datetime.now()
            diff = now - clock_in_dt
            # Format duration
            hours = diff.seconds // 3600
            minutes = (diff.seconds % 3600) // 60
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
        now_dt = datetime.datetime.now()
        today = now_dt.strftime('%Y-%m-%d')
        
        # Next Shift (First one starting after NOW or later today/future)
        # Use datetime string comparison but ensure we get shifts from now onwards
        ns = conn.execute('''
            SELECT * FROM scheduled_shifts 
            WHERE user_id = ? AND datetime(start_time) > datetime(?) 
            ORDER BY start_time ASC LIMIT 1
        ''', (current_user.id, now_dt.strftime('%Y-%m-%d %H:%M:%S'))).fetchone()
        
        if ns:
            ns_start = datetime.datetime.strptime(ns['start_time'], '%Y-%m-%d %H:%M:%S')
            ns_end = datetime.datetime.strptime(ns['end_time'], '%Y-%m-%d %H:%M:%S')
            
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
        week_end = (now_dt + datetime.timedelta(days=7)).strftime('%Y-%m-%d 23:59:59')
        ws_rows = conn.execute('''
            SELECT * FROM scheduled_shifts 
            WHERE user_id = ? AND datetime(start_time) > datetime(?) AND datetime(start_time) <= datetime(?)
            ORDER BY start_time ASC
        ''', (current_user.id, now_dt.strftime('%Y-%m-%d %H:%M:%S'), week_end)).fetchall()
        
        for r in ws_rows:
            s = datetime.datetime.strptime(r['start_time'], '%Y-%m-%d %H:%M:%S')
            e = datetime.datetime.strptime(r['end_time'], '%Y-%m-%d %H:%M:%S')
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
            SELECT clock_in, clock_out FROM time_entries
            WHERE user_id = ? AND type = 'shift'
            AND date(clock_in) >= ? AND date(clock_in) <= ?
        ''', (current_user.id, period_start.strftime('%Y-%m-%d'), period_end.strftime('%Y-%m-%d'))).fetchall()
        
        period_hours = 0.0
        for entry in period_entries:
            try:
                c_in = datetime.datetime.strptime(entry['clock_in'], '%Y-%m-%d %H:%M:%S')
                if entry['clock_out']:
                    c_out = datetime.datetime.strptime(entry['clock_out'], '%Y-%m-%d %H:%M:%S')
                else:
                    c_out = datetime.datetime.now()
                period_hours += (c_out - c_in).total_seconds() / 3600
            except:
                pass
                
        period_hours = round(period_hours, 2)
        ot_hours = round(max(0, period_hours - fulltime_hours), 2)
        reg_hours = round(min(period_hours, fulltime_hours), 2)
            
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
@login_required
def shifts():
    if not has_permission(current_user, 'timeclock.manage'):
        return redirect(url_for('timeclock.index'))
    
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
            
            start_clean = start_time.replace('T', ' ') if start_time else None
            end_clean = end_time.replace('T', ' ') if end_time else None
            
            if start_clean and len(start_clean) == 16: start_clean += ':00'
            if end_clean and len(end_clean) == 16: end_clean += ':00'
            
            conn.execute('INSERT INTO scheduled_shifts (user_id, start_time, end_time) VALUES (?, ?, ?)', 
                        (user_id, start_clean, end_clean))
            conn.commit()
            flash("Shift Assigned")
            return redirect(url_for('timeclock.shifts', selected_user=user_id))
            
        # GET List
        today = datetime.datetime.now().strftime('%Y-%m-%d')
        
        # One-time Shifts
        shift_rows = conn.execute('''
            SELECT s.*, u.username 
            FROM scheduled_shifts s
            JOIN users u ON s.user_id = u.id
            WHERE date(s.start_time) >= ?
            ORDER BY s.start_time ASC
        ''', (today,)).fetchall()
        
        shifts = []
        for r in shift_rows:
            s = dict(r)
            dt = datetime.datetime.strptime(s['start_time'], '%Y-%m-%d %H:%M:%S')
            s['date_fmt'] = dt.strftime('%A, %b %d')
            # Manual formatting to prevent platform-specific zero-stripping (Fix "5:1" issue)
            s_hr = dt.strftime('%I')
            s_min = dt.strftime('%M')
            s_ampm = dt.strftime('%p')
            
            e_dt = datetime.datetime.strptime(s['end_time'], '%Y-%m-%d %H:%M:%S')
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
            rule['day_name'] = days[rule['day_of_week']]
            rules.append(rule)
            
        active_tab = request.args.get('tab', 'shifts')
        selected_user = request.args.get('selected_user', '')
            
        return render_template('timeclock/shifts.html', shifts=shifts, rules=rules, users=users, active_tab=active_tab, selected_user=selected_user)
    finally:
        conn.close()

@timeclock_bp.route('/manager/shift/delete/<int:shift_id>', methods=['POST'])
@login_required
def delete_shift(shift_id):
    if not has_permission(current_user, 'timeclock.manage'):
        flash("Access Denied")
        return redirect(url_for('timeclock.index'))
        
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

@timeclock_bp.route('/clock_in', methods=['POST'])
@login_required
def clock_in():
    conn = get_db_connection()
    try:
        # Verify not already clocked in
        existing = conn.execute('SELECT id FROM time_entries WHERE user_id = ? AND clock_out IS NULL', 
                              (current_user.id,)).fetchone()
        if existing:
            flash("You are already clocked in!")
            return redirect(url_for('timeclock.index'))
            
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('''
            INSERT INTO time_entries (user_id, type, clock_in)
            VALUES (?, 'shift', ?)
        ''', (current_user.id, now))
        conn.commit()
        flash("Clocked In Successfully")
    except Exception as e:
        logger.error(f"Clock In Error: {e}")
        flash("Error clocking in.")
    finally:
        conn.close()
    return redirect(url_for('timeclock.index'))

@timeclock_bp.route('/clock_out', methods=['POST'])
@login_required
def clock_out():
    conn = get_db_connection()
    try:
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('''
            UPDATE time_entries 
            SET clock_out = ? 
            WHERE user_id = ? AND clock_out IS NULL
        ''', (now, current_user.id))
        conn.commit()
        flash("Clocked Out Successfully")
    except Exception as e:
        logger.error(f"Clock Out Error: {e}")
        flash("Error clocking out.")
    finally:
        conn.close()
    return redirect(url_for('timeclock.index'))

@timeclock_bp.route('/break_start', methods=['POST'])
@login_required
def break_start():
    # To start a break, we essentially PAUSE the shift? 
    # OR we treat break as a separate entry type?
    # Design doc said: "Active Break: A record where clock_out is NULL and type is 'break'."
    # BUT we also need to end the 'shift' entry? Or do we nest them?
    # Simpler: End 'shift', Start 'break'. 
    # When break ends: End 'break', Start 'shift'.
    
    conn = get_db_connection()
    try:
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # 1. End current shift
        conn.execute('''
            UPDATE time_entries 
            SET clock_out = ? 
            WHERE user_id = ? AND type = 'shift' AND clock_out IS NULL
        ''', (now, current_user.id))
        
        # 2. Start break
        conn.execute('''
            INSERT INTO time_entries (user_id, type, clock_in)
            VALUES (?, 'break', ?)
        ''', (current_user.id, now))
        
        conn.commit()
        flash("Break Started")
    except Exception as e:
        logger.error(f"Break Start Error: {e}")
        flash("Error starting break")
    finally:
        conn.close()
    return redirect(url_for('timeclock.index'))

@timeclock_bp.route('/break_end', methods=['POST'])
@login_required
def break_end():
    conn = get_db_connection()
    try:
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        # 1. End break
        conn.execute('''
            UPDATE time_entries 
            SET clock_out = ? 
            WHERE user_id = ? AND type = 'break' AND clock_out IS NULL
        ''', (now, current_user.id))
        
        # 2. Start shift (resume)
        conn.execute('''
            INSERT INTO time_entries (user_id, type, clock_in)
            VALUES (?, 'shift', ?)
        ''', (current_user.id, now))
        
        conn.commit()
        flash("Break Ended - Back to Work")
    except Exception as e:
        logger.error(f"Break End Error: {e}")
        flash("Error ending break")
    finally:
        conn.close()
    return redirect(url_for('timeclock.index'))

@timeclock_bp.route('/manager/recurring/add', methods=['POST'])
@login_required
def add_recurring_rule():
    if not has_permission(current_user, 'timeclock.manage'):
        return "Unauthorized", 403
    
    conn = get_db_connection()
    try:
        user_id = request.form.get('user_id')
        day = request.form.get('day_of_week')
        start = request.form.get('start_time')
        end = request.form.get('end_time')
        freq = request.form.get('frequency', 'weekly')
        ref_date = request.form.get('reference_date') # Only for biweekly
        
        if not ref_date:
            ref_date = datetime.datetime.now().strftime('%Y-%m-%d')
            
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
@login_required
def delete_recurring_rule(rule_id):
    if not has_permission(current_user, 'timeclock.manage'):
        return "Unauthorized", 403
    conn = get_db_connection()
    conn.execute('DELETE FROM recurring_shift_rules WHERE id = ?', (rule_id,))
    conn.commit()
    conn.close()
    flash("Rule deleted.")
    return redirect(url_for('timeclock.shifts', tab='recurring'))

@timeclock_bp.route('/manager/generate_schedule', methods=['POST'])
@login_required
def generate_schedule():
    if not has_permission(current_user, 'timeclock.manage'):
        return "Unauthorized", 403
        
    start_date_str = request.form.get('start_date')
    end_date_str = request.form.get('end_date')
    
    conn = get_db_connection()
    try:
        start_date = datetime.datetime.strptime(start_date_str, '%Y-%m-%d')
        end_date = datetime.datetime.strptime(end_date_str, '%Y-%m-%d')
        
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
                        s_dt = f"{current.strftime('%Y-%m-%d')} {rule['start_time']}:00"
                        e_dt = f"{current.strftime('%Y-%m-%d')} {rule['end_time']}:00"
                        
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
@login_required
def manager_dashboard():
    # Permission Check
    if not has_permission(current_user, 'timeclock.manage'):
        flash("Access Denied")
        return redirect(url_for('timeclock.index'))

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
                    created_at DATETIME DEFAULT (datetime('now', 'localtime')),
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
                start = datetime.datetime.strptime(entry['clock_in'], '%Y-%m-%d %H:%M:%S')
                now = datetime.datetime.now()
                diff = now - start
                hours = diff.seconds // 3600
                minutes = (diff.seconds % 3600) // 60
                entry['duration'] = f"{hours}h {minutes}m"
                # Format start time
                entry['clock_in'] = start.strftime('%I:%M %p')
            except:
                entry['duration'] = "Err"
            
            active_entries.append(entry)
            if entry['type'] == 'shift':
                active_cnt += 1
            else:
                break_cnt += 1
                
        # Total Today (Unique Users worked today)
        today_str = datetime.datetime.now().strftime('%Y-%m-%d')
        today_cnt = conn.execute('''
            SELECT COUNT(DISTINCT user_id) as c 
            FROM time_entries 
            WHERE date(clock_in) = ?
        ''', (today_str,)).fetchone()['c']
        
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
@login_required
def force_clock_out(user_id):
    if not has_permission(current_user, 'timeclock.manage'):
        flash("Access Denied")
        return redirect(url_for('timeclock.index'))
        
    conn = get_db_connection()
    try:
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn.execute('''
            UPDATE time_entries 
            SET clock_out = ?, notes = 'Force clock out by Admin'
            WHERE user_id = ? AND clock_out IS NULL
        ''', (now, user_id))
        conn.commit()
        flash("User force clocked out.")
    except Exception as e:
        logger.error(f"Force Clock Out Error: {e}")
        flash("Error performing force clock out.")
    finally:
        conn.close()
    return redirect(url_for('timeclock.manager_dashboard'))

@timeclock_bp.route('/manager/force_clock_in/<int:user_id>', methods=['POST'])
@login_required
def force_clock_in(user_id):
    if not has_permission(current_user, 'timeclock.manage'):
        flash("Access Denied")
        return redirect(url_for('timeclock.index'))
        
    conn = get_db_connection()
    try:
        # Check if user is already clocked in
        existing = conn.execute('SELECT id FROM time_entries WHERE user_id = ? AND clock_out IS NULL', 
                              (user_id,)).fetchone()
        if existing:
            flash("User is already clocked in!")
            return redirect(url_for('timeclock.manager_dashboard'))
            
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
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
@login_required
def export_csv():
    if not has_permission(current_user, 'timeclock.manage'):
        return redirect(url_for('timeclock.index'))
    
    import csv
    import io
    from flask import Response
    
    conn = get_db_connection()
    try:
        # Default: Last 30 days
        limit_date = (datetime.datetime.now() - datetime.timedelta(days=30)).strftime('%Y-%m-%d')
        
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
            
            # Format Dates
            c_in = datetime.datetime.strptime(entry['clock_in'], '%Y-%m-%d %H:%M:%S')
            date_str = c_in.strftime('%Y-%m-%d')
            time_in = c_in.strftime('%I:%M %p')
            
            time_out = ""
            duration = ""
            
            if entry['clock_out']:
                c_out = datetime.datetime.strptime(entry['clock_out'], '%Y-%m-%d %H:%M:%S')
                time_out = c_out.strftime('%I:%M %p')
                
                diff = c_out - c_in
                hours = diff.seconds // 3600
                minutes = (diff.seconds % 3600) // 60
                duration = f"{hours}h {minutes}m"
            else:
                time_out = "Active"
                now = datetime.datetime.now()
                diff = now - c_in
                hours = diff.seconds // 3600
                minutes = (diff.seconds % 3600) // 60
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
        filename = f"timesheets_{datetime.datetime.now().strftime('%Y%m%d')}.csv"
        
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
@login_required
def timesheets():
    if not has_permission(current_user, 'timeclock.manage'):
        return redirect(url_for('timeclock.index'))
    
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
        
        if date_from:
            query += " AND t.clock_in >= ?"
            params.append(date_from)
        if date_to:
            query += " AND t.clock_in <= ?"
            params.append(date_to + " 23:59:59")
        if user_filter:
            query += " AND t.user_id = ?"
            params.append(user_filter)
            
        query += " ORDER BY t.clock_in DESC LIMIT 100"
        
        rows = conn.execute(query, params).fetchall()
        
        processed_rows = []
        for r in rows:
            entry = dict(r)
            # Duration Calc
            try:
                start = datetime.datetime.strptime(entry['clock_in'], '%Y-%m-%d %H:%M:%S')
                # Manual format to ensure minutes are zero-padded (windows strftime issue)
                h = start.strftime('%I').lstrip('0') or '12' # optional: remove leading zero from hour
                m = start.strftime('%M')
                p = start.strftime('%p')
                entry['clock_in_fmt'] = f"{start.strftime('%m/%d')} {h}:{m} {p}"
                
                if entry['clock_out']:
                    end = datetime.datetime.strptime(entry['clock_out'], '%Y-%m-%d %H:%M:%S')
                    h_end = end.strftime('%I').lstrip('0') or '12'
                    m_end = end.strftime('%M')
                    p_end = end.strftime('%p')
                    entry['clock_out_fmt'] = f"{end.strftime('%m/%d')} {h_end}:{m_end} {p_end}"
                    diff = end - start
                    hours = diff.seconds // 3600
                    minutes = (diff.seconds % 3600) // 60
                    entry['duration'] = f"{hours}h {minutes}m"
                else:
                    entry['clock_out_fmt'] = "Active"
                    now = datetime.datetime.now()
                    diff = now - start
                    hours = diff.seconds // 3600
                    minutes = (diff.seconds % 3600) // 60
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
@login_required
def update_settings():
    if not has_permission(current_user, 'timeclock.manage'):
        return redirect(url_for('timeclock.index'))
        
    from app.services.data_manager import save_config, load_config
    
    start_date = request.form.get('pay_period_start')
    freq = request.form.get('pay_period_type')
    fulltime_hours = request.form.get('fulltime_hours', '40')
    
    try:
        fulltime_hours = float(fulltime_hours)
    except:
        fulltime_hours = 40.0
    
    new_conf = {
        'TIMECLOCK_PAY_PERIOD_START': start_date,
        'TIMECLOCK_PAY_PERIOD_TYPE': freq,
        'TIMECLOCK_FULLTIME_HOURS': fulltime_hours
    }
    save_config(new_conf)
    flash("Settings updated.")
    return redirect(url_for('timeclock.manager_dashboard'))

@timeclock_bp.route('/request_pto', methods=['GET', 'POST'])
@login_required
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
        balance = row['balance_hours'] if row else 0.0
        
        if request.method == 'POST':
            req_type = request.form.get('request_type') # 'full', 'partial'
            date_str = request.form.get('date')
            start_time = request.form.get('start_time')
            end_time = request.form.get('end_time')
            notes = request.form.get('notes')
            
            hours_requested = 0.0
            
            if req_type == 'full':
                # Get Shift Duration for that day
                # Find Shift
                shift = conn.execute('''
                    SELECT start_time, end_time FROM scheduled_shifts 
                    WHERE user_id = ? AND date(start_time) = ?
                ''', (current_user.id, date_str)).fetchone()
                
                if not shift:
                    flash("No scheduled shift found on that date to request off.")
                    return redirect(url_for('timeclock.request_pto'))
                    
                s = datetime.datetime.strptime(shift['start_time'], '%Y-%m-%d %H:%M:%S')
                e = datetime.datetime.strptime(shift['end_time'], '%Y-%m-%d %H:%M:%S')
                diff = (e - s).total_seconds() / 3600
                hours_requested = round(diff, 2)
                
            elif req_type == 'partial':
                # Calc requested hours
                s_t = datetime.datetime.strptime(f"{date_str} {start_time}", '%Y-%m-%d %H:%M')
                e_t = datetime.datetime.strptime(f"{date_str} {end_time}", '%Y-%m-%d %H:%M')
                
                hours_requested = round((e_t - s_t).total_seconds() / 3600, 2)
                
                # Check Minimum Shift Rule
                # Find original shift
                shift = conn.execute('''
                    SELECT start_time, end_time FROM scheduled_shifts 
                    WHERE user_id = ? AND date(start_time) = ?
                ''', (current_user.id, date_str)).fetchone()
                
                if shift:
                    orig_s = datetime.datetime.strptime(shift['start_time'], '%Y-%m-%d %H:%M:%S')
                    orig_e = datetime.datetime.strptime(shift['end_time'], '%Y-%m-%d %H:%M:%S')
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
            
        today_str = datetime.datetime.now().strftime('%Y-%m-%d')
        return render_template('timeclock/request_pto.html', balance=balance, today_str=today_str)
    finally:
        conn.close()

@timeclock_bp.route('/manager/pto')
@login_required
def manager_pto():
    if not has_permission(current_user, 'timeclock.manage'):
        return redirect(url_for('timeclock.index'))
        
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
@login_required
def update_pto_balance():
    if not has_permission(current_user, 'timeclock.manage'):
        return "Unauthorized", 403
    conn = get_db_connection()
    try:
        user_id = request.form.get('user_id')
        amount = float(request.form.get('amount'))
        
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
@login_required
def pto_action(req_id):
    if not has_permission(current_user, 'timeclock.manage'):
        return "Unauthorized", 403
        
    action = request.form.get('action') # 'approve', 'deny'
    conn = get_db_connection()
    try:
        req = conn.execute('SELECT * FROM pto_requests WHERE id = ?', (req_id,)).fetchone()
        if not req:
            flash("Request not found")
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
            curr_bal = bal_row['balance_hours'] if bal_row else 0.0
            
            if curr_bal < hrs:
                flash(f"Cannot approve: User has {curr_bal} hours, needs {hrs}.")
                return redirect(url_for('timeclock.manager_pto'))
                
            new_bal = curr_bal - hrs
            conn.execute('UPDATE user_pto_balances SET balance_hours = ? WHERE user_id = ?', (new_bal, user_id))
            
            # 2. Log Time Entry? 
            # Design decision: Log a 'pto' type entry so it shows on export?
            # Or just update schedule?
            # User requirement: "Export... included pto?" (Implied)
            # Let's create a 'pto' time_entry for visualization.
            
            # Format times
            # If partial, use start/end. If full, use shift start/end.
            if req['request_type'] == 'full':
                # Fetch shift again
                shift = conn.execute('''
                    SELECT start_time, end_time FROM scheduled_shifts 
                    WHERE user_id = ? AND date(start_time) = ?
                ''', (user_id, req['date'])).fetchone()
                if shift:
                    s_dt = shift['start_time']
                    e_dt = shift['end_time']
                    # Insert Time Entry
                    conn.execute('''
                        INSERT INTO time_entries (user_id, type, clock_in, clock_out, notes)
                        VALUES (?, 'pto', ?, ?, 'PTO Approved')
                    ''', (user_id, s_dt, e_dt))
            else:
                # Partial
                s_dt = f"{req['date']} {req['start_time']}:00"
                e_dt = f"{req['date']} {req['end_time']}:00"
                 # Insert Time Entry
                conn.execute('''
                    INSERT INTO time_entries (user_id, type, clock_in, clock_out, notes)
                    VALUES (?, 'pto', ?, ?, 'Partial PTO Approved')
                ''', (user_id, s_dt, e_dt))
            
            conn.execute("UPDATE pto_requests SET status='approved' WHERE id=?", (req_id,))
            conn.commit()
            flash("PTO Approved & Logged.")
            
    finally:
        conn.close()
    return redirect(url_for('timeclock.manager_pto'))


@timeclock_bp.route('/manager/timesheet/edit/<int:entry_id>', methods=['GET', 'POST'])
@login_required
def edit_entry(entry_id):
    if not has_permission(current_user, 'timeclock.manage'):
        return redirect(url_for('timeclock.index'))
        
    conn = get_db_connection()
    try:
        if request.method == 'POST':
            # Handle Update
            clock_in = request.form.get('clock_in') # Expected: YYYY-MM-DDTHH:MM
            clock_out = request.form.get('clock_out') # Expected: YYYY-MM-DDTHH:MM
            notes = request.form.get('notes')
            
            # Convert datetime-local to YYYY-MM-DD HH:MM:SS
            # datetime-local inputs return "2023-11-01T14:30"
            c_in_clean = clock_in.replace('T', ' ') if clock_in else None
            c_out_clean = clock_out.replace('T', ' ') if clock_out else None
            
            # If seconds missing, append :00
            if c_in_clean and len(c_in_clean) == 16: c_in_clean += ':00'
            if c_out_clean and len(c_out_clean) == 16: c_out_clean += ':00'
            
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
        c_in_str = str(entry['clock_in']) if entry['clock_in'] else ''
        c_out_str = str(entry['clock_out']) if entry['clock_out'] else ''
        
        entry['clock_in_val'] = c_in_str.replace(' ', 'T')[:16]
        entry['clock_out_val'] = c_out_str.replace(' ', 'T')[:16]
        
        user = conn.execute('SELECT username FROM users WHERE id = ?', (entry['user_id'],)).fetchone()
        username = user['username'] if user else "Unknown User (Deleted)"
        
        return render_template('timeclock/entry_edit.html', entry=entry, username=username)
    finally:
        conn.close()

@timeclock_bp.route('/manager/timesheet/delete/<int:entry_id>', methods=['POST'])
@login_required
def delete_entry(entry_id):
    if not has_permission(current_user, 'timeclock.manage'):
        return redirect(url_for('timeclock.index'))
    
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
@login_required
def export_custom_form():
    if not has_permission(current_user, 'timeclock.manage'):
        return redirect(url_for('timeclock.index'))
    
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
@login_required
def export_report():
    if not has_permission(current_user, 'timeclock.manage'):
        return redirect(url_for('timeclock.index'))
        
    selected_users = request.form.getlist('user_ids')
    period_str = request.form.get('period') # "YYYY-MM-DD|YYYY-MM-DD" or "custom"
    
    start_date = None
    end_date = None
    
    if period_str == 'custom':
        start_date = request.form.get('custom_start')
        end_date = request.form.get('custom_end')
        date_label = f"{start_date} to {end_date}"
    else:
        parts = period_str.split('|')
        start_date = parts[0]
        end_date = parts[1]
        
        # Format label
        s = datetime.datetime.strptime(start_date, '%Y-%m-%d')
        e = datetime.datetime.strptime(end_date, '%Y-%m-%d')
        date_label = f"{s.strftime('%b %d, %Y')} - {e.strftime('%b %d, %Y')}"
        
    conn = get_db_connection()
    try:
        # Build Query
        query = '''
            SELECT t.*, u.username, u.id as u_id
            FROM time_entries t
            JOIN users u ON t.user_id = u.id
            WHERE date(t.clock_in) >= ? AND date(t.clock_in) <= ?
        '''
        params = [start_date, end_date]
        
        if selected_users:
            placeholders = ','.join(['?'] * len(selected_users))
            query += f" AND t.user_id IN ({placeholders})"
            params.extend(selected_users)
            
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
                start = datetime.datetime.strptime(entry['clock_in'], '%Y-%m-%d %H:%M:%S')
                # Time format
                entry['date_fmt'] = start.strftime('%m/%d/%Y')
                entry['in_fmt'] = start.strftime('%I:%M %p')
                
                hours = 0.0
                
                if entry['clock_out']:
                    end = datetime.datetime.strptime(entry['clock_out'], '%Y-%m-%d %H:%M:%S')
                    entry['out_fmt'] = end.strftime('%I:%M %p')
                    diff = (end - start).total_seconds()
                    hours = diff / 3600
                else:
                    entry['out_fmt'] = "Active"
                    diff = (datetime.datetime.now() - start).total_seconds()
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
                
        # Round totals and calculate OT
        fulltime_hours = float(load_config().get('TIMECLOCK_FULLTIME_HOURS', 40))
        for uid in report_data:
            report_data[uid]['total_hours'] = round(report_data[uid]['total_hours'], 2)
            report_data[uid]['reg_hours'] = round(report_data[uid]['reg_hours'], 2)
            report_data[uid]['pto_hours'] = round(report_data[uid]['pto_hours'], 2)
            # OT = reg_hours that exceed threshold (PTO doesn't count toward OT)
            report_data[uid]['ot_hours'] = round(max(0, report_data[uid]['reg_hours'] - fulltime_hours), 2)
            # Adjust reg_hours to cap at threshold for display
            if report_data[uid]['reg_hours'] > fulltime_hours:
                report_data[uid]['reg_hours'] = round(fulltime_hours, 2)
            
        return render_template('timeclock/report_print.html', 
                             report_data=report_data, 
                             date_label=date_label,
                             generated_at=datetime.datetime.now().strftime('%Y-%m-%d %I:%M %p'))
                             
    finally:
        conn.close()

