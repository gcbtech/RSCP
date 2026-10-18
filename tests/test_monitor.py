"""
Email alerts (app/services/monitor.py, the `monitor` job): failed and stale
jobs/backups, customer displays gone quiet during store hours, "last seen"
in the register's device list, the throttled last_seen write, the settings
and their admin form. No real email is sent: pos_email.send_email is
replaced by a recorder (or smtplib.SMTP by a fake, for the sender itself).

Store hours are America/Denver; times below are built in Denver time and
converted to UTC, so the results don't depend on the machine's timezone.
"""
import datetime
import sqlite3

import pytest

from tests.conftest import login

UTC = datetime.timezone.utc


@pytest.fixture(scope='module')
def denver():
    zoneinfo = pytest.importorskip('zoneinfo')
    try:
        return zoneinfo.ZoneInfo('America/Denver')
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip('tz database unavailable (pip install tzdata)')


@pytest.fixture
def at(denver):
    """at(day, hour, minute) -> aware UTC datetime for 2026-10-<day> in Denver.
    2026-10-19 is a Monday, -20 a Tuesday, -24 a Saturday, -25 a Sunday."""
    return lambda day, hour, minute=0: datetime.datetime(2026, 10, day, hour, minute, tzinfo=denver).astimezone(UTC)


def _sql_utc(dt):
    """How SQLite's CURRENT_TIMESTAMP stores a time."""
    return dt.astimezone(UTC).strftime('%Y-%m-%d %H:%M:%S')


def _db(app):
    conn = sqlite3.connect(app.config['DATABASE'])
    conn.row_factory = sqlite3.Row
    return conn


class Mon:
    """The monitor under test: email set up, a recording mailer, no backup
    status, and the server's local clock (job_status, status.json) in Denver
    time, as on the store's server."""

    def __init__(self, app, monkeypatch, denver):
        from app import backup
        from app.services import monitor, pos_email
        monkeypatch.setattr(monitor, '_server_local', lambda now: now.astimezone(denver).replace(tzinfo=None))
        self.app, self.db = app, app.config['DATABASE']
        self.sent, self.fail = [], None

        def send_email(subject, html, recipients, text=None):
            if self.fail:
                raise OSError(self.fail)
            self.sent.append({'subject': subject, 'html': html, 'to': recipients, 'text': text})
        monkeypatch.setattr(pos_email, 'send_email', send_email)
        self.backup_status = None
        monkeypatch.setattr(backup, 'read_status', lambda root=None: self.backup_status)
        self.settings(POS_EMAIL_HOST='smtp.test', POS_EMAIL_RECIPIENTS='owner@example.test')

    def settings(self, **values):
        from app.services import settings as st
        with self.app.app_context():
            st.set_many(values)

    def check(self, now):
        from app.services import monitor
        with self.app.app_context():
            return monitor.run_checks(now=now)

    def subjects(self):
        return [m['subject'] for m in self.sent]

    def open_keys(self):
        conn = _db(self.app)
        try:
            return {r['key'] for r in conn.execute('SELECT key FROM monitor_alerts')}
        finally:
            conn.close()


@pytest.fixture
def mon(fresh_app, monkeypatch, denver):
    return Mon(fresh_app, monkeypatch, denver)


# ---------------------------------------------------------------------------
# Schema, settings, store hours
# ---------------------------------------------------------------------------

def test_monitor_alerts_table_comes_from_schema_migration_14(fresh_app):
    from app.services.migration import LATEST_SCHEMA_VERSION, SCHEMA_MIGRATIONS
    assert 14 in {v for v, _, _ in SCHEMA_MIGRATIONS} and LATEST_SCHEMA_VERSION >= 14
    conn = _db(fresh_app)
    cols = {r[1] for r in conn.execute('PRAGMA table_info(monitor_alerts)')}
    conn.close()
    assert {'key', 'kind', 'subject', 'title', 'since', 'last_sent', 'detail'} <= cols


