import os
import copy
import json
import logging
import datetime
import time
import csv # Replaced pandas with csv
import re
import threading
from typing import Dict, Any, List, Optional, Set
import sqlite3

from app.services.db import get_db_connection, BASE_DIR, get_request_db
from app.utils.helpers import parse_date

logger = logging.getLogger(__name__)

# --- CONFIG ---
# --- CONFIG ---
from app import paths as _paths
MANIFEST_FILE = os.path.join(_paths.DATA_DIR, 'manifest.csv')
CONFIG_FILE = _paths.CONFIG_FILE

# Cache for Sync checking
DATA_CACHE = {
    'manifest_mtime': 0,
    'last_sync_date': None
}

# config.json holds only bootstrap values now (SECRET_KEY); every other
# setting lives in the database (app/services/settings.py, migration 6).
# load_config()/save_config() remain as compatibility shims over both.
# CONFIG_CACHE caches the config.json file itself (5s TTL).
CONFIG_LOCK = threading.Lock()
CONFIG_CACHE = {
    'data': None,
    'loaded_at': 0,
    'ttl': 5  # seconds
}


def _load_config_file(force_reload: bool = False) -> Dict[str, Any]:
    """config.json as a dict (cached for CONFIG_CACHE['ttl'] seconds)."""
    now = time.time()
    if not force_reload and CONFIG_CACHE['data'] is not None:
        if (now - CONFIG_CACHE['loaded_at']) < CONFIG_CACHE['ttl']:
            return CONFIG_CACHE['data']
    with CONFIG_LOCK:
        if not force_reload and CONFIG_CACHE['data'] is not None:
            if (now - CONFIG_CACHE['loaded_at']) < CONFIG_CACHE['ttl']:
                return CONFIG_CACHE['data']
        config = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    config = json.load(f)
            except Exception as e:
                logger.error(f"[Config] Failed to load {CONFIG_FILE}: {e}")
                config = {}
        CONFIG_CACHE['data'] = config
        CONFIG_CACHE['loaded_at'] = time.time()
        return config


def load_config(force_reload: bool = False) -> Dict[str, Any]:
    """All settings as one dict, with the same keys/values as the old config.json.

    Bootstrap keys come from config.json; everything else from the settings
    table once the database is migrated (before that, config.json as before).
    Environment variables (RSCP_*) override both. Returns a fresh copy.
    """
    from app.services import settings as settings_store
    file_conf = _load_config_file(force_reload)
    stored = settings_store.stored_values(force=force_reload)
    if stored is None:
        config = dict(file_conf)
    else:
        config = {k: v for k, v in file_conf.items() if k in settings_store.BOOTSTRAP_KEYS}
        config.update(copy.deepcopy(stored))
    for key, var in settings_store.ENV_OVERRIDES.items():
        env_val = os.environ.get(var)
        if env_val:
            config[key] = env_val
    return config


def save_config(new_config: Dict[str, Any] = None, updater=None) -> bool:
    """Save settings. Bootstrap keys (SECRET_KEY) go to config.json; every other
    key goes to the settings table (or, before migration 6, to config.json).

    config.json writes are safe across threads (CONFIG_LOCK) and worker
    processes (an flock on config.json.lock): a unique temp file is fsynced
    and atomically renamed over config.json. If the existing file cannot be
    parsed, nothing is written.

    `updater`, if given, is called with the current settings (config.json
    merged with the database) while the config.json lock is held and returns
    the keys to change; use it for read-modify-write updates of bootstrap
    values that must not race with other workers.
    """
    from app.services import settings as settings_store
    updates = dict(new_config or {})
    store_ready = settings_store.store_ready()

    def is_file_key(key):
        return key in settings_store.BOOTSTRAP_KEYS or not store_ready

    to_store = {k: v for k, v in updates.items() if not is_file_key(k)}
    to_file = {k: v for k, v in updates.items() if is_file_key(k)}

    if to_file or updater is not None:
        deferred = {}
        ok = _write_config_file(to_file, updater, is_file_key, deferred)
        if not ok:
            return False
        to_store.update(deferred)

    if to_store:
        try:
            settings_store.set_lenient(to_store)
        except Exception as e:
            logger.error(f"[Config] Failed to save settings {sorted(to_store)}: {e}")
            return False
    return True


