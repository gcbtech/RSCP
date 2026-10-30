"""
Phone UI, Time: clock in and out and breaks, this pay period's hours, the
schedule, time off (balance, request, past requests) and, for managers, who
is on the clock now.

Clocking and time-off requests run the desktop timeclock's own view functions
in-process (app/routes/timeclock.py: the same checks, messages and one-lock
clock transitions); only the pages they lead back to differ. Schedules,
timesheets and approving time off stay on the desktop manager pages.
"""
import datetime
import logging

from flask import abort, flash, get_flashed_messages, redirect, render_template, request, url_for
from flask_login import current_user

from app.routes import timeclock as desktop_tc
from app.routes.mobile import allowed_modules, mobile_bp
from app.services import timeclock_hours as tc
from app.services.data_manager import load_config
from app.services.db import get_request_db
from app.utils.permissions import has_permission

logger = logging.getLogger(__name__)

SCHEDULE_DAYS = 14
# phone action -> desktop view
CLOCK_ACTIONS = {'in': 'clock_in', 'out': 'clock_out', 'break': 'break_start', 'back': 'break_end'}
PTO_STATUS = {'pending': ('Pending', 'wn'), 'approved': ('Approved', 'ok'), 'denied': ('Denied', 'bd')}
# The desktop's clock messages, in the phone's words (others pass through as they are)
CLOCK_MESSAGES = {
    'Clocked In Successfully': 'Clocked in',
    'Clocked Out Successfully': 'Clocked out',
    'Break Started': 'Break started',
    'Break Ended - Back to Work': 'Break over, back to work',
}


def _require_time():
    if 'time' not in allowed_modules():
        abort(403)


def _clock(dt):
    return dt.strftime('%I:%M %p').lstrip('0')


def _day(local_dt):
    """'Today', 'Tomorrow', 'Yesterday' or 'Fri, Oct 30'."""
    delta = (local_dt.date() - tc.local_today()).days
    return {0: 'Today', 1: 'Tomorrow', -1: 'Yesterday'}.get(delta, local_dt.strftime('%a, %b %d').replace(' 0', ' '))


def _duration(seconds):
    """'2h 14m', or '12m' under an hour (static/m/time.js counts the same way)."""
    mins = max(0, int(seconds)) // 60
    return f'{mins // 60}h {mins % 60}m' if mins >= 60 else f'{mins}m'


def _hours(value):
    return f'{round(value or 0, 2):g}'


def _page(template, section, **ctx):
    return render_template(template, active_tab='time', section=section,
                           can_manage=has_permission(current_user, 'timeclock.manage'), **ctx)


def _status(conn):
    """The open entry as the clock card shows it, or the last shift's end."""
    entry = desktop_tc.get_current_status(current_user.id)
    if entry:
        start = tc.as_utc(entry['clock_in'])
        return {'state': 'break' if entry['type'] == 'break' else 'shift',
                'since': _clock(tc.to_local(entry['clock_in'])),
                'since_ms': int(start.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000),
                'duration': _duration((tc.utc_now() - start).total_seconds())}
    last = conn.execute('SELECT clock_out FROM time_entries WHERE user_id = ? AND clock_out IS NOT NULL '
                        'ORDER BY clock_out DESC LIMIT 1', (current_user.id,)).fetchone()
    ended = None
    if last:
        local = tc.to_local(last['clock_out'])
        ended = f'{_day(local)} {_clock(local)}'
    return {'state': 'out', 'ended': ended}


def _period_hours(conn):
    """This pay period's regular / overtime / total hours, worked out like the desktop page."""
    conf = load_config() or {}
    start, end = desktop_tc.get_current_pay_period()
    rows = conn.execute('''
        SELECT type, clock_in, clock_out FROM time_entries
        WHERE user_id = ? AND type IN ('shift', 'break') AND clock_in >= ? AND clock_in <= ?
    ''', (current_user.id, *tc.local_dates_to_utc_range(start, end))).fetchall()
    totals = tc.summarize(rows, desktop_tc._workweek_anchor(),
                          float(conf.get('TIMECLOCK_FULLTIME_HOURS', 40)), desktop_tc._paid_breaks())
    label = f"{start.strftime('%b %d')} – {end.strftime('%b %d')}".replace(' 0', ' ')
    return {'label': label, 'regular': _hours(totals['regular']), 'overtime': _hours(totals['overtime']),
            'total': _hours(totals['regular'] + totals['overtime']), 'has_overtime': totals['overtime'] > 0}


def _shifts(conn, days, limit=None):
    """Scheduled shifts still to end, over the next `days` days."""
    now = tc.utc_now_str()
    until = tc.local_dates_to_utc_range(tc.local_today() + datetime.timedelta(days=days))[1]
    rows = conn.execute(f'''
        SELECT start_time, end_time FROM scheduled_shifts
        WHERE user_id = ? AND datetime(end_time) > datetime(?) AND datetime(start_time) <= datetime(?)
        ORDER BY start_time {'LIMIT ' + str(int(limit)) if limit else ''}
    ''', (current_user.id, now, until)).fetchall()
    shifts = []
    for r in rows:
        s, e = tc.to_local(r['start_time']), tc.to_local(r['end_time'])
        shifts.append({'day': _day(s), 'range': f'{_clock(s)} – {_clock(e)}',
                       'hours': _hours((tc.as_utc(r['end_time']) - tc.as_utc(r['start_time'])).total_seconds() / 3600),
                       'now': tc.as_utc(r['start_time']) <= tc.utc_now()})
    return shifts


