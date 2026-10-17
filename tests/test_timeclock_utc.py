"""Timeclock times are stored in UTC and shown/typed in business-local time.

The business timezone is pinned to America/Denver here so the tests do not
depend on the machine's timezone.
"""
import datetime
import sqlite3

import pytest

from app.services import migration
from app.services import timeclock_hours as tc
from tests.conftest import login


@pytest.fixture(autouse=True)
def denver(monkeypatch):
    zoneinfo = pytest.importorskip('zoneinfo')
    try:
        tz = zoneinfo.ZoneInfo('America/Denver')
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip('tz database unavailable (pip install tzdata)')
    monkeypatch.setattr(tc, 'LOCAL_TZ', tz)
    monkeypatch.setattr(migration, '_M7_LOCAL_TZ', tz)
    return tz


def _db(app):
    conn = sqlite3.connect(app.config['DATABASE'])
    conn.row_factory = sqlite3.Row
    return conn


def _add_entry(app, clock_in, clock_out=None, user_id=1, kind='shift'):
    conn = _db(app)
    cur = conn.execute('INSERT INTO time_entries (user_id, type, clock_in, clock_out) VALUES (?, ?, ?, ?)',
                       (user_id, kind, clock_in, clock_out))
    conn.commit()
    conn.close()
    return cur.lastrowid


# --- storage ------------------------------------------------------------------

def test_clock_in_stores_utc(fresh_app, fresh_client):
    login(fresh_client, 1)
    before = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0)
    fresh_client.post('/timeclock/clock_in')
    after = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    conn = _db(fresh_app)
    row = conn.execute('SELECT clock_in, created_at FROM time_entries WHERE user_id = 1').fetchone()
    conn.close()
    stored = datetime.datetime.strptime(row['clock_in'], tc.FMT)
    assert before <= stored <= after
    # created_at now defaults to CURRENT_TIMESTAMP (UTC) too
    created = datetime.datetime.strptime(row['created_at'], tc.FMT)
    assert abs((created - stored).total_seconds()) < 5


def test_clock_out_and_breaks_store_utc(fresh_app, fresh_client):
    login(fresh_client, 1)
    for url in ('/timeclock/clock_in', '/timeclock/break_start', '/timeclock/break_end',
                '/timeclock/clock_out'):
        fresh_client.post(url)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    conn = _db(fresh_app)
    rows = conn.execute('SELECT type, clock_in, clock_out FROM time_entries ORDER BY id').fetchall()
    conn.close()
    assert [r['type'] for r in rows] == ['shift', 'break', 'shift']
    for r in rows:
        for value in (r['clock_in'], r['clock_out']):
            assert abs((now - datetime.datetime.strptime(value, tc.FMT)).total_seconds()) < 60


def test_force_clock_in_and_out_store_utc(fresh_app, fresh_client):
    login(fresh_client, 1)
    fresh_client.post('/timeclock/manager/force_clock_in/2')
    fresh_client.post('/timeclock/manager/force_clock_out/2')
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    conn = _db(fresh_app)
    row = conn.execute('SELECT clock_in, clock_out FROM time_entries WHERE user_id = 2').fetchone()
    conn.close()
    for value in (row['clock_in'], row['clock_out']):
        assert abs((now - datetime.datetime.strptime(value, tc.FMT)).total_seconds()) < 60


# --- display / input ------------------------------------------------------------

def test_status_shows_local_time(fresh_app, fresh_client):
    _add_entry(fresh_app, '2026-07-15 15:05:00')  # 09:05 MDT
    login(fresh_client, 1)
    html = fresh_client.get('/timeclock/').get_data(as_text=True)
    assert '09:05 AM' in html


def test_manager_dashboard_shows_local_time(fresh_app, fresh_client):
    _add_entry(fresh_app, '2026-01-15 17:30:00', user_id=2)  # 10:30 MST
    login(fresh_client, 1)
    html = fresh_client.get('/timeclock/manager').get_data(as_text=True)
    assert '10:30 AM' in html


def test_edit_form_round_trips_local_time(fresh_app, fresh_client):
    entry_id = _add_entry(fresh_app, '2026-07-15 15:00:00', '2026-07-15 23:00:00')
    login(fresh_client, 1)
    html = fresh_client.get(f'/timeclock/manager/timesheet/edit/{entry_id}').get_data(as_text=True)
    assert 'value="2026-07-15T09:00"' in html
    assert 'value="2026-07-15T17:00"' in html

    # A January edit is typed in MST and stored with that date's offset
    fresh_client.post(f'/timeclock/manager/timesheet/edit/{entry_id}',
                      data={'clock_in': '2026-01-15T09:00', 'clock_out': '2026-01-15T17:00', 'notes': ''})
    conn = _db(fresh_app)
    row = conn.execute('SELECT clock_in, clock_out FROM time_entries WHERE id = ?', (entry_id,)).fetchone()
    conn.close()
    assert (row['clock_in'], row['clock_out']) == ('2026-01-15 16:00:00', '2026-01-16 00:00:00')