def test_alert_settings_defaults_and_validation(fresh_app):
    from app.services import settings as st
    with fresh_app.app_context():
        assert st.get('ALERT_JOB_FAILURES') is True
        assert st.get('ALERT_DISPLAY_OFFLINE') is True
        assert st.get('ALERT_DISPLAY_OFFLINE_MINUTES') == 10
        assert st.get('ALERT_EMAIL_RECIPIENTS') == ''
        assert st.set('ALERT_EMAIL_RECIPIENTS', ' a@x.test; b@y.test ,') == 'a@x.test, b@y.test'
        for bad in ({'ALERT_EMAIL_RECIPIENTS': 'not an address'}, {'ALERT_DISPLAY_OFFLINE_MINUTES': 1},
                    {'ALERT_DISPLAY_OFFLINE_MINUTES': 241}):
            with pytest.raises(st.SettingsError):
                st.set_many(bad)


def test_alert_settings_save_through_the_admin_form(fresh_app, fresh_client):
    from app.services import settings as st
    login(fresh_client, 1)
    resp = fresh_client.post('/admin/settings/save', data={
        'group': 'notifications', 'ALERT_JOB_FAILURES': ['false', 'true'], 'ALERT_DISPLAY_OFFLINE': 'false',
        'ALERT_DISPLAY_OFFLINE_MINUTES': '15', 'ALERT_EMAIL_RECIPIENTS': 'alerts@example.test'})
    assert resp.status_code == 302
    with fresh_app.app_context():
        st.invalidate()
        assert st.get_many(['ALERT_JOB_FAILURES', 'ALERT_DISPLAY_OFFLINE', 'ALERT_DISPLAY_OFFLINE_MINUTES',
                            'ALERT_EMAIL_RECIPIENTS']) == {
            'ALERT_JOB_FAILURES': True, 'ALERT_DISPLAY_OFFLINE': False,
            'ALERT_DISPLAY_OFFLINE_MINUTES': 15, 'ALERT_EMAIL_RECIPIENTS': 'alerts@example.test'}

    login(fresh_client, 3)                                # POS manager: not an admin setting for them
    resp = fresh_client.post('/admin/settings/save', data={'group': 'notifications', 'ALERT_DISPLAY_OFFLINE': 'true'})
    assert resp.status_code == 403


def test_admin_panel_shows_the_email_alerts_form(mon, fresh_client, at):
    login(fresh_client, 1)
    html = fresh_client.get('/admin/').get_data(as_text=True)
    assert 'Email Alerts' in html and 'name="ALERT_DISPLAY_OFFLINE_MINUTES"' in html
    assert 'owner@example.test' in html and 'Email is not set up' not in html
    assert 'Tuesday-Saturday 10:00-18:00' in html

    conn = _db(mon.app)
    conn.execute("INSERT INTO monitor_alerts (key, kind, subject, title, since) "
                 "VALUES ('display_offline:p1', 'display_offline', 'p1', 'Front Display (Register 1)', '10:02')")
    conn.commit()
    conn.close()
    html = fresh_client.get('/admin/').get_data(as_text=True)
    assert 'Open alerts' in html and 'Front Display (Register 1) offline' in html

    mon.settings(POS_EMAIL_HOST='')
    assert 'Email is not set up' in fresh_client.get('/admin/').get_data(as_text=True)


def test_store_hours(at):
    from app.services import monitor
    assert monitor.store_open_for(at(20, 10, 5)) == datetime.timedelta(minutes=5)   # Tuesday
    assert monitor.store_open_for(at(24, 17, 59)) is not None                       # Saturday
    for closed in (at(20, 9, 59), at(24, 18, 0), at(19, 12), at(25, 12)):           # before/after, Mon, Sun
        assert monitor.store_open_for(closed) is None
    # Denver, not UTC: 17:00 UTC on a Tuesday in October is 11:00 in the store
    assert monitor.store_open_for(datetime.datetime(2026, 10, 20, 17, 0, tzinfo=UTC)) == datetime.timedelta(hours=1)


# ---------------------------------------------------------------------------
# Email set-up and sending
# ---------------------------------------------------------------------------

def test_does_nothing_until_email_is_set_up(mon, at):
    mon.settings(POS_EMAIL_HOST='', POS_EMAIL_RECIPIENTS='')
    _fail_job(mon)
    assert 'not set up' in mon.check(at(20, 12))
    assert mon.sent == [] and mon.open_keys() == set()