def _pto_balance(conn):
    row = conn.execute('SELECT balance_hours FROM user_pto_balances WHERE user_id = ?', (current_user.id,)).fetchone()
    return _hours(row['balance_hours'] if row and row['balance_hours'] is not None else 0)


# ---------------------------------------------------------------- clock

@mobile_bp.route('/time')
def time():
    _require_time()
    conn = get_request_db()
    shifts = _shifts(conn, SCHEDULE_DAYS, limit=1)
    return _page('m/time/clock.html', 'clock', status=_status(conn), period=_period_hours(conn),
                 next_shift=shifts[0] if shifts else None, pto_balance=_pto_balance(conn),
                 now_ms=int(tc.utc_now().replace(tzinfo=datetime.timezone.utc).timestamp() * 1000))


@mobile_bp.route('/time/clock/<action>', methods=['POST'])
def time_clock(action):
    _require_time()
    view = getattr(desktop_tc, CLOCK_ACTIONS.get(action, ''), None)
    if view is None:
        abort(404)
    view()   # flashes its message and redirects to the desktop page: come back here instead
    for category, message in get_flashed_messages(with_categories=True):
        flash(CLOCK_MESSAGES.get(message, message), category)
    return redirect(url_for('mobile.time'))


# ---------------------------------------------------------------- schedule

@mobile_bp.route('/time/schedule')
def time_schedule():
    _require_time()
    days = []
    for shift in _shifts(get_request_db(), SCHEDULE_DAYS):
        if not days or days[-1]['day'] != shift['day']:
            days.append({'day': shift['day'], 'shifts': []})
        days[-1]['shifts'].append(shift)
    return _page('m/time/schedule.html', 'schedule', days=days, span=SCHEDULE_DAYS)


# ---------------------------------------------------------------- time off

@mobile_bp.route('/time/off', methods=['GET', 'POST'])
def time_off():
    _require_time()
    if request.method == 'POST':
        # The desktop's request handler: same fields, checks and messages
        desktop_tc.request_pto()
        return redirect(url_for('mobile.time_off'))
    conn = get_request_db()
    history = []
    for r in conn.execute('SELECT request_type, date, start_time, end_time, hours_requested, status, admin_notes '
                          'FROM pto_requests WHERE user_id = ? ORDER BY date DESC, id DESC LIMIT 20',
                          (current_user.id,)).fetchall():
        try:
            day = datetime.datetime.strptime(r['date'], '%Y-%m-%d').strftime('%a, %b %d').replace(' 0', ' ')
        except (TypeError, ValueError):
            day = r['date']
        label, tone = PTO_STATUS.get(r['status'], (str(r['status'] or '').title(), 'nu'))
        history.append({'day': day, 'hours': _hours(r['hours_requested']), 'status': label, 'tone': tone,
                         'what': 'Full shift' if r['request_type'] == 'full'
                                 else f"{r['start_time'] or '?'} – {r['end_time'] or '?'}",
                         'notes': r['admin_notes']})
    return _page('m/time/off.html', 'off', balance=_pto_balance(conn), history=history,
                 today=tc.local_today().isoformat())


# ---------------------------------------------------------------- team (managers)

@mobile_bp.route('/time/team')
def time_team():
    _require_time()
    if not has_permission(current_user, 'timeclock.manage'):
        abort(403)
    conn = get_request_db()
    on_clock = []
    for r in conn.execute('''
        SELECT t.type, t.clock_in, u.username FROM time_entries t JOIN users u ON u.id = t.user_id
        WHERE t.clock_out IS NULL ORDER BY t.type DESC, u.username COLLATE NOCASE
    ''').fetchall():
        on_clock.append({'name': r['username'], 'state': 'break' if r['type'] == 'break' else 'shift',
                         'since': _clock(tc.to_local(r['clock_in'])),
                         'duration': _duration((tc.utc_now() - tc.as_utc(r['clock_in'])).total_seconds())})
    today = conn.execute('SELECT COUNT(DISTINCT user_id) AS c FROM time_entries WHERE clock_in >= ? AND clock_in <= ?',
                         tc.local_dates_to_utc_range(tc.local_today())).fetchone()['c']
    return _page('m/time/team.html', 'team', on_clock=on_clock, today=today, pending=pending_time_off(conn))


def pending_time_off(conn):
    return conn.execute("SELECT COUNT(*) AS c FROM pto_requests WHERE status = 'pending'").fetchone()['c']


def team_summary(conn):
    """Home's Team row for managers: how many are on the clock, time-off requests waiting."""
    on_clock = conn.execute('SELECT COUNT(DISTINCT user_id) AS c FROM time_entries WHERE clock_out IS NULL').fetchone()['c']
    return {'on_clock': on_clock, 'pending': pending_time_off(conn)}
