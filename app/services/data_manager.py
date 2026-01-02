import os
import json
import logging
import datetime
import time
import csv # Replaced pandas with csv
import threading
from typing import Dict, Any, List, Optional, Set
import sqlite3

from app.services.db import get_db_connection, BASE_DIR
from app.utils.helpers import parse_date

logger = logging.getLogger(__name__)

# --- CONFIG ---
# --- CONFIG ---
MANIFEST_FILE = os.path.join(BASE_DIR, 'manifest.csv')
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

# Cache for Sync checking
DATA_CACHE = {
    'manifest_mtime': 0,
    'last_sync_date': None
}

# Performance: Config cache with TTL to avoid disk I/O on every request
# Lock to prevent race conditions during concurrent load/save
CONFIG_LOCK = threading.Lock()
CONFIG_CACHE = {
    'data': None,
    'loaded_at': 0,
    'ttl': 5  # seconds (reduced from 60 for more responsive updates)
}

def load_config(force_reload: bool = False) -> Dict[str, Any]:
    """Load configuration with caching. Uses 5s TTL to avoid disk I/O.
    
    Thread-safe: Uses CONFIG_LOCK to prevent race conditions.
    
    Args:
        force_reload: If True, bypasses cache and reloads from disk.
    """
    global CONFIG_CACHE
    now = time.time()
    
    # Return cached config if valid and not forcing reload (no lock needed for read)
    if not force_reload and CONFIG_CACHE['data'] is not None:
        if (now - CONFIG_CACHE['loaded_at']) < CONFIG_CACHE['ttl']:
            return CONFIG_CACHE['data'].copy()  # Return copy to prevent mutation
    
    # Lock for disk I/O and cache update
    with CONFIG_LOCK:
        # Double-check cache after acquiring lock (another thread may have updated)
        if not force_reload and CONFIG_CACHE['data'] is not None:
            if (now - CONFIG_CACHE['loaded_at']) < CONFIG_CACHE['ttl']:
                return CONFIG_CACHE['data'].copy()
        
        config = {}
        
        # Load from config.json first
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    config = json.load(f)
            except Exception as e:
                logger.error(f"[Config] Failed to load {CONFIG_FILE}: {e}")
                config = {}
        
        # Environment variable overrides for sensitive values
        # These take priority over config.json values
        env_overrides = {
            'WEBHOOK_URL': os.environ.get('RSCP_WEBHOOK_URL'),
            'IMAP_SERVER': os.environ.get('RSCP_IMAP_SERVER'),
            'EMAIL_USER': os.environ.get('RSCP_EMAIL_USER'),
            'EMAIL_PASS': os.environ.get('RSCP_EMAIL_PASS'),
            'SECRET_KEY': os.environ.get('RSCP_SECRET_KEY'),
        }
        
        for key, env_val in env_overrides.items():
            if env_val:
                config[key] = env_val
        
        # Update cache
        CONFIG_CACHE['data'] = config
        CONFIG_CACHE['loaded_at'] = time.time()
        
        return config.copy()

def save_config(new_config: Dict[str, Any]) -> bool:
    """Save configuration to config.json and update cache.
    
    Thread-safe: Uses CONFIG_LOCK to prevent race conditions.
    """
    global CONFIG_CACHE
    
    with CONFIG_LOCK:
        try:
            # Load existing first to preserve other keys
            current = {}
            if os.path.exists(CONFIG_FILE):
                try:
                    with open(CONFIG_FILE, 'r') as f:
                        current = json.load(f)
                except Exception as e:
                    logger.error(f"[Config] Failed to load existing config: {e}")
                    current = {}
            
            current.update(new_config)
            
            # Write atomically by writing to temp file first
            temp_file = CONFIG_FILE + '.tmp'
            with open(temp_file, 'w') as f:
                json.dump(current, f, indent=4)
            
            # Replace original with temp (atomic on most file systems)
            os.replace(temp_file, CONFIG_FILE)
            
            # Update cache with new data immediately
            CONFIG_CACHE['data'] = current
            CONFIG_CACHE['loaded_at'] = time.time()
            
            return True
        except Exception as e:
            logger.error(f"[Config] Failed to save {CONFIG_FILE}: {e}")
            return False

def get_file_age(filepath: str) -> float:
    if not os.path.exists(filepath): return 999.0
    try:
        stats = os.stat(filepath)
        return round((time.time() - stats.st_mtime) / 3600, 1)
    except OSError:
        return 999.0

