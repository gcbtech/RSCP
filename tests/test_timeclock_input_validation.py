"""Empty or junk timeclock input gets a flash + redirect, never a 500, and
writes nothing.

Every timeclock endpoint that reads form fields or query parameters is posted
blank, missing, non-numeric, out-of-range and backwards values, as a manager
(super admin and timeclock_admin) and, for the staff pages, as regular users.
After each request the timeclock tables and settings must be unchanged.

TIMECLOCK_ENABLED is switched on by tests/conftest.py's config.json. The
business timezone is pinned to America/Denver (CI runs in UTC).

One app is shared by the module (building one per case is slow); that is safe
precisely because invalid input must not change anything, which every case
asserts.
"""
import sqlite3
from urllib.parse import urlparse

import pytest
from werkzeug.security import generate_password_hash

from app.services import timeclock_hours as tc
from tests.conftest import FRESH_USERS, login

ADMIN, CASHIER, POS_MANAGER, TC_ADMIN = 1, 2, 3, 4
MANAGERS = (ADMIN, TC_ADMIN)
STAFF = (CASHIER, POS_MANAGER)

TABLES = ('time_entries', 'scheduled_shifts', 'recurring_shift_rules',
          'pto_requests', 'user_pto_balances')
SETTINGS = ('TIMECLOCK_PAY_PERIOD_START', 'TIMECLOCK_PAY_PERIOD_TYPE', 'TIMECLOCK_FULLTIME_HOURS')

# Seeded rows (ids are fixed by the fixture)
ENTRY_ID = 1          # closed shift for the cashier
PENDING_FULL = 1      # pending full-shift PTO request
PENDING_JUNK = 2      # pending partial request with unusable stored times
APPROVED = 3          # already-approved request
SHIFT_DAY = '2099-07-15'


@pytest.fixture(scope='module')
def app(tmp_path_factory):
    zoneinfo = pytest.importorskip('zoneinfo')
    try:
        tz = zoneinfo.ZoneInfo('America/Denver')
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip('tz database unavailable (pip install tzdata)')

    from app import create_app
    from app.services.db import get_db_connection
    from app.services.migration import run_migrations

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tc, 'LOCAL_TZ', tz)
        db_path = str(tmp_path_factory.mktemp('tc_input') / 'rscp_test.db')
        app = create_app(test_config={'TESTING': True, 'DATABASE': db_path,
                                      'WTF_CSRF_ENABLED': False, 'SECRET_KEY': 'test_key'})
        with app.app_context():
            run_migrations()
            conn = get_db_connection()
            pw = generate_password_hash('Correct-Horse-1')
            users = list(FRESH_USERS) + [(TC_ADMIN, 'tc_boss', 0, '["timeclock_admin"]')]
            conn.executemany('INSERT INTO users (id, username, password_hash, is_admin, roles) '
                             'VALUES (?, ?, ?, ?, ?)', [(u, n, pw, a, r) for u, n, a, r in users])
            conn.execute("INSERT INTO time_entries (id, user_id, type, clock_in, clock_out) "
                         "VALUES (?, 2, 'shift', '2026-07-15 15:00:00', '2026-07-15 23:00:00')", (ENTRY_ID,))
            conn.execute("INSERT INTO scheduled_shifts (user_id, start_time, end_time) VALUES (?, ?, ?)",
                         (CASHIER, tc.local_to_utc_str(f'{SHIFT_DAY} 09:00'),
                          tc.local_to_utc_str(f'{SHIFT_DAY} 17:00')))
            conn.execute("INSERT INTO recurring_shift_rules (user_id, day_of_week, start_time, end_time, "
                         "frequency, reference_date) VALUES (2, 5, '09:00', '17:00', 'weekly', '2026-01-03')")
            conn.executemany("INSERT INTO user_pto_balances (user_id, balance_hours) VALUES (?, ?)",
                             [(CASHIER, 40.0), (POS_MANAGER, 40.0)])
            conn.executemany(
                "INSERT INTO pto_requests (id, user_id, request_type, date, start_time, end_time, "
                "hours_requested, status) VALUES (?, 2, ?, ?, ?, ?, ?, ?)", [
                    (PENDING_FULL, 'full', SHIFT_DAY, '', '', 8, 'pending'),
                    (PENDING_JUNK, 'partial', SHIFT_DAY, '', 'abc', 2, 'pending'),
                    (APPROVED, 'partial', SHIFT_DAY, '13:00', '17:00', 4, 'approved'),
                ])
            conn.commit()
            conn.close()
        yield app