def test_timesheets_filter_by_local_day(fresh_app, fresh_client):
    _add_entry(fresh_app, '2026-07-16 03:00:00', '2026-07-16 04:00:00')  # Jul 15 21:00 local
    login(fresh_client, 1)
    html = fresh_client.get('/timeclock/manager/timesheets?date_from=2026-07-15&date_to=2026-07-15'
                            ).get_data(as_text=True)
    assert '07/15/2026' in html and '9:00 PM' in html
    html = fresh_client.get('/timeclock/manager/timesheets?date_from=2026-07-16&date_to=2026-07-16'
                            ).get_data(as_text=True)
    assert '9:00 PM' not in html


def test_one_time_shift_input_is_local(fresh_app, fresh_client):
    login(fresh_client, 1)
    fresh_client.post('/timeclock/manager/shifts',
                      data={'user_id': '2', 'start_time': '2099-07-15T09:00', 'end_time': '2099-07-15T17:00'})
    conn = _db(fresh_app)
    row = conn.execute('SELECT start_time, end_time FROM scheduled_shifts').fetchone()
    conn.close()
    assert (row['start_time'], row['end_time']) == ('2099-07-15 15:00:00', '2099-07-15 23:00:00')


def test_recurring_rule_is_local_wall_time_on_both_sides_of_dst(fresh_app, fresh_client):
    login(fresh_client, 1)
    # Saturdays (5) at 09:00-17:00; Oct 31 is MDT, Nov 7 is MST
    fresh_client.post('/timeclock/manager/recurring/add',
                      data={'user_id': '2', 'day_of_week': '5', 'start_time': '09:00', 'end_time': '17:00'})
    fresh_client.post('/timeclock/manager/generate_schedule',
                      data={'start_date': '2026-10-31', 'end_date': '2026-11-07'})
    fresh_client.post('/timeclock/manager/generate_schedule',  # idempotent
                      data={'start_date': '2026-10-31', 'end_date': '2026-11-07'})
    conn = _db(fresh_app)
    rows = conn.execute('SELECT start_time, end_time FROM scheduled_shifts ORDER BY start_time').fetchall()
    conn.close()
    assert [tuple(r) for r in rows] == [('2026-10-31 15:00:00', '2026-10-31 23:00:00'),
                                        ('2026-11-07 16:00:00', '2026-11-08 00:00:00')]


def test_partial_pto_approval_logs_utc_entry(fresh_app, fresh_client):
    conn = _db(fresh_app)
    conn.execute("INSERT INTO user_pto_balances (user_id, balance_hours) VALUES (2, 10)")
    conn.execute("INSERT INTO pto_requests (user_id, request_type, date, start_time, end_time, hours_requested) "
                 "VALUES (2, 'partial', '2026-01-15', '13:00', '17:00', 4)")
    conn.commit()
    conn.close()
    login(fresh_client, 1)
    fresh_client.post('/timeclock/manager/pto/action/1', data={'action': 'approve'})
    conn = _db(fresh_app)
    row = conn.execute("SELECT clock_in, clock_out FROM time_entries WHERE type = 'pto'").fetchone()
    conn.close()
    assert (row['clock_in'], row['clock_out']) == ('2026-01-15 20:00:00', '2026-01-16 00:00:00')


# --- payroll report -------------------------------------------------------------

def test_payroll_report_uses_local_days_and_real_hours(fresh_app, fresh_client):
    period = '2026-10-25|2026-11-07'
    # Before the period: Sat Oct 24 23:00-23:30 local (Oct 25 UTC) -> excluded
    _add_entry(fresh_app, tc.local_to_utc_str('2026-10-24 23:00'), tc.local_to_utc_str('2026-10-24 23:30'))
    # Tue Oct 27 10:00-18:00 local: 8h
    _add_entry(fresh_app, tc.local_to_utc_str('2026-10-27 10:00'), tc.local_to_utc_str('2026-10-27 18:00'))
    # Sat Oct 31 22:00 MDT -> Sun Nov 1 06:00 MST across the fall-back: 9 real hours
    _add_entry(fresh_app, tc.local_to_utc_str('2026-10-31 22:00'), tc.local_to_utc_str('2026-11-01 06:00'))
    # Last day of the period, Sat Nov 7 17:00-23:00 local (Nov 8 UTC) -> included: 6h
    _add_entry(fresh_app, tc.local_to_utc_str('2026-11-07 17:00'), tc.local_to_utc_str('2026-11-07 23:00'))
    login(fresh_client, 1)
    html = fresh_client.post('/timeclock/manager/export/report',
                             data={'period': period, 'user_ids': ['1']}).get_data(as_text=True)
    assert 'Total: 23.0' in html
    assert '10:00 AM' in html and '06:00 PM' in html   # local times shown
    assert '10/24/2026' not in html
    assert '11/07/2026' in html