def _write_config_file(new_config, updater, is_file_key, deferred) -> bool:
    """Merge into config.json under the cross-process lock (see save_config).
    Keys produced by `updater` that belong in the database go to `deferred`."""
    global CONFIG_CACHE

    with CONFIG_LOCK:
        lock_fd = None
        try:
            try:
                import fcntl
                lock_fd = open(CONFIG_FILE + '.lock', 'a+')
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            except ImportError:
                pass

            current = {}
            if os.path.exists(CONFIG_FILE):
                try:
                    with open(CONFIG_FILE, 'r') as f:
                        current = json.load(f)
                except Exception as e:
                    logger.error(f"[Config] Refusing to save: existing {CONFIG_FILE} is unreadable ({e})")
                    return False

            if new_config:
                current.update(new_config)
            if updater is not None:
                from app.services import settings as settings_store
                view = dict(current)
                view.update(settings_store.stored_values() or {})
                for key, value in (updater(view) or {}).items():
                    if is_file_key(key):
                        current[key] = value
                    else:
                        deferred[key] = value

            import tempfile
            # Resolve symlinks: in the release layout config.json links into the
            # shared data dir, and renaming over the link would replace it.
            target = os.path.realpath(CONFIG_FILE)
            fd, temp_file = tempfile.mkstemp(dir=os.path.dirname(target) or '.',
                                             prefix='.config.', suffix='.tmp')
            try:
                with os.fdopen(fd, 'w') as f:
                    json.dump(current, f, indent=4)
                    f.flush()
                    os.fsync(f.fileno())
                # config.json holds credentials: keep it private to the service user
                os.chmod(temp_file, 0o600)
                os.replace(temp_file, target)
            except Exception:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
                raise

            CONFIG_CACHE['data'] = current
            CONFIG_CACHE['loaded_at'] = time.time()
            return True
        except Exception as e:
            logger.error(f"[Config] Failed to save {CONFIG_FILE}: {e}")
            return False
        finally:
            if lock_fd is not None:
                lock_fd.close()

