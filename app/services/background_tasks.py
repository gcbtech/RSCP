import time
import threading
import json
import os
import logging
import datetime
import sqlite3
from app.services.data_manager import load_config, MANIFEST_FILE
from app.services.file_handler import SimpleFileLock, atomic_write
try:
    import email_ingest
except ImportError:
    # Handle if email_ingest.py is not in path or app module context issues.
    # Since email_ingest is in root, and we are in app/services, we might need to fix path or move email_ingest.
    # For now, simplistic approach:
    import sys
    sys.path.append(os.getcwd())
    import email_ingest

logger = logging.getLogger(__name__)

def run_email_scheduler():
    """Background loop to check emails every 15 minutes."""
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
            
        except Exception as e:
            logger.error(f"[Auto-Ingest] Error: {e}")
            
        time.sleep(900) # 15 Minutes

def start_email_thread():
    t = threading.Thread(target=run_email_scheduler, daemon=True)
    t.start()