def test_pay_period_hours_on_home_page(fresh_app, fresh_client, monkeypatch):
    from app.routes import timeclock as routes
    monkeypatch.setattr(routes, 'get_current_pay_period',
                        lambda: (datetime.date(2026, 10, 25), datetime.date(2026, 11, 7)))
    _add_entry(fresh_app, tc.local_to_utc_str('2026-10-31 22:00'), tc.local_to_utc_str('2026-11-01 06:00'))
    login(fresh_client, 1)
    html = fresh_client.get('/timeclock/').get_data(as_text=True)
    assert '9.0 hrs' in html


# --- migration 7 ----------------------------------------------------------------

def _legacy_db(path):
    """Timeclock tables as the baseline created them (local-time defaults)."""
    conn = sqlite3.connect(path, isolation_level=None)
    migration._create_timeclock_tables(conn)
    migration._create_scheduled_shifts_table(conn)
    return conn


def test_migration_7_converts_local_rows_to_utc(tmp_path):
    conn = _legacy_db(str(tmp_path / 'legacy.db'))
    conn.executemany(
        'INSERT INTO time_entries (id, user_id, type, clock_in, clock_out, created_at) VALUES (?, 1, ?, ?, ?, ?)', [
            (1, 'shift', '2026-01-15 09:00:00', '2026-01-15 17:30:00', '2026-01-15 09:00:00'),  # MST
            (2, 'shift', '2026-07-15 09:00:00', '2026-07-15 17:30:00', '2026-07-15 09:00:00'),  # MDT
            (3, 'shift', '2026-07-16 09:00:00', None, '2026-07-16 09:00:00'),                   # still open
        ])
    # malformed clock_in, clock_out without seconds, created_at from the local default
    conn.execute("INSERT INTO time_entries (id, user_id, clock_in, clock_out) VALUES (9, 1, 'garbage', '2026-07-17 17:00')")
    conn.execute("INSERT INTO scheduled_shifts (id, user_id, start_time, end_time, created_at) "
                 "VALUES (1, 2, '2026-01-20 10:00:00', '2026-01-20 18:00:00', '2026-01-10 08:00:00'), "
                 "       (2, 2, '2026-07-20 10:00:00', '2026-07-20 18:00:00', '2026-07-10 08:00:00')")

    conn.execute('BEGIN IMMEDIATE')
    migration._m7_timeclock_utc(conn)
    conn.execute('COMMIT')

    rows = {r[0]: r[1:] for r in conn.execute(
        'SELECT id, clock_in, clock_out, created_at FROM time_entries').fetchall()}
    assert rows[1] == ('2026-01-15 16:00:00', '2026-01-16 00:30:00', '2026-01-15 16:00:00')
    assert rows[2] == ('2026-07-15 15:00:00', '2026-07-15 23:30:00', '2026-07-15 15:00:00')
    assert rows[3] == ('2026-07-16 15:00:00', None, '2026-07-16 15:00:00')
    # malformed value left alone, the good value in the same row still converted;
    # created_at default was local at insert time and is converted as well
    assert rows[9][:2] == ('garbage', '2026-07-17 23:00:00')

    shifts = conn.execute('SELECT start_time, end_time, created_at FROM scheduled_shifts ORDER BY id').fetchall()
    assert shifts == [('2026-01-20 17:00:00', '2026-01-21 01:00:00', '2026-01-10 15:00:00'),
                      ('2026-07-20 16:00:00', '2026-07-21 00:00:00', '2026-07-10 14:00:00')]

    # Schema rebuilt: created_at now defaults to UTC, indexes kept, ids keep counting up
    for table in ('time_entries', 'scheduled_shifts'):
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
        assert 'localtime' not in sql and 'CURRENT_TIMESTAMP' in sql
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {'idx_time_entries_user_date', 'idx_shifts_user_start'} <= indexes
    cur = conn.execute("INSERT INTO time_entries (user_id, clock_in) VALUES (1, '2026-10-01 16:00:00')")
    assert cur.lastrowid == 10
    created = conn.execute('SELECT created_at FROM time_entries WHERE id = 10').fetchone()[0]
    utc_now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    assert abs((utc_now - datetime.datetime.strptime(created, tc.FMT)).total_seconds()) < 60
    conn.close()


def test_migration_7_is_registered_and_fresh_schema_is_utc(fresh_app):
    assert (7, 'timeclock times to UTC', migration._m7_timeclock_utc) in migration.SCHEMA_MIGRATIONS
    conn = _db(fresh_app)
    for table in ('time_entries', 'scheduled_shifts'):
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
        assert 'localtime' not in sql
    conn.close()
