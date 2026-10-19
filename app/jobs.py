"""
RSCP's periodic and one-shot maintenance jobs, runnable on their own.

    python -m app.jobs --list                  # the jobs and their last result
    python -m app.jobs package-maintenance     # run one job now
    python -m app.jobs eod-email
    python -m app.jobs system-migrations
    python -m app.jobs monitor                 # email alerts (app/services/monitor.py)

Exit code 0 when the job succeeded or had nothing to do (including "another
run of this job is still going"), 1 when it failed, 2 for a usage error.
Logs go to stderr (journald under systemd).

Who runs them: systemd, and nothing else. install.sh installs
rscp-package-maintenance.timer and rscp-eod-email.timer, which start
rscp-job@<name>.service, and rscp.service runs `system-migrations` as an
ExecStartPre, once per start and before any gunicorn worker exists. The web
process never schedules anything; when the units are missing (or the install
still uses the 2.x layout) scheduling_warning() says so, in the log at
startup and on the admin panel.

Each run takes a per-job lock (an flock on <tmp>/job-locks/<name>.lock), so
two runs of one job never overlap, whoever started them; a run that finds
the lock taken logs that and exits 0. Every run is recorded in the job_status
table (schema migration 13), which Admin > Logs shows as "Scheduled jobs".
The backup job lives in app/backup.py with its own unit and timer; the panel
shows its status too.
"""
import argparse
import datetime
import logging
import os
import sqlite3
import sys
import time
import traceback

from app import paths

logger = logging.getLogger('app.jobs')  # not __main__ under python -m

SYSTEMD_UNIT_DIR = '/etc/systemd/system'
JOB_SERVICE_UNIT = 'rscp-job@.service'

# A run is reported as failed when it exits non-zero; how many characters of
# the error the status table keeps.
MAX_ERROR_CHARS = 2000


class JobError(RuntimeError):
    """A job failed in an expected way (message is shown as the error)."""


class Job:
    def __init__(self, name, title, fn, schedule, timer=None):
        self.name = name          # CLI / systemd instance name
        self.title = title
        self.fn = fn              # fn(trigger) -> detail string; raises on failure
        self.schedule = schedule  # human description for the admin panel
        self.timer = timer        # systemd timer unit, or None


def _now():
    return datetime.datetime.now()


def _iso(dt):
    return dt.isoformat(timespec='seconds') if dt else None


# --- the jobs ----------------------------------------------------------------

def package_maintenance(trigger):
    """Package statuses follow the calendar (on_time -> expected -> past_due);
    AUTO_TRIM removes old packages. Also checks every item's cached stock
    against the stock ledger and repairs drift (logged; the ledger wins)."""
    from app.services.inventory import run_stock_ledger_check
    from app.services.package_imports import run_package_maintenance
    changed, trimmed = run_package_maintenance()
    if changed or trimmed:
        logger.info(f"[Packages] {changed} statuses refreshed, {trimmed} old packages trimmed")
    detail = f'{changed} statuses refreshed, {trimmed} old packages trimmed'
    stock = run_stock_ledger_check()
    return f'{detail}; {stock}' if stock else detail


EOD_MARKER = 'eod-email'


def _eod_claim(db_path, today, now):
    """Atomically claim today's EOD send. Returns (state, previous value):

    'claimed'          we may send (marker now 'sending:<today>:<time>')
    'sent'             already sent today
    'interrupted-new'  an earlier send today never finished: its process died
                       between claiming and recording the result (we hold the
                       job lock, so it is not still running). Whether the mail
                       went out is unknown, so it is not sent again; the
                       marker becomes 'interrupted:<today>'
    'interrupted'      as above, already reported
    """
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.execute('PRAGMA foreign_keys = ON')
    try:
        conn.execute('BEGIN IMMEDIATE')
        try:
            row = conn.execute('SELECT value FROM job_markers WHERE name = ?', (EOD_MARKER,)).fetchone()
            value = row[0] if row else None
            if value == f'sent:{today}':
                state = 'sent'
            elif value == f'interrupted:{today}':
                state = 'interrupted'
            elif value and value.startswith(f'sending:{today}:'):
                _set_marker(conn, EOD_MARKER, f'interrupted:{today}', now)
                state = 'interrupted-new'
            else:
                _set_marker(conn, EOD_MARKER, f'sending:{today}:{_iso(now)}', now)
                state = 'claimed'
            conn.execute('COMMIT')
        except Exception:
            conn.execute('ROLLBACK')
            raise
    finally:
        conn.close()
    return state, value


