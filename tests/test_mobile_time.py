"""Phone UI, Time (/m/time): clocking through the desktop's own handlers,
pay period hours, schedule, time-off requests and the managers' team view."""
import re
import sqlite3

from tests.conftest import login


def _db(app):
    conn = sqlite3.connect(app.config['DATABASE'])
    conn.row_factory = sqlite3.Row
    return conn


def _run(app, sql, args=()):
    conn = _db(app)
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def _open_entries(app, user_id=1):
    conn = _db(app)
    rows = conn.execute('SELECT type FROM time_entries WHERE user_id = ? AND clock_out IS NULL', (user_id,)).fetchall()
    conn.close()
    return [r['type'] for r in rows]


def _page(client, path='/m/time'):
    return client.get(path).get_data(as_text=True)


def test_clock_in_break_and_out_from_the_phone(fresh_app, fresh_client):
    login(fresh_client, 1)
    html = _page(fresh_client)
    assert 'Clocked out' in html and 'action="/m/time/clock/in"' in html

    resp = fresh_client.post('/m/time/clock/in')
    assert resp.status_code == 302 and resp.headers['Location'].endswith('/m/time')
    html = _page(fresh_client)
    assert 'Clocked in' in html                       # the desktop's message in the phone's words
    assert 'Working' in html and 'action="/m/time/clock/break"' in html
    assert re.search(r'action="/m/time/clock/out" data-confirm="Clock out now\?"', html)
    assert _open_entries(fresh_app) == ['shift']

    fresh_client.post('/m/time/clock/break')
    html = _page(fresh_client)
    assert 'On break' in html and 'action="/m/time/clock/back"' in html
    assert _open_entries(fresh_app) == ['break']

    fresh_client.post('/m/time/clock/back')
    assert 'Break over, back to work' in _page(fresh_client)
    fresh_client.post('/m/time/clock/out')
    html = _page(fresh_client)
    assert 'Clocked out' in html and 'Last shift ended Today' in html
    assert _open_entries(fresh_app) == []


def test_wrong_state_and_unknown_actions(fresh_client):
    login(fresh_client, 1)
    fresh_client.post('/m/time/clock/back')
    assert 'You are not on a break.' in _page(fresh_client)    # the desktop's check, passed through
    fresh_client.post('/m/time/clock/in')
    fresh_client.post('/m/time/clock/in')
    assert 'You are already clocked in!' in _page(fresh_client)
    assert fresh_client.post('/m/time/clock/nap').status_code == 404


def test_time_module_off_or_not_permitted_is_forbidden(fresh_client, monkeypatch):
    import app.routes.mobile as mobile
    monkeypatch.setattr(mobile, 'load_config', lambda *a, **k: {'INVENTORY_ENABLED': True})
    login(fresh_client, 1)
    for path in ('/m/time', '/m/time/schedule', '/m/time/off', '/m/time/team'):
        assert fresh_client.get(path).status_code == 403
    assert fresh_client.post('/m/time/clock/in').status_code == 403


def test_pay_period_hours_and_next_shift(fresh_app, fresh_client):
    _run(fresh_app, "INSERT INTO time_entries (user_id, type, clock_in, clock_out) VALUES "
                    "(1, 'shift', datetime('now', '-9 hours'), datetime('now', '-1 hours'))")
    _run(fresh_app, "INSERT INTO scheduled_shifts (user_id, start_time, end_time) VALUES "
                    "(1, datetime('now', '+2 days'), datetime('now', '+2 days', '+8 hours'))")
    login(fresh_client, 1)
    html = _page(fresh_client)
    assert re.search(r'<dt>Total hrs</dt><dd>8</dd>', html)
    assert 'Next shift' in html and 'Nothing scheduled' not in html


