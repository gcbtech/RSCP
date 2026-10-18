"""
WSGI Entry Point for Production Deployment
Use with Gunicorn: gunicorn -c gunicorn.conf.py wsgi:app
"""
import os
import logging

from app import create_app, paths
from app.services.background_tasks import start_background_tasks

logger = logging.getLogger(__name__)

# Create the Flask application
app = create_app()


def _acquire_singleton_lock():
    """Try to become the one process that runs background tasks.

    Gunicorn imports this module once per worker, so anything started here
    would otherwise run in EVERY worker (duplicate scheduled jobs). An
    exclusive non-blocking flock lets exactly one worker win; the fd is held for the life of the process so the lock releases
    automatically if that worker dies (a future worker restart re-acquires).

    Returns the open fd on success (kept referenced by the caller), True on
    platforms without fcntl (single-process dev server), or None.
    """
    try:
        import fcntl  # Linux/Unix only
    except ImportError:
        # No fcntl means no gunicorn (Windows dev via app.py): this is the
        # only process, so it owns the background tasks.
        return True

    # In the data directory: the code directory is read-only under systemd.
    lock_path = os.path.join(paths.DATA_DIR, '.background_tasks.lock')
    try:
        # 'a+' so losers don't truncate the owner's PID note; truncate only
        # after we actually hold the lock.
        fd = open(lock_path, 'a+')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.seek(0)
        fd.truncate()
        fd.write(str(os.getpid()))
        fd.flush()
        return fd
    except (OSError, IOError):
        return None


# Periodic jobs (package status upkeep, POS EOD email, backup fallback) and
# post-update system migrations (app/jobs.py).
#
# Data-dir install with the job units installed: systemd runs them
# (rscp-*.timer -> rscp-job@<name>.service; system migrations as
# rscp.service's ExecStartPre, once per start), so no worker starts a scheduler.
# Otherwise (legacy layout, or an updater that has not installed the units yet):
# exactly ONE worker runs them, as in 2.x.
from app import jobs as _jobs

_lock_fd = None
if _jobs.use_systemd_timers():
    logger.info(f"[Background Tasks] Worker {os.getpid()}: scheduled jobs run from systemd timers "
                f"(systemctl list-timers 'rscp-*'); not starting the in-app scheduler")
else:
    _lock_fd = _acquire_singleton_lock()
    if _lock_fd is not None:
        logger.info(f"[Background Tasks] Worker {os.getpid()} owns background tasks")
        start_background_tasks()
        # Post-update system steps (unit rewrites, backfills) — versioned and
        # idempotent, so in-app updates need no manual follow-up commands.
        _jobs.run_job('system-migrations', trigger='app-startup')
    else:
        logger.info(f"[Background Tasks] Worker {os.getpid()} skipping (another worker owns them)")

# Gunicorn will import 'app' from this module