def ensure_secret_key() -> str:
    """Return SECRET_KEY, generating and persisting one if config.json lacks it.

    Runs under save_config's cross-process lock, so when several workers boot
    at once they all end up with the same key.
    """
    existing = load_config(force_reload=True).get('SECRET_KEY')
    if existing:
        return existing
    import secrets
    save_config(updater=lambda cur: {} if cur.get('SECRET_KEY') else {'SECRET_KEY': secrets.token_hex(32)})
    key = load_config(force_reload=True).get('SECRET_KEY')
    if not key:
        raise RuntimeError("SECRET_KEY is missing from config.json and could not be generated")
    logger.warning("[Config] SECRET_KEY was missing; generated a new one (existing sessions are invalid)")
    return key


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
                # ... check for binary/xlsx signature? 
                # Basic check: read first char. If it's PK (zip), it's likely xlsx.
                first_char = f.read(1)
                f.seek(0)
                if first_char == 'P': 
                    # loose check for 'PK' header of zip/xlsx
                    start = f.read(2)
                    f.seek(0)
                    if start == 'PK':
                         logger.error("Manifest appears to be binary/XLSX. Please convert to CSV.")
                         return
                
                # Check for null bytes (binary)
                sample = f.read(1024)
                if '\0' in sample:
                     logger.error("Manifest appears to be binary. Please save as standard CSV.")
                     return
                f.seek(0)
                
                csv_reader = csv.reader(f)
                try:
                    raw_headers = next(csv_reader)
                except StopIteration:
                    return # Empty file
                
                # Clean headers and Deduplicate
                clean_headers = []
                seen_headers = {}
                for h in raw_headers:
                    h_clean = h.strip()
                    if h_clean in seen_headers:
                         seen_headers[h_clean] += 1
                         h_clean = f"{h_clean}_{seen_headers[h_clean]}"
                    else:
                         seen_headers[h_clean] = 0
                    clean_headers.append(h_clean)
                
                reader = csv.DictReader(f, fieldnames=clean_headers)
                rows = list(reader)
            
            conn = get_db_connection()
            cur = conn.cursor()
            
            c = load_config()
            do_trim = c.get('AUTO_TRIM', False)
            sixty_days_ago = today - datetime.timedelta(days=60)
            
            skipped_count = 0
            
            # Helper to get value from aliases
            def get_val(row_dict, aliases):
                for a in aliases:
                    if a in row_dict:
                        return str(row_dict[a]).strip()
                return ""

            # Check for eBay duplicate headers
            has_duplicate_headers = 'TrackingNumber.1' in clean_headers
            
            ids_to_clean = set()
            
            for row in rows:
                tracking = ""
                is_placeholder_manifest = False
                
                # Custom Header Logic
                if has_duplicate_headers:
                    # eBay format: 
                    # TrackingNumber (Col 0) is Order ID
                    # TrackingNumber_1 (Col 10, duplicated) is Real Tracking
                    # TrackingNumber.1 (Col X) might be literal
                    
                    real_tracking = ""
                    # Check the deduced duplicate names first
                    for key in ['TrackingNumber_1', 'TrackingNumber_2', 'TrackingNumber.1', 'TrackingNumber.2', 'TrackingNumber.3']:
                        val = str(row.get(key, '')).strip().replace('="', '').replace('"', '')
                        if val:
                            real_tracking = val
                            break
                            
                    order_id = str(row.get('TrackingNumber', '')).strip().replace('="', '').replace('"', '')
                    
                    if real_tracking:
                        tracking = real_tracking
                    elif order_id:
                         tracking = order_id
                         is_placeholder_manifest = True 
                    
                    if order_id and len(order_id) > 5 and not is_placeholder_manifest:
                         ids_to_clean.add(order_id)
                else:
                    # Generic Format (Amazon, etc)
                    # Try known tracking aliases
                    raw_tracking = get_val(row, ['TrackingNumber', 'Carrier Tracking #', 'Tracking Number', 'Tracking'])
                    tracking = raw_tracking.replace('="', '').replace('"', '').strip()

                # Fallback: Use Order ID as tracking if tracking is missing?
                if not tracking:
                     # Check Order ID
                     oid = get_val(row, ['Order ID', 'Order Number', 'Order #', 'Reference Number'])
                     if oid:
                         tracking = oid.replace('="', '').replace('"', '').strip()
                         if tracking: is_placeholder_manifest = True

                if not tracking: 
                    skipped_count += 1
                    continue
                
                # Clean other fields
                # Item Name Support
                item_name = get_val(row, ['ItemName', 'Title', 'Product Name', 'Item Description']).replace('="', '').replace('"', '')
                if not item_name: item_name = 'Unknown'
                
                # Date Support
                date_val = get_val(row, ['Date', 'Order Date', 'Purchase Date', 'Time'])
                date_str = parse_date(date_val)
                
                # Quantity Support
                qty_val = get_val(row, ['Quantity', 'Item Quantity', 'Qty', 'Order Quantity'])
                try:
                    qty = int(float(qty_val or 1))
                except ValueError:
                    qty = 1
                
                # Image Support
                img = get_val(row, ['Image', 'ImageUrl', 'Photo', 'Image URL'])
                if img.lower() == 'nan': img = ""
                
                # ASIN Support
                asin = get_val(row, ['ASIN', 'Item ID', 'ItemID']).replace('="', '').replace('"', '')
                if asin.lower() == 'nan': asin = ""
                
                # Source URL Support
                source_url = ""
                for key in ['SourceURL', 'URL', 'PurchaseURL', 'Link', 'ProductLink', 'Product URL', 'View Order Detail']:
                    if key in row:
                        val = str(row[key]).strip()
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

                    # --- ORDER ID MERGE LOGIC ---
                    current_order_id = None
                    
                    # 1. Explicit Column Lookup (The User Asked for This!)
                    # Check for generic "Order ID" keys
                    for key in ['Order ID', 'Order Number', 'Order #', 'Reference Number', 'Reference #', 'Ref Number', 'Order']:
                        val = str(row.get(key, '')).strip()
                        if val and val.lower() != 'nan' and len(val) > 4:
                            # Clean it up slightly?
                            current_order_id = val.replace('="', '').replace('"', '').strip()
                            break
                            
                    # 2. Fallbacks (eBay / Amazon Tracking)
                    if not current_order_id:
                        if has_duplicate_headers and order_id and len(order_id) > 5 and order_id != tracking:
                            current_order_id = order_id
                        elif re.match(r'^\d{3}-\d{7}-\d{7}$', tracking):
                             current_order_id = tracking

                    if current_order_id:
                        placeholder = f"ORDER-{current_order_id}%" # Use wildcard
                        # Find all email records for this order (suffix 01, 02, etc)
                        email_recs = conn.execute("SELECT * FROM packages WHERE tracking_number LIKE ?", (placeholder,)).fetchall()
                        
                        if email_recs:
                            best_match = None
                            if len(email_recs) == 1:
                                best_match = email_recs[0]
                            else:
                                # Fuzzy match name
                                try:
                                    from Levenshtein import ratio
                                    best_score = 0
                                    for rec in email_recs:
                                        score = ratio(item_name.lower(), rec['item_name'].lower())
                                        if score > best_score:
                                            best_score = score
                                            best_match = rec
                                    # Relax threshold to 0.2 because email names vs manifest names can differ wildly
                                    if best_score < 0.2: best_match = None
                                except ImportError:
                                    # Substring fallback
                                    for rec in email_recs:
                                        # Use both-ways substring
                                        if item_name.lower() in rec['item_name'].lower() or rec['item_name'].lower() in item_name.lower():
                                            best_match = rec
                                            break
                                    if not best_match: best_match = email_recs[0] # Fallback: Best Guess

                            if best_match:
                                if not img and best_match['image_url']:
                                    img = best_match['image_url']
                                
                                # Trust Email Quantity
                                # sqlite3.Row has no .get(); this used to raise and abort the whole sync
                                email_qty = best_match['quantity'] if 'quantity' in best_match.keys() else 1
                                if email_qty and email_qty > 0:
                                    qty = email_qty

                                # CLEANUP: Delete the temp email record since we merged it!
                                # Prevents duplicate "ORDER-" items staying in the list
                                # BUT, skip cleanup if we are using the Order ID as the tracking number (is_placeholder_manifest)
                                # Reason: If we delete 'ORDER-123', and our current tracking is '123' (fallback),
                                # we have effectively merged. But if we later get '1Z999', we delete '123'. 
                                # If 'ORDER-123' is gone, '1Z999' can't find the image.
                                # So, if is_placeholder_manifest, we KEEP the 'ORDER-123' record so it can serve the REAL tracking later.
                                if not is_placeholder_manifest:
                                    try:
                                        cur.execute("DELETE FROM packages WHERE id = ?", (best_match['id'],))
                                    except Exception as c_e:
                                        logger.error(f"Failed to cleanup placeholder {best_match['tracking_number']}: {c_e}")

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
    #   BUT exclude packages already scanned on a PRIOR day (they arrived early)
    # Scanned = count of those items that have date_scanned today
    
    expected_rows = conn.execute("""
        SELECT date_scanned, status FROM packages 
        WHERE date_expected = ?
          AND (date_scanned IS NULL OR date(date_scanned, 'localtime') = ?)
    """, (today_str, today_str)).fetchall()
    
    total_expected = len(expected_rows)
    scanned_count = 0
    
    for r in expected_rows:
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

