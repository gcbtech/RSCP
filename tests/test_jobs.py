"""
Scheduled jobs (app/jobs.py): the `python -m app.jobs` entry point, per-job
locks, the idempotent end-of-day email, where the jobs run (systemd timers
only; a warning when they are missing), the status table and its admin panel,
and the systemd units.
"""
import datetime
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

from tests.conftest import login

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DT = datetime.datetime


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


@pytest.fixture
def jobs(monkeypatch, tmp_path):
    """app.jobs with its locks in a private folder."""
    from app import jobs as jobs_mod
    lock_dir = tmp_path / 'locks'
    monkeypatch.setattr(jobs_mod, 'lock_dir', lambda: str(lock_dir))
    return jobs_mod


@pytest.fixture
def db(fresh_app):
    return fresh_app.config['DATABASE']


def _status(db_path, name):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute('SELECT * FROM job_status WHERE name = ?', (name,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI against a temporary data directory (a real child process each)
# ---------------------------------------------------------------------------

def _data_dir(tmp_path):
    from app.services.migration import run_migrations
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'config.json').write_text(json.dumps({'SECRET_KEY': 'k'}))
    run_migrations(str(data / 'rscp.db'))
    return data


def _cli(data, *args):
    env = {k: v for k, v in os.environ.items() if k != 'TESTING'}
    env['RSCP_DATA_DIR'] = str(data)
    return subprocess.run([sys.executable, '-m', 'app.jobs', *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


def test_each_job_runs_from_the_cli_against_a_data_dir(tmp_path):
    data = _data_dir(tmp_path)
    db_path = str(data / 'rscp.db')
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO packages (tracking_number, date_expected, status) VALUES ('1Z-JOB', ?, 'on_time')",
                 (yesterday,))
    conn.commit()
    conn.close()

    for name in ('package-maintenance', 'eod-email', 'system-migrations'):
        proc = _cli(data, name)
        assert proc.returncode == 0, (name, proc.stderr[-2000:])
        st = _status(db_path, name)
        assert st['last_result'] == 'ok' and st['last_trigger'] == 'cli' and st['run_count'] == 1, st

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT status FROM packages WHERE tracking_number = '1Z-JOB'").fetchone()[0] == 'past_due'
    applied = {r[0] for r in conn.execute('SELECT id FROM system_migrations')}
    conn.close()
    assert '002_backfill_thumbnails' in applied          # ran inline, not in a dying thread
    assert 'statuses refreshed' in _status(db_path, 'package-maintenance')['last_detail']
    assert _status(db_path, 'eod-email')['last_detail'] == 'automatic end-of-day email is off'

    listing = _cli(data, '--list')
    assert listing.returncode == 0
    assert 'package-maintenance' in listing.stdout and '-> ok' in listing.stdout
    # the run's lines went to stderr (journald), under a stable logger name
    assert 'app.jobs' in proc.stderr


def test_cli_usage_errors(tmp_path):
    from app import jobs
    with pytest.raises(SystemExit) as e:
        jobs.main([])
    assert e.value.code == 2
    with pytest.raises(SystemExit) as e:
        jobs.main(['no-such-job'])
    assert e.value.code == 2


def test_a_running_job_blocks_a_second_process(tmp_path, monkeypatch):
    from app import jobs
    data = _data_dir(tmp_path)
    monkeypatch.setattr(jobs, 'lock_dir', lambda: str(data / 'tmp' / 'job-locks'))
    with jobs.JobLock('package-maintenance') as got:
        assert got
        proc = _cli(data, 'package-maintenance')
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert 'another run is still going' in proc.stderr
    assert _status(str(data / 'rscp.db'), 'package-maintenance') is None   # it never started


# ---------------------------------------------------------------------------
# Results, failures, locks (in process)
# ---------------------------------------------------------------------------

def test_failure_exits_nonzero_and_is_recorded(jobs, db, monkeypatch, capsys):
    from app.services import package_imports

    def boom():
        raise RuntimeError('disk on fire')
    monkeypatch.setattr(package_imports, 'run_package_maintenance', boom)
    assert jobs.main(['package-maintenance', '--db', db]) == 1
    assert 'disk on fire' in capsys.readouterr().err
    assert jobs.main(['package-maintenance', '--db', db]) == 1
    st = _status(db, 'package-maintenance')
    assert st['last_result'] == 'failed' and 'disk on fire' in st['last_error']
    assert st['consecutive_failures'] == 2 and st['failure_count'] == 2

    monkeypatch.setattr(package_imports, 'run_package_maintenance', lambda: (3, 1))
    assert jobs.main(['package-maintenance', '--db', db]) == 0
    st = _status(db, 'package-maintenance')
    assert st['last_result'] == 'ok' and st['consecutive_failures'] == 0 and st['run_count'] == 3
    assert st['last_detail'] == '3 statuses refreshed, 1 old packages trimmed'
    assert 'disk on fire' in st['last_error'] and st['last_failure']   # history kept for the panel


def test_lock_prevents_overlapping_runs(jobs, db, monkeypatch):
    from app.services import package_imports
    started, release, calls = threading.Event(), threading.Event(), []

    def slow():
        calls.append(1)
        started.set()
        release.wait(10)
        return 0, 0
    monkeypatch.setattr(package_imports, 'run_package_maintenance', slow)
    first = {}
    t = threading.Thread(target=lambda: first.setdefault('r', jobs.run_job('package-maintenance', db_path=db)))
    t.start()
    assert started.wait(10)
    second = jobs.run_job('package-maintenance', db_path=db)       # while the first is running
    assert second.state == 'busy' and second.ok
    assert jobs.main(['package-maintenance', '--db', db]) == 0      # the CLI reports busy as success
    release.set()
    t.join(10)
    assert first['r'].state == 'ok' and calls == [1]
    # the lock is released afterwards
    assert jobs.run_job('package-maintenance', db_path=db).state == 'ok' and calls == [1, 1]


def test_job_tables_come_from_schema_migration_13(db):
    from app.services.migration import LATEST_SCHEMA_VERSION, SCHEMA_MIGRATIONS
    assert 13 in {v for v, _, _ in SCHEMA_MIGRATIONS} and LATEST_SCHEMA_VERSION >= 13
    conn = sqlite3.connect(db)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()
    assert {'job_status', 'job_markers'} <= names


def test_system_migration_failures_fail_the_job(jobs, db, monkeypatch):
    from app.services import system_migrations as sm

    def broken():
        raise OSError('read-only file system')
    monkeypatch.setattr(sm, 'MIGRATIONS', [('900_test_broken', broken), ('901_test_ok', lambda: (True, 'fine'))])
    assert jobs.main(['system-migrations', '--db', db]) == 1
    st = _status(db, 'system-migrations')
    assert st['last_result'] == 'failed'
    assert '900_test_broken: read-only file system' in st['last_error'] and 'applied 901_test_ok' in st['last_error']
    # the applied one is not repeated; the broken one is retried next start
    monkeypatch.setattr(sm, 'MIGRATIONS', [('900_test_broken', lambda: (True, 'ok now')),
                                           ('901_test_ok', lambda: pytest.fail('ran twice'))])
    assert jobs.main(['system-migrations', '--db', db]) == 0
    assert _status(db, 'system-migrations')['last_detail'] == 'applied 900_test_broken'


# ---------------------------------------------------------------------------
# End-of-day email: once a day, never twice
# ---------------------------------------------------------------------------

@pytest.fixture
def eod(jobs, fresh_app, db, monkeypatch):
    """EOD on at 21:00, clock at 22:00, a fake mailer that records sends."""
    from app.services import pos_email
    from app.services import settings as st
    with fresh_app.app_context():
        st.set_many({'POS_AUTO_EMAIL_ENABLED': True, 'POS_AUTO_EMAIL_TIME': '21:00'})
    clock = {'now': DT(2026, 10, 17, 22, 0)}
    monkeypatch.setattr(jobs, '_now', lambda: clock['now'])
    sent, behaviour = [], {'ok': True, 'delay': 0}

    def deliver(report_date):
        if behaviour['delay']:
            time.sleep(behaviour['delay'])
        if not behaviour['ok']:
            return False, 'Error sending email: connection refused'
        sent.append(report_date)
        return True, 'EOD Report sent to 1 recipient(s).'
    monkeypatch.setattr(pos_email, 'deliver_eod_email', deliver)

    def last_date():
        with fresh_app.app_context():
            st.invalidate()
            return st.get('POS_LAST_AUTO_EMAIL_DATE')

    class E:
        pass
    e = E()
    e.clock, e.sent, e.behaviour, e.last_date, e.db = clock, sent, behaviour, last_date, db
    e.run = lambda: jobs.main(['eod-email', '--db', db])
    return e


def test_eod_sends_once_per_day(eod):
    assert eod.run() == 0 and eod.sent == [datetime.date(2026, 10, 17)]
    assert eod.last_date() == '2026-10-17'
    for _ in range(3):                                    # timer ticks, retries
        assert eod.run() == 0
    assert len(eod.sent) == 1
    assert 'already sent for 2026-10-17' in _status(eod.db, 'eod-email')['last_detail']
    eod.clock['now'] = DT(2026, 10, 18, 21, 5)            # next day
    assert eod.run() == 0 and len(eod.sent) == 2


def test_eod_waits_for_the_configured_time(eod):
    eod.clock['now'] = DT(2026, 10, 17, 20, 59)
    assert eod.run() == 0 and eod.sent == []
    assert _status(eod.db, 'eod-email')['last_detail'] == 'not due yet (sends at 21:00)'
    eod.clock['now'] = DT(2026, 10, 17, 21, 0)
    assert eod.run() == 0 and len(eod.sent) == 1


def test_eod_not_sent_when_disabled(eod, fresh_app):
    from app.services import settings as st
    with fresh_app.app_context():
        st.set('POS_AUTO_EMAIL_ENABLED', False)
    assert eod.run() == 0 and eod.sent == []


def test_eod_honours_the_2x_marker(eod, fresh_app):
    """Sent earlier today by the 2.x scheduler (before an upgrade): not again."""
    from app.services import settings as st
    with fresh_app.app_context():
        st.set_lenient({'POS_LAST_AUTO_EMAIL_DATE': '2026-10-17'})
    assert eod.run() == 0 and eod.sent == []


def test_eod_failed_send_is_retried_and_reported(eod):
    eod.behaviour['ok'] = False
    assert eod.run() == 1 and eod.sent == []
    st = _status(eod.db, 'eod-email')
    assert st['last_result'] == 'failed' and 'connection refused' in st['last_error']
    eod.behaviour['ok'] = True
    assert eod.run() == 0 and len(eod.sent) == 1          # the claim was released
    assert eod.run() == 0 and len(eod.sent) == 1


def test_eod_interrupted_send_is_not_repeated(eod):
    """A run that died between claiming the day and recording the result
    (killed mid-SMTP): the mail may have gone out, so it is not resent."""
    conn = sqlite3.connect(eod.db)
    conn.execute("INSERT INTO job_markers (name, value) VALUES ('eod-email', 'sending:2026-10-17:2026-10-17T21:00:05')")
    conn.commit()
    conn.close()
    assert eod.run() == 1 and eod.sent == []
    assert 'interrupted' in _status(eod.db, 'eod-email')['last_error']
    assert eod.run() == 0 and eod.sent == []              # reported once, then quiet
    eod.clock['now'] = DT(2026, 10, 18, 22, 0)
    assert eod.run() == 0 and len(eod.sent) == 1          # a stale claim from yesterday doesn't block


def test_eod_overlapping_runs_send_once(eod, jobs):
    eod.behaviour['delay'] = 0.5
    results = []
    threads = [threading.Thread(target=lambda: results.append(jobs.run_job('eod-email', db_path=eod.db)))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert len(eod.sent) == 1
    assert sorted(r.state for r in results).count('ok') >= 1 and all(r.ok for r in results)


def test_eod_claim_alone_prevents_a_double_send(eod, jobs):
    """Even without the lock (e.g. a manual run on another box against the
    same file), the day is claimed atomically before sending."""
    today, now = '2026-10-17', eod.clock['now']
    assert jobs._eod_claim(eod.db, today, now)[0] == 'claimed'
    assert jobs._eod_claim(eod.db, today, now)[0] == 'interrupted-new'
    assert jobs._eod_claim(eod.db, today, now)[0] == 'interrupted'


# ---------------------------------------------------------------------------
# Who runs the jobs: systemd timers only (warning when they are missing)
# ---------------------------------------------------------------------------

def _install_units(tmp_path, monkeypatch, with_backup_timer=True):
    from app import backup, jobs
    unit_dir = tmp_path / 'systemd'
    unit_dir.mkdir()
    for name in ['rscp-job@.service'] + [j.timer for j in jobs.JOBS.values() if j.timer]:
        (unit_dir / name).write_text('[Unit]\n')
    if with_backup_timer:
        (unit_dir / 'rscp-backup.timer').write_text('[Timer]\n')
    monkeypatch.setattr(jobs, 'SYSTEMD_UNIT_DIR', str(unit_dir))
    monkeypatch.setattr(backup, 'SYSTEMD_TIMER', str(unit_dir / 'rscp-backup.timer'))
    return unit_dir


def test_use_systemd_timers_and_the_warning(tmp_path, monkeypatch):
    from app import jobs, paths
    monkeypatch.setattr(jobs, 'SYSTEMD_UNIT_DIR', str(tmp_path / 'none'))
    monkeypatch.setattr(paths, 'USES_DATA_DIR', True)
    assert not jobs.use_systemd_timers()                 # units not installed
    warning = jobs.scheduling_warning()
    assert 'NOT running' in warning and 'rscp-job@.service' in warning
    assert '--install-units' in warning

    unit_dir = _install_units(tmp_path, monkeypatch)
    assert jobs.use_systemd_timers()
    assert jobs.scheduling_warning() is None and jobs.missing_units() == []

    (unit_dir / 'rscp-eod-email.timer').unlink()
    assert not jobs.use_systemd_timers()                 # all or nothing
    assert jobs.missing_units() == ['rscp-eod-email.timer']
    (unit_dir / 'rscp-eod-email.timer').write_text('[Timer]')
    (unit_dir / 'rscp-backup.timer').unlink()
    assert not jobs.use_systemd_timers()                 # backups would otherwise stop
    assert jobs.missing_units() == ['rscp-backup.timer']
    (unit_dir / 'rscp-backup.timer').write_text('[Timer]')

    monkeypatch.setattr(paths, 'USES_DATA_DIR', False)   # 2.x layout, even with units
    assert not jobs.use_systemd_timers()
    warning = jobs.scheduling_warning()
    assert '2.x folder layout' in warning and '--migrate-from' in warning


def _import_wsgi(monkeypatch):
    """Import wsgi.py fresh; returns (jobs it ran, warnings it logged)."""
    import logging
    from app import jobs
    calls, records = [], []
    monkeypatch.setattr(jobs, 'run_job', lambda name, **kw: calls.append((name, kw.get('trigger'))))

    class Collect(logging.Handler):
        def emit(self, record):
            if record.levelno >= logging.WARNING:
                records.append(record.getMessage())
    handler = Collect()
    log = logging.getLogger('wsgi')
    log.addHandler(handler)
    sys.modules.pop('wsgi', None)
    try:
        importlib.import_module('wsgi')
    finally:
        sys.modules.pop('wsgi', None)
        log.removeHandler(handler)
    return calls, records


def test_wsgi_with_timers_runs_nothing_and_warns_nothing(tmp_path, monkeypatch):
    from app import paths
    monkeypatch.setattr(paths, 'USES_DATA_DIR', True)
    _install_units(tmp_path, monkeypatch)
    assert _import_wsgi(monkeypatch) == ([], [])


def test_wsgi_without_timers_warns_instead_of_scheduling(tmp_path, monkeypatch):
    from app import jobs, paths
    monkeypatch.setattr(paths, 'USES_DATA_DIR', True)
    monkeypatch.setattr(jobs, 'SYSTEMD_UNIT_DIR', str(tmp_path / 'none'))
    calls, warnings = _import_wsgi(monkeypatch)
    assert calls == []                                   # no in-app scheduler, no migrations
    assert len(warnings) == 1 and 'Scheduled jobs are NOT running' in warnings[0]


def test_wsgi_in_legacy_layout_warns_instead_of_scheduling(tmp_path, monkeypatch):
    from app import paths
    monkeypatch.setattr(paths, 'USES_DATA_DIR', False)
    _install_units(tmp_path, monkeypatch)                # even if unit files exist
    calls, warnings = _import_wsgi(monkeypatch)
    assert calls == []
    assert len(warnings) == 1 and '2.x folder layout' in warnings[0]


def test_the_in_app_scheduler_is_gone():
    import importlib.util
    assert importlib.util.find_spec('app.services.background_tasks') is None
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'requirements.txt'), encoding='utf-8') as f:
        assert 'apscheduler' not in f.read().lower()


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------

def test_admin_logs_page_shows_scheduled_jobs(jobs, db, fresh_client, monkeypatch):
    from app.services import package_imports

    def boom():
        raise RuntimeError('database is locked')
    monkeypatch.setattr(package_imports, 'run_package_maintenance', boom)
    jobs.run_job('package-maintenance', db_path=db)
    jobs.run_job('eod-email', db_path=db)

    login(fresh_client, 1)
    html = fresh_client.get('/admin/logs').get_data(as_text=True)
    assert 'Scheduled jobs' in html
    assert 'Package status upkeep' in html and 'End-of-day email' in html and 'System migrations' in html
    assert 'RuntimeError: database is locked' in html
    assert 'automatic end-of-day email is off' in html
    assert 'Nightly backup' in html


def test_scheduled_jobs_panel_shows_the_missing_timer_warning(fresh_client, tmp_path, monkeypatch):
    from app import jobs, paths
    monkeypatch.setattr(paths, 'USES_DATA_DIR', True)
    monkeypatch.setattr(jobs, 'SYSTEMD_UNIT_DIR', str(tmp_path / 'none'))
    login(fresh_client, 1)
    html = fresh_client.get('/admin/logs').get_data(as_text=True)
    assert 'id="scheduled-jobs-warning"' in html
    assert 'Scheduled jobs are NOT running' in html and 'rscp-job@.service' in html

    _install_units(tmp_path, monkeypatch)
    html = fresh_client.get('/admin/logs').get_data(as_text=True)
    assert 'Scheduled jobs' in html and 'id="scheduled-jobs-warning"' not in html
    assert 'systemd timers' in html


def test_scheduled_jobs_panel_is_admin_only(fresh_client):
    login(fresh_client, 3)                                # pos_admin, not an admin
    resp = fresh_client.get('/admin/logs')
    assert resp.status_code in (302, 403)
    assert 'Scheduled jobs' not in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# systemd units and installer
# ---------------------------------------------------------------------------

def test_job_units_match_layout():
    from app import jobs
    service = _read('deploy/rscp-job@.service').splitlines()
    for line in ('Type=oneshot', 'User=rscp', 'Group=rscp', 'Environment=RSCP_DATA_DIR=/var/lib/rscp',
                 'WorkingDirectory=/opt/rscp/current',
                 'ExecStart=/opt/rscp/current/venv/bin/python -m app.jobs %i',
                 'NoNewPrivileges=true', 'ProtectSystem=strict', 'ReadWritePaths=/var/lib/rscp',
                 'ProtectHome=true', 'PrivateTmp=true', 'RestrictSUIDSGID=true', 'LockPersonality=true'):
        assert line in service, line
    timers = [j for j in jobs.JOBS.values() if j.timer]
    assert {j.name for j in timers} == {'package-maintenance', 'eod-email', 'monitor'}
    for job in timers:
        timer = _read(f'deploy/{job.timer}').splitlines()
        for line in (f'Unit=rscp-job@{job.name}.service', 'Persistent=true', 'WantedBy=timers.target'):
            assert line in timer, (job.timer, line)
    assert 'OnCalendar=*:0/15' in _read('deploy/rscp-package-maintenance.timer')
    assert 'OnCalendar=*:0/5' in _read('deploy/rscp-eod-email.timer')
    assert 'OnCalendar=*:0/2' in _read('deploy/rscp-monitor.timer')

    unit = _read('deploy/rscp.service').splitlines()
    migrate = unit.index('ExecStartPre=/opt/rscp/current/venv/bin/python -m app.migrate')
    sysmig = unit.index('ExecStartPre=-/opt/rscp/current/venv/bin/python -m app.jobs system-migrations')
    assert migrate < sysmig                               # schema first, then system steps

    script = _read('install.sh')
    assert 'JOB_UNITS="rscp-job@.service"' in script
    assert f'JOB_TIMERS="{" ".join(j.timer for j in timers)}"' in script
    assert '$JOB_UNITS $JOB_TIMERS; do' in script
    assert 'sync_job_timers "$rel"' in script and 'systemctl enable --now --quiet "$f"' in script