def sync_manifest():
    """Reads manifest.csv and updates the packages table (Uses CSV module)."""
    global DATA_CACHE
    
    if not os.path.exists(MANIFEST_FILE):
        return

    mtime = os.path.getmtime(MANIFEST_FILE)
    today = datetime.date.today()
    
    # Check if sync is needed
    if mtime > DATA_CACHE['manifest_mtime'] or DATA_CACHE['last_sync_date'] != today:
        logger.info("Syncing Manifest to DB...")
        try:
            # Robust CSV Reading
            with open(MANIFEST_FILE, mode='r', encoding='utf-8-sig', errors='replace') as f:
                # Read header line first to clean it
                header_line = f.readline()
                if not header_line: return # Empty file
                
                # Split and clean headers manually
                headers = [h.strip().replace('"', '') for h in header_line.split(',')]
                
                # Use these cleaned headers
                reader = csv.DictReader(f, fieldnames=headers)
                
                # Note: f pointer is now past header, but DictReader might expect to consume header?
                # If we pass fieldnames, DictReader assumes first read is DATA.
                # So this is correct. readline() consumed proper header line.
                
                rows = list(reader)
            
            conn = get_db_connection()
            cur = conn.cursor()
            
            c = load_config()
            do_trim = c.get('AUTO_TRIM', False)
            sixty_days_ago = today - datetime.timedelta(days=60)
            
            skipped_count = 0
            
            for row in rows:
                # Clean Tracking Number (Handle Excel format ="123")
                raw_tracking = str(row.get('TrackingNumber', '')).strip()
                tracking = raw_tracking.replace('="', '').replace('"', '').strip()
                
                if not tracking: 
                    skipped_count += 1
                    continue
                
                # Clean other fields
                item_name = str(row.get('ItemName', 'Unknown')).strip().replace('="', '').replace('"', '')
                
                # ... existing date/qty logic ...
                # Re-implementing simplified for the replacement block
                date_str = parse_date(str(row.get('Date', '')))
                
                try:
                    qty = int(float(row.get('Quantity', '1') or 1)) 
                except ValueError:
                    qty = 1
                
                img = str(row.get('Image', '')).strip()
                if img.lower() == 'nan': img = ""
                
                # ASIN/URL Cleaning
                asin = str(row.get('ASIN', '')).strip().replace('="', '').replace('"', '')
                if asin.lower() == 'nan': asin = ""
                
                source_url = str(row.get('SourceURL', row.get('URL', row.get('PurchaseURL', row.get('Link', row.get('ProductLink', row.get('Product URL', ''))))))).strip()
                if source_url.lower() == 'nan': source_url = ""

                # Composite Key Match: Strict sync to avoid merging different items
                # We match on Tracking + Item Name to allows multiple items per tracking number
                cur.execute("SELECT id, manual_date, status, date_scanned, quantity, item_name FROM packages WHERE tracking_number = ? AND item_name = ?", (tracking, item_name))
                existing = cur.fetchone()
                
                status = 'on_time'
                date_final = date_str
                
                if existing:
                    # Update Existing Record
                    if existing['manual_date']:
                        date_final = existing['manual_date']
                    
                    # Calculate Math Status
                    try:
                         if date_final != "Pending":
                            d_dt = datetime.datetime.strptime(date_final, '%Y-%m-%d').date()
                            if d_dt == today: status = 'expected'
                            elif d_dt < today: status = 'past_due'
                    except ValueError:
                        pass 
                    
                    # Trim Check logic ...
                    if do_trim and status == 'past_due' and existing['date_scanned']: 
                        try:
                            d_dt = datetime.datetime.strptime(date_final, '%Y-%m-%d').date()
                            if d_dt < sixty_days_ago: 
                                cur.execute("DELETE FROM packages WHERE id = ?", (existing['id'],))
                                continue
                        except ValueError:
                            pass

                    if existing['status'] in ['expected', 'past_due', 'pending', 'on_time', 'received']:
                         if existing['date_scanned']:
                             status = 'received' 
                
                    # Update strict match
                    cur.execute('''
                        UPDATE packages SET 
                        date_expected=?, quantity=?, image_url=?, status=?, asin=?, source_url=?
                        WHERE id=?
                    ''', (date_final, qty, img, status, asin, source_url, existing['id']))
                    
                else:
                    # New Package (Distinct Item)
                    try:
                        if date_str != "Pending":
                            d_dt = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
                            if d_dt == today: status = 'expected'
                            elif d_dt < today: status = 'past_due'
                            else: status = 'on_time'
                    except ValueError:
                        pass 

                    cur.execute('''
                        INSERT INTO packages (tracking_number, item_name, date_expected, quantity, image_url, status, asin, source_url)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (tracking, item_name, date_str, qty, img, status, asin, source_url))
            
            if skipped_count > 0:
                logger.info(f"Manifest Sync: Skipped {skipped_count} rows due to missing tracking numbers.")
            
            conn.commit()
            conn.close()
            
            DATA_CACHE['manifest_mtime'] = mtime
            DATA_CACHE['last_sync_date'] = today
            
        except Exception as e:
            logger.error(f"Sync Manifest Error: {e}")



def get_dashboard_stats() -> Dict[str, Any]:
    # Note: sync_manifest() is now called by background task scheduler (every 5 min)
    # This significantly improves dashboard response time
    
    stats = {
        "expected": {"total": 0, "scanned": 0, "status": "gold"}, 
        "past_due": {"count": 0, "status": "green"}, 
        "returns": {"open": 0, "status": "green"}, 
        "refunded": {"count": 0, "status": "green"}
    }
    
    conn = get_db_connection()
    today_str = datetime.date.today().strftime('%Y-%m-%d')
    thirty_days_ago = (datetime.date.today() - datetime.timedelta(days=30)).strftime('%Y-%m-%d')

    # Expected
    # Expected
    today_scanned_filter = []
    rows = conn.execute("SELECT status, date_scanned FROM packages WHERE date_expected = ?", (today_str,)).fetchall()
    for r in rows:
        # If not scanned, it's expected.
        # If scanned TODAY, it's expected (and arrived).
        # If scanned PREVIOUSLY, it is NOT expected today (it's done).
        
        is_scanned = (r['date_scanned'] is not None)
        scanned_today = (is_scanned and str(r['date_scanned']).startswith(today_str))
        
        if not is_scanned or scanned_today:
             stats["expected"]["total"] += 1
             if is_scanned:
                 stats["expected"]["scanned"] += 1
            
    # Past Due
    pd_count = conn.execute("SELECT count(*) as c FROM packages WHERE status='past_due' AND date_scanned IS NULL").fetchone()['c']
    stats["past_due"]["count"] = pd_count
    
    # Returns
    ret_count = conn.execute("SELECT count(*) as c FROM packages WHERE status='return_pending'").fetchone()['c']
    stats["returns"]["open"] = ret_count
    
    # Refunded
    ref_count = conn.execute("SELECT count(*) as c FROM packages WHERE status='refunded' AND refund_date > ?", (thirty_days_ago,)).fetchone()['c']
    stats["refunded"]["count"] = ref_count
    
    conn.close()
    
    # Colors
    e = stats["expected"]
    if e["total"] == 0: e["status"] = "gold"
    elif e["scanned"] == e["total"]: e["status"] = "green"
    else: e["status"] = "red"

    # Past due should be red if any are past due
    if stats["past_due"]["count"] > 0: stats["past_due"]["status"] = "red"
    
    # Returns should be red if any are open
    if stats["returns"]["open"] > 0: stats["returns"]["status"] = "red"
    
    if stats["refunded"]["count"] > 0: stats["refunded"]["status"] = "green"
    
    return stats

def get_analytics_stats(days: int = 14) -> List[Dict[str, Any]]:
    """Returns daily scan counts for the last N days."""
    conn = get_db_connection()
    today = datetime.date.today()
    start_date = today - datetime.timedelta(days=days-1) # Inclusive of today
    
    # Initialize all dates with 0
    results = {}
    for i in range(days):
        d = (start_date + datetime.timedelta(days=i)).strftime('%Y-%m-%d')
        results[d] = 0
        
    try:
        # Optimised Group Query
        query = """
            SELECT date(timestamp, 'localtime') as day, count(*) as c 
            FROM history 
            WHERE action='received' 
            AND date(timestamp, 'localtime') >= ? 
            GROUP BY day
        """
        rows = conn.execute(query, (start_date.strftime('%Y-%m-%d'),)).fetchall()
        
        for r in rows:
            if r['day'] in results:
                results[r['day']] = r['c']
                
    except Exception as e:
        logger.error(f"Analytics Error: {e}")
    finally:
        conn.close()
        
    # Convert to sorted list
    return [{"date": k, "count": v} for k, v in results.items()]

def send_priority_alert(tracking: str, item_name: str, quantity: str, user: str, webhook_url_enc: str, secret_key: str):
    """Sends a Webhook POST request (Async)."""
    try:
        from app.utils.helpers import reveal_string
        import urllib.request
        import json
        
        # 1. Reveal URL
        url = reveal_string(webhook_url_enc, secret_key)
        if not url.startswith('http'): return
        
        # 2. Build Payload (Discord/Slack compatible)
        # Discord uses 'content', Slack uses 'text'. We'll send both.
        msg = f"🚨 **Priority Item Received!**\n📦 **Item:** {item_name}\n🔢 **Qty:** {quantity}\n🔍 **Tracking:** {tracking}\n👤 **User:** {user}"
        
        payload = {
            "content": msg, # Discord
            "text": msg     # Slack
        }
        
        req = urllib.request.Request(
            url, 
            data=json.dumps(payload).encode('utf-8'),
            headers={'Content-Type': 'application/json', 'User-Agent': 'RSCP-Bot'}
        )
        
        urllib.request.urlopen(req, timeout=5)
        logger.info(f"Webhook alert sent for {tracking}")
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")

def log_receipt(tracking: str, item_name: str, quantity: str, user: str) -> None:
    # 1. Add to History Table
    # 2. Update Package as Scanned
    conn = get_db_connection()
    try:
        # Get User ID
        res = conn.execute("SELECT id FROM users WHERE username = ?", (user,)).fetchone()
        user_id = res['id'] if res else None
        
        # Get ALL matching packages (Handle multiple items with same tracking)
        packages = conn.execute("SELECT id, status, priority, quantity FROM packages WHERE tracking_number = ?", (tracking,)).fetchall()
        
        if packages:
            for pkg in packages:
                pkg_id = pkg['id']
                is_priority = bool(pkg['priority']) if pkg['priority'] else False
                qty = pkg['quantity'] or 1
                
                # Update Package
                current_status = pkg['status']
                new_status = current_status
                if current_status in ['expected', 'past_due', 'pending', 'on_time']:
                     new_status = 'received'
                
                conn.execute("UPDATE packages SET date_scanned=CURRENT_TIMESTAMP, status=? WHERE id=?", (new_status, pkg_id))
                
                # Log History for EACH item
                conn.execute("INSERT INTO history (package_id, user_id, action, details) VALUES (?, ?, ?, ?)", 
                             (pkg_id, user_id, 'received', f"Qty: {qty}"))
                
                # Trigger Webhook if Priority (for each priority item)
                if is_priority:
                     conf = load_config()
                     if conf.get('WEBHOOK_ENABLED') and conf.get('WEBHOOK_URL'):
                         enc_url = conf.get('WEBHOOK_URL')
                         key = conf.get('SECRET_KEY', 'dev_key_fallback')
                         
                         import threading
                         # Use item_name passed or fetch? main.py passes "Unknown" usually if not found? 
                         # But here we found it. We should fetch name if we want accurate alerts.
                         # Since this loop updates IDs, let's fetch name?
                         # Or just pass the generic 'item_name' from arg (which comes from main.py's fetchone).
                         # Simplest: use arg.
                         t = threading.Thread(target=send_priority_alert, args=(tracking, item_name, quantity, user, enc_url, key))
                         t.start()

        else:
            # Create Package (Auto-Manifest)
            conn.execute('''
                INSERT INTO packages (tracking_number, item_name, quantity, status, source, date_expected, date_scanned)
                VALUES (?, ?, ?, 'received', 'scan', CURRENT_DATE, CURRENT_TIMESTAMP)
            ''', (tracking, item_name, quantity))
            
            # Get the new ID
            pkg_id = conn.execute("SELECT id FROM packages WHERE tracking_number = ?", (tracking,)).fetchone()['id']
            
            # Log History
            conn.execute("INSERT INTO history (package_id, user_id, action, details) VALUES (?, ?, ?, ?)", 
                         (pkg_id, user_id, 'received', f"Qty: {quantity}"))
        
        conn.commit()
        
        # Trigger Webhook if Priority
        if is_priority:
            conf = load_config()
            if conf.get('WEBHOOK_ENABLED') and conf.get('WEBHOOK_URL'):
                enc_url = conf.get('WEBHOOK_URL')
                key = conf.get('SECRET_KEY', 'dev_key_fallback')
                
                import threading
                t = threading.Thread(target=send_priority_alert, args=(tracking, item_name, quantity, user, enc_url, key))
                t.start()
        
    except Exception as e:
        logger.error(f"Log receipt error: {e}")
    finally:
        conn.close()

def get_scan_count(days: int = 1) -> int:
    conn = get_db_connection()
    try:
        # Calculate start date
        start_date = (datetime.date.today() - datetime.timedelta(days=days-1)).strftime('%Y-%m-%d')
        
        count = conn.execute("""
            SELECT count(*) as c FROM history 
            WHERE action='received' 
            AND date(timestamp, 'localtime') >= ?
        """, (start_date,)).fetchone()['c']
        return count
    finally:
        conn.close()

def check_history(tracking: str) -> bool:
    conn = get_db_connection()
    res = conn.execute("SELECT count(*) as c FROM history h JOIN packages p ON h.package_id = p.id WHERE p.tracking_number = ?", (tracking,)).fetchone()
    found = res['c'] > 0
    conn.close()
    return found
