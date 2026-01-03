from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from flask_login import login_required, current_user
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

@timeclock_bp.route('/')
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
    alert = None
    conn = get_db_connection()
    try:
        now = datetime.datetime.now()
        today = now.strftime('%Y-%m-%d')
        
        # Get Schedule for today
        # Note: simplistic "start_time today" logic
        sched = conn.execute('''
            SELECT * FROM scheduled_shifts 
            WHERE user_id = ? AND date(start_time) = ? 
            ORDER BY start_time ASC LIMIT 1
        ''', (current_user.id, today)).fetchone()
        
        if sched:
            sched_start = datetime.datetime.strptime(sched['start_time'], '%Y-%m-%d %H:%M:%S')
            sched_end = datetime.datetime.strptime(sched['end_time'], '%Y-%m-%d %H:%M:%S')
            grace = int(config.get('TIMECLOCK_GRACE_PERIOD', 15))
            
            # Late In?
            if not status:
                late_threshold = sched_start + datetime.timedelta(minutes=grace)
                if now > late_threshold:
                    alert = f"⚠ You are late! Shift started at {sched_start.strftime('%I:%M %p')}."
            
            # Late Out? (Overdue to clock out)
            elif status and status['type'] == 'shift':
                # If current time is past end time + grace
                late_threshold = sched_end + datetime.timedelta(minutes=grace)
                if now > late_threshold:
                    alert = f"⚠ Overdue: Shift ended at {sched_end.strftime('%I:%M %p')}."
            
    except Exception as e:
        logger.error(f"Schedule Check Error: {e}")
    finally:
        conn.close()

    return render_template('timeclock/index.html', status=status, duration=duration, clock_in_time=clock_in_formatted, alert=alert)

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
    if not current_user.is_admin and not current_user.has_role('timeclock_admin'):
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
            return redirect(url_for('timeclock.shifts'))
            
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
            s['time_range'] = f"{dt.strftime('%I:%M %p')} - {datetime.datetime.strptime(s['end_time'], '%Y-%m-%d %H:%M:%S').strftime('%I:%M %p')}"
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
            
        return render_template('timeclock/shifts.html', shifts=shifts, rules=rules, users=users, active_tab=active_tab)
    finally:
        conn.close()

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
    if not current_user.is_admin and not current_user.has_role('timeclock_admin'):
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
    if not current_user.is_admin and not current_user.has_role('timeclock_admin'):
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
    if not current_user.is_admin and not current_user.has_role('timeclock_admin'):
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
    if not current_user.is_admin and not current_user.has_role('timeclock_admin'):
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
        
        return render_template('timeclock/manager.html', 
                             active_entries=active_entries,
                             active_count=active_cnt,
                             break_count=break_cnt,
                             today_count=today_cnt)
    finally:
        conn.close()

@timeclock_bp.route('/manager/force_clock_out/<int:user_id>', methods=['POST'])
@login_required
def force_clock_out(user_id):
    if not current_user.is_admin and not current_user.has_role('timeclock_admin'):
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

@timeclock_bp.route('/manager/export')
@login_required
def export_csv():
    if not current_user.is_admin and not current_user.has_role('timeclock_admin'):
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
    if not current_user.is_admin and not current_user.has_role('timeclock_admin'):
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
                entry['clock_in_fmt'] = start.strftime('%m/%d %I:%M %p')
                
                if entry['clock_out']:
                    end = datetime.datetime.strptime(entry['clock_out'], '%Y-%m-%d %H:%M:%S')
                    entry['clock_out_fmt'] = end.strftime('%m/%d %I:%M %p')
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

@timeclock_bp.route('/manager/timesheet/edit/<int:entry_id>', methods=['GET', 'POST'])
@login_required
def edit_entry(entry_id):
    if not current_user.is_admin and not current_user.has_role('timeclock_admin'):
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
        entry['clock_in_val'] = entry['clock_in'].replace(' ', 'T')[:16] if entry['clock_in'] else ''
        entry['clock_out_val'] = entry['clock_out'].replace(' ', 'T')[:16] if entry['clock_out'] else ''
        
        user = conn.execute('SELECT username FROM users WHERE id = ?', (entry['user_id'],)).fetchone()
        
        return render_template('timeclock/entry_edit.html', entry=entry, username=user['username'])
    finally:
        conn.close()

@timeclock_bp.route('/manager/timesheet/delete/<int:entry_id>', methods=['POST'])
@login_required
def delete_entry(entry_id):
    if not current_user.is_admin and not current_user.has_role('timeclock_admin'):
        flash("Access Denied")
        return redirect(url_for('timeclock.index'))
        
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM time_entries WHERE id = ?', (entry_id,))
        conn.commit()
        flash("Time entry deleted.")
    except Exception as e:
        logger.error(f"Delete Error: {e}")
        flash("Error deleting entry.")
    finally:
        conn.close()
    return redirect(url_for('timeclock.timesheets'))
