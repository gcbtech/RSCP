from flask import Blueprint, render_template, request, redirect, url_for, session, flash, g, current_app
from werkzeug.security import check_password_hash, generate_password_hash
import datetime
import os
import time
import json
import secrets
import logging

logger = logging.getLogger(__name__)

from flask_login import current_user, login_required

# DB Services
from app.services.db import get_db_connection, DB_PATH
from app.services.auth import create_user, BASE_DIR # load_users shim used for login selection
from app.services.data_manager import (
    get_dashboard_stats, sync_manifest, log_receipt, load_config, MANIFEST_FILE,
    get_scan_count, get_file_age, check_history
)
from app.utils.helpers import sanitize_for_csv
from app.services.file_handler import atomic_write # Still used for config?
from app.services.logger import log_error

main_bp = Blueprint('main', __name__)
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

@main_bp.route('/')
def index():
    # Setup Check (Check if users table has admin?)
    conn = get_db_connection()
    try:
        user_count = conn.execute("SELECT count(*) as c FROM users").fetchone()['c']
        if user_count == 0:
            return redirect(url_for('main.setup_wizard'))
    finally:
        conn.close()
    
    if not current_user.is_authenticated:
        logger.info(f"[Index] No user in session. Redirecting to login.")
        return redirect(url_for('auth.login'))
        
    return render_template('gateway.html')

@main_bp.route('/receiving')
@login_required
def receiving_dashboard():
    stats = get_dashboard_stats()
    scans = get_scan_count(14)
    m_age = get_file_age(MANIFEST_FILE)
    
    refresh_info = []
    if m_age == 999:
        refresh_info.append({"label": "Data", "age": "No Files", "status": "grey"})
    elif m_age < 24:
        refresh_info.append({"label": "Manifest", "age": m_age, "status": "green"})
    else:
        refresh_info.append({"label": "Manifest", "age": m_age, "status": "red"})
    
    server_ts = int(time.time() * 1000)
    return render_template('dashboard.html', stats=stats, scans=scans, refresh_info=refresh_info, server_ts=server_ts)

@main_bp.route('/setup', methods=['GET', 'POST'])
def setup_wizard():
    # Check if users exist to prevent re-setup
    conn = get_db_connection()
    try:
        if conn.execute("SELECT count(*) as c FROM users").fetchone()['c'] > 0:
            return redirect(url_for('auth.login'))
    finally:
        conn.close()
    
    if request.method == 'POST':
        # Save Config
        config_data = {
            "ORG_NAME": request.form.get('org_name', ''),
            "ADMIN_HASH": generate_password_hash(request.form.get('admin_pass')),
            "SECRET_KEY": secrets.token_hex(16),
            "AUTO_TRIM": False
        }
        try:
            with atomic_write(CONFIG_FILE, 'w') as f:
                json.dump(config_data, f, indent=4)
        except: pass
        
        # Create Users
        create_user("Admin", config_data["ADMIN_HASH"], is_admin=True) 
        
        staff_user = request.form.get('first_user', 'Staff').strip()
        staff_pass = request.form.get('first_password', '').strip()
        
        if staff_pass:
            # User requested that the first created user is also an Admin
            create_user(staff_user, generate_password_hash(staff_pass), is_admin=True)
            
        return redirect(url_for('auth.login'))
        
    return render_template('setup.html')

@main_bp.route('/settings')
def settings_page():
    return render_template('settings.html')

@main_bp.route('/report_issue', methods=['POST'])
def report_issue():
    message = request.form.get('message')
    source_url = request.form.get('source_url')
    user_agent = request.form.get('user_agent')
    
    # Store minimal trace info
    details = f"User Report from {source_url}\nUA: {user_agent}"
    
    log_error(message, level="USER_REPORT", source="Frontend", user_id=session.get('user'), trace=details)
    
    flash("Issue Reported. Admins have been notified.")
    # Redirect back to source if valid, otherwise index
    # Security: Validate redirect URL to prevent open redirect attacks
    from urllib.parse import urlparse
    if source_url:
        parsed = urlparse(source_url)
        # Only allow same-origin redirects (no host means relative URL)
        if parsed.netloc == '' or parsed.netloc == urlparse(request.host_url).netloc:
            return redirect(source_url)
    return redirect(url_for('main.index'))

