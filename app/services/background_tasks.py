import time
import threading
import json
import os
import logging
import datetime
import sqlite3
from app.services.data_manager import load_config, MANIFEST_FILE, sync_manifest
from app.services.file_handler import SimpleFileLock, atomic_write
try:
    import email_ingest
except ImportError:
    # Handle if email_ingest.py is not in path or app module context issues.
    import sys
    sys.path.append(os.getcwd())
    import email_ingest

logger = logging.getLogger(__name__)

# Background sync status (for monitoring)
SYNC_STATUS = {
    'last_manifest_sync': None,
    'last_email_check': None,
    'manifest_sync_count': 0,
    'errors': []
}

def run_manifest_sync_scheduler():
    """Background loop to sync manifest.csv every 5 minutes.
    
    This decouples manifest sync from the request cycle, improving
    dashboard response times significantly.
    """
    global SYNC_STATUS
    
    while True:
        try:
            logger.debug("[Manifest Sync] Starting background sync...")
            sync_manifest()
            SYNC_STATUS['last_manifest_sync'] = datetime.datetime.now().isoformat()
            SYNC_STATUS['manifest_sync_count'] += 1
            logger.debug("[Manifest Sync] Background sync completed.")
            
        except Exception as e:
            error_msg = f"[Manifest Sync] Error: {e}"
            logger.error(error_msg)
            SYNC_STATUS['errors'].append({
                'time': datetime.datetime.now().isoformat(),
                'error': str(e)
            })
            # Keep only last 10 errors
            if len(SYNC_STATUS['errors']) > 10:
                SYNC_STATUS['errors'] = SYNC_STATUS['errors'][-10:]
        
        time.sleep(300)  # 5 minutes


def run_email_scheduler():
    """Background loop to check emails every 15 minutes."""
    global SYNC_STATUS
    from app.services.db import get_db_connection
    
    while True:
        try:
            conf = load_config()
            if conf and conf.get('EMAIL_INGEST_ENABLED') == True:
                srv = conf.get('IMAP_SERVER')
                usr = conf.get('EMAIL_USER')
                pwd = conf.get('EMAIL_PASS')
                
                if srv and usr and pwd:
                    logger.info(f"[Auto-Ingest] Checking {usr} on {srv}...")
                    items = email_ingest.check_amazon_emails(srv, usr, pwd)
                    
                    if items:
                        count = 0
                        conn = get_db_connection()
                        try:
                            # Thread-Safe Insert
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
                                except: pass
                                
                                # Insert or Ignore
                                try:
                                    conn.execute('''
                                        INSERT INTO packages (tracking_number, item_name, date_expected, quantity, status, source, image_url)
                                        VALUES (?, ?, ?, 1, ?, 'auto-email', ?)
                                    ''', (tracking, name, date_str, status, image))
                                    count += 1
                                except sqlite3.IntegrityError:
                                    # Already exists
                                    pass
                                    
                            conn.commit()
                            
                            # Optional: Append to Manifest CSV for record keeping
                            if os.path.exists(MANIFEST_FILE) and count > 0:
                                try:
                                    with open(MANIFEST_FILE, 'a') as f:
                                        for item in items:
                                            # CSV Format: TrackingNumber,ItemName,Quantity,Date,Image
                                            f.write(f"{item['tracking']},{item.get('name','Item')},1,{item.get('date','Pending')},{item.get('image_url','')}\n")
                                except: pass
                                
                        finally:
                            conn.close()

                        if count > 0:
                            logger.info(f"[Auto-Ingest] Added {count} new packages.")
                        else:
                            logger.info("[Auto-Ingest] No new packages found (Duplicates).")
                            
                    SYNC_STATUS['last_email_check'] = datetime.datetime.now().isoformat()
            
        except Exception as e:
            logger.error(f"[Auto-Ingest] Error: {e}")
            
        time.sleep(900)  # 15 Minutes


def start_background_tasks():
    """Start all background task threads.
    
    Called once at application startup from app.py.
    """
    # Manifest sync thread (every 5 minutes)
    manifest_thread = threading.Thread(target=run_manifest_sync_scheduler, daemon=True, name="ManifestSync")
    manifest_thread.start()
    logger.info("[Background] Manifest sync scheduler started (every 5 minutes)")
    
    # Email ingest thread (every 15 minutes)
    email_thread = threading.Thread(target=run_email_scheduler, daemon=True, name="EmailIngest")
    email_thread.start()
    logger.info("[Background] Email ingest scheduler started (every 15 minutes)")


# Legacy function for backwards compatibility
def start_email_thread():
    """Deprecated: Use start_background_tasks() instead."""
    start_background_tasks()


def get_sync_status():
    """Get current background sync status for monitoring."""
    return SYNC_STATUS