def log_receipt(tracking: str, item_name: str, quantity: str, user: str, conn=None) -> None:
    # 1. Add to History Table
    # 2. Update Package as Scanned
    close_conn = False
    if conn is None:
        try:
            from flask import has_app_context
            if has_app_context():
                conn = get_request_db()
            else:
                conn = get_db_connection()
                close_conn = True
        except ImportError:
            conn = get_db_connection()
            close_conn = True
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

            # date_expected is a calendar date: use the store's local date, not
            # UTC (CURRENT_DATE), or evening scans land on tomorrow.
            cur = conn.execute('''
                INSERT INTO packages (tracking_number, item_name, quantity, status, source, date_expected, date_scanned, sku)
                VALUES (?, ?, ?, 'received', 'scan', date('now', 'localtime'), CURRENT_TIMESTAMP, ?)
            ''', (tracking, item_name, quantity, sku))
            pkg_id = cur.lastrowid
            
            # Log History
            conn.execute("INSERT INTO history (package_id, user_id, action, details) VALUES (?, ?, ?, ?)", 
                         (pkg_id, user_id, 'received', f"Qty: {quantity}"))
        
        conn.commit()
        # (Priority alerts are sent per package in the loop above. A second
        # copy here fired the webhook twice, and raised NameError for newly
        # created packages, where is_priority was never defined.)

    except Exception as e:
        logger.error(f"Log receipt error: {e}")
    finally:
        if close_conn:
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

