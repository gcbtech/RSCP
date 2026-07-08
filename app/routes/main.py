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
from app.utils.permissions import has_permission

# DB Services
from app.services.db import get_db_connection, DB_PATH, get_request_db
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
    
    config = load_config()
    inv_enabled = config.get('INVENTORY_ENABLED', False)
    pos_enabled = config.get('POS_ENABLED', False)
    timeclock_enabled = config.get('TIMECLOCK_ENABLED', False)
    
    return render_template('gateway.html', inventory_enabled=inv_enabled, pos_enabled=pos_enabled, timeclock_enabled=timeclock_enabled)

@main_bp.route('/receiving')
@login_required
def receiving_dashboard():
    # Check user permission
    if not has_permission(current_user, 'receiving.view'):
        flash("You don't have access to the Receiving module.")
        return redirect(url_for('main.index'))
    
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
        except Exception as e:
            logger.error(f"Failed to write config file during setup: {e}")
        
        # Create Admin user with custom username
        admin_user = request.form.get('admin_user', 'Admin').strip()
        create_user(admin_user, config_data["ADMIN_HASH"], is_admin=True)
            
        return redirect(url_for('auth.login'))
        
    return render_template('setup.html')

@main_bp.route('/settings')
@login_required
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
@login_required
def scan_page_legacy():
    logger.info(f"[Scan] scan_page_legacy called. Method: {request.method}, Session: {dict(session)}")
    if request.method == 'POST':
        tracking = request.form.get('tracking_input')
        user_input = request.form.get('user_input')
        
        # Call Logic (Simulating API logic but returning template)
        # We can repurpose logic or just call handle_scan if we mocked request? 
        # Better: Duplicate minimal logic or extract to service.
        # Extracting logic is best, but for now, inline.
        
        conn = get_request_db()
        msg = "Scan Failed"
        color = ""
        items = []
        is_priority = False
        try:

            # 1. Sanitize but KEEP the string intact for searching
            # sanitize_for_csv is for WRITE safety, here we need READ accuracy.
            raw_input = (tracking or "").strip()
            tracking_clean = raw_input.replace('=', '').replace('"', '').replace("'", "")
            

            
            # 2. Try Exact Match (Case Insensitive)
            rows = conn.execute("SELECT * FROM packages WHERE tracking_number = ? COLLATE NOCASE", (tracking_clean,)).fetchall()

            
            # 3. Fallback: Substring Search (for FedEx/Long barcodes)
            if not rows and len(tracking_clean) > 8:
                # Find valid tracking numbers that are substrings of the scan OR vice versa
                # Reverse check: Is the DB tracking inside the Scan?
                candidates = conn.execute("""
                    SELECT * FROM packages 
                    WHERE length(tracking_number) > 5 
                    AND instr(UPPER(?), UPPER(tracking_number)) > 0
                """, (tracking_clean,)).fetchall()
                
                best_match = None
                for c in candidates:
                    t = c['tracking_number']
                    # Pick the longest match that is at the END of the scan (common barcode format)
                    if len(t) > 6:
                        # Check strict suffix first
                        if tracking_clean.upper().endswith(t.upper()):
                             if best_match is None or len(t) > len(best_match['tracking_number']):
                                 best_match = c
                
                if best_match:
                    # Found a match from the long string!
                    real_tracking = best_match['tracking_number']
                    # Fetch ALL items for this found tracking number
                    rows = conn.execute("SELECT * FROM packages WHERE tracking_number = ?", (real_tracking,)).fetchall()
                    # Update tracking variable for logging references
                    tracking = real_tracking
            
            # Logic: Receive
            if not rows:
                 # Inventory Lookup (Legacy)
                 inv_item = None
                 try:
                     conf = load_config()
                     if conf and conf.get('INVENTORY_ENABLED'):
                         from app.routes.inventory import get_inventory_item
                         inv_item = get_inventory_item(tracking, conn=conn)
                 except Exception as e:
                     logger.warning(f"Inventory SKU lookup failed during scan: {e}")
                 
                 if inv_item:
                     return render_template('scan.html', 
                         message=f"📦 {inv_item['name']}", color="#6f42c1", mode='inventory',
                         inventory_item={
                             'id': inv_item['id'],
                             'sku': inv_item['sku'],
                             'name': inv_item['name'],
                             'quantity': inv_item['quantity'],
                             'image_url': inv_item['image_url'],
                             'sell_price': inv_item['sell_price'],
                             'location': ' / '.join(filter(None, [inv_item['location_area'], inv_item['location_aisle'], inv_item['location_shelf'], inv_item['location_bin']]))
                         })
                 else:
                     msg = "Unknown Item (Not in Manifest)"
                     color = "#ff6961" # Red
            
            else:
                # Check status BEFORE processing (to detect duplicates)
                all_received = all(r['date_scanned'] for r in rows)
                already_in_history = check_history(tracking, conn=conn)
                
                if all_received and already_in_history:
                     msg = f"Duplicate: {len(rows)} Items"
                     color = "#ffd700" # Gold
                else:
                     # Log Receipt (Updates ALL items with this tracking)
                     # We use the first item's name for the log summary, but specific items are updated in DB
                     log_receipt(tracking, rows[0]['item_name'], str(rows[0]['quantity']), current_user.username, conn=conn)
                     
                     # Check Priority (If ANY item is priority)
                     is_priority = any((r['priority'] for r in rows if 'priority' in r.keys() and r['priority']))
                     
                     if is_priority:
                         msg = "PRIORITY RECEIVED"
                         color = "#C77DFF" 
                     else:
                         msg = f"Received: {len(rows)} Items"
                         color = "#77dd77" # Green
                
                # Build Item List
                items = []
                for r in rows:
                    items.append({
                        "name": r['item_name'], 
                        "image": r['image_url'], 
                        "subtext": tracking,
                        "qty": r['quantity'] or 1,
                        "image_url": r['image_url'],
                        "asin": r['asin'] if 'asin' in r.keys() else '',
                        "source_url": r['source_url'] if 'source_url' in r.keys() else ''
                    })
            
        except Exception as e:
            msg = f"Error: {e}"
            color = "#ff6961"
            items = []
        finally:
            pass
        
        # Check if inventory is enabled for Add to Inventory button
        inventory_match = None
        config = load_config()
        inv_enabled = config.get('INVENTORY_ENABLED', False) if config else False
        
        # V1.16.1: Check if received package matches existing inventory item
        if inv_enabled and color == "#77dd77" and items:
            try:
                pkg_asin = items[0].get('asin', '') or ''
                pkg_name = items[0].get('name', '') or ''
                
                # 1. Try SKU Match (if package has one from Manifest or Mapping)
                # We need to fetch the SKU from the package row we just fetched if it exists
                pkg_sku = None
                if rows and 'sku' in rows[0].keys():
                    pkg_sku = rows[0]['sku']
                
                # If no SKU on package, check product_mappings (Real-time check)
                if not pkg_sku:
                    mapping = conn.execute("SELECT inventory_sku FROM product_mappings WHERE package_name = ?", (pkg_name,)).fetchone()
                    if mapping: pkg_sku = mapping['inventory_sku']

                if pkg_sku:
                    match = conn.execute("SELECT id, name, sku, quantity, image_url FROM inventory_items WHERE sku = ?", (pkg_sku,)).fetchone()
                    if match:
                        inventory_match = dict(match)

                # 2. Try ASIN match (exact)
                if not inventory_match and pkg_asin and pkg_asin.strip():
                    match = conn.execute(
                        "SELECT id, name, sku, quantity, image_url FROM inventory_items WHERE asin = ?",
                        (pkg_asin.strip(),)
                    ).fetchone()
                    if match:
                        inventory_match = dict(match)
                
                # 3. Try name match (fuzzy - case insensitive contains)
                if not inventory_match and pkg_name and pkg_name.strip():
                    # Use first 30 chars for matching (handles truncated names)
                    search_prefix = pkg_name[:30].lower().rstrip('.').rstrip()
                    match = conn.execute(
                        """SELECT id, name, sku, quantity, image_url FROM inventory_items 
                           WHERE LOWER(name) LIKE ? 
                           LIMIT 1""",
                        (f'{search_prefix}%',)
                    ).fetchone()
                    if match:
                        inventory_match = dict(match)
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
@login_required
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
@login_required
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
@login_required
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
@login_required
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
@login_required
def refunded_view():
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM packages WHERE status='refunded' ORDER BY refund_date DESC").fetchall()
    items = [{"name": r['item_name'], "tracking": r['tracking_number'], "date": r['refund_date']} for r in rows]
    conn.close()
    return render_template('refunded.html', items=items)