def test_schedule_lists_the_coming_shifts(fresh_app, fresh_client):
    _run(fresh_app, "INSERT INTO scheduled_shifts (user_id, start_time, end_time) VALUES "
                    "(1, datetime('now', '-1 hours'), datetime('now', '+7 hours'))")      # on now
    _run(fresh_app, "INSERT INTO scheduled_shifts (user_id, start_time, end_time) VALUES "
                    "(1, datetime('now', '+3 days'), datetime('now', '+3 days', '+6 hours'))")
    _run(fresh_app, "INSERT INTO scheduled_shifts (user_id, start_time, end_time) VALUES "
                    "(1, datetime('now', '+30 days'), datetime('now', '+30 days', '+6 hours'))")  # too far out
    _run(fresh_app, "INSERT INTO scheduled_shifts (user_id, start_time, end_time) VALUES "
                    "(2, datetime('now', '+1 days'), datetime('now', '+1 days', '+5 hours'))")    # someone else's
    login(fresh_client, 1)
    html = _page(fresh_client, '/m/time/schedule')
    assert html.count('class="m-row" role="listitem"') == 2
    assert '8 hrs' in html and '6 hrs' in html and '5 hrs' not in html
    assert 'm-pill m-tone-ok">Now<' in html
    assert 'Shift now' in _page(fresh_client)


def test_time_off_request_runs_the_desktop_handler(fresh_app, fresh_client):
    _run(fresh_app, 'INSERT INTO user_pto_balances (user_id, balance_hours) VALUES (1, 16)')
    _run(fresh_app, "INSERT INTO scheduled_shifts (user_id, start_time, end_time) VALUES "
                    "(1, datetime('now', '+5 days', 'start of day', '+16 hours'), "
                    "datetime('now', '+5 days', 'start of day', '+20 hours'))")
    # The shift's business-local date, as the handler looks it up
    from app.services import timeclock_hours as tc
    conn = _db(fresh_app)
    start = conn.execute('SELECT start_time FROM scheduled_shifts WHERE user_id = 1').fetchone()[0]
    conn.close()
    day = tc.to_local(start).date().isoformat()

    login(fresh_client, 1)
    html = _page(fresh_client, '/m/time/off')
    assert '16 hrs' in html and 'name="request_type" value="full"' in html and 'No requests yet.' in html

    resp = fresh_client.post('/m/time/off', data={'request_type': 'full', 'date': day, 'notes': 'Dentist'})
    assert resp.status_code == 302 and resp.headers['Location'].endswith('/m/time/off')
    html = _page(fresh_client, '/m/time/off')
    assert 'PTO Request Sent.' in html
    assert 'Full shift' in html and 'm-pill m-tone-wn">Pending<' in html and '4 hrs' in html
    conn = _db(fresh_app)
    row = conn.execute('SELECT request_type, hours_requested, status FROM pto_requests WHERE user_id = 1').fetchone()
    conn.close()
    assert (row['request_type'], row['hours_requested'], row['status']) == ('full', 4, 'pending')


def test_time_off_errors_come_back_to_the_phone(fresh_client):
    login(fresh_client, 1)
    resp = fresh_client.post('/m/time/off', data={'request_type': 'partial', 'date': '2999-01-01',
                                                  'start_time': 'later', 'end_time': ''})
    assert resp.headers['Location'].endswith('/m/time/off')
    assert 'Please enter a valid start and end time.' in _page(fresh_client, '/m/time/off')


def test_team_view_is_for_managers(fresh_app, fresh_client):
    _run(fresh_app, "INSERT INTO time_entries (user_id, type, clock_in) VALUES (2, 'break', datetime('now', '-12 minutes'))")
    login(fresh_client, 2)                                  # operator: no timeclock.manage
    assert 'href="/m/time/team"' not in _page(fresh_client)
    assert fresh_client.get('/m/time/team').status_code == 403
    login(fresh_client, 1)
    assert 'href="/m/time/team"' in _page(fresh_client)
    html = _page(fresh_client, '/m/time/team')
    assert 'On the clock now · 1' in html and 'On break · since' in html and '12m' in html
    assert 'href="/timeclock/manager"' in html and 'href="/timeclock/manager/pto"' in html


def test_home_links_to_the_phone_time_page(fresh_client):
    login(fresh_client, 1)
    html = fresh_client.get('/m/').get_data(as_text=True)
    assert re.search(r'<a class="m-row" href="/m/time">\s*<span class="m-lead m-tone-nu">', html)