def test_alert_recipients_override_the_report_recipients(mon, at):
    _fail_job(mon)
    mon.check(at(20, 12))
    assert mon.sent[-1]['to'] == 'owner@example.test'
    _clear_alerts(mon)
    mon.settings(ALERT_EMAIL_RECIPIENTS='alerts@example.test')
    mon.check(at(20, 12, 2))
    assert mon.sent[-1]['to'] == 'alerts@example.test'


def test_send_email_uses_the_pos_smtp_settings(fresh_app, monkeypatch):
    from app.services import pos_email
    from app.services import settings as st
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent.append({'host': host, 'port': port})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            sent[-1]['tls'] = True

        def login(self, user, password):
            sent[-1]['login'] = (user, password)

        def sendmail(self, sender, recipients, message):
            sent[-1].update(sender=sender, recipients=recipients, message=message)

    monkeypatch.setattr(pos_email.smtplib, 'SMTP', FakeSMTP)
    with fresh_app.app_context():
        st.set_many({'POS_EMAIL_HOST': 'smtp.test', 'POS_EMAIL_PORT': 2525, 'POS_EMAIL_USER': 'rscp@example.test'})
        pos_email.send_email('[RSCP] Test', '<p>hi</p>', 'a@x.test, b@y.test', text='hi')
        st.set('POS_EMAIL_HOST', '')
        with pytest.raises(ValueError):
            pos_email.send_email('x', 'y', ['a@x.test'])
    m = sent[0]
    assert (m['host'], m['port'], m['tls']) == ('smtp.test', 2525, True) and 'login' not in m
    assert m['recipients'] == ['a@x.test', 'b@y.test'] and m['sender'] == 'rscp@example.test'
    assert 'Subject: [RSCP] Test' in m['message'] and 'text/plain' in m['message'] and 'text/html' in m['message']


def test_failed_send_records_nothing_and_is_retried(mon, at, monkeypatch, tmp_path):
    from app import jobs
    from app.services import monitor
    monkeypatch.setattr(jobs, 'lock_dir', lambda: str(tmp_path / 'locks'))
    _fail_job(mon)
    mon.fail = 'connection refused'
    with pytest.raises(monitor.AlertSendError, match='connection refused'):
        mon.check(at(20, 12))
    assert mon.open_keys() == set() and mon.sent == []

    # As a job: recorded as failed, with the reason
    result = jobs.run_job('monitor', db_path=mon.db)
    assert result.state == 'failed' and 'connection refused' in result.error
    assert jobs.read_status(mon.db)['monitor']['last_result'] == 'failed'
    # The next run sends it; the monitor's own failure is not alerted on
    mon.fail = None
    result = jobs.run_job('monitor', db_path=mon.db)
    assert result.state == 'ok', result
    assert mon.subjects() == ['[Test Store RSCP] Package status upkeep failed']
    assert mon.open_keys() == {'job_failed:package-maintenance'}


# ---------------------------------------------------------------------------
# Jobs and backups
# ---------------------------------------------------------------------------

def _boom():
    raise RuntimeError('disk on fire')


def _fail_job(mon, name='package-maintenance'):
    _set_job(mon, name, 'failed', error='RuntimeError: disk on fire')


def _set_job(mon, name, result, error=None, last_success=None, failures=1):
    conn = _db(mon.app)
    conn.execute('INSERT INTO job_status (name) VALUES (?) ON CONFLICT(name) DO NOTHING', (name,))
    if result == 'failed':
        conn.execute("UPDATE job_status SET last_result = 'failed', last_error = ?, last_failure = ?, "
                     "last_finished = ?, consecutive_failures = ? WHERE name = ?",
                     (error, '2026-10-20T11:58:00', '2026-10-20T11:58:00', failures, name))
    else:
        conn.execute("UPDATE job_status SET last_result = 'ok', last_success = ?, consecutive_failures = 0 "
                     "WHERE name = ?", (last_success or '2026-10-20T12:30:00', name))
    conn.commit()
    conn.close()


def _clear_alerts(mon):
    conn = _db(mon.app)
    conn.execute('DELETE FROM monitor_alerts')
    conn.commit()
    conn.close()


