"""
Background Tasks Module
Handles scheduled background jobs: package status upkeep and the POS EOD email.

Only used where systemd does not run the jobs (app/jobs.py, use_systemd_timers()):
the legacy/2.x layout, and data-dir installs whose updater has not installed
the rscp-job units yet. The job bodies, their per-job locks, the EOD
"sent today" marker and the status table are shared with `python -m app.jobs`.

Uses APScheduler for reliable job scheduling with:
- Retry logic with exponential backoff
- Max failure tracking (jobs disable after N consecutive failures)
- Health status endpoint for monitoring
- Graceful shutdown handling
"""
import os
import logging
import datetime
import sqlite3
import atexit
from app.services.data_manager import load_config
from app.services.file_handler import SimpleFileLock, atomic_write

logger = logging.getLogger(__name__)

# APScheduler import - fall back to threading if not available
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False
    logger.warning("[Background] APScheduler not installed, falling back to threading")

# Background sync status (for monitoring)
SYNC_STATUS = {
    'last_maintenance': None,
    'maintenance_count': 0,
    'maintenance_failures': 0,
    'errors': [],
    'scheduler_running': False
}

# Configuration
MAX_CONSECUTIVE_FAILURES = 5
MAINTENANCE_INTERVAL_MINUTES = 15

# Global scheduler instance
scheduler = None


def _record_failure(job_id, counter, label, e):
    SYNC_STATUS[counter] += 1
    logger.error(f"{label} Error (failure {SYNC_STATUS[counter]}/{MAX_CONSECUTIVE_FAILURES}): {e}")
    SYNC_STATUS['errors'].append({
        'time': datetime.datetime.now().isoformat(),
        'job': job_id,
        'error': str(e)
    })
    # Keep only last 10 errors
    if len(SYNC_STATUS['errors']) > 10:
        SYNC_STATUS['errors'] = SYNC_STATUS['errors'][-10:]
    # If too many failures, pause the job
    if SYNC_STATUS[counter] >= MAX_CONSECUTIVE_FAILURES:
        logger.error(f"{label} ⚠ Pausing job after {MAX_CONSECUTIVE_FAILURES} consecutive failures")
        if scheduler and scheduler.get_job(job_id):
            scheduler.pause_job(job_id)


def package_maintenance_job():
    """Keep package statuses in step with the calendar (on_time -> expected ->
    past_due) and apply AUTO_TRIM (app.jobs.package_maintenance)."""
    from app import jobs
    result = jobs.run_job('package-maintenance', trigger='app-scheduler')
    if result.state == 'ok':
        SYNC_STATUS['last_maintenance'] = datetime.datetime.now().isoformat()
        SYNC_STATUS['maintenance_count'] += 1
        SYNC_STATUS['maintenance_failures'] = 0
    elif result.state == 'failed':
        _record_failure('package_maintenance', 'maintenance_failures', '[Packages]', result.error)


def eod_email_job():
    """Check and send the automated POS EOD email (app.jobs.eod_email: once a
    day at/after POS_AUTO_EMAIL_TIME, never twice for one day)."""
    try:
        from app import jobs
        jobs.run_job('eod-email', trigger='app-scheduler')
    except Exception as e:
        logger.error(f"[Auto-Email] Job error: {e}")


