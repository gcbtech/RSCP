"""
Automatic backups of the database, uploads and config.json.

    python -m app.backup               # back up now (ignores BACKUP_ENABLED/BACKUP_TIME)
    python -m app.backup --scheduled   # back up only if one is due (rscp-backup.timer)
    python -m app.backup --status      # print the last run's status as JSON

Each run writes, under <backups_dir>/nightly/:

    rscp-YYYYmmdd-HHMMSS.db             SQLite online-backup copy, integrity-checked
    uploads-YYYYmmdd-HHMMSS.tar.gz      uploads/ plus config.json (holds SECRET_KEY)
    status.json                         last run: time, result, sizes, error

Files are written to a temporary name and renamed into place, so a crash
never leaves a half-written backup under a real name. After a successful
run, old backups are pruned: the newest backup of each of the last
BACKUP_KEEP_DAILY days and of each of the last BACKUP_KEEP_WEEKLY ISO weeks
are kept (a backup may count for both), and the newest backup is always
kept. Files whose names don't match the patterns above are never touched.

Off-box copy: when BACKUP_OFFSITE_DIR is set (a mounted NAS/SMB/NFS
directory), the new files are copied there afterwards and pruned with the
same retention. A failure there is recorded in status.json but does not fail
the local backup. The directory must already exist and must not be on the
same filesystem as the local backups (an unmounted share's empty mount
point would otherwise silently fill the local disk). Under systemd only
rscp-backup.service may write there (install.sh adds the folder to its
ReadWritePaths, see README "Backups"); so "Run backup now" in the web UI
leaves the copy to the next timer run, which also retries a failed copy
(at most hourly).

Scheduling: rscp-backup.timer runs `--scheduled` every 15 minutes; a backup
is due when none has succeeded since the most recent BACKUP_TIME, so the
time set in the admin page applies without touching systemd. Installs
without that timer (legacy layout, or a data-dir install whose updater
predates it) run the same check from the app's background scheduler.

One run at a time: a thread lock plus an flock on nightly/.lock (where
available) covers both the web process ("Run backup now") and the timer.
"""
import argparse
import datetime
import errno
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import threading
import time

from app import paths

logger = logging.getLogger(__name__)

NIGHTLY_SUBDIR = 'nightly'
STATUS_NAME = 'status.json'
LOCK_NAME = '.lock'
STAMP_FORMAT = '%Y%m%d-%H%M%S'
DB_RE = re.compile(r'^rscp-(\d{8}-\d{6})\.db$')
UPLOADS_RE = re.compile(r'^uploads-(\d{8}-\d{6})\.tar\.gz$')

DEFAULTS = {
    'BACKUP_ENABLED': True,
    'BACKUP_TIME': '02:30',
    'BACKUP_KEEP_DAILY': 14,
    'BACKUP_KEEP_WEEKLY': 8,
    'BACKUP_KEEP_UPLOADS': 3,
    'BACKUP_OFFSITE_DIR': '',
}
SETTING_KEYS = tuple(DEFAULTS)

STALE_AFTER = datetime.timedelta(hours=36)   # admin warning threshold
RETRY_AFTER = datetime.timedelta(hours=1)    # scheduled retry after a failed run
SYSTEMD_TIMER = '/etc/systemd/system/rscp-backup.timer'

# The off-box directory must be on another filesystem than the local backups.
# Tests turn this off (their temp dirs share one disk).
OFFSITE_REQUIRE_SEPARATE_DEVICE = True

_THREAD_LOCK = threading.Lock()


class BackupBusy(RuntimeError):
    """Another backup is already running."""


class BackupError(RuntimeError):
    """The local backup failed."""


# --- locations ---------------------------------------------------------------

def nightly_dir():
    return os.path.join(paths.backups_dir(), NIGHTLY_SUBDIR)


def status_path(root=None):
    return os.path.join(root or nightly_dir(), STATUS_NAME)


def _stamp(dt):
    return dt.strftime(STAMP_FORMAT)


def _parse_stamp(s):
    try:
        return datetime.datetime.strptime(s, STAMP_FORMAT)
    except ValueError:
        return None


def _iso(dt):
    return dt.isoformat(timespec='seconds') if dt else None


def _from_iso(s):
    if not s:
        return None
    try:
        return datetime.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


# --- settings ----------------------------------------------------------------

def load_settings():
    """The backup settings (registry defaults if the store can't be read)."""
    out = dict(DEFAULTS)
    try:
        from app.services import settings as settings_store
        for key in SETTING_KEYS:
            if key in settings_store.SETTINGS:
                out[key] = settings_store.get(key)
    except Exception as e:  # never let a settings problem stop a backup
        logger.warning(f"[Backup] Could not read settings, using defaults: {e}")
    return out