def test_job_failure_is_mailed_once_then_reminded_then_recovered(mon, at):
    _fail_job(mon)
    mon.check(at(20, 12))
    assert mon.subjects() == ['[Test Store RSCP] Package status upkeep failed']
    assert 'RuntimeError: disk on fire' in mon.sent[0]['text'] and 'disk on fire' in mon.sent[0]['html']
    assert mon.open_keys() == {'job_failed:package-maintenance'}

    for minutes in (2, 4, 30, 5 * 60):                    # every check while it keeps failing
        _fail_job(mon)
        mon.check(at(20, 12) + datetime.timedelta(minutes=minutes))
    assert len(mon.sent) == 1

    mon.check(at(20, 18, 1))                              # 6 hours after the first mail
    assert mon.subjects()[-1] == '[Test Store RSCP] Package status upkeep is still failing'
    mon.check(at(20, 18, 3))
    assert len(mon.sent) == 2

    _set_job(mon, 'package-maintenance', 'ok', last_success='2026-10-20T18:04:00')
    mon.check(at(20, 18, 5))
    assert mon.subjects()[-1] == '[Test Store RSCP] Package status upkeep is working again'
    assert mon.open_keys() == set()
    mon.check(at(20, 18, 7))
    assert len(mon.sent) == 3


def test_real_job_failure_end_to_end(mon, at, monkeypatch, tmp_path):
    """A job that fails through run_job is reported by the monitor job."""
    from app import jobs
    from app.services import package_imports
    monkeypatch.setattr(jobs, 'lock_dir', lambda: str(tmp_path / 'locks'))
    monkeypatch.setattr(package_imports, 'run_package_maintenance', _boom)
    assert jobs.run_job('package-maintenance', db_path=mon.db).state == 'failed'
    assert jobs.main(['monitor', '--db', mon.db]) == 0
    assert mon.subjects() == ['[Test Store RSCP] Package status upkeep failed']
    assert jobs.read_status(mon.db)['monitor']['last_detail'].startswith('emailed 1: Package status upkeep failed')

    monkeypatch.setattr(package_imports, 'run_package_maintenance', lambda: (0, 0))
    assert jobs.run_job('package-maintenance', db_path=mon.db).state == 'ok'
    assert jobs.main(['monitor', '--db', mon.db]) == 0
    assert mon.subjects()[-1] == '[Test Store RSCP] Package status upkeep is working again'


def test_backup_failure_and_recovery(mon, at):
    mon.backup_status = {'last_run': '2026-10-20T02:30:00', 'ok': False, 'error': 'No space left on device',
                         'last_success': '2026-10-19T02:30:00'}
    mon.check(at(20, 8))
    assert mon.subjects() == ['[Test Store RSCP] Nightly backup failed']
    assert 'No space left on device' in mon.sent[0]['text']
    mon.check(at(20, 8, 2))
    assert len(mon.sent) == 1
    mon.backup_status = {'last_run': '2026-10-20T03:30:00', 'ok': True, 'error': None,
                         'last_success': '2026-10-20T03:30:00'}
    mon.check(at(20, 8, 4))
    assert mon.subjects()[-1] == '[Test Store RSCP] Nightly backup is working again'


def test_stale_backup_is_reported_once(mon, at):
    mon.backup_status = {'last_run': '2026-10-18T09:00:00', 'ok': True, 'last_success': '2026-10-18T09:00:00'}
    mon.check(at(19, 20, 58))
    mon.check(at(19, 21, 0))                              # exactly 36 hours: not yet
    assert mon.sent == []
    mon.check(at(19, 21, 2))
    assert mon.subjects() == ['[Test Store RSCP] Nightly backup has not run for 36+ hours']
    assert 'Last success: 2026-10-18 09:00' in mon.sent[0]['text']
    mon.check(at(19, 21, 4))
    assert len(mon.sent) == 1
    mon.backup_status = {'last_run': '2026-10-19T21:05:00', 'ok': True, 'last_success': '2026-10-19T21:05:00'}
    mon.check(at(19, 21, 6))
    assert mon.subjects()[-1] == '[Test Store RSCP] Nightly backup is running again'
    assert mon.open_keys() == set()


def test_stale_backup_is_ignored_when_backups_are_off(mon, at):
    mon.settings(BACKUP_ENABLED=False)
    mon.backup_status = {'last_run': '2026-10-10T02:30:00', 'ok': True, 'last_success': '2026-10-10T02:30:00'}
    mon.check(at(19, 20, 0))
    mon.check(at(19, 20, 2))
    assert mon.sent == []


