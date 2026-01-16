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
            'EMAIL_PASS': os.environ.get('RSCP_EMAIL_PASS'),
            'SECRET_KEY': os.environ.get('RSCP_SECRET_KEY'),
            'SSO_CLIENT_ID': os.environ.get('RSCP_SSO_CLIENT_ID'),
            'SSO_CLIENT_SECRET': os.environ.get('RSCP_SSO_CLIENT_SECRET'),
            'SSO_DISCOVERY_URL': os.environ.get('RSCP_SSO_DISCOVERY_URL'),
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
                # Use csv.reader to safely parse headers (handles quotes/commas)
                csv_reader = csv.reader(f)
                try:
                    raw_headers = next(csv_reader)
                except StopIteration:
                    return # Empty file
                
                # Clean headers
                headers = [h.strip() for h in raw_headers]
                
                # Use these cleaned headers with DictReader
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
            
            # Detect Header Format
            has_duplicate_headers = 'TrackingNumber.1' in headers
            
            ids_to_clean = set()
            
            for row in rows:
                tracking = ""
                # Strict Header Logic
                if has_duplicate_headers:
                    # eBay format: TrackingNumber (Col 0) is Order ID, TrackingNumber.1 (Col 12) is Real Tracking
                    real_tracking = str(row.get('TrackingNumber.1', '')).strip().replace('="', '').replace('"', '')
                    order_id = str(row.get('TrackingNumber', '')).strip().replace('="', '').replace('"', '')
                    
                    if real_tracking:
                        tracking = real_tracking
                    
                    # Always mark the Order ID for cleanup if it looks like an ID
                    if order_id and len(order_id) > 5 and order_id != tracking:
                        ids_to_clean.add(order_id)
                else:
                    # Standard Format
                    raw_tracking = str(row.get('TrackingNumber', '')).strip()
                    tracking = raw_tracking.replace('="', '').replace('"', '').strip()
                
                if not tracking: 
                    skipped_count += 1
                    continue
                
                # Clean other fields
                item_name = str(row.get('ItemName', 'Unknown')).strip().replace('="', '').replace('"', '')
                
                # ... existing date/qty logic ...
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
                
                # Fix Source URL: Check all possible variants
                source_url = ""
                for key in ['SourceURL', 'URL', 'PurchaseURL', 'Link', 'ProductLink', 'Product URL', 'View Order Detail']:
                    val = str(row.get(key, '')).strip()
                    if val and val.lower() != 'nan':
                        source_url = val
                        break

                # Composite Key Match: Strict sync to avoid merging different items
                # We match on Tracking + Item Name to allows multiple items per tracking number
                cur.execute("SELECT id, manual_date, status, date_scanned, quantity, item_name, sku FROM packages WHERE tracking_number = ? AND item_name = ?", (tracking, item_name))
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
                
                    # Check mapping for update as well
                    sku = existing['sku']
                    if not sku:
                         mapping = conn.execute("SELECT inventory_sku FROM product_mappings WHERE package_name = ?", (item_name,)).fetchone()
                         if mapping: sku = mapping['inventory_sku']

                    # Update strict match
                    cur.execute('''
                        UPDATE packages SET 
                        date_expected=?, quantity=?, image_url=?, status=?, asin=?, source_url=?, sku=?
                        WHERE id=?
                    ''', (date_final, qty, img, status, asin, source_url, sku, existing['id']))
                    
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

                    # Auto-Map Check
                    sku = None
                    mapping = conn.execute("SELECT inventory_sku FROM product_mappings WHERE package_name = ?", (item_name,)).fetchone()
                    if mapping:
                        sku = mapping['inventory_sku']

                    cur.execute('''
                        INSERT INTO packages (tracking_number, item_name, date_expected, quantity, image_url, status, asin, source_url, sku)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (tracking, item_name, date_str, qty, img, status, asin, source_url, sku))
            
            if skipped_count > 0:
                logger.info(f"Manifest Sync: Skipped {skipped_count} rows due to missing tracking numbers.")
            
            # Batch Cleanup of Invalid Order IDs
            if ids_to_clean:
                logger.info(f"Cleaning occurred: Removes {len(ids_to_clean)} invalid tracking numbers (Order IDs)")
                # Delete in chunks OF 500
                clean_list = list(ids_to_clean)
                chunk_size = 500
                for i in range(0, len(clean_list), chunk_size):
                    chunk = clean_list[i:i + chunk_size]
                    placeholders = ','.join('?' for _ in chunk)
                    # Protect Manual Items from deletion
                    cur.execute(f"DELETE FROM packages WHERE tracking_number IN ({placeholders}) AND source != 'manual'", chunk)
            
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
    # Expected Today
    # Total = count of items where date_expected == today
    # Scanned = count of those items that have date_scanned not null
    
    expected_rows = conn.execute("SELECT date_scanned, status FROM packages WHERE date_expected = ?", (today_str,)).fetchall()
    
    total_expected = len(expected_rows)
    scanned_count = 0
    
    for r in expected_rows:
        # Check if scanned (date_scanned is not None or status is 'received'/'refunded'/'archived', etc?)
        # Simplest: if date_scanned is set.
        if r['date_scanned']:
            scanned_count += 1
            
    stats["expected"]["total"] = total_expected
    stats["expected"]["scanned"] = scanned_count
            
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
            # Check mapping for auto-manifest items too
            sku = None
            mapping = conn.execute("SELECT inventory_sku FROM product_mappings WHERE package_name = ?", (item_name,)).fetchone()
            if mapping: sku = mapping['inventory_sku']

            conn.execute('''
                INSERT INTO packages (tracking_number, item_name, quantity, status, source, date_expected, date_scanned, sku)
                VALUES (?, ?, ?, 'received', 'scan', CURRENT_DATE, CURRENT_TIMESTAMP, ?)
            ''', (tracking, item_name, quantity, sku))
            
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