@main_bp.route('/scan', methods=['GET', 'POST'])
def scan_page_legacy():
    logger.info(f"[Scan] scan_page_legacy called. Method: {request.method}, Session: {dict(session)}")
    if request.method == 'POST':
        tracking = request.form.get('tracking_input')
        user_input = request.form.get('user_input')
        
        # Call Logic (Simulating API logic but returning template)
        # We can repurpose logic or just call handle_scan if we mocked request? 
        # Better: Duplicate minimal logic or extract to service.
        # Extracting logic is best, but for now, inline.
        
        conn = get_db_connection()
        msg = "Scan Failed"
        color = ""
        items = []
        is_priority = False
        try:
            tracking = sanitize_for_csv(tracking)
            item = conn.execute("SELECT * FROM packages WHERE tracking_number = ?", (tracking,)).fetchone()
            
            # Fallback: FedEx barcodes (Long string containing tracking at end)
            if not item and len(tracking) > 10:
                # Find any package where the tracking number is contained in the scan
                # instr(A, B) > 0 checks if B is inside A
                candidates = conn.execute("SELECT * FROM packages WHERE instr(?, tracking_number) > 0", (tracking,)).fetchall()
                
                best_match = None
                for c in candidates:
                    t = c['tracking_number']
                    # Safety: Ensure it matches the END of the barcode (FedEx standard)
                    # And ensure the tracking number isn't too short (avoid matching "123")
                    if len(t) > 8 and tracking.endswith(t):
                         if best_match is None or len(t) > len(best_match['tracking_number']):
                             best_match = c
                
                if best_match:
                    item = best_match
                    tracking = best_match['tracking_number'] # Use the real tracking for logging
            
            item_name = item['item_name'] if item else "Unknown Item"
            
            # Logic: Receive
            if not item:
                 # V1.16.1: Check if it's an inventory SKU
                 inv_item = None
                 try:
                     conf = load_config()
                     if conf and conf.get('INVENTORY_ENABLED'):
                         inv_item = conn.execute(
                             "SELECT * FROM inventory_items WHERE sku = ?", (tracking,)
                         ).fetchone()
                 except:
                     pass
                 
                 if inv_item:
                     # Inventory mode - pass item to template
                     return render_template('scan.html', 
                         message=f"📦 {inv_item['name']}",
                         color="#6f42c1",  # Purple for inventory
                         mode='inventory',
                         inventory_item={
                             'id': inv_item['id'],
                             'sku': inv_item['sku'],
                             'name': inv_item['name'],
                             'quantity': inv_item['quantity'],
                             'image_url': inv_item['image_url'],
                             'sell_price': inv_item['sell_price'],
                             'location': ' / '.join(filter(None, [
                                 inv_item['location_area'],
                                 inv_item['location_aisle'],
                                 inv_item['location_shelf'],
                                 inv_item['location_bin']
                             ]))
                         })
                 else:
                     msg = "Unknown Item (Not in Manifest)"
                     color = "#ff6961" # Red
            elif item['date_scanned'] and check_history(tracking):
                 msg = f"Duplicate: {item_name}"
                 color = "#ffd700" # Gold
            else:
                 qty = str(item['quantity'])
                 log_receipt(tracking, item_name, qty, current_user.username)
                 
                 # Check Priority
                 is_priority = bool(item['priority']) if 'priority' in item.keys() and item['priority'] else False
                 
                 if is_priority:
                     msg = "PRIORITY RECEIVED"
                     color = "#C77DFF" # Purple ("proper purple background")
                 else:
                     msg = f"Received: {item_name}"
                     color = "#77dd77" # Green
                 
            # Add to items list for display
            items = [{
                "name": item_name, 
                "image": item['image_url'] if item else None, 
                "subtext": tracking,
                "qty": item['quantity'] if item else 1,
                "image_url": item['image_url'] if item else '',
                "asin": item['asin'] if item and 'asin' in item.keys() else '',
                "source_url": item['source_url'] if item and 'source_url' in item.keys() else ''
            }]
            
        except Exception as e:
            msg = f"Error: {e}"
            color = "#ff6961"
            items = []
        finally:
            conn.close()
        
        # Check if inventory is enabled for Add to Inventory button
        inventory_match = None
        config = load_config()
        inv_enabled = config.get('INVENTORY_ENABLED', False) if config else False
        
        # V1.16.1: Check if received package matches existing inventory item
        if inv_enabled and color == "#77dd77" and items:
            try:
                inv_conn = get_db_connection()
                try:
                    pkg_asin = items[0].get('asin', '') or ''
                    pkg_name = items[0].get('name', '') or ''
                    
                    # Try ASIN match first (exact)
                    if pkg_asin and pkg_asin.strip():
                        match = inv_conn.execute(
                            "SELECT id, name, sku, quantity, image_url FROM inventory_items WHERE asin = ?",
                            (pkg_asin.strip(),)
                        ).fetchone()
                        if match:
                            inventory_match = dict(match)
                    
                    # Try name match (fuzzy - case insensitive contains)
                    if not inventory_match and pkg_name and pkg_name.strip():
                        # Use first 30 chars for matching (handles truncated names)
                        search_prefix = pkg_name[:30].lower().rstrip('.').rstrip()
                        match = inv_conn.execute(
                            """SELECT id, name, sku, quantity, image_url FROM inventory_items 
                               WHERE LOWER(name) LIKE ? 
                               LIMIT 1""",
                            (f'{search_prefix}%',)
                        ).fetchone()
                        if match:
                            inventory_match = dict(match)
                finally:
                    inv_conn.close()
            except Exception as e:
                logger.error(f"Error checking inventory match: {e}")
            
        return render_template('scan.html', message=msg, color=color, items=items, mode='receive', 
                               priority_alert=is_priority, inventory_enabled=inv_enabled,
                               inventory_match=inventory_match)
        
    return scan_page('receive') 