def _snapshot(app):
    from app.services.data_manager import load_config
    conn = sqlite3.connect(app.config['DATABASE'])
    try:
        rows = {t: conn.execute(f'SELECT * FROM {t} ORDER BY rowid').fetchall() for t in TABLES}
    finally:
        conn.close()
    config = load_config()
    rows['settings'] = [config.get(k) for k in SETTINGS]
    return rows


def _request(app, user_id, method, url, data):
    client = login(app.test_client(), user_id)
    if method == 'GET':
        return client.get(url, query_string=data)
    return client.post(url, data=data)


def _assert_rejected(app, user_id, method, url, data, ok_statuses=(302,)):
    before = _snapshot(app)
    resp = _request(app, user_id, method, url, data)
    assert resp.status_code != 500, f'{method} {url} {data!r} -> 500'
    assert resp.status_code in ok_statuses, f'{method} {url} {data!r} -> {resp.status_code}'
    assert _snapshot(app) == before, f'{method} {url} {data!r} wrote data'
    return resp


# --- manager forms ---------------------------------------------------------------

GOOD_SHIFT = {'user_id': '2', 'start_time': '2099-08-01T09:00', 'end_time': '2099-08-01T17:00'}
GOOD_RULE = {'user_id': '2', 'day_of_week': '1', 'start_time': '09:00', 'end_time': '17:00',
             'frequency': 'weekly', 'reference_date': ''}
GOOD_REPORT_RANGE = {'period': 'custom', 'custom_start': '2026-07-01', 'custom_end': '2026-07-31'}


def _with(base, **changes):
    out = dict(base)
    for k, v in changes.items():
        if v is None:
            out.pop(k, None)
        else:
            out[k] = v
    return out