def check_history(tracking: str, conn=None) -> bool:
    close_conn = False
    if conn is None:
        try:
            from flask import has_app_context
            if has_app_context():
                conn = get_request_db()
            else:
                conn = get_db_connection()
                close_conn = True
        except ImportError:
            conn = get_db_connection()
            close_conn = True
    try:
        res = conn.execute("SELECT count(*) as c FROM history h JOIN packages p ON h.package_id = p.id WHERE p.tracking_number = ?", (tracking,)).fetchone()
        return res['c'] > 0
    finally:
        if close_conn:
            conn.close()

# --- EMAIL INGEST INTEGRATION ---
from email_ingest import check_amazon_emails

def sync_email_ingest():
    """Run the email ingest process."""
    try:
        config = load_config()
        if not config.get('EMAIL_INGEST_ENABLED', False):
            logger.info("Email Ingest Disabled in Config.")
            return {"status": "skipped", "message": "Email Ingest Disabled"}
            
        imap_server = config.get('IMAP_SERVER')
        user = config.get('EMAIL_USER')
        password = config.get('EMAIL_PASS') or config.get('EMAIL_PASSWORD') # Support both keys (legacy fix)
        
        if not imap_server or not user or not password:
            logger.warning("Email Ingest Missing Credentials.")
            return {"status": "error", "message": "Missing Credentials"}
            
        logger.info(f"Starting Email Ingest for {user}...")
        
        # 1. Fetch Emails
        # We use check_amazon_emails which handles both Amazon and eBay logic now
        results = check_amazon_emails(imap_server, user, password)
        
        if not results:
            logger.info("No new email orders found.")
            return {"status": "success", "count": 0, "message": "No new emails found"}
            
        # 2. Save to DB
        conn = get_db_connection()
        count = 0
        
        for item in results:
            try:
                # Check for duplicate tracking (ORDER-ID or regular)
                exists = conn.execute("SELECT id FROM packages WHERE tracking_number = ?", (item['tracking'],)).fetchone()
                
                if not exists:
                    # Insert
                    conn.execute('''
                        INSERT INTO packages (tracking_number, item_name, date_expected, quantity, status, image_url, source)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        item['tracking'], 
                        item['name'], 
                        item['date'], 
                        item.get('quantity', 1),
                        'incoming', # Status
                        item['image_url'], 
                        'auto_email'
                    ))
                    count += 1
                else:
                    # Optional: Update existing if needed? Usually we don't overwrite manual changes.
                    pass
            except Exception as e:
                logger.error(f"Error saving email item {item['tracking']}: {e}")
                
        conn.commit()
        conn.close()
        
        logger.info(f"Email Ingest Complete. Imported {count} new items.")
        return {"status": "success", "count": count, "message": f"Imported {count} items"}
        
    except Exception as e:
        logger.error(f"Email Ingest Failed: {e}")
        return {"status": "error", "message": str(e)}