def _set_marker(conn, name, value, now):
    conn.execute('INSERT INTO job_markers (name, value, updated_at) VALUES (?, ?, ?) '
                 'ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at',
                 (name, value, _iso(now)))


def _eod_write_marker(db_path, value, now):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute('PRAGMA foreign_keys = ON')
    try:
        if value is None:
            conn.execute('DELETE FROM job_markers WHERE name = ?', (EOD_MARKER,))
        else:
            _set_marker(conn, EOD_MARKER, value, now)
        conn.commit()
    finally:
        conn.close()


def eod_email(trigger):
    """Send the POS end-of-day email once a day, at the first check at or
    after POS_AUTO_EMAIL_TIME (so a box that was off at that time sends when
    it is back, the same day).

    Never sends twice for one day: the day is claimed in job_markers in one
    transaction before sending and marked sent afterwards; a failed send
    releases the claim so the next check retries. POS_LAST_AUTO_EMAIL_DATE is
    still written (and honoured), as by 2.x.
    """
    from app.routes.pos.core import get_pos_setting, set_pos_setting
    from app.services import pos_email

    if get_pos_setting('POS_AUTO_EMAIL_ENABLED', 'false') != 'true':
        return 'automatic end-of-day email is off'
    target = get_pos_setting('POS_AUTO_EMAIL_TIME', '') or ''
    if not target:
        return 'no end-of-day email time set'
    now = _now()
    try:
        h, m = (int(p) for p in target.split(':'))
        due = now.replace(hour=h, minute=m, second=0, microsecond=0)
    except ValueError:
        raise JobError(f'invalid end-of-day email time: {target!r}')
    today = now.strftime('%Y-%m-%d')
    if now < due:
        return f'not due yet (sends at {target})'
    if get_pos_setting('POS_LAST_AUTO_EMAIL_DATE', '') == today:
        return f'already sent for {today}'

    db_path = _db_path()
    state, previous = _eod_claim(db_path, today, now)
    if state == 'sent':
        return f'already sent for {today}'
    if state == 'interrupted':
        return f'not resent for {today}: an earlier attempt was interrupted (see the last error)'
    if state == 'interrupted-new':
        raise JobError(f'An earlier end-of-day email attempt for {today} was interrupted; it is not '
                       'resent automatically to avoid a duplicate. Use "Send Email Report" on the '
                       'daily report page if it did not arrive.')

    logger.info(f"[Auto-Email] Sending EOD report for {today}...")
    try:
        ok, message = pos_email.deliver_eod_email(now.date())
    except Exception as e:
        ok, message = False, f'Error sending email: {e}'
    if not ok:
        # Release the claim so the next check tries again.
        _eod_write_marker(db_path, previous, _now())
        raise JobError(message or 'sending the end-of-day email failed')
    _eod_write_marker(db_path, f'sent:{today}', _now())
    set_pos_setting('POS_LAST_AUTO_EMAIL_DATE', today)
    logger.info("[Auto-Email] Sent successfully.")
    return f'sent for {today}: {message}'


def system_migrations(trigger):
    """Post-update system steps (app/services/system_migrations.py). From the
    CLI (rscp.service ExecStartPre) they run inline; in a web worker the
    long thumbnail backfill keeps running in a background thread."""
    from app.services import system_migrations as sm
    summary = sm.run_system_migrations(inline=(trigger == 'cli')) or {}
    parts = []
    if summary.get('applied'):
        parts.append('applied ' + ', '.join(summary['applied']))
    if summary.get('pending'):
        parts.append('pending ' + ', '.join(summary['pending']))
    if summary.get('failed'):
        raise JobError('; '.join(f'{mig}: {err}' for mig, err in summary['failed'])
                       + (f" ({'; '.join(parts)})" if parts else ''))
    return '; '.join(parts) or 'nothing to do'