MANAGER_POSTS = [
    # one-time shift
    ('/timeclock/manager/shifts', {}),
    ('/timeclock/manager/shifts', _with(GOOD_SHIFT, user_id='')),
    ('/timeclock/manager/shifts', _with(GOOD_SHIFT, user_id=None)),
    ('/timeclock/manager/shifts', _with(GOOD_SHIFT, user_id='abc')),
    ('/timeclock/manager/shifts', _with(GOOD_SHIFT, user_id='999')),
    ('/timeclock/manager/shifts', _with(GOOD_SHIFT, start_time='')),
    ('/timeclock/manager/shifts', _with(GOOD_SHIFT, start_time='abc')),
    ('/timeclock/manager/shifts', _with(GOOD_SHIFT, end_time='2099-13-01T17:00')),
    ('/timeclock/manager/shifts', _with(GOOD_SHIFT, end_time='2099-08-01T08:00')),   # end before start
    ('/timeclock/manager/shifts', _with(GOOD_SHIFT, end_time='2099-08-01T09:00')),   # zero length
    ('/timeclock/manager/shifts', _with(GOOD_SHIFT, end_time='9999-12-31T23:59')),
    ('/timeclock/manager/shifts', _with(GOOD_SHIFT, start_time='0001-01-01T00:00')),
    # recurring rule
    ('/timeclock/manager/recurring/add', {}),
    ('/timeclock/manager/recurring/add', _with(GOOD_RULE, user_id='')),
    ('/timeclock/manager/recurring/add', _with(GOOD_RULE, user_id='abc')),
    ('/timeclock/manager/recurring/add', _with(GOOD_RULE, user_id='999')),
    ('/timeclock/manager/recurring/add', _with(GOOD_RULE, day_of_week='')),
    ('/timeclock/manager/recurring/add', _with(GOOD_RULE, day_of_week=None)),
    ('/timeclock/manager/recurring/add', _with(GOOD_RULE, day_of_week='7')),
    ('/timeclock/manager/recurring/add', _with(GOOD_RULE, day_of_week='-1')),
    ('/timeclock/manager/recurring/add', _with(GOOD_RULE, day_of_week='mon')),
    ('/timeclock/manager/recurring/add', _with(GOOD_RULE, start_time='')),
    ('/timeclock/manager/recurring/add', _with(GOOD_RULE, start_time='25:00')),
    ('/timeclock/manager/recurring/add', _with(GOOD_RULE, end_time='abc')),
    ('/timeclock/manager/recurring/add', _with(GOOD_RULE, end_time='08:00')),       # end before start
    ('/timeclock/manager/recurring/add', _with(GOOD_RULE, frequency='daily')),
    ('/timeclock/manager/recurring/add', _with(GOOD_RULE, frequency='biweekly', reference_date='abc')),
    ('/timeclock/manager/recurring/add', _with(GOOD_RULE, frequency='biweekly', reference_date='2026-02-30')),
    # schedule generation (a rule is seeded, so a valid range would write)
    ('/timeclock/manager/generate_schedule', {}),
    ('/timeclock/manager/generate_schedule', {'start_date': '', 'end_date': ''}),
    ('/timeclock/manager/generate_schedule', {'start_date': 'abc', 'end_date': '2099-08-31'}),
    ('/timeclock/manager/generate_schedule', {'start_date': '2099-08-01', 'end_date': '2099-02-30'}),
    ('/timeclock/manager/generate_schedule', {'start_date': '2099-08-31', 'end_date': '2099-08-01'}),
    ('/timeclock/manager/generate_schedule', {'start_date': '0001-01-01', 'end_date': '9999-12-31'}),
    # force clock in/out for a user that does not exist
    ('/timeclock/manager/force_clock_in/999', {}),
    ('/timeclock/manager/force_clock_out/999', {}),
    # timeclock settings
    ('/timeclock/manager/settings', {'pay_period_type': 'fortnightly'}),
    ('/timeclock/manager/settings', {'fulltime_hours': 'abc'}),
    ('/timeclock/manager/settings', {'fulltime_hours': 'nan'}),
    ('/timeclock/manager/settings', {'fulltime_hours': '-5'}),
    ('/timeclock/manager/settings', {'pay_period_start': 'abc'}),
    ('/timeclock/manager/settings', {'pay_period_start': '2026-02-30'}),
    ('/timeclock/manager/settings', {'pay_period_start': '2026-01-04', 'pay_period_type': 'yearly'}),
    # PTO balance
    ('/timeclock/manager/pto/update_balance', {}),
    ('/timeclock/manager/pto/update_balance', {'user_id': '2', 'amount': ''}),
    ('/timeclock/manager/pto/update_balance', {'user_id': '2'}),
    ('/timeclock/manager/pto/update_balance', {'user_id': '2', 'amount': 'abc'}),
    ('/timeclock/manager/pto/update_balance', {'user_id': '2', 'amount': 'nan'}),
    ('/timeclock/manager/pto/update_balance', {'user_id': '2', 'amount': 'inf'}),
    ('/timeclock/manager/pto/update_balance', {'user_id': '2', 'amount': '1e309'}),
    ('/timeclock/manager/pto/update_balance', {'user_id': '', 'amount': '8'}),
    ('/timeclock/manager/pto/update_balance', {'user_id': 'abc', 'amount': '8'}),
    ('/timeclock/manager/pto/update_balance', {'user_id': '999', 'amount': '8'}),
    # PTO approve/deny
    (f'/timeclock/manager/pto/action/{PENDING_FULL}', {}),
    (f'/timeclock/manager/pto/action/{PENDING_FULL}', {'action': ''}),
    (f'/timeclock/manager/pto/action/{PENDING_FULL}', {'action': 'delete'}),
    ('/timeclock/manager/pto/action/999', {'action': 'approve'}),
    (f'/timeclock/manager/pto/action/{PENDING_JUNK}', {'action': 'approve'}),   # stored times unusable
    (f'/timeclock/manager/pto/action/{APPROVED}', {'action': 'approve'}),       # would deduct twice
    (f'/timeclock/manager/pto/action/{APPROVED}', {'action': 'deny'}),
    # manual punch edits
    (f'/timeclock/manager/timesheet/edit/{ENTRY_ID}', {}),
    (f'/timeclock/manager/timesheet/edit/{ENTRY_ID}', {'clock_in': '', 'clock_out': '2026-07-15T17:00'}),
    (f'/timeclock/manager/timesheet/edit/{ENTRY_ID}', {'clock_in': 'abc', 'clock_out': ''}),
    (f'/timeclock/manager/timesheet/edit/{ENTRY_ID}', {'clock_in': '2026-07-15T09:00', 'clock_out': 'abc'}),
    (f'/timeclock/manager/timesheet/edit/{ENTRY_ID}', {'clock_in': '2026-07-15T09:00',
                                                      'clock_out': '2026-07-15T08:00'}),
    (f'/timeclock/manager/timesheet/edit/{ENTRY_ID}', {'clock_in': '9999-12-31T23:59', 'clock_out': ''}),
    (f'/timeclock/manager/timesheet/edit/{ENTRY_ID}', {'clock_in': '0001-01-01T00:00', 'clock_out': ''}),
    ('/timeclock/manager/timesheet/edit/999', {'clock_in': '2026-07-15T09:00', 'clock_out': '2026-07-15T17:00'}),
    # payroll report
    ('/timeclock/manager/export/report', {}),
    ('/timeclock/manager/export/report', {'period': ''}),
    ('/timeclock/manager/export/report', {'period': 'abc'}),
    ('/timeclock/manager/export/report', {'period': '2026-07-01'}),
    ('/timeclock/manager/export/report', {'period': 'abc|def'}),
    ('/timeclock/manager/export/report', {'period': '2026-13-01|2026-13-14'}),
    ('/timeclock/manager/export/report', {'period': '2026-07-14|2026-07-01'}),
    ('/timeclock/manager/export/report', {'period': '0001-01-01|9999-12-31'}),
    ('/timeclock/manager/export/report', {'period': 'custom'}),
    ('/timeclock/manager/export/report', _with(GOOD_REPORT_RANGE, custom_start='')),
    ('/timeclock/manager/export/report', _with(GOOD_REPORT_RANGE, custom_end='abc')),
    ('/timeclock/manager/export/report', _with(GOOD_REPORT_RANGE, custom_end='2026-06-01')),
    ('/timeclock/manager/export/report', _with(GOOD_REPORT_RANGE, custom_end='9999-12-31')),
    ('/timeclock/manager/export/report', _with(GOOD_REPORT_RANGE, user_ids=['abc'])),
    ('/timeclock/manager/export/report', _with(GOOD_REPORT_RANGE, user_ids=['999'])),
]

