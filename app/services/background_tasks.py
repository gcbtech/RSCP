"""
Background Tasks Module
Handles scheduled background jobs for manifest sync and email ingestion.

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
from app.services.data_manager import load_config, MANIFEST_FILE, sync_manifest
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

# Email ingest import
try:
    import email_ingest
except ImportError:
    import sys
    sys.path.append(os.getcwd())
    try:
        import email_ingest
    except ImportError:
        email_ingest = None
        logger.warning("[Background] email_ingest module not available")

# Background sync status (for monitoring)
SYNC_STATUS = {
    'last_manifest_sync': None,
    'last_email_check': None,
    'manifest_sync_count': 0,
    'email_check_count': 0,
    'manifest_failures': 0,
    'email_failures': 0,
    'errors': [],
    'scheduler_running': False
}

# Configuration
MAX_CONSECUTIVE_FAILURES = 5
MANIFEST_SYNC_INTERVAL_MINUTES = 5
EMAIL_CHECK_INTERVAL_MINUTES = 15

# Global scheduler instance
scheduler = None


def manifest_sync_job():
    """Background job to sync manifest.csv.
    
    This decouples manifest sync from the request cycle, improving
    dashboard response times significantly.
    """
    global SYNC_STATUS
    
    try:
        logger.debug("[Manifest Sync] Starting background sync...")
        sync_manifest()
        
        # Success - reset failure count
        SYNC_STATUS['last_manifest_sync'] = datetime.datetime.now().isoformat()
        SYNC_STATUS['manifest_sync_count'] += 1
        SYNC_STATUS['manifest_failures'] = 0
        logger.debug("[Manifest Sync] ✓ Background sync completed.")
        
    except Exception as e:
        SYNC_STATUS['manifest_failures'] += 1
        error_msg = f"[Manifest Sync] Error (failure {SYNC_STATUS['manifest_failures']}/{MAX_CONSECUTIVE_FAILURES}): {e}"
        logger.error(error_msg)
        
        SYNC_STATUS['errors'].append({
            'time': datetime.datetime.now().isoformat(),
            'job': 'manifest_sync',
            'error': str(e)
        })
        # Keep only last 10 errors
        if len(SYNC_STATUS['errors']) > 10:
            SYNC_STATUS['errors'] = SYNC_STATUS['errors'][-10:]
        
        # If too many failures, pause the job
        if SYNC_STATUS['manifest_failures'] >= MAX_CONSECUTIVE_FAILURES:
            logger.error(f"[Manifest Sync] ⚠ Pausing job after {MAX_CONSECUTIVE_FAILURES} consecutive failures")
            if scheduler and scheduler.get_job('manifest_sync'):
                scheduler.pause_job('manifest_sync')


def email_ingest_job():
    """Background job to check for Amazon shipping emails."""
    global SYNC_STATUS
    from app.services.db import get_db_connection
    
    try:
        conf = load_config()
        if not conf or conf.get('EMAIL_INGEST_ENABLED') != True:
            return  # Email ingestion not enabled
            
        srv = conf.get('IMAP_SERVER')
        usr = conf.get('EMAIL_USER')
        pwd = conf.get('EMAIL_PASS')
        
        if not (srv and usr and pwd):
            return  # Missing credentials
            
        if email_ingest is None:
            logger.warning("[Auto-Ingest] email_ingest module not available")
            return
            
        logger.info(f"[Auto-Ingest] Checking {usr} on {srv}...")
        items = email_ingest.check_amazon_emails(srv, usr, pwd)
        
        if items:
            count = 0
            conn = get_db_connection()
            try:
                today = datetime.date.today()
                
                for item in items:
                    tracking = item['tracking']
                    name = item.get('name', 'Amazon Item')
                    date_str = item.get('date', 'Pending')
                    image = item.get('image_url')
                    
                    # Determine Status
                    status = 'on_time'
                    try:
                        if date_str != 'Pending':
                            d_dt = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
                            if d_dt == today: status = 'expected'
                            elif d_dt < today: status = 'past_due'
                    except ValueError:
                        pass  # Date parsing failed
                    
                    # Insert or Ignore
                    try:
                        conn.execute('''
                            INSERT INTO packages (tracking_number, item_name, date_expected, quantity, status, source, image_url)
                            VALUES (?, ?, ?, 1, ?, 'auto-email', ?)
                        ''', (tracking, name, date_str, status, image))
                        count += 1
                    except sqlite3.IntegrityError:
                        pass  # Already exists
                        
                conn.commit()
                
                # Append to Manifest CSV for record keeping
                if os.path.exists(MANIFEST_FILE) and count > 0:
                    try:
                        with open(MANIFEST_FILE, 'a') as f:
                            for item in items:
                                f.write(f"{item['tracking']},{item.get('name','Item')},1,{item.get('date','Pending')},{item.get('image_url','')}\n")
                    except IOError as e:
                        logger.warning(f"[Auto-Ingest] Could not update manifest CSV: {e}")
                    
            finally:
                conn.close()

            if count > 0:
                logger.info(f"[Auto-Ingest] ✓ Added {count} new packages.")
            else:
                logger.info("[Auto-Ingest] No new packages found (Duplicates).")
        
        # Success - reset failure count
        SYNC_STATUS['last_email_check'] = datetime.datetime.now().isoformat()
        SYNC_STATUS['email_check_count'] += 1
        SYNC_STATUS['email_failures'] = 0
        
    except Exception as e:
        SYNC_STATUS['email_failures'] += 1
        error_msg = f"[Auto-Ingest] Error (failure {SYNC_STATUS['email_failures']}/{MAX_CONSECUTIVE_FAILURES}): {e}"
        logger.error(error_msg)
        
        SYNC_STATUS['errors'].append({
            'time': datetime.datetime.now().isoformat(),
            'job': 'email_ingest',
            'error': str(e)
        })
        if len(SYNC_STATUS['errors']) > 10:
            SYNC_STATUS['errors'] = SYNC_STATUS['errors'][-10:]
        
        if SYNC_STATUS['email_failures'] >= MAX_CONSECUTIVE_FAILURES:
            logger.error(f"[Auto-Ingest] ⚠ Pausing job after {MAX_CONSECUTIVE_FAILURES} consecutive failures")
            if scheduler and scheduler.get_job('email_ingest'):
                scheduler.pause_job('email_ingest')


def eod_email_job():
    """Background job to check and send automated POS EOD emails."""
    try:
        from app.routes.pos.core import get_pos_setting, set_pos_setting
        from app.services.pos_email import send_eod_email
        
        # 1. Check if enabled
        enabled = get_pos_setting('POS_AUTO_EMAIL_ENABLED', 'false') == 'true'
        if not enabled:
            return

        # 2. Check time
        target_time_str = get_pos_setting('POS_AUTO_EMAIL_TIME', '')
        if not target_time_str:
            return
            
        now = datetime.datetime.now()
        current_time_str = now.strftime('%H:%M')
        
        # Compare time (simple string compare for HH:MM)
        # We want to run if current time >= target time
        # BUT we only want to run ONCE per day.
        
        # Check last run date
        last_run_date = get_pos_setting('POS_LAST_AUTO_EMAIL_DATE', '')
        today_str = now.strftime('%Y-%m-%d')
        
        if last_run_date == today_str:
            # Already ran today
            return
        
        # Parse target time to compare properly
        try:
            target_h, target_m = map(int, target_time_str.split(':'))
            target_dt = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
            
            if now >= target_dt:
                # Time to send!
                logger.info(f"[Auto-Email] Sending EOD report for {today_str}...")
                success = send_eod_email(now.date())
                
                if success:
                    set_pos_setting('POS_LAST_AUTO_EMAIL_DATE', today_str)
                    logger.info("[Auto-Email] ✓ Sent successfully.")
                else:
                    logger.error("[Auto-Email] Failed to send.")
                    
        except ValueError:
            logger.error(f"[Auto-Email] Invalid time format: {target_time_str}")
            
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
            manifest_sync_job,
            'interval',
            minutes=MANIFEST_SYNC_INTERVAL_MINUTES,
            id='manifest_sync',
            name='Manifest CSV Sync'
        )
        
        scheduler.add_job(
            email_ingest_job,
            'interval',
            minutes=EMAIL_CHECK_INTERVAL_MINUTES,
            id='email_ingest',
            name='Email Ingestion'
        )

        scheduler.add_job(
            eod_email_job,
            'interval',
            minutes=1,
            id='eod_email',
            name='POS EOD Email'
        )
        
        # Start scheduler
        scheduler.start()
        SYNC_STATUS['scheduler_running'] = True
        logger.info(f"[Background] APScheduler started (manifest: every {MANIFEST_SYNC_INTERVAL_MINUTES}min, email: every {EMAIL_CHECK_INTERVAL_MINUTES}min)")
        
        # Register shutdown handler
        atexit.register(shutdown_scheduler)
        
        # Run initial sync immediately
        manifest_sync_job()
        
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
    
    def manifest_loop():
        while True:
            try:
                manifest_sync_job()
            except Exception as e:
                logger.error(f"[Manifest Sync] Thread error: {e}")
            time.sleep(MANIFEST_SYNC_INTERVAL_MINUTES * 60)
    
    def email_loop():
        while True:
            try:
                email_ingest_job()
            except Exception as e:
                logger.error(f"[Auto-Ingest] Thread error: {e}")
            time.sleep(EMAIL_CHECK_INTERVAL_MINUTES * 60)
    
    manifest_thread = threading.Thread(target=manifest_loop, daemon=True, name="ManifestSync")
    manifest_thread.start()
    
    email_thread = threading.Thread(target=email_loop, daemon=True, name="EmailIngest")
    email_thread.start()
    
    SYNC_STATUS['scheduler_running'] = True
    logger.info("[Background] Threading fallback started (APScheduler not available)")


# Legacy compatibility
def start_background_tasks():
    """Legacy function - now uses start_scheduler()."""
    start_scheduler()


def start_email_thread():
    """Deprecated: Use start_background_tasks() instead."""
    start_background_tasks()


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
        if job_id == 'manifest_sync':
            SYNC_STATUS['manifest_failures'] = 0
        elif job_id == 'email_ingest':
            SYNC_STATUS['email_failures'] = 0
        logger.info(f"[Background] Resumed job: {job_id}")
        return True
    return False