@main_bp.route('/past_due')
@login_required
def past_due_view():
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM packages WHERE status='past_due' AND date_scanned IS NULL").fetchall()
    items = [{"name": r['item_name'], "tracking": r['tracking_number'], "date": r['date_expected']} for r in rows]
    conn.close()
    return render_template('past_due.html', items=items)

@main_bp.route('/expected')
@login_required
def expected_view():
    from app.utils.helpers import guess_shipper
    conn = get_db_connection()
    today = datetime.date.today().strftime('%Y-%m-%d')
    # Filter for TODAY only, matching dashboard
    rows = conn.execute("SELECT * FROM packages WHERE status IN ('expected','on_time') AND date_scanned IS NULL AND date_expected = ?", (today,)).fetchall()
    items = [{"name": r['item_name'], "tracking": r['tracking_number'], "shipper": guess_shipper(r['tracking_number'])} for r in rows]
    conn.close()
    
    # Group items by shipper
    grouped_items = {}
    for item in items:
        sh = item['shipper']
        if sh not in grouped_items:
            grouped_items[sh] = []
        grouped_items[sh].append(item)
        
    return render_template('expected.html', items=items, grouped_items=grouped_items)

@main_bp.route('/search')
@main_bp.route('/receiving/link_item', methods=['POST'])
@login_required
def link_item():
    data = request.json
    tracking = data.get('tracking')
    inventory_sku = data.get('sku')
    save_mapping = data.get('save_mapping', False)
    package_name = data.get('package_name')

    if not tracking or not inventory_sku:
        return {"status": "error", "message": "Missing Data"}

    conn = get_db_connection()
    try:
        # 1. Update Package SKU
        conn.execute("UPDATE packages SET sku = ? WHERE tracking_number = ?", (inventory_sku, tracking))
        
        # 2. Update Product Mapping (if requested)
        if save_mapping and package_name:
            # Check if mapping exists
            existing = conn.execute("SELECT id FROM product_mappings WHERE package_name = ?", (package_name,)).fetchone()
            if existing:
                conn.execute("UPDATE product_mappings SET inventory_sku = ? WHERE id = ?", (inventory_sku, existing['id']))
            else:
                conn.execute("INSERT INTO product_mappings (package_name, inventory_sku) VALUES (?, ?)", (package_name, inventory_sku))
        
        # 3. Add to Inventory (if package was already received)
        # Calculate total quantity of RECEIVED packages with this tracking that were just linked
        # We assume they weren't added before because the user is manually linking them now.
        received_rows = conn.execute("""
            SELECT quantity FROM packages 
            WHERE tracking_number = ? AND date_scanned IS NOT NULL
        """, (tracking,)).fetchall()
        
        total_qty = sum([r['quantity'] for r in received_rows])
        logger.info(f"Link Item: Tracking={tracking}, SKU={inventory_sku}, ReceivedRows={len(received_rows)}, TotalQty={total_qty}")
        
        if total_qty > 0:
            inv_item = conn.execute("SELECT id, name FROM inventory_items WHERE sku = ?", (inventory_sku,)).fetchone()
            if inv_item:
                conn.execute("UPDATE inventory_items SET quantity = quantity + ? WHERE id = ?", (total_qty, inv_item['id']))
                logger.info(f"Linked & Added Stock: ID={inv_item['id']} ({inv_item['name']}) += {total_qty}")
            else:
                logger.warning(f"Link Item: Inventory Item NOT FOUND for SKU={inventory_sku}")
        else:
             logger.warning(f"Link Item: No received packages found (or qty=0) for {tracking}. date_scanned might be NULL.")

        conn.commit()
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Link Item Error: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()