def test_stale_job_needs_its_cadence(mon, at):
    """Package upkeep runs every 15 minutes: 3 hours without a success is stale."""
    _set_job(mon, 'package-maintenance', 'ok', last_success='2026-10-20T08:00:00')
    _set_job(mon, 'system-migrations', 'ok', last_success='2026-09-01T08:00:00')   # once per start: never stale
    mon.check(at(20, 10, 58))
    mon.check(at(20, 11, 0))
    assert mon.sent == []
    mon.check(at(20, 11, 2))
    assert mon.subjects() == ['[Test Store RSCP] Package status upkeep has not run for 3+ hours']


def test_alerts_of_jobs_that_no_longer_exist_are_dropped(mon, at):
    conn = _db(mon.app)
    conn.execute("INSERT INTO monitor_alerts (key, kind, subject, title) "
                 "VALUES ('job_failed:old-job', 'job_failed', 'old-job', 'Old job')")
    conn.commit()
    conn.close()
    mon.check(at(20, 12))
    assert mon.sent == [] and mon.open_keys() == set()


def test_job_alerts_can_be_switched_off(mon, at):
    _fail_job(mon)
    mon.check(at(20, 12))
    assert len(mon.sent) == 1
    mon.settings(ALERT_JOB_FAILURES=False)
    _set_job(mon, 'package-maintenance', 'ok')
    mon.check(at(20, 12, 2))
    assert len(mon.sent) == 1 and mon.open_keys() == set()   # dropped quietly, no recovery mail


# ---------------------------------------------------------------------------
# Customer displays
# ---------------------------------------------------------------------------

def _pair(mon, peripheral_id='disp-1', role='display', last_seen=None, paired_at=None, name='Front Display'):
    conn = _db(mon.app)
    conn.execute("INSERT OR IGNORE INTO pos_registers (register_id, friendly_name) VALUES ('reg-1', 'Register 1')")
    conn.execute('INSERT OR REPLACE INTO pos_peripherals (peripheral_id, register_id, role, token_hash, '
                 'friendly_name, paired_at, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?)',
                 (peripheral_id, 'reg-1', role, f'hash-{peripheral_id}', name,
                  _sql_utc(paired_at) if paired_at else '2026-10-01 16:00:00',
                  _sql_utc(last_seen) if last_seen else None))
    conn.commit()
    conn.close()


def _seen(mon, peripheral_id, when):
    conn = _db(mon.app)
    conn.execute('UPDATE pos_peripherals SET last_seen = ? WHERE peripheral_id = ?', (_sql_utc(when), peripheral_id))
    conn.commit()
    conn.close()


def test_quiet_display_is_mailed_once_and_back_online_once(mon, at):
    _pair(mon, last_seen=at(20, 10, 40))
    mon.check(at(20, 10, 48))                             # first check: warms up, 8 min quiet anyway
    mon.check(at(20, 10, 50))                             # exactly 10 min: not yet
    assert mon.sent == []
    mon.check(at(20, 10, 52))
    assert mon.subjects() == ['[Test Store RSCP] Front Display (Register 1) is offline']
    assert 'Last seen 10:40 (store time)' in mon.sent[0]['text']
    assert mon.open_keys() == {'display_offline:disp-1'}

    # Still off for the rest of the day, overnight, and the next open day: no repeats
    for day, hour, minute in ((20, 11, 0), (20, 17, 58), (20, 18, 30), (21, 3, 0), (21, 10, 30), (21, 15, 0)):
        mon.check(at(day, hour, minute))
        mon.check(at(day, hour, minute) + datetime.timedelta(minutes=2))
    assert len(mon.sent) == 1

    _seen(mon, 'disp-1', at(21, 15, 3))
    mon.check(at(21, 15, 4))
    assert mon.subjects()[-1] == '[Test Store RSCP] Front Display (Register 1) is back online'
    assert 'offline since 10:40' in mon.sent[-1]['text']
    assert mon.open_keys() == set()
    mon.check(at(21, 15, 6))
    assert len(mon.sent) == 2


