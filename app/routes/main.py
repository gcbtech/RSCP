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
from app.services.auth import BASE_DIR, create_first_admin, validate_password
from app.services.data_manager import (
    get_dashboard_stats, load_config, save_config, get_scan_count,
)
from app.services import receiving
from app.services.logger import log_error

main_bp = Blueprint('main', __name__)
# The receiving pages (scan, history, returns, expected...) need receiving.view.
# The gateway and first-run setup handle their own access; settings and issue
# reports only need a login.
from app.utils.permissions import protect_blueprint
protect_blueprint(
    main_bp, 'receiving.view',
    module_name='Receiving',
    public={'main.index', 'main.setup_wizard'},
    login_only={'main.settings_page', 'main.report_issue'},
)
from app.paths import CONFIG_FILE

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
def receiving_dashboard():
    stats = get_dashboard_stats()
    scans = get_scan_count(14)

    # When package data last came in (upload or manual add; old rows may say email)
    from app.services.package_imports import last_import
    last = last_import(get_request_db())
    refresh_info = []
    if not last:
        refresh_info.append({"label": "Last import", "age": "None yet", "status": "grey"})
    else:
        age = round(max(last['age_hours'] or 0, 0), 1)
        refresh_info.append({"label": f"Last import ({last['source']})", "age": age,
                             "status": "green" if age < 24 else "red"})
    
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
        admin_user = (request.form.get('admin_user') or 'Admin').strip()
        admin_pass = request.form.get('admin_pass') or ''
        ok, message = validate_password(admin_pass)
        if not ok:
            return render_template('setup.html', error=message)

        if not create_first_admin(admin_user, generate_password_hash(admin_pass)):
            return redirect(url_for('auth.login'))  # someone else finished setup first

        # Merge into the existing config. The installer already generated
        # SECRET_KEY and the running workers are using it; replacing it here
        # would silently invalidate sessions after the next restart.
        updates = {"ORG_NAME": request.form.get('org_name', ''), "AUTO_TRIM": False}
        if not load_config(force_reload=True).get('SECRET_KEY'):
            updates["SECRET_KEY"] = secrets.token_hex(32)
        save_config(updates)

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
    
    log_error(message, level="USER_REPORT", source="Frontend", user_id=current_user.username, trace=details)
    
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
    logger.debug(f"[Scan] scan_page_legacy called. Method: {request.method}")
    if request.method == 'POST':
        config = load_config()
        inv_enabled = config.get('INVENTORY_ENABLED', False) if config else False

        msg, color, items, is_priority, inventory_match = "Scan Failed", "", [], False, None
        try:
            result = receiving.receive_scan(get_request_db(), request.form.get('tracking_input'),
                                            current_user.username, inventory_enabled=inv_enabled,
                                            conf=config)
            if result.kind == 'inventory':
                return render_template('scan.html',
                    message=f"📦 {result.inventory_item['name']}", color="#6f42c1", mode='inventory',
                    inventory_item=result.inventory_item)
            if result.kind == 'unknown':
                msg, color = "Unknown Item (Not in Manifest)", "#ff6961"  # Red
            elif result.kind == 'duplicate':
                msg, color = f"Duplicate: {len(result.rows)} Items", "#ffd700"  # Gold
            elif result.kind == 'priority':
                msg, color, is_priority = "PRIORITY RECEIVED", "#C77DFF", True
            else:
                msg, color = f"Received: {len(result.rows)} Items", "#77dd77"  # Green
            items = result.items
            inventory_match = result.inventory_match
        except Exception as e:
            msg = f"Error: {e}"
            color = "#ff6961"
            items = []

        return render_template('scan.html', message=msg, color=color, items=items, mode='receive',
                               priority_alert=is_priority, inventory_enabled=inv_enabled,
                               inventory_match=inventory_match)

    return scan_page('receive')

@main_bp.route('/scan/<mode>', methods=['GET'])
def scan_page(mode):
    logger.info(f"[Scan] scan_page called. Mode: {mode}, User: {current_user.username}")
    return render_template('scan.html', mode=mode)

@main_bp.route('/api/dashboard_stats')
def api_dashboard_stats():
    from app.services.data_manager import get_dashboard_stats, get_scan_count, get_analytics_stats
    
    return {
        "stats": get_dashboard_stats(),
        "scans": get_scan_count(14),
        "graph": get_analytics_stats(14)
    }

@main_bp.route('/history')
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
            receiving.initiate_return(conn, original_tracking, return_tracking, reason, current_user.username)
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
        receiving.mark_refunded(conn, tracking, current_user.username)
    except Exception as e:
        logger.error(f"mark_refunded failed for {tracking}: {e}")
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
        receiving.link_package_to_item(conn, tracking, inventory_sku, current_user.username,
                                       save_mapping=save_mapping, package_name=package_name)
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Link Item Error: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()