@main_bp.route('/scan/<mode>', methods=['GET'])
@login_required
def scan_page(mode):
    logger.info(f"[Scan] scan_page called. Mode: {mode}, User: {current_user.username}")
    return render_template('scan.html', mode=mode)

@main_bp.route('/api/scan', methods=['POST'])
@login_required
def handle_scan():
    pass  # Legacy endpoint, authentication now handled by decorator

@main_bp.route('/api/dashboard_stats')
@login_required
def api_dashboard_stats():
    from app.services.data_manager import get_dashboard_stats, get_scan_count, get_analytics_stats
    
    return {
        "stats": get_dashboard_stats(),
        "scans": get_scan_count(14),
        "graph": get_analytics_stats(14)
    }
    
    data = request.json
    tracking = sanitize_for_csv(data.get('tracking'))
    mode = data.get('mode')
    
    conn = get_db_connection()
    try:
        item = conn.execute("SELECT * FROM packages WHERE tracking_number = ?", (tracking,)).fetchone()
        item_name = item['item_name'] if item else "Unknown Item"
        
        if mode == 'receive':
            if item:
                if item['date_scanned']: # Treated as 'received' if scanned date exists
                     if check_history(tracking):
                         return {"status": "duplicate", "msg": "Already Received", "name": item_name}
            
            # Log It (Sync Manifest called inside get_dashboard_stats? no need here?)
            # log_receipt handles DB updates and history logging
            qty = str(item['quantity']) if item else '1'
            log_receipt(tracking, item_name, qty, session['user'])
            return {"status": "success", "msg": "Received", "name": item_name}
            
    finally:
        conn.close()
    
    return {"status": "error", "msg": "Invalid Mode"}