# --- retention ---------------------------------------------------------------

def select_keep(stamps, keep_daily, keep_weekly):
    """The subset of `stamps` (datetimes) to keep.

    Keeps the newest backup of each of the `keep_daily` most recent days that
    have one, and of each of the `keep_weekly` most recent ISO weeks that have
    one; the two rules are independent. The newest backup is always kept.
    """
    ordered = sorted(set(stamps), reverse=True)
    keep = set()
    rules = ((max(0, int(keep_daily)), lambda t: t.date()),
             (max(0, int(keep_weekly)), lambda t: tuple(t.isocalendar())[:2]))
    for count, period in rules:
        seen = set()
        for ts in ordered:
            key = period(ts)
            if key in seen:
                continue
            if len(seen) >= count:
                break
            seen.add(key)
            keep.add(ts)
    if ordered:
        keep.add(ordered[0])
    return keep


def list_backups(root):
    """{stamp datetime: [file names]} for the backup files in `root`."""
    found = {}
    try:
        names = os.listdir(root)
    except OSError:
        return found
    for name in names:
        m = DB_RE.match(name) or UPLOADS_RE.match(name)
        if not m:
            continue
        ts = _parse_stamp(m.group(1))
        if ts:
            found.setdefault(ts, []).append(name)
    return found


def prune(root, keep_daily, keep_weekly, keep_uploads=None):
    """Delete backup files in `root` outside the retention. Returns deleted names.

    Uploads archives are large (every image, every night) and change slowly,
    so beyond the daily/weekly rule only the newest `keep_uploads` of them
    are kept; database copies follow the full daily/weekly retention.
    """
    found = list_backups(root)
    keep = select_keep(found.keys(), keep_daily, keep_weekly)
    if keep_uploads is None:
        keep_uploads = DEFAULTS['BACKUP_KEEP_UPLOADS']
    upload_stamps = sorted((ts for ts, names in found.items()
                            if ts in keep and any(UPLOADS_RE.match(n) for n in names)), reverse=True)
    drop_uploads = set(upload_stamps[max(1, int(keep_uploads)):])
    deleted = []
    for ts, names in found.items():
        if ts in keep:
            if ts in drop_uploads:
                for name in names:
                    if UPLOADS_RE.match(name):
                        try:
                            os.remove(os.path.join(root, name))
                            deleted.append(name)
                        except OSError as e:
                            logger.warning(f"[Backup] Could not delete old backup {name}: {e}")
            continue
        for name in names:
            try:
                os.remove(os.path.join(root, name))
                deleted.append(name)
            except OSError as e:
                logger.warning(f"[Backup] Could not delete old backup {name}: {e}")
    return sorted(deleted)


def list_db_backups(root=None):
    """Local nightly database backups, newest first: [{name, when, size}]."""
    root = root or nightly_dir()
    out = []
    for ts, names in list_backups(root).items():
        for name in names:
            if DB_RE.match(name):
                try:
                    size = os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
                out.append({'name': name, 'when': ts, 'size': size})
    return sorted(out, key=lambda b: b['when'], reverse=True)


def db_backup_path(name, root=None):
    """Path of the nightly database backup called `name`, or None if the name
    is not a nightly backup name or the file does not exist."""
    if not name or not DB_RE.match(name):
        return None
    path = os.path.join(root or nightly_dir(), name)
    return path if os.path.isfile(path) else None


# --- writing -----------------------------------------------------------------

def _tmp_name(root, final_name):
    return os.path.join(root, f'.{final_name}.{os.getpid()}.tmp')


