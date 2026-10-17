import os
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
# Legacy only: packages live in the database. Migration 10 imported this
# file once; nothing reads or writes it. Kept so old imports keep working.
MANIFEST_FILE = os.path.join(_paths.DATA_DIR, 'manifest.csv')
CONFIG_FILE = _paths.CONFIG_FILE

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

def save_config(new_config: Dict[str, Any] = None, updater=None) -> bool:
    """Merge `new_config` into config.json and update the cache.

    Safe across threads (CONFIG_LOCK) and worker processes (an flock on
    config.json.lock). The write goes to a unique temp file that is fsynced
    and then atomically renamed over config.json. If the existing file cannot
    be parsed, nothing is written: saving only the new keys would silently
    wipe SECRET_KEY and every stored credential.

    `updater`, if given, is called with the current on-disk config while the
    lock is held and returns the keys to change; use it for read-modify-write
    updates that must not race with other workers.
    """
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
                current.update(updater(dict(current)) or {})

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

def get_dashboard_stats() -> Dict[str, Any]:
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

def sync_email_ingest():
    """Run the email ingest now (admin button). Same path as the background job."""
    from app.services.package_imports import run_email_ingest
    return run_email_ingest()