MANAGER_GETS = [
    ('/timeclock/manager/timesheets', {'date_from': 'abc'}),
    ('/timeclock/manager/timesheets', {'date_to': '2026-02-30'}),
    ('/timeclock/manager/timesheets', {'date_from': '0001-01-01'}),
    ('/timeclock/manager/timesheets', {'date_to': '9999-12-31'}),
    ('/timeclock/manager/timesheets', {'date_from': '2026-07-31', 'date_to': '2026-07-01'}),
    ('/timeclock/manager/timesheets', {'user_filter': 'abc'}),
    ('/timeclock/manager/timesheet/edit/999', {}),
]


@pytest.mark.parametrize('user_id', MANAGERS)
@pytest.mark.parametrize('url,data', MANAGER_POSTS)
def test_manager_post_junk_is_rejected(app, user_id, url, data):
    _assert_rejected(app, user_id, 'POST', url, data)


@pytest.mark.parametrize('user_id', MANAGERS)
@pytest.mark.parametrize('url,data', MANAGER_GETS)
def test_manager_get_junk_params_redirect(app, user_id, url, data):
    _assert_rejected(app, user_id, 'GET', url, data)


@pytest.mark.parametrize('url,data', MANAGER_POSTS[::7])
def test_staff_cannot_reach_manager_posts(app, url, data):
    # Refused before any validation: a page refusal redirects to the gateway
    for user_id in STAFF:
        resp = _assert_rejected(app, user_id, 'POST', url, data, ok_statuses=(302, 403))
        if resp.status_code == 302:
            assert urlparse(resp.headers['Location']).path == '/'


# --- staff forms -------------------------------------------------------------------

GOOD_PARTIAL = {'request_type': 'partial', 'date': SHIFT_DAY, 'start_time': '09:00',
                'end_time': '11:00', 'notes': ''}

PTO_POSTS = [
    {},
    {'request_type': '', 'date': SHIFT_DAY},
    {'request_type': 'vacation', 'date': SHIFT_DAY},
    {'request_type': 'full'},
    {'request_type': 'full', 'date': ''},
    {'request_type': 'full', 'date': 'abc'},
    {'request_type': 'full', 'date': '2099-02-30'},
    {'request_type': 'full', 'date': '9999-12-31'},
    {'request_type': 'full', 'date': '0001-01-01'},
    _with(GOOD_PARTIAL, date=''),
    _with(GOOD_PARTIAL, date='abc'),
    _with(GOOD_PARTIAL, start_time=''),
    _with(GOOD_PARTIAL, start_time=None, end_time=None),
    _with(GOOD_PARTIAL, end_time='abc'),
    _with(GOOD_PARTIAL, end_time='25:00'),
    _with(GOOD_PARTIAL, end_time='08:00'),     # end before start: negative hours
    _with(GOOD_PARTIAL, end_time='09:00'),     # zero hours
    _with(GOOD_PARTIAL, date='9999-12-31', start_time='23:00', end_time='23:59'),
]