@main_bp.route('/history')
@login_required
def history_view():
    
    conn = get_db_connection()
    try:
        # Fetch Users for Filter
        user_list = conn.execute("SELECT username FROM users ORDER BY username").fetchall()
        users = [u['username'] for u in user_list]
        
        # Base Query
        query = '''
            SELECT h.timestamp as Timestamp, h.action, u.username as User, p.tracking_number as Tracking, p.item_name as ItemName, h.details
            FROM history h
            LEFT JOIN users u ON h.user_id = u.id
            LEFT JOIN packages p ON h.package_id = p.id
            WHERE 1=1
        '''
        params = []
        
        # Filters
        start_date = request.args.get('start_date')
        end_date = request.args.get('end_date')
        user_filter = request.args.get('user_filter')
        search_term = request.args.get('search_term')
        
        if start_date:
            query += " AND date(h.timestamp) >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date(h.timestamp) <= ?"
            params.append(end_date)
        if user_filter:
            query += " AND u.username = ?"
            params.append(user_filter)
        if search_term:
            query += " AND (p.tracking_number LIKE ? OR p.item_name LIKE ?)"
            params.append(f"%{search_term}%")
            params.append(f"%{search_term}%")
            
        if request.args.get('filter') == 'today': # Legacy support / specific button
             query += " AND date(h.timestamp, 'localtime') = date('now', 'localtime')"

        # Only Limit if No Filters (to show full search results)
        if not (start_date or end_date or user_filter or search_term):
            query += " ORDER BY h.timestamp DESC LIMIT 100"
        else:
            query += " ORDER BY h.timestamp DESC LIMIT 500" # Safety cap for search

        rows = conn.execute(query, params).fetchall()
        
        history = []
        for r in rows:
            history.append({
                "Timestamp": r['Timestamp'],
                "User": r['User'] or "Unknown",
                "Tracking": r['Tracking'] or "Deleted Package",
                "ItemName": r['ItemName'] or "Unknown",
                "Quantity": r['details'].split(': ')[1] if r['details'] and 'Qty:' in r['details'] else '1' # Extract simple qty or pass details
            })
            
        return render_template('history.html', history=history, users=users)
    except Exception as e:
        logger.error(f"History Error: {e}")
        return render_template('history.html', history=[], users=[])
    finally:
        conn.close()

@main_bp.route('/return_mode', methods=['GET', 'POST'])
def return_mode():
    found_item = None
    message = None
    candidates = []
    
    conn = get_db_connection()
    try:
        if request.args.get('track'):
            t = request.args.get('track')
            item = conn.execute("SELECT * FROM packages WHERE tracking_number=?", (t,)).fetchone()
            if item:
                found_item = dict(item)
                found_item['tracking'] = item['tracking_number']
                found_item['name'] = item['item_name']

        elif request.method == 'POST':
            search_term = request.form.get('search_term', '').strip()
            if search_term:
                # Search by Name or Tracking
                st = f"%{search_term}%"
                rows = conn.execute('''
                    SELECT * FROM packages 
                    WHERE (item_name LIKE ? OR tracking_number LIKE ?)
                    AND status NOT IN ('refunded', 'return_pending')
                ''', (st, st)).fetchall()
                
                if len(rows) == 1:
                    item = rows[0]
                    found_item = dict(item)
                    found_item['tracking'] = item['tracking_number']
                    found_item['name'] = item['item_name']
                elif len(rows) > 1:
                    candidates = []
                    for r in rows:
                        c = dict(r)
                        c['tracking'] = r['tracking_number']
                        c['name'] = r['item_name']
                        candidates.append(c)
                    message = f"Found {len(candidates)} matches. Please select one."
                else:
                    message = "Item not found."

        # Recent Items
        recents = conn.execute('''
            SELECT * FROM packages 
            WHERE status NOT IN ('return_pending', 'refunded') 
            ORDER BY date_expected DESC LIMIT 50
        ''').fetchall()
        
        recent_items = []
        for r in recents:
            row = dict(r)
            row['tracking'] = row['tracking_number']
            row['name'] = row['item_name']
            row['date'] = row['date_expected']
            recent_items.append(row)
            
    finally:
        conn.close()

    return render_template('return_mode.html', item=found_item, message=message, candidates=candidates, recent_items=recent_items)