def test_quiet_display_outside_store_hours_is_not_reported(mon, at):
    _pair(mon, last_seen=at(19, 9, 0))
    for day, hour, minute in ((19, 12, 0), (19, 12, 2),     # Monday: closed
                              (25, 12, 0), (25, 12, 2),     # Sunday
                              (20, 9, 50), (20, 9, 52),     # Tuesday before opening
                              (20, 10, 4), (20, 10, 6),     # open, but not yet 10 minutes
                              (20, 18, 0), (20, 18, 2)):    # closing time
        mon.check(at(day, hour, minute))
    assert mon.sent == []
    mon.check(at(20, 10, 10))                             # 10 minutes after opening, still quiet
    assert mon.subjects() == ['[Test Store RSCP] Front Display (Register 1) is offline']
    assert 'Mon 2026-10-19 09:00' in mon.sent[0]['text']


def test_back_online_outside_store_hours_is_still_mailed(mon, at):
    _pair(mon, last_seen=at(20, 16, 0))
    mon.check(at(20, 16, 20))
    mon.check(at(20, 16, 22))
    assert len(mon.sent) == 1
    _seen(mon, 'disp-1', at(20, 19, 0))
    mon.check(at(20, 19, 1))
    assert mon.subjects()[-1].endswith('is back online')


def test_first_check_after_a_gap_does_not_report_quiet_displays(mon, at):
    """RSCP was down: every display looked quiet. Failures still go out."""
    _pair(mon, last_seen=at(20, 9, 0))
    _fail_job(mon)
    mon.check(at(20, 12))
    assert mon.subjects() == ['[Test Store RSCP] Package status upkeep failed']
    _seen(mon, 'disp-1', at(20, 12, 1))                   # the display reconnects at once
    mon.check(at(20, 12, 2))
    assert len(mon.sent) == 1
    # A gap longer than WARM_GAP cools it down again
    mon.check(at(20, 13, 0))
    assert len(mon.sent) == 1


def test_display_never_seen_counts_from_pairing(mon, at):
    _pair(mon, paired_at=at(20, 11, 0))
    mon.check(at(20, 11, 5))
    mon.check(at(20, 11, 7))
    assert mon.sent == []
    mon.check(at(20, 11, 12))
    assert mon.subjects() == ['[Test Store RSCP] Front Display (Register 1) is offline']


def test_scanners_are_not_alerted_on(mon, at):
    _pair(mon, 'scan-1', role='scanner', last_seen=at(20, 9, 0), name='Staff Scanner')
    mon.check(at(20, 12))
    mon.check(at(20, 12, 2))
    assert mon.sent == []


def test_unpaired_display_alert_is_dropped_quietly(mon, at):
    _pair(mon, last_seen=at(20, 10, 0))
    mon.check(at(20, 12))
    mon.check(at(20, 12, 2))
    assert len(mon.sent) == 1
    conn = _db(mon.app)
    conn.execute("DELETE FROM pos_peripherals WHERE peripheral_id = 'disp-1'")
    conn.commit()
    conn.close()
    mon.check(at(20, 12, 4))
    assert len(mon.sent) == 1 and mon.open_keys() == set()


def test_display_alerts_follow_the_settings(mon, at):
    _pair(mon, last_seen=at(20, 10, 0))
    mon.settings(ALERT_DISPLAY_OFFLINE_MINUTES=30)
    mon.check(at(20, 10, 25))
    mon.check(at(20, 10, 27))
    assert mon.sent == []
    mon.check(at(20, 10, 31))
    assert len(mon.sent) == 1
    mon.settings(ALERT_DISPLAY_OFFLINE=False)
    mon.check(at(20, 10, 33))
    assert mon.open_keys() == set()
    _pair(mon, 'disp-2', last_seen=at(20, 9, 0), name='Side Display')
    mon.check(at(20, 11, 0))
    assert len(mon.sent) == 1


def test_several_problems_go_out_in_one_email(mon, at):
    _pair(mon, last_seen=at(20, 10, 0))
    mon.check(at(20, 11, 58))                             # first check: warms up
    _fail_job(mon)
    mon.check(at(20, 12))
    assert len(mon.sent) == 1
    m = mon.sent[0]
    assert m['subject'] == ('[Test Store RSCP] 2 alerts: Package status upkeep failed; '
                            'Front Display (Register 1) is offline')
    assert 'disk on fire' in m['text'] and 'Last seen 10:00' in m['text']
    assert mon.open_keys() == {'job_failed:package-maintenance', 'display_offline:disp-1'}