def monitor(trigger):
    """Email alerts (app/services/monitor.py): failed or stale jobs and
    backups, customer displays gone quiet during store hours."""
    from app.services import monitor as mon
    try:
        return mon.run_checks()
    except mon.AlertSendError as e:
        raise JobError(str(e))


JOBS = {j.name: j for j in (
    Job('package-maintenance', 'Package status upkeep', package_maintenance,
        'every 15 minutes', timer='rscp-package-maintenance.timer'),
    Job('eod-email', 'End-of-day email', eod_email,
        'once a day, at the time set in POS settings',
        timer='rscp-eod-email.timer'),
    Job('system-migrations', 'System migrations', system_migrations,
        'once per service start'),
    Job('monitor', 'Email alerts', monitor,
        'every 2 minutes (failed jobs/backups, quiet customer displays)',
        timer='rscp-monitor.timer'),
)}


# --- where the jobs run ------------------------------------------------------

def missing_units():
    """Job units (and the backup timer) that install.sh should have put in
    SYSTEMD_UNIT_DIR but are not there."""
    from app import backup
    names = [JOB_SERVICE_UNIT] + [j.timer for j in JOBS.values() if j.timer]
    missing = [n for n in names if not os.path.exists(os.path.join(SYSTEMD_UNIT_DIR, n))]
    if not backup.timer_installed():
        missing.append(os.path.basename(backup.SYSTEMD_TIMER))
    return missing


def use_systemd_timers():
    """True when systemd runs the jobs: the data-dir layout with every job
    unit and the backup timer installed."""
    return paths.USES_DATA_DIR and not missing_units()


def scheduling_warning():
    """Why scheduled jobs are not running, or None when systemd runs them."""
    if not paths.USES_DATA_DIR:
        return ('This install still uses the 2.x folder layout, so package status upkeep, '
                'the end-of-day email and nightly backups are NOT running (RSCP 3 runs them '
                'from systemd timers). Move it to the 3.x layout with the installer: '
                f'sudo bash install.sh --migrate-from {paths.APP_DIR}')
    missing = missing_units()
    if missing:
        return (f"Scheduled jobs are NOT running: systemd unit(s) {', '.join(missing)} "
                f"missing from {SYSTEMD_UNIT_DIR}. Reinstall them on the server: "
                'sudo /opt/rscp/bin/rscp-update --install-units /opt/rscp/current')
    return None


# --- locking -----------------------------------------------------------------

def lock_dir():
    return os.path.join(paths.TEMP_DIR, 'job-locks')