def _fsync_file(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def backup_database(src_db, dest):
    """Copy the live database to `dest` with SQLite's online backup API,
    integrity-check the copy, then rename it into place."""
    if not os.path.exists(src_db):
        raise BackupError(f'database not found: {src_db}')
    tmp = _tmp_name(os.path.dirname(dest), os.path.basename(dest))
    try:
        src = sqlite3.connect(src_db, timeout=30)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
                # One self-contained file (no -wal sidecar needed to read it)
                dst.execute('PRAGMA journal_mode=DELETE')
                result = dst.execute('PRAGMA integrity_check').fetchone()[0]
            finally:
                dst.close()
        finally:
            src.close()
        if result != 'ok':
            raise BackupError(f'integrity check of the copy failed: {result}')
        _fsync_file(tmp)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return os.path.getsize(dest)


def archive_uploads(uploads_dir, dest, config_file=None):
    """tar.gz of the uploads directory (as uploads/) plus config.json."""
    tmp = _tmp_name(os.path.dirname(dest), os.path.basename(dest))
    try:
        with tarfile.open(tmp, 'w:gz') as tar:
            # realpath: in the legacy layout static/uploads is a symlink, and
            # tarfile would otherwise store the link instead of the files.
            real = os.path.realpath(uploads_dir) if uploads_dir else ''
            if real and os.path.isdir(real):
                tar.add(real, arcname='uploads')
            if config_file and os.path.isfile(config_file):
                tar.add(os.path.realpath(config_file), arcname='config.json')
        _fsync_file(tmp)
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return os.path.getsize(dest)


def _clean_temp_files(root):
    """Leftovers of an interrupted run (only called while holding the lock)."""
    try:
        for name in os.listdir(root):
            if name.startswith('.') and name.endswith('.tmp'):
                try:
                    os.remove(os.path.join(root, name))
                except OSError:
                    pass
    except OSError:
        pass


def copy_offsite(files, offsite_dir, keep_daily, keep_weekly, local_root=None, keep_uploads=None):
    """Copy `files` (paths) into `offsite_dir`, then prune it. Raises on failure."""
    if not os.path.isdir(offsite_dir):
        raise OSError(errno.ENOENT, f'{offsite_dir} does not exist or is not a directory '
                                    '(is the share mounted?)')
    if OFFSITE_REQUIRE_SEPARATE_DEVICE and local_root:
        if os.stat(offsite_dir).st_dev == os.stat(local_root).st_dev:
            raise OSError(errno.EXDEV, f'{offsite_dir} is on the same disk as the local backups '
                                       '(is the share mounted?)')
    for path in files:
        name = os.path.basename(path)
        dest = os.path.join(offsite_dir, name)
        tmp = _tmp_name(offsite_dir, name)
        try:
            shutil.copyfile(path, tmp)   # data only: shares often refuse chmod/utime
            if os.path.getsize(tmp) != os.path.getsize(path):
                raise OSError(errno.EIO, f'size mismatch copying {name}')
            os.replace(tmp, dest)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return prune(offsite_dir, keep_daily, keep_weekly, keep_uploads)


def _offsite_error_text(e):
    if isinstance(e, OSError) and e.errno == errno.EROFS:
        return (f'{e} - the backup service may not write there. Run '
                '"sudo /opt/rscp/bin/rscp-update --backup-paths" after changing the off-box folder.')
    return str(e)


# --- status ------------------------------------------------------------------

def read_status(root=None):
    try:
        with open(status_path(root), 'r') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_status(status, root=None):
    path = status_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = _tmp_name(os.path.dirname(path), STATUS_NAME)
    try:
        with open(tmp, 'w') as f:
            json.dump(status, f, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# --- locking -----------------------------------------------------------------

class _RunLock:
    def __init__(self, root):
        self.root = root
        self.fd = None
        self.have_thread_lock = False

    def __enter__(self):
        if not _THREAD_LOCK.acquire(blocking=False):
            raise BackupBusy('a backup is already running')
        self.have_thread_lock = True
        try:
            import fcntl
        except ImportError:
            return self  # Windows dev: one process, the thread lock is enough
        try:
            os.makedirs(self.root, exist_ok=True)
            self.fd = open(os.path.join(self.root, LOCK_NAME), 'a+')
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.__exit__(None, None, None)
            raise BackupBusy('a backup is already running')
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            try:
                self.fd.close()   # releases the flock
            except OSError:
                pass
            self.fd = None
        if self.have_thread_lock:
            _THREAD_LOCK.release()
            self.have_thread_lock = False
        return False


def is_running(root=None):
    try:
        with _RunLock(root or nightly_dir()):
            return False
    except BackupBusy:
        return True


# --- the run -----------------------------------------------------------------

def run_backup(db_path=None, uploads_dir=None, root=None, config_file=None,
               settings=None, now=None, trigger='manual', defer_offsite=False):
    """Make one backup. Returns the status dict (also written to status.json).

    defer_offsite: leave the off-box copy to the next `--scheduled` run (the
    web process may not write to the off-box folder; rscp-backup.service can).

    Raises BackupBusy if another run holds the lock. Other failures are
    recorded in the status (ok=False) rather than raised.
    """
    from app.services import db as _db
    db_path = db_path or _db.DB_PATH
    uploads_dir = uploads_dir if uploads_dir is not None else paths.UPLOADS_DIR
    config_file = config_file if config_file is not None else paths.CONFIG_FILE
    root = root or nightly_dir()
    conf = dict(DEFAULTS)
    conf.update(settings if settings is not None else load_settings())
    now = now or datetime.datetime.now()

    os.makedirs(root, exist_ok=True)
    with _RunLock(root):
        _clean_temp_files(root)
        previous = read_status(root) or {}
        stamp = _stamp(now)
        db_name = f'rscp-{stamp}.db'
        up_name = f'uploads-{stamp}.tar.gz'
        started = time.monotonic()
        status = {
            'last_run': _iso(now),
            'last_success': previous.get('last_success'),
            'trigger': trigger,
            'ok': False,
            'error': None,
            'location': root,
            'db_file': None, 'db_size': None,
            'uploads_file': None, 'uploads_size': None,
            'pruned': 0,
            'duration_s': None,
            'offsite': {
                'dir': conf.get('BACKUP_OFFSITE_DIR') or '',
                'ok': None, 'error': None, 'file': None, 'pending': False, 'last_attempt': None,
                'last_success': (previous.get('offsite') or {}).get('last_success'),
            },
        }
        try:
            db_size = backup_database(db_path, os.path.join(root, db_name))
            status.update(db_file=db_name, db_size=db_size)
            up_size = archive_uploads(uploads_dir, os.path.join(root, up_name), config_file)
            status.update(uploads_file=up_name, uploads_size=up_size)
            status['pruned'] = len(prune(root, conf['BACKUP_KEEP_DAILY'], conf['BACKUP_KEEP_WEEKLY'],
                                         conf['BACKUP_KEEP_UPLOADS']))
            status['ok'] = True
            status['last_success'] = _iso(now)
            logger.info(f"[Backup] Wrote {db_name} ({db_size} bytes) and {up_name} ({up_size} bytes)")
        except Exception as e:
            status['error'] = str(e)
            logger.exception('[Backup] Local backup failed')
            # Don't leave half a pair behind under real names
            for name in (db_name, up_name):
                try:
                    os.remove(os.path.join(root, name))
                except OSError:
                    pass
            status.update(db_file=None, db_size=None, uploads_file=None, uploads_size=None)

        offsite = (conf.get('BACKUP_OFFSITE_DIR') or '').strip()
        if status['ok'] and offsite:
            if defer_offsite:
                status['offsite']['pending'] = True
            else:
                _offsite_step(status, root, offsite, conf, now)

        status['duration_s'] = round(time.monotonic() - started, 2)
        write_status(status, root)
        return status


def _offsite_step(status, root, offsite, conf, now):
    """Copy the backup named in `status` off-box; record the result in it."""
    off = status.setdefault('offsite', {})
    off.update(dir=offsite, last_attempt=_iso(now), pending=False)
    files = [os.path.join(root, status['db_file']), os.path.join(root, status['uploads_file'])]
    try:
        copy_offsite(files, offsite, conf['BACKUP_KEEP_DAILY'], conf['BACKUP_KEEP_WEEKLY'], local_root=root,
                     keep_uploads=conf['BACKUP_KEEP_UPLOADS'])
        off.update(ok=True, error=None, file=status['db_file'], last_success=_iso(now))
        logger.info(f"[Backup] Copied {status['db_file']} to {offsite}")
    except Exception as e:
        off.update(ok=False, error=_offsite_error_text(e))
        logger.error(f"[Backup] Off-box copy to {offsite} failed: {e}")


def sync_offsite(root=None, settings=None, now=None):
    """Copy the latest local backup off-box if it isn't there yet: after a
    deferred ("Run backup now") copy, or to retry a failed one (at most once
    per RETRY_AFTER). Returns the updated status, or None if nothing was done."""
    conf = dict(DEFAULTS)
    conf.update(settings if settings is not None else load_settings())
    offsite = (conf.get('BACKUP_OFFSITE_DIR') or '').strip()
    if not offsite:
        return None
    root = root or nightly_dir()
    now = now or datetime.datetime.now()
    try:
        with _RunLock(root):
            status = read_status(root)
            if not status or not status.get('ok') or not status.get('db_file') or not status.get('uploads_file'):
                return None
            off = status.get('offsite') or {}
            if off.get('ok') and off.get('file') == status['db_file'] and off.get('dir') == offsite:
                return None
            last = _from_iso(off.get('last_attempt'))
            if not off.get('pending') and off.get('dir') == offsite and last and now - last < RETRY_AFTER:
                return None
            if not all(os.path.isfile(os.path.join(root, status[k])) for k in ('db_file', 'uploads_file')):
                return None
            _offsite_step(status, root, offsite, conf, now)
            write_status(status, root)
            return status
    except BackupBusy:
        return None


# --- scheduling --------------------------------------------------------------

def _slot(now, hhmm):
    """The most recent scheduled time at or before `now`."""
    try:
        h, m = (int(p) for p in (hhmm or DEFAULTS['BACKUP_TIME']).split(':'))
    except ValueError:
        h, m = 2, 30
    slot = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if slot > now:
        slot -= datetime.timedelta(days=1)
    return slot


def is_due(status, backup_time, now=None):
    """True when no backup has succeeded since the last scheduled time, and the
    last attempt was not a failure within RETRY_AFTER."""
    now = now or datetime.datetime.now()
    status = status or {}
    slot = _slot(now, backup_time)
    last_success = _from_iso(status.get('last_success'))
    if last_success and last_success >= slot:
        return False
    last_run = _from_iso(status.get('last_run'))
    if last_run and last_run >= slot and now - last_run < RETRY_AFTER:
        return False
    return True


def run_if_due(now=None, root=None, trigger='scheduled', **kwargs):
    """Run a backup if enabled and due; otherwise catch up on the off-box copy.
    Returns the status, or None if nothing was done."""
    conf = load_settings()
    if not conf.get('BACKUP_ENABLED'):
        return None
    root = root or nightly_dir()
    if not is_due(read_status(root), conf.get('BACKUP_TIME'), now):
        return sync_offsite(root=root, settings=conf, now=now)
    try:
        return run_backup(root=root, settings=conf, now=now, trigger=trigger, **kwargs)
    except BackupBusy:
        logger.info('[Backup] Skipped: another backup is running')
        return None


def app_scheduler_should_run():
    """Whether the app's background scheduler must run backups itself: legacy
    installs, and data-dir installs whose systemd timer isn't installed yet
    (the first in-app update to this version runs the previous updater,
    which doesn't know about the timer)."""
    return not paths.USES_DATA_DIR or not os.path.exists(SYSTEMD_TIMER)


def scheduler_job():
    """APScheduler job (every 15 minutes) for installs without the timer."""
    if not app_scheduler_should_run():
        return
    try:
        run_if_due(trigger='app-scheduler')
    except Exception as e:
        logger.error(f"[Backup] Scheduled backup error: {e}")


# --- admin warning -----------------------------------------------------------

def warning_for(status, enabled, now=None):
    """The admin warning text, or None when backups are fine (or disabled)."""
    if not enabled:
        return None
    now = now or datetime.datetime.now()
    last_success = _from_iso((status or {}).get('last_success'))
    if last_success is None:
        return 'No automatic backup has completed yet.'
    if now - last_success > STALE_AFTER:
        # No running hour count: the text doubles as the banner's dismiss key
        return (f'The last successful backup was on {last_success:%Y-%m-%d %H:%M}, '
                f'more than {int(STALE_AFTER.total_seconds() // 3600)} hours ago.')
    return None


_WARNING_CACHE = {'at': 0.0, 'key': None, 'value': None}
_WARNING_TTL = 60.0


def cached_warning(root=None):
    """warning_for() on the status file, cached for a minute per process."""
    from app.services import settings as settings_store
    try:
        enabled = settings_store.get('BACKUP_ENABLED')
    except Exception:
        enabled = True
    path = status_path(root)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = None
    key = (path, mtime, enabled)
    nowm = time.monotonic()
    if _WARNING_CACHE['key'] == key and nowm - _WARNING_CACHE['at'] < _WARNING_TTL:
        return _WARNING_CACHE['value']
    value = warning_for(read_status(root) if mtime is not None else None, enabled)
    _WARNING_CACHE.update(at=nowm, key=key, value=value)
    return value


# --- CLI ---------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m app.backup',
                                     description='Back up the RSCP database, uploads and config.json.')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--scheduled', action='store_true',
                       help='only back up if enabled and due (BACKUP_TIME); used by rscp-backup.timer')
    group.add_argument('--status', action='store_true', help='print the last status and exit')
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format='[backup] %(message)s', stream=sys.stdout)

    if args.status:
        print(json.dumps(read_status(), indent=2))
        return 0
    try:
        status = run_if_due() if args.scheduled else run_backup(trigger='cli')
    except BackupBusy:
        print('Another backup is already running.')
        return 0
    if status is None:
        return 0
    if not status['ok']:
        print(f"Backup FAILED: {status['error']}")
        return 1
    off = status['offsite']
    if off['dir'] and not off['ok']:
        print(f"Off-box copy failed: {off['error']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