@pytest.mark.parametrize('user_id', STAFF + MANAGERS)
@pytest.mark.parametrize('data', PTO_POSTS)
def test_pto_request_junk_is_rejected(app, user_id, data):
    _assert_rejected(app, user_id, 'POST', '/timeclock/request_pto', data)


@pytest.mark.parametrize('user_id', STAFF)
@pytest.mark.parametrize('url', ['/timeclock/clock_out', '/timeclock/break_start', '/timeclock/break_end'])
def test_clock_actions_in_wrong_state_are_rejected(app, user_id, url):
    # Not clocked in: nothing to close, nothing written
    _assert_rejected(app, user_id, 'POST', url, {'junk': 'abc'})


# --- valid input still works -------------------------------------------------------

def _count(app, sql, *params):
    conn = sqlite3.connect(app.config['DATABASE'])
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def test_valid_partial_pto_request_still_saved(fresh_app, fresh_client):
    """Sanity check on a fresh app that the validation did not block real requests."""
    conn = sqlite3.connect(fresh_app.config['DATABASE'])
    conn.execute('INSERT INTO user_pto_balances (user_id, balance_hours) VALUES (2, 10)')
    conn.commit()
    conn.close()
    login(fresh_client, CASHIER)
    resp = fresh_client.post('/timeclock/request_pto', data=GOOD_PARTIAL)
    assert resp.status_code == 302
    row = sqlite3.connect(fresh_app.config['DATABASE']).execute(
        'SELECT request_type, date, start_time, end_time, hours_requested, status FROM pto_requests').fetchone()
    assert row == ('partial', SHIFT_DAY, '09:00', '11:00', 2.0, 'pending')


def test_valid_manager_forms_still_write(fresh_app, fresh_client):
    login(fresh_client, ADMIN)
    db = fresh_app
    fresh_client.post('/timeclock/manager/shifts', data=GOOD_SHIFT)
    assert _count(db, 'SELECT COUNT(*) FROM scheduled_shifts') == 1
    fresh_client.post('/timeclock/manager/recurring/add', data=GOOD_RULE)
    assert _count(db, 'SELECT COUNT(*) FROM recurring_shift_rules') == 1
    # The rules page renders with the new rule (a junk day_of_week used to break it)
    assert fresh_client.get('/timeclock/manager/shifts?tab=recurring').status_code == 200
    fresh_client.post('/timeclock/manager/generate_schedule',
                      data={'start_date': '2099-09-01', 'end_date': '2099-09-30'})
    assert _count(db, 'SELECT COUNT(*) FROM scheduled_shifts') == 1 + 5   # five Tuesdays
    fresh_client.post('/timeclock/manager/pto/update_balance', data={'user_id': '2', 'amount': '12.5'})
    assert _count(db, 'SELECT balance_hours FROM user_pto_balances WHERE user_id = 2') == 12.5
    fresh_client.post('/timeclock/manager/force_clock_in/2')
    assert _count(db, 'SELECT COUNT(*) FROM time_entries WHERE user_id = 2 AND clock_out IS NULL') == 1
    resp = fresh_client.post('/timeclock/manager/export/report', data=_with(GOOD_REPORT_RANGE, user_ids=['2']))
    assert resp.status_code == 200


def test_pto_approval_happens_once(fresh_app, fresh_client):
    conn = sqlite3.connect(fresh_app.config['DATABASE'])
    conn.execute('INSERT INTO user_pto_balances (user_id, balance_hours) VALUES (2, 10)')
    conn.execute("INSERT INTO pto_requests (user_id, request_type, date, start_time, end_time, hours_requested) "
                 "VALUES (2, 'partial', '2026-01-15', '13:00', '17:00', 4)")
    conn.commit()
    conn.close()
    login(fresh_client, ADMIN)
    for _ in range(2):
        assert fresh_client.post('/timeclock/manager/pto/action/1', data={'action': 'approve'}).status_code == 302
    assert _count(fresh_app, 'SELECT balance_hours FROM user_pto_balances WHERE user_id = 2') == 6
    assert _count(fresh_app, "SELECT COUNT(*) FROM time_entries WHERE type = 'pto'") == 1