# ---------------------------------------------------------------------------
# Last seen: throttled write, register device list
# ---------------------------------------------------------------------------

def test_poll_writes_last_seen_only_when_due(fresh_app):
    from app.services import pos_registers
    conn = sqlite3.connect(fresh_app.config['DATABASE'])
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT INTO pos_peripherals (peripheral_id, register_id, role, token_hash) "
                 "VALUES ('d1', 'reg-1', 'display', ?)", (pos_registers.hash_token('tok'),))
    conn.commit()
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        for _ in range(5):                                # 5 polls in quick succession
            assert pos_registers.resolve_peripheral(conn, 'tok')['peripheral_id'] == 'd1'
        writes = [s for s in statements if s.lstrip().upper().startswith(('UPDATE', 'BEGIN', 'INSERT'))]
        assert len(writes) == 2, writes                   # one BEGIN + one UPDATE, on the first poll only
        statements.clear()
        conn.execute("UPDATE pos_peripherals SET last_seen = datetime('now', '-30 seconds')")
        conn.commit()
        statements.clear()
        pos_registers.resolve_peripheral(conn, 'tok')
        assert any(s.lstrip().upper().startswith('UPDATE') for s in statements)
        seen = conn.execute("SELECT strftime('%s', 'now') - strftime('%s', last_seen) FROM pos_peripherals").fetchone()[0]
        assert seen <= 2
        assert pos_registers.resolve_peripheral(conn, 'tok', touch=False) is not None
    finally:
        conn.close()


def test_register_device_list_reports_last_seen(fresh_app, fresh_client):
    conn = _db(fresh_app)
    conn.execute("INSERT INTO pos_registers (register_id, friendly_name) VALUES ('reg-1', 'Register 1')")
    conn.executemany(
        "INSERT INTO pos_peripherals (peripheral_id, register_id, role, token_hash, last_seen) "
        "VALUES (?, 'reg-1', ?, ?, datetime('now', ?))",
        [('d1', 'display', 'h1', '-2 seconds'), ('s1', 'scanner', 'h2', '-7200 seconds')])
    conn.execute("INSERT INTO pos_peripherals (peripheral_id, register_id, role, token_hash) "
                 "VALUES ('d2', 'reg-1', 'display', 'h3')")
    conn.commit()
    conn.close()
    login(fresh_client, 2)
    data = fresh_client.get('/pos/api/register/peripherals?register_id=reg-1').get_json()
    assert data['success']
    by_id = {p['peripheral_id']: p for p in data['peripherals']}
    assert by_id['d1']['connected'] and 0 <= by_id['d1']['seen_ago_s'] <= 5
    assert by_id['d1']['last_seen'].endswith('Z') and 'T' in by_id['d1']['last_seen']
    assert not by_id['s1']['connected'] and 7195 <= by_id['s1']['seen_ago_s'] <= 7205
    assert by_id['d2']['last_seen'] is None and by_id['d2']['seen_ago_s'] is None


def test_register_page_script_renders_last_seen():
    import os
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    with open(os.path.join(root, 'static', 'js', 'pages', 'pos-sales.js'), encoding='utf-8') as f:
        js = f.read()
    assert 'seen_ago_s' in js and 'last seen' in js and 'peripheral-status' in js


# ---------------------------------------------------------------------------
# Timer and installer
# ---------------------------------------------------------------------------

def test_monitor_timer_and_installer():
    import os
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

    def read(rel):
        with open(os.path.join(root, rel), encoding='utf-8') as f:
            return f.read()
    from app import jobs
    assert jobs.JOBS['monitor'].timer == 'rscp-monitor.timer'
    timer = read('deploy/rscp-monitor.timer').splitlines()
    for line in ('Unit=rscp-job@monitor.service', 'OnCalendar=*:0/2', 'Persistent=true', 'WantedBy=timers.target'):
        assert line in timer
    script = read('install.sh')
    assert 'rscp-monitor.timer' in script.split('JOB_TIMERS="', 1)[1].split('"', 1)[0]
    # A rollback to a release without the monitor job stops its timer first,
    # before handing over to that release's installer (which doesn't know it).
    body = script.split('install_units() {', 1)[1]
    assert body.index('drop_unshipped_timers "$rel"') < body.index('--install-units')
    assert 'drop_unshipped_timers() {' in script