def start_scheduler():
    """Start the APScheduler with all background jobs.
    
    Called once at application startup from app.py.
    Uses a file-based lock to ensure only one worker starts the scheduler
    when running with multiple gunicorn workers.
    """
    global scheduler, SYNC_STATUS
    
    # Already running check
    if scheduler is not None and SYNC_STATUS['scheduler_running']:
        logger.debug("[Background] Scheduler already running in this process, skipping")
        return
    
    # Try to acquire lock (only one worker should start scheduler)
    lock_file_path = '/tmp/rscp_scheduler.lock'
    
    try:
        # Try to create lock file exclusively
        lock_fd = os.open(lock_file_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(lock_fd, str(os.getpid()).encode())
        os.close(lock_fd)
        logger.info("[Background] This worker acquired scheduler lock")
    except FileExistsError:
        # Another worker already has the lock - check if it's still running
        try:
            with open(lock_file_path, 'r') as f:
                pid = int(f.read().strip())
            # Check if that process is still alive
            os.kill(pid, 0)  # Doesn't actually kill, just checks
            logger.info(f"[Background] Scheduler owned by worker PID {pid}, skipping")
            return
        except (ProcessLookupError, ValueError, FileNotFoundError):
            # Process died or lock file is stale, take over
            try:
                os.remove(lock_file_path)
                lock_fd = os.open(lock_file_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(lock_fd, str(os.getpid()).encode())
                os.close(lock_fd)
                logger.info("[Background] Took over stale scheduler lock")
            except OSError:
                logger.info("[Background] Could not acquire lock, skipping scheduler")
                return
    except Exception as e:
        logger.warning(f"[Background] Lock error: {e}, starting scheduler anyway (dev mode?)")
    
    if APSCHEDULER_AVAILABLE:
        scheduler = BackgroundScheduler(
            daemon=True,
            job_defaults={
                'coalesce': True,  # Combine missed runs
                'max_instances': 1,  # Only 1 instance of each job at a time
                'misfire_grace_time': 60  # Allow 60 seconds grace period
            }
        )
        
        # Add jobs
        scheduler.add_job(
            package_maintenance_job,
            'interval',
            minutes=MAINTENANCE_INTERVAL_MINUTES,
            id='package_maintenance',
            name='Package Status Upkeep'
        )
        
        scheduler.add_job(
            eod_email_job,
            'interval',
            minutes=1,
            id='eod_email',
            name='POS EOD Email'
        )

        # Nightly backups for installs without rscp-backup.timer (legacy
        # layout, or before the updater installed the timer); a no-op otherwise.
        from app import backup as nightly_backup
        if nightly_backup.app_scheduler_should_run():
            scheduler.add_job(
                nightly_backup.scheduler_job,
                'interval',
                minutes=15,
                id='nightly_backup',
                name='Nightly Backup'
            )
        
        # Start scheduler
        scheduler.start()
        SYNC_STATUS['scheduler_running'] = True
        logger.info(f"[Background] APScheduler started (packages: every {MAINTENANCE_INTERVAL_MINUTES}min)")
        
        # Register shutdown handler
        atexit.register(shutdown_scheduler)
        
        # Bring statuses up to date immediately
        package_maintenance_job()
        
    else:
        # Fallback to threading
        _start_threading_fallback()


def shutdown_scheduler():
    """Gracefully shutdown the scheduler and release the lock file."""
    global scheduler, SYNC_STATUS
    
    if scheduler and SYNC_STATUS['scheduler_running']:
        logger.info("[Background] Shutting down scheduler...")
        scheduler.shutdown(wait=False)
        SYNC_STATUS['scheduler_running'] = False
        
        # Clean up lock file
        lock_file_path = '/tmp/rscp_scheduler.lock'
        try:
            if os.path.exists(lock_file_path):
                os.remove(lock_file_path)
                logger.debug("[Background] Scheduler lock file removed")
        except Exception as e:
            logger.warning(f"[Background] Could not remove lock file: {e}")
        
        logger.info("[Background] Scheduler stopped.")


def _start_threading_fallback():
    """Fallback to simple threading if APScheduler is not available."""
    import time
    import threading
    
    global SYNC_STATUS
    
    def maintenance_loop():
        while True:
            try:
                package_maintenance_job()
            except Exception as e:
                logger.error(f"[Packages] Thread error: {e}")
            time.sleep(MAINTENANCE_INTERVAL_MINUTES * 60)
    
    maintenance_thread = threading.Thread(target=maintenance_loop, daemon=True, name="PackageMaintenance")
    maintenance_thread.start()
    
    SYNC_STATUS['scheduler_running'] = True
    logger.info("[Background] Threading fallback started (APScheduler not available)")


# Legacy compatibility
def start_background_tasks():
    """Legacy function - now uses start_scheduler()."""
    start_scheduler()


def get_sync_status():
    """Get current background sync status for monitoring."""
    global scheduler
    
    status = SYNC_STATUS.copy()
    
    # Add scheduler-specific info if available
    if scheduler and APSCHEDULER_AVAILABLE:
        jobs = []
        for job in scheduler.get_jobs():
            jobs.append({
                'id': job.id,
                'name': job.name,
                'next_run': job.next_run_time.isoformat() if job.next_run_time else 'paused',
                'paused': job.next_run_time is None
            })
        status['jobs'] = jobs
        status['scheduler_type'] = 'apscheduler'
    else:
        status['scheduler_type'] = 'threading'
    
    return status


def resume_job(job_id):
    """Resume a paused job and reset its failure counter."""
    global scheduler, SYNC_STATUS
    
    if scheduler and scheduler.get_job(job_id):
        scheduler.resume_job(job_id)
        if job_id == 'package_maintenance':
            SYNC_STATUS['maintenance_failures'] = 0
        logger.info(f"[Background] Resumed job: {job_id}")
        return True
    return False