class JobLock:
    """Non-blocking exclusive lock for one job, across processes and threads
    (an flock, or a byte-range lock on Windows; both are per open file, so two
    threads of one process exclude each other too)."""

    def __init__(self, name):
        self.path = os.path.join(lock_dir(), f'{name}.lock')
        self.fd = None

    def acquire(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd = open(self.path, 'a+')
        try:
            try:
                import fcntl
            except ImportError:
                import msvcrt
                fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fd.close()
            return False
        self.fd = fd
        try:
            fd.seek(0)
            fd.truncate()
            fd.write(f'{os.getpid()}\n')
            fd.flush()
        except OSError:
            pass
        return True

    def release(self):
        if self.fd is None:
            return
        try:
            try:
                import fcntl  # noqa: F401  (closing releases the flock)
            except ImportError:
                import msvcrt
                self.fd.seek(0)
                msvcrt.locking(self.fd.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            self.fd.close()
            self.fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


# --- status ------------------------------------------------------------------

def _db_path(db_path=None):
    if db_path:
        return db_path
    try:
        from flask import current_app
        return current_app.config.get('DATABASE') or _default_db()
    except RuntimeError:
        return _default_db()


def _default_db():
    from app.services import db as _db
    return _db.DB_PATH


def create_tables(conn):
    from app.services.migration import JOB_TABLES_SQL
    for sql in JOB_TABLES_SQL:
        conn.execute(sql)


def _status_conn(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute('PRAGMA foreign_keys = ON')
    conn.row_factory = sqlite3.Row
    create_tables(conn)
    return conn


def _record_start(db_path, name, trigger, started):
    try:
        conn = _status_conn(db_path)
        try:
            conn.execute('INSERT INTO job_status (name) VALUES (?) ON CONFLICT(name) DO NOTHING', (name,))
            conn.execute("UPDATE job_status SET last_started = ?, last_trigger = ?, last_result = 'running' "
                         "WHERE name = ?", (_iso(started), trigger, name))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[Jobs] Could not record the start of {name}: {e}")


def _record_end(db_path, name, ok, detail, error, finished, duration):
    try:
        conn = _status_conn(db_path)
        try:
            if ok:
                conn.execute(
                    "UPDATE job_status SET last_finished = ?, last_result = 'ok', last_detail = ?, "
                    "duration_s = ?, last_success = ?, run_count = run_count + 1, "
                    "consecutive_failures = 0 WHERE name = ?",
                    (_iso(finished), detail, duration, _iso(finished), name))
            else:
                conn.execute(
                    "UPDATE job_status SET last_finished = ?, last_result = 'failed', last_detail = ?, "
                    "duration_s = ?, last_failure = ?, last_error = ?, run_count = run_count + 1, "
                    "failure_count = failure_count + 1, consecutive_failures = consecutive_failures + 1 "
                    "WHERE name = ?",
                    (_iso(finished), detail, duration, _iso(finished), (error or '')[:MAX_ERROR_CHARS], name))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[Jobs] Could not record the result of {name}: {e}")


def read_status(db_path=None):
    """{job name: row dict} from job_status ({} if it can't be read)."""
    try:
        conn = sqlite3.connect(_db_path(db_path), timeout=10)
        conn.execute('PRAGMA foreign_keys = ON')
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute('SELECT * FROM job_status').fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return {r['name']: dict(r) for r in rows}


def overview(db_path=None):
    """What the admin panel shows: (runner description, [row per job])."""
    status = read_status(db_path)
    systemd = use_systemd_timers()
    rows = []
    for job in JOBS.values():
        st = status.get(job.name, {})
        rows.append({
            'name': job.name, 'title': job.title, 'schedule': job.schedule,
            'unit': (f'rscp-job@{job.name}.service' if job.timer else 'rscp.service (ExecStartPre)')
            if systemd else None,
            'last_started': st.get('last_started'), 'last_finished': st.get('last_finished'),
            'result': st.get('last_result'), 'detail': st.get('last_detail'),
            'trigger': st.get('last_trigger'), 'duration_s': st.get('duration_s'),
            'last_success': st.get('last_success'), 'last_failure': st.get('last_failure'),
            'last_error': st.get('last_error'), 'failures': st.get('consecutive_failures') or 0,
        })
    try:  # the backup job keeps its own status file
        from app import backup
        bst = backup.read_status() or {}
        rows.append({
            'name': 'backup', 'title': 'Nightly backup',
            'schedule': 'checks every 15 minutes; backs up once a day (Admin > Settings)',
            'unit': 'rscp-backup.service' if systemd else None,
            'last_started': bst.get('last_run'), 'last_finished': bst.get('last_run'),
            'result': None if not bst else ('ok' if bst.get('ok') else 'failed'),
            'detail': bst.get('db_file'), 'trigger': bst.get('trigger'),
            'duration_s': bst.get('duration_s'), 'last_success': bst.get('last_success'),
            'last_failure': None if bst.get('ok') else bst.get('last_run'),
            'last_error': bst.get('error'), 'failures': 0 if bst.get('ok', True) else 1,
        })
    except Exception as e:
        logger.debug(f"[Jobs] backup status unavailable: {e}")
    runner = 'systemd timers (systemctl list-timers \'rscp-*\')' if systemd else None
    return runner, rows


# --- running -----------------------------------------------------------------

class Result:
    def __init__(self, name, state, detail=None, error=None):
        self.name = name
        self.state = state      # 'ok' | 'failed' | 'busy'
        self.detail = detail
        self.error = error

    @property
    def ok(self):
        return self.state != 'failed'

    def __repr__(self):
        return f'<Result {self.name} {self.state}: {self.error or self.detail}>'


_JOB_APP = None


def _job_app(db_path):
    """A bare Flask app for the job's app context. Not create_app(): that also
    deletes the updater's status file and runs schema migrations, neither of
    which a job (possibly running mid-update) may do."""
    global _JOB_APP
    from flask import Flask
    if _JOB_APP is None:
        from app.services.db import init_app as init_db_app
        _JOB_APP = Flask('rscp.jobs', root_path=paths.APP_DIR,
                         template_folder=os.path.join(paths.APP_DIR, 'templates'))
        init_db_app(_JOB_APP)   # closes the context's shared DB connection
    _JOB_APP.config['DATABASE'] = db_path
    return _JOB_APP


def run_job(name, trigger='cli', db_path=None):
    """Run one job under its lock in its own app context; record the outcome.
    Never raises for a job failure: returns a Result."""
    job = JOBS[name]
    db_path = _db_path(db_path)
    lock = JobLock(name)
    if not lock.acquire():
        logger.info(f"[Jobs] {name}: skipped, another run is still going")
        return Result(name, 'busy', detail='another run is still going')
    try:
        started = _now()
        t0 = time.monotonic()
        _record_start(db_path, name, trigger, started)
        try:
            with _job_app(db_path).app_context():
                detail = job.fn(trigger)
            ok, error = True, None
        except JobError as e:
            ok, detail, error = False, None, str(e)
            logger.error(f"[Jobs] {name} failed: {e}")
        except Exception as e:
            ok, detail = False, None
            error = f'{type(e).__name__}: {e}'
            logger.error(f"[Jobs] {name} failed: {error}\n{traceback.format_exc()}")
        duration = round(time.monotonic() - t0, 2)
        _record_end(db_path, name, ok, detail, error, _now(), duration)
        if ok:
            logger.info(f"[Jobs] {name}: {detail} ({duration}s)")
        return Result(name, 'ok' if ok else 'failed', detail=detail, error=error)
    finally:
        lock.release()


# --- CLI ---------------------------------------------------------------------

def _print_list(db_path):
    status = read_status(db_path)
    for job in JOBS.values():
        st = status.get(job.name) or {}
        line = f"{job.name:22} {job.schedule}"
        if st:
            line += (f"\n{'':22} last run {st.get('last_started')} -> {st.get('last_result')}"
                     + (f" ({st.get('last_detail')})" if st.get('last_detail') else ''))
            if st.get('last_result') == 'failed' and st.get('last_error'):
                line += f"\n{'':22} error: {st.get('last_error')}"
        print(line)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m app.jobs',
                                     description='Run one RSCP maintenance job (systemd timers call this).')
    parser.add_argument('job', nargs='?', choices=sorted(JOBS), help='the job to run')
    parser.add_argument('--list', action='store_true', help='list the jobs and their last result')
    parser.add_argument('--db', help='database file (default: the configured RSCP database)')
    args = parser.parse_args(argv)
    if not args.list and not args.job:
        parser.error('name a job, or use --list')

    from app.services.logger import configure_logging
    configure_logging()

    if args.list:
        _print_list(args.db)
        return 0
    result = run_job(args.job, trigger='cli', db_path=args.db)
    if result.state == 'failed':
        print(f"{args.job} FAILED: {result.error}", file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    # Run the canonical app.jobs module, not this __main__ copy, so code that
    # imports app.jobs (wsgi, the admin panel) shares its state and classes.
    from app.jobs import main as _main
    sys.exit(_main())