@main_bp.route('/process_return', methods=['POST'])
def process_return():
    original_tracking = request.form.get('original_tracking')
    return_tracking = request.form.get('return_tracking')
    reason = request.form.get('reason')
    
    if original_tracking:
        conn = get_db_connection()
        try:
             conn.execute('''
                 UPDATE packages SET 
                 status='return_pending'
                 WHERE tracking_number=?
             ''', (original_tracking,))
             # Store reason? (packages doesn't have reason column, HistoryLog does!)
             # We should log this action.
             
             # Need package ID
             res = conn.execute("SELECT id FROM packages WHERE tracking_number=?", (original_tracking,)).fetchone()
             if res:
                 pid = res['id']
                 conn.execute('''
                     INSERT INTO history (package_id, user_id, action, details)
                     VALUES (?, (SELECT id FROM users WHERE username=?), ?, ?)
                 ''', (pid, session.get('user'), 'return_initiated', f"Reason: {reason}, New Track: {return_tracking}"))
                 
             conn.commit()
             flash("Return Initiated")
        except Exception as e:
            flash(f"Error: {e}")
        finally:
            conn.close()
            
    return redirect(url_for('main.return_mode'))

@main_bp.route('/mark_refunded/<tracking>', methods=['POST'])
def mark_refunded(tracking):
    conn = get_db_connection()
    try:
        today = datetime.datetime.now().strftime('%Y-%m-%d')
        conn.execute("UPDATE packages SET status='refunded', refund_date=? WHERE tracking_number=?", (today, tracking))
        
        # Log Logic?
        res = conn.execute("SELECT id FROM packages WHERE tracking_number=?", (tracking,)).fetchone()
        if res:
             conn.execute("INSERT INTO history (package_id, user_id, action) VALUES (?, (SELECT id FROM users WHERE username=?), 'refunded')", 
                          (res['id'], session.get('user')))
        conn.commit()
    except Exception as e:
        print(e)
    finally:
         conn.close()
    return redirect(url_for('main.open_returns_view'))

@main_bp.route('/open_returns')
def open_returns_view():
    conn = get_db_connection()
    try:
        # We need join to get recent return reason?
        # Or did we add columns to Package? We didn't. 
        # History table has the reason.
        # Complex query: Get package, and Latest 'return_initiated' history log.
        # Simplification: Just show package name/tracking for now.
        
        rows = conn.execute("SELECT * FROM packages WHERE status='return_pending'").fetchall()
        items = []
        for r in rows:
            items.append({
                "name": r['item_name'], 
                "tracking": r['tracking_number'],
                "reason": "Check History", # Or fetch history
                "return_track": ""
            })
        return render_template('open_returns.html', items=items)
    finally:
        conn.close()

@main_bp.route('/refunded_log')
def refunded_view():
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM packages WHERE status='refunded' ORDER BY refund_date DESC").fetchall()
    items = [{"name": r['item_name'], "tracking": r['tracking_number'], "date": r['refund_date']} for r in rows]
    conn.close()
    return render_template('refunded.html', items=items)

@main_bp.route('/past_due')
def past_due_view():
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM packages WHERE status='past_due' AND date_scanned IS NULL").fetchall()
    items = [{"name": r['item_name'], "tracking": r['tracking_number'], "date": r['date_expected']} for r in rows]
    conn.close()
    return render_template('past_due.html', items=items)

@main_bp.route('/expected')
def expected_view():
    conn = get_db_connection()
    today = datetime.date.today().strftime('%Y-%m-%d')
    # Filter for TODAY only, matching dashboard
    rows = conn.execute("SELECT * FROM packages WHERE status IN ('expected','on_time') AND date_scanned IS NULL AND date_expected = ?", (today,)).fetchall()
    items = [{"name": r['item_name'], "tracking": r['tracking_number']} for r in rows]
    conn.close()
    return render_template('expected.html', items=items)

@main_bp.route('/search')
def global_search():
    return redirect(url_for('main.history_view'))
