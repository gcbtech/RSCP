"""
Inventory Module Blueprint
Handles inventory item management, quantity tracking, and ASIN matching.
"""
import os
import logging
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, jsonify

from app.services.db import get_db_connection
from app.services.data_manager import load_config, save_config, BASE_DIR
from werkzeug.utils import secure_filename
import time

inventory_bp = Blueprint('inventory', __name__, url_prefix='/inventory')
logger = logging.getLogger(__name__)

# Predefined category codes
CATEGORY_CODES = {
    'PRI': 'Primary',
    'SEC': 'Secondary',
    'ASC': 'Accessories',
    'ATO': 'Automatic',
}


def is_inventory_enabled():
    """Check if inventory module is enabled in config."""
    config = load_config()
    return config.get('INVENTORY_ENABLED', False) if config else False


def generate_sku(category='ATO'):
    """Generate a unique SKU in format RSCP-XXX-XXXX-XXXX."""
    category = category.upper()[:3].ljust(3, 'X')  # Ensure 3 chars
    
    conn = get_db_connection()
    try:
        # Find the highest SKU number for this category
        result = conn.execute('''
            SELECT sku FROM inventory_items 
            WHERE sku LIKE ? 
            ORDER BY sku DESC LIMIT 1
        ''', (f'RSCP-{category}-%',)).fetchone()
        
        if result:
            # Parse the number from existing SKU
            try:
                parts = result['sku'].split('-')
                if len(parts) == 4:
                    # Format: RSCP-XXX-XXXX-XXXX
                    num_str = parts[2] + parts[3]  # Combine last two parts
                    next_num = int(num_str) + 1
                else:
                    next_num = 1
            except:
                next_num = 1
        else:
            next_num = 1
        
        # Format as XXXX-XXXX (8 digits split)
        num_str = str(next_num).zfill(8)
        sku = f"RSCP-{category}-{num_str[:4]}-{num_str[4:]}"
        
        return sku
    finally:
        conn.close()


def validate_location(area, aisle, shelf, bin_loc):
    """Validate that at least one location field is provided."""
    return any([area, aisle, shelf, bin_loc])


# --- ROUTES ---

@inventory_bp.before_request
def check_inventory_enabled():
    """Redirect if inventory module is disabled."""
    # Allow API endpoints to return proper JSON errors
    if request.endpoint and 'api' in request.endpoint:
        return
    
    if not is_inventory_enabled():
        flash("Inventory module is not enabled.")
        return redirect(url_for('main.index'))


def get_inventory_stats():
    """Helper to calculate inventory statistics."""
    conn = get_db_connection()
    try:
        stats = {}
        # Low Stock Threshold
        conf = load_config()
        low_threshold = int(conf.get('LOW_STOCK_THRESHOLD', 5))
        
        stats['total_items'] = conn.execute('SELECT COUNT(*) FROM inventory_items').fetchone()[0]
        stats['total_quantity'] = conn.execute('SELECT COALESCE(SUM(quantity), 0) FROM inventory_items').fetchone()[0]
        stats['out_of_stock'] = conn.execute('SELECT COUNT(*) FROM inventory_items WHERE quantity = 0').fetchone()[0]
        stats['low_stock'] = conn.execute('''
            SELECT COUNT(*) FROM inventory_items 
            WHERE quantity > 0 
            AND (
                (COALESCE(alert_threshold, 0) > 0 AND quantity <= alert_threshold)
                OR 
                (COALESCE(alert_threshold, 0) = 0 AND ? > 0 AND quantity <= ?)
            )
        ''', (low_threshold, low_threshold)).fetchone()[0]
        
        # Profit Calculation
        financials = conn.execute('''
            SELECT 
                SUM(quantity * sell_price) as revenue,
                SUM(quantity * buy_price) as cost
            FROM inventory_items
            WHERE quantity > 0 AND buy_price > 0 AND sell_price > 0
        ''').fetchone()
        
        revenue = financials['revenue'] or 0
        cost = financials['cost'] or 0
        potential_profit = revenue - cost
        margin_percent = 0
        if revenue > 0:
            margin_percent = round((potential_profit / revenue) * 100, 1)
        
        stats['margin_percent'] = margin_percent
        stats['potential_profit'] = potential_profit

        # Last Audit Date
        last_audit = conn.execute("SELECT end_time FROM audit_sessions WHERE status='completed' ORDER BY end_time DESC LIMIT 1").fetchone()
        last_audit_str = "Never"
        if last_audit and last_audit['end_time']:
            audit_dt = datetime.strptime(str(last_audit['end_time']).split('.')[0], '%Y-%m-%d %H:%M:%S')
            delta = datetime.utcnow() - audit_dt
            
            if delta.days >= 1:
                last_audit_str = f"{delta.days} days"
            else:
                seconds = delta.seconds
                if seconds < 3600:
                    mins = int(seconds / 60)
                    last_audit_str = f"{mins} mins"
                else:
                    hours = int(seconds / 3600)
                    last_audit_str = f"{hours} hours"
        
        stats['last_audit'] = last_audit_str
        return stats
    finally:
        conn.close()


@inventory_bp.route('/')
def overview():
    """Inventory analytics overview page."""
    stats = get_inventory_stats()
    
    conn = get_db_connection()
    try:
        # Top movers (most sold/consumed in last 30 days)
        import datetime
        thirty_days_ago = (datetime.date.today() - datetime.timedelta(days=30)).strftime('%Y-%m-%d')
        top_movers = conn.execute('''
            SELECT i.id, i.name, i.sku, ABS(SUM(t.quantity_change)) as sold
            FROM inventory_transactions t
            JOIN inventory_items i ON t.inventory_item_id = i.id
            WHERE t.quantity_change < 0 
              AND t.reason IN ('Sold/Consumed', 'Damaged')
              AND t.created_at > ?
            GROUP BY i.id
            ORDER BY sold DESC
            LIMIT 5
        ''', (thirty_days_ago,)).fetchall()
        
        # Items needing attention (OOS or low stock)
        conf = load_config()
        low_threshold = int(conf.get('LOW_STOCK_THRESHOLD', 5))
        
        attention_items = conn.execute('''
            SELECT id, name, quantity 
            FROM inventory_items 
            WHERE quantity = 0
            OR (
                quantity > 0 AND (
                    (COALESCE(alert_threshold, 0) > 0 AND quantity <= alert_threshold)
                    OR 
                    (COALESCE(alert_threshold, 0) = 0 AND ? > 0 AND quantity <= ?)
                )
            )
            ORDER BY quantity ASC, name ASC
            LIMIT 10
        ''', (low_threshold, low_threshold)).fetchall()
        
        # Sales trend (daily for last 30 days) - Optimized: Single query with GROUP BY
        thirty_days_ago = (datetime.date.today() - datetime.timedelta(days=30)).strftime('%Y-%m-%d')
        
        # Single query instead of 31 separate queries
        sales_data = conn.execute('''
            SELECT date(created_at) as day, COALESCE(SUM(ABS(quantity_change)), 0) as cnt
            FROM inventory_transactions 
            WHERE quantity_change < 0 
              AND reason = 'Sold/Consumed'
              AND date(created_at) >= ?
            GROUP BY date(created_at)
        ''', (thirty_days_ago,)).fetchall()
        
        # Convert to dict for O(1) lookup
        sales_dict = {row['day']: row['cnt'] for row in sales_data}
        
        # Build sales_trend with all 31 days (fills in zeros for missing days)
        sales_trend = []
        for i in range(30, -1, -1):
            day = (datetime.date.today() - datetime.timedelta(days=i)).strftime('%Y-%m-%d')
            count = sales_dict.get(day, 0)
            sales_trend.append({'date': day[-5:], 'count': count})  # MM-DD format
        
        return render_template('inventory/overview.html',
                               stats=stats,
                               top_movers=top_movers,
                               attention_items=attention_items,
                               sales_trend=sales_trend)
    finally:
        conn.close()


@inventory_bp.route('/api/overview')
def overview_api():
    """API to get current stats for auto-refresh."""
    return jsonify(get_inventory_stats())


@inventory_bp.route('/items')
def list_items():
    """List all inventory items."""
    conn = get_db_connection()
    try:
        items = conn.execute('''
            SELECT * FROM inventory_items 
            ORDER BY name ASC
        ''').fetchall()
        
        if request.args.get('partial'):
            return render_template('inventory/_list_rows.html', items=items)
            
        return render_template('inventory/list.html', items=items)
    finally:
        conn.close()


@inventory_bp.route('/add', methods=['GET', 'POST'])
def add_item():
    """Add a new inventory item."""
    if request.method == 'POST':
        # Get form data
        sku = request.form.get('sku', '').strip()
        category = request.form.get('category', 'ATO').strip()
        name = request.form.get('name', '').strip()
        quantity = int(request.form.get('quantity', 0))
        
        # Location fields
        location_area = request.form.get('location_area', '').strip()
        location_aisle = request.form.get('location_aisle', '').strip()
        location_shelf = request.form.get('location_shelf', '').strip()
        location_bin = request.form.get('location_bin', '').strip()
        
        # Optional fields
        asin = request.form.get('asin', '').strip() or None
        buy_price = request.form.get('buy_price', '').strip()
        sell_price = request.form.get('sell_price', '').strip()
        supplier = request.form.get('supplier', '').strip() or None
        first_stock_date = request.form.get('first_stock_date', '').strip() or None
        resupply_interval = request.form.get('resupply_interval', '').strip()
        
        # Validation
        if not name:
            flash("Name is required.")
            return redirect(url_for('inventory.add_item'))
        
        if not validate_location(location_area, location_aisle, location_shelf, location_bin):
            flash("At least one location field is required.")
            return redirect(url_for('inventory.add_item'))
        
        # Auto-generate SKU if not provided
        if not sku:
            sku = generate_sku(category)
        
        # Convert numeric fields
        buy_price = float(buy_price) if buy_price else None
        sell_price = float(sell_price) if sell_price else None
        resupply_interval = int(resupply_interval) if resupply_interval else None
        
        conn = get_db_connection()
        try:
            # Get image_url from form if provided (from import)
            # Get image_url from form if provided (from import)
            image_url = request.form.get('image_url', '').strip() or None
            
            # File Upload Handling
            if 'image' in request.files:
                file = request.files['image']
                if file and file.filename:
                    filename = secure_filename(file.filename)
                    ext = os.path.splitext(filename)[1]
                    unique_name = f"{secure_filename(sku)}_{int(time.time())}{ext}"
                    
                    upload_folder = os.path.join(BASE_DIR, 'static', 'uploads', 'inventory')
                    if not os.path.exists(upload_folder):
                        os.makedirs(upload_folder)
                        
                    file.save(os.path.join(upload_folder, unique_name))
                    image_url = url_for('static', filename=f'uploads/inventory/{unique_name}')

            source_url = request.form.get('source_url', '').strip() or None

            # Auto-populate Amazon details if ASIN is present
            if asin:
                if not image_url:
                    image_url = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SX200_.jpg"
                if not source_url:
                    source_url = f"https://www.amazon.com/dp/{asin}"
            alert_enabled = request.form.get('alert_enabled') == 'on'
            alert_threshold = int(request.form.get('alert_threshold', 0) or 0)
            
            conn.execute('''
                INSERT INTO inventory_items 
                (sku, name, quantity, location_area, location_aisle, location_shelf, location_bin,
                 asin, image_url, source_url, buy_price, sell_price, supplier, first_stock_date, 
                 resupply_interval, alert_enabled, alert_threshold)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (sku, name, quantity, location_area or None, location_aisle or None, 
                  location_shelf or None, location_bin or None, asin, image_url, source_url, 
                  buy_price, sell_price, supplier, first_stock_date, resupply_interval,
                  1 if alert_enabled else 0, alert_threshold))
            conn.commit()
            
            # Log the initial stock as a transaction
            item_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            if quantity > 0:
                conn.execute('''
                    INSERT INTO inventory_transactions 
                    (inventory_item_id, quantity_change, reason, user_id)
                    VALUES (?, ?, ?, ?)
                ''', (item_id, quantity, 'Initial Stock', session.get('user')))
                conn.commit()
            
            flash(f"Item added: {sku}")
            return redirect(url_for('inventory.list_items'))
            
        except Exception as e:
            logger.error(f"Error adding inventory item: {e}")
            flash(f"Error adding item: {e}")
        finally:
            conn.close()
    
    # GET - Show form with optional prefill from query params
    prefill = {
        'asin': request.args.get('asin', ''),
        'tracking': request.args.get('tracking', ''),
        'quantity': request.args.get('qty', '1'),
        'image_url': request.args.get('image_url', ''),
        'name': request.args.get('name', ''),
        'source_url': request.args.get('source_url', ''),
    }
    
    return render_template('inventory/add.html', 
                           prefill=prefill, 
                           categories=CATEGORY_CODES)


@inventory_bp.route('/edit/<int:item_id>', methods=['GET', 'POST'])
def edit_item(item_id):
    """Edit an existing inventory item."""
    conn = get_db_connection()
    
    if request.method == 'POST':
        try:
            sku = request.form.get('sku', '').strip()
            name = request.form.get('name', '').strip()
            location_area = request.form.get('location_area', '').strip()
            location_aisle = request.form.get('location_aisle', '').strip()
            location_shelf = request.form.get('location_shelf', '').strip()
            location_bin = request.form.get('location_bin', '').strip()
            
            asin = request.form.get('asin', '').strip() or None
            buy_price = request.form.get('buy_price', '').strip()
            sell_price = request.form.get('sell_price', '').strip()
            supplier = request.form.get('supplier', '').strip() or None
            first_stock_date = request.form.get('first_stock_date', '').strip() or None
            resupply_interval = request.form.get('resupply_interval', '').strip()
            
            if not sku:
                flash("SKU is required.")
                return redirect(url_for('inventory.edit_item', item_id=item_id))
            
            if not name:
                flash("Name is required.")
                return redirect(url_for('inventory.edit_item', item_id=item_id))
            
            if not validate_location(location_area, location_aisle, location_shelf, location_bin):
                flash("At least one location field is required.")
                return redirect(url_for('inventory.edit_item', item_id=item_id))
            
            # Check if new SKU conflicts with another item
            existing = conn.execute('SELECT id FROM inventory_items WHERE sku = ? AND id != ?', 
                                   (sku, item_id)).fetchone()
            if existing:
                flash("SKU already exists for another item.")
                return redirect(url_for('inventory.edit_item', item_id=item_id))
            
            buy_price = float(buy_price) if buy_price else None
            sell_price = float(sell_price) if sell_price else None
            resupply_interval = int(resupply_interval) if resupply_interval else None
            source_url = request.form.get('source_url', '').strip() or None
            alert_enabled = request.form.get('alert_enabled') == 'on'
            alert_threshold = int(request.form.get('alert_threshold', 0) or 0)
            
            # Preserve image unless replaced
            current_img = conn.execute('SELECT image_url FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
            image_url = current_img['image_url'] if current_img else None
            
            if 'image' in request.files:
                file = request.files['image']
                if file and file.filename:
                    filename = secure_filename(file.filename)
                    ext = os.path.splitext(filename)[1]
                    unique_name = f"{secure_filename(sku)}_{int(time.time())}{ext}"
                    
                    upload_folder = os.path.join(BASE_DIR, 'static', 'uploads', 'inventory')
                    if not os.path.exists(upload_folder):
                        os.makedirs(upload_folder)
                        
                    file.save(os.path.join(upload_folder, unique_name))
                    image_url = url_for('static', filename=f'uploads/inventory/{unique_name}')

            # Auto-populate Amazon details if ASIN is present and fields are empty
            if asin:
                if not image_url:
                    image_url = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SX200_.jpg"
                if not source_url:
                    source_url = f"https://www.amazon.com/dp/{asin}"

            conn.execute('''
                UPDATE inventory_items SET
                    sku = ?, name = ?, location_area = ?, location_aisle = ?, 
                    location_shelf = ?, location_bin = ?, asin = ?,
                    buy_price = ?, sell_price = ?, supplier = ?,
                    first_stock_date = ?, resupply_interval = ?, source_url = ?,
                    image_url = ?, alert_enabled = ?, alert_threshold = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (sku, name, location_area or None, location_aisle or None,
                  location_shelf or None, location_bin or None, asin,
                  buy_price, sell_price, supplier, first_stock_date,
                  resupply_interval, source_url, image_url, 1 if alert_enabled else 0, 
                  alert_threshold, item_id))
            conn.commit()
            
            flash("Item updated.")
            return redirect(url_for('inventory.list_items'))
            
        except Exception as e:
            logger.error(f"Error updating inventory item: {e}")
            flash(f"Error updating item: {e}")
        finally:
            conn.close()
    
    # GET - Load item for editing
    try:
        item = conn.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
        if not item:
            flash("Item not found.")
            return redirect(url_for('inventory.list_items'))
        
        transactions = conn.execute('''
            SELECT * FROM inventory_transactions 
            WHERE inventory_item_id = ? 
            ORDER BY created_at DESC LIMIT 50
        ''', (item_id,)).fetchall()
        
        return render_template('inventory/add.html', 
                               item=item, 
                               transactions=transactions,
                               edit_mode=True,
                               categories=CATEGORY_CODES)
    finally:
        conn.close()


@inventory_bp.route('/transaction/delete/<int:tx_id>', methods=['POST'])
def delete_transaction(tx_id):
    """Delete a transaction and revert the stock change."""
    conn = get_db_connection()
    try:
        tx = conn.execute('SELECT * FROM inventory_transactions WHERE id = ?', (tx_id,)).fetchone()
        if not tx:
            flash("Transaction not found.")
            return redirect(request.referrer or url_for('inventory.list_items'))
        
        # Revert quantity change (subtract the change)
        # e.g. If change was -5 (Sale), we do - (-5) = +5
        change = tx['quantity_change']
        item_id = tx['inventory_item_id']
        
        conn.execute('UPDATE inventory_items SET quantity = quantity - ? WHERE id = ?', (change, item_id))
        conn.execute('DELETE FROM inventory_transactions WHERE id = ?', (tx_id,))
        conn.commit()
        
        flash("Transaction reverted.")
    except Exception as e:
        logger.error(f"Error deleting transaction: {e}")
        flash("Error reverting transaction.")
    finally:
        conn.close()
    
    return redirect(request.referrer or url_for('inventory.edit_item', item_id=item_id))

# --- AUDIT ROUTES ---

@inventory_bp.route('/audit')
def audit_dashboard():
    conn = get_db_connection()
    # Fetch active and recent completed audits
    active_session = conn.execute("SELECT * FROM audit_sessions WHERE status = 'active' ORDER BY start_time DESC LIMIT 1").fetchone()
    past_sessions = conn.execute("SELECT * FROM audit_sessions WHERE status = 'completed' ORDER BY end_time DESC LIMIT 10").fetchall()
    conn.close()
    return render_template('inventory/audit_dashboard.html', active_session=active_session, past_sessions=past_sessions)

@inventory_bp.route('/audit/start', methods=['POST'])
def start_audit():
    mode = request.form.get('mode') # 'item' or 'shelf'
    user = session.get('user', 'Unknown')
    
    if mode not in ['item', 'shelf']:
        flash("Invalid audit mode.")
        return redirect(url_for('inventory.audit_dashboard'))
        
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO audit_sessions (user_id, mode) VALUES (?, ?)", (user, mode))
    session_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return redirect(url_for('inventory.audit_live', session_id=session_id))

@inventory_bp.route('/audit/live/<int:session_id>')
def audit_live(session_id):
    conn = get_db_connection()
    sess = conn.execute("SELECT * FROM audit_sessions WHERE id = ?", (session_id,)).fetchone()
    
    if not sess or sess['status'] != 'active':
        conn.close()
        flash("Invalid or completed session.")
        return redirect(url_for('inventory.audit_dashboard'))
        
    conn.close()
    return render_template('inventory/audit_live.html', audit_session=sess)

@inventory_bp.route('/audit/scan', methods=['POST'])
def audit_scan():
    data = request.json
    session_id = data.get('session_id')
    barcode = data.get('barcode').strip()
    
    conn = get_db_connection()
    
    # 1. Verify Session
    sess = conn.execute("SELECT * FROM audit_sessions WHERE id = ?", (session_id,)).fetchone()
    if not sess or sess['status'] != 'active':
        conn.close()
        return jsonify({'error': 'Invalid session'}), 400
        
    # 2. Find Item
    # Try exact match on SKU, then Tracking/Barcode
    item = conn.execute("SELECT * FROM inventory_items WHERE sku = ?", (barcode,)).fetchone()
    if not item:
        # Fallback to source_url or other fields if you had a barcode column, 
        # but for now we'll assume SKU or we could check a 'barcode' field if we added one. 
        # Using SKU for now as primary scan target.
        pass
        
    if not item:
        conn.close()
        return jsonify({'error': 'Item not found in inventory'}), 404
        
    # 3. Check for existing record in this session
    record = conn.execute("SELECT * FROM audit_records WHERE session_id = ? AND item_id = ?", 
                          (session_id, item['id'])).fetchone()
                          
    response_data = {
        'item': dict(item),
        'mode': sess['mode'],
        'prev_count': record['counted_qty'] if record else 0
    }
    
    # In Item Mode, we auto-increment immediately? 
    # User plan said: "Item Mode: Increments counted_qty".
    # BUT user also asked for "Unsaved Item" logic. 
    # So actually, we should just RETURN the item data, and let the frontend confirm/save.
    # We DO NOT save to DB yet. Frontend handles the "session" state until "Confirm" is pressed.
    
    conn.close()
    return jsonify(response_data)

@inventory_bp.route('/audit/submit_count', methods=['POST'])
def audit_submit_count():
    data = request.json
    session_id = data.get('session_id')
    item_id = data.get('item_id')
    count = int(data.get('count'))
    
    conn = get_db_connection()
    
    # Get Item details for snapshot
    item = conn.execute("SELECT * FROM inventory_items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        conn.close()
        return jsonify({'error': 'Item not found'}), 404
        
    # Check if record exists
    record = conn.execute("SELECT * FROM audit_records WHERE session_id = ? AND item_id = ?", 
                          (session_id, item_id)).fetchone()
                          
    if record:
        conn.execute("UPDATE audit_records SET counted_qty = ?, timestamp = CURRENT_TIMESTAMP WHERE id = ?", 
                     (count, record['id']))
    else:
        conn.execute('''
            INSERT INTO audit_records (session_id, item_id, sku, name, expected_qty, counted_qty)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (session_id, item_id, item['sku'], item['name'], item['quantity'], count))
        
    conn.commit()
    conn.close()
    
    return jsonify({'success': True})

@inventory_bp.route('/audit/history')
def audit_history():
    conn = get_db_connection()
    sessions = conn.execute("SELECT * FROM audit_sessions WHERE status='completed' ORDER BY end_time DESC").fetchall()
    conn.close()
    return render_template('inventory/audit_history.html', sessions=sessions)

@inventory_bp.route('/audit/finalize/<int:session_id>', methods=['POST'])
def audit_finalize(session_id):
    conn = get_db_connection()
    
    # 1. Auto-Commit Counts to Inventory
    records = conn.execute("SELECT * FROM audit_records WHERE session_id = ?", (session_id,)).fetchall()
    for rec in records:
        conn.execute("UPDATE inventory_items SET quantity = ? WHERE id = ?", (rec['counted_qty'], rec['item_id']))
        
    # 2. Mark Complete
    conn.execute("UPDATE audit_sessions SET status = 'completed', end_time = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('inventory.audit_report', session_id=session_id))

@inventory_bp.route('/audit/report/<int:session_id>')
def audit_report(session_id):
    conn = get_db_connection()
    sess = conn.execute("SELECT * FROM audit_sessions WHERE id = ?", (session_id,)).fetchone()
    
    # Fetch all records with difference
    records = conn.execute('''
        SELECT *, (counted_qty - expected_qty) as diff 
        FROM audit_records 
        WHERE session_id = ? 
        ORDER BY diff DESC
    ''', (session_id,)).fetchall()
    
    conn.close()
    # Fetch all records with difference
    # ...
    
    conn.close()
    return render_template('inventory/audit_report.html', audit_session=sess, records=records)

@inventory_bp.route('/audit/apply_fix/<int:record_id>', methods=['POST'])
def audit_apply_fix(record_id):
    # Only applies one fix at a time for safety
    conn = get_db_connection()
    record = conn.execute("SELECT * FROM audit_records WHERE id = ?", (record_id,)).fetchone()
    
    if record:
        # Update Inventory
        conn.execute("UPDATE inventory_items SET quantity = ? WHERE id = ?", 
                     (record['counted_qty'], record['item_id']))
        # Log Transaction
        diff = record['counted_qty'] - record['expected_qty']
        conn.execute("INSERT INTO inventory_transactions (inventory_item_id, quantity_change, reason, user_id) VALUES (?, ?, 'Audit Correction', ?)", 
                     (record['item_id'], diff, session.get('user')))
                     
        flash(f"Updated stock for {record['name']} to {record['counted_qty']}")
        conn.commit()
        
    conn.close()
    return redirect(request.referrer)


@inventory_bp.route('/adjust/<int:item_id>', methods=['POST'])
def adjust_quantity(item_id):
    """Adjust quantity for an inventory item."""
    quantity_change = int(request.form.get('quantity_change', 0))
    reason = request.form.get('reason', 'Sold/Consumed').strip()
    source_tracking = request.form.get('source_tracking', '').strip() or None
    
    if quantity_change == 0:
        flash("No change specified.")
        return redirect(url_for('inventory.list_items'))
    
    conn = get_db_connection()
    try:
        # Check if reduction would go below 0
        if quantity_change < 0:
            current = conn.execute('SELECT quantity FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
            if current and (current['quantity'] + quantity_change) < 0:
                flash(f"Cannot reduce by {abs(quantity_change)} - only {current['quantity']} in stock.")
                conn.close()
                return redirect(url_for('inventory.list_items'))
        
        # Update quantity
        conn.execute('''
            UPDATE inventory_items 
            SET quantity = quantity + ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (quantity_change, item_id))
        
        # Log transaction
        conn.execute('''
            INSERT INTO inventory_transactions 
            (inventory_item_id, quantity_change, reason, user_id, source_tracking)
            VALUES (?, ?, ?, ?, ?)
        ''', (item_id, quantity_change, reason, session.get('user'), source_tracking))
        
        conn.commit()
        
        # Check for stock alert
        if quantity_change < 0:  # Only on reductions
            item = conn.execute('''
                SELECT name, sku, quantity, alert_enabled, alert_threshold 
                FROM inventory_items WHERE id = ?
            ''', (item_id,)).fetchone()
            
            if item and item['alert_enabled']:
                new_qty = item['quantity']
                threshold = item['alert_threshold'] or 0
                
                # Alert if quantity dropped to/below threshold
                if new_qty <= threshold:
                    # Use existing webhook infrastructure
                    from app.services.data_manager import load_config
                    from app.utils.helpers import reveal_string
                    import threading
                    
                    conf = load_config()
                    if conf.get('WEBHOOK_ENABLED') and conf.get('WEBHOOK_URL'):
                        def send_stock_alert():
                            try:
                                import urllib.request
                                import json
                                
                                url = reveal_string(conf['WEBHOOK_URL'], conf.get('SECRET_KEY', 'dev_key'))
                                if not url.startswith('http'):
                                    return
                                
                                status = "OUT OF STOCK" if new_qty == 0 else f"LOW STOCK ({new_qty} remaining)"
                                msg = f"📦 **{status}**: {item['name']} (SKU: {item['sku']})"
                                
                                payload = {"content": msg, "text": msg}
                                req = urllib.request.Request(
                                    url,
                                    data=json.dumps(payload).encode('utf-8'),
                                    headers={'Content-Type': 'application/json', 'User-Agent': 'RSCP-Bot'}
                                )
                                urllib.request.urlopen(req, timeout=5)
                                logger.info(f"Stock alert sent for {item['sku']}")
                            except Exception as e:
                                logger.error(f"Stock alert webhook error: {e}")
                        
                        threading.Thread(target=send_stock_alert, daemon=True).start()
        
        action = "Added" if quantity_change > 0 else "Removed"
        flash(f"{action} {abs(quantity_change)} units.")
        
    except Exception as e:
        logger.error(f"Error adjusting quantity: {e}")
        flash(f"Error: {e}")
    finally:
        conn.close()
    
    return redirect(url_for('inventory.list_items'))


@inventory_bp.route('/delete/<int:item_id>', methods=['POST'])
def delete_item(item_id):
    """Delete an inventory item."""
    conn = get_db_connection()
    try:
        # Delete transactions first (foreign key)
        conn.execute('DELETE FROM inventory_transactions WHERE inventory_item_id = ?', (item_id,))
        conn.execute('DELETE FROM inventory_items WHERE id = ?', (item_id,))
        conn.commit()
        flash("Item deleted.")
    except Exception as e:
        logger.error(f"Error deleting inventory item: {e}")
        flash(f"Error: {e}")
    finally:
        conn.close()
    
    return redirect(url_for('inventory.list_items'))


# --- API ENDPOINTS ---

@inventory_bp.route('/api/match/<asin>')
def match_asin(asin):
    """Check if an ASIN exists in inventory, return item if found."""
    if not is_inventory_enabled():
        return jsonify({"enabled": False}), 200
    
    conn = get_db_connection()
    try:
        item = conn.execute('''
            SELECT id, sku, name, quantity, location_area, location_aisle, 
                   location_shelf, location_bin 
            FROM inventory_items 
            WHERE asin = ?
        ''', (asin,)).fetchone()
        
        if item:
            return jsonify({
                "found": True,
                "item": {
                    "id": item['id'],
                    "sku": item['sku'],
                    "name": item['name'],
                    "quantity": item['quantity'],
                    "location": ' / '.join(filter(None, [
                        item['location_area'],
                        item['location_aisle'],
                        item['location_shelf'],
                        item['location_bin']
                    ]))
                }
            })
        else:
            return jsonify({"found": False})
    finally:
        conn.close()


@inventory_bp.route('/api/search')
def search_items():
    """Search inventory items by name for autocomplete."""
    query = request.args.get('q', '').strip()
    if len(query) < 2:
        return jsonify([])
    
    conn = get_db_connection()
    try:
        items = conn.execute('''
            SELECT id, sku, name, quantity 
            FROM inventory_items 
            WHERE name LIKE ? 
            ORDER BY name 
            LIMIT 10
        ''', (f'%{query}%',)).fetchall()
        
        return jsonify([{
            "id": item['id'],
            "sku": item['sku'],
            "name": item['name'],
            "quantity": item['quantity']
        } for item in items])
    finally:
        conn.close()


@inventory_bp.route('/api/add_quantity/<int:item_id>', methods=['POST'])
def api_add_quantity(item_id):
    """API endpoint to add quantity to existing item (used from scan screen)."""
    if not is_inventory_enabled():
        return jsonify({"error": "Inventory not enabled"}), 400
    
    data = request.get_json() or {}
    quantity = int(data.get('quantity', 1))
    tracking = data.get('tracking', '')
    
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE inventory_items 
            SET quantity = quantity + ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (quantity, item_id))
        
        conn.execute('''
            INSERT INTO inventory_transactions 
            (inventory_item_id, quantity_change, reason, user_id, source_tracking)
            VALUES (?, ?, ?, ?, ?)
        ''', (item_id, quantity, 'Received from Scan', session.get('user'), tracking))
        
        conn.commit()
        
        # Get updated quantity
        item = conn.execute('SELECT quantity FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
        
        return jsonify({
            "success": True,
            "new_quantity": item['quantity'] if item else 0
        })
    except Exception as e:
        logger.error(f"Error adding quantity via API: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


# --- ALERT MANAGEMENT ---

@inventory_bp.route('/alerts')
def alerts_page():
    """Manage stock alerts for all items."""
    conn = get_db_connection()
    try:
        items = conn.execute('''
            SELECT id, name, sku, quantity, alert_enabled, alert_threshold
            FROM inventory_items
            ORDER BY name ASC
        ''').fetchall()
        
        # Get Global Threshold
        conf = load_config()
        low_threshold = int(conf.get('LOW_STOCK_THRESHOLD', 5))
        
        return render_template('inventory/alerts.html', items=items, low_threshold=low_threshold)
    finally:
        conn.close()





@inventory_bp.route('/alerts/config', methods=['POST'])
def save_alerts_config():
    """Save global alert configuration."""
    try:
        threshold = int(request.form.get('low_stock_threshold', 5))
        if threshold < 0: threshold = 0
        
        if save_config({'LOW_STOCK_THRESHOLD': threshold}):
            flash(f"Global low stock threshold set to {threshold}.")
        else:
            flash("Error saving configuration.")
            
    except ValueError:
        flash("Invalid threshold value.")
    except Exception as e:
        logger.error(f"Error saving alert config: {e}")
        flash(f"Error: {e}")
        
    return redirect(url_for('inventory.alerts_page'))


@inventory_bp.route('/alerts/bulk', methods=['POST'])
def save_alerts_bulk():
    """Bulk save alert settings for all items."""
    conn = get_db_connection()
    try:
        # Get all item IDs
        items = conn.execute('SELECT id FROM inventory_items').fetchall()
        
        for item in items:
            item_id = item['id']
            alert_enabled = f'alert_{item_id}' in request.form
            threshold = int(request.form.get(f'threshold_{item_id}', 0) or 0)
            
            conn.execute('''
                UPDATE inventory_items 
                SET alert_enabled = ?, alert_threshold = ?
                WHERE id = ?
            ''', (1 if alert_enabled else 0, threshold, item_id))
        
        conn.commit()
        flash("Alert settings saved.")
    except Exception as e:
        logger.error(f"Error saving alert settings: {e}")
        flash(f"Error: {e}")
    finally:
        conn.close()
    
    return redirect(url_for('inventory.alerts_page'))


# --- V1.16.1: ADD STOCK FROM SCAN ---

@inventory_bp.route('/add_stock/<int:item_id>', methods=['GET', 'POST'])
def add_stock(item_id):
    """Confirmation page for adding stock to existing inventory item from scan."""
    conn = get_db_connection()
    try:
        item = conn.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
        if not item:
            flash("Item not found.")
            return redirect(url_for('main.scan_page', mode='receive'))
        
        if request.method == 'POST':
            qty_to_add = int(request.form.get('qty', 1))
            tracking = request.form.get('tracking', '')
            
            # Update quantity
            new_qty = item['quantity'] + qty_to_add
            conn.execute('UPDATE inventory_items SET quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                         (new_qty, item_id))
            
            # Log transaction
            conn.execute('''
                INSERT INTO inventory_transactions (inventory_item_id, quantity_change, reason, source_tracking)
                VALUES (?, ?, ?, ?)
            ''', (item_id, qty_to_add, 'Received from Scan', tracking))
            
            conn.commit()
            # No flash message - just redirect back to scan
            return redirect(url_for('main.scan_page', mode='receive'))
        
        # GET: Show confirmation form
        qty_to_add = int(request.args.get('qty', 1))
        tracking = request.args.get('tracking', '')
        
        return render_template('inventory/add_stock.html', 
                               item=item, qty_to_add=qty_to_add, tracking=tracking)
    except Exception as e:
        logger.error(f"Error in add_stock: {e}")
        flash(f"Error: {e}")
        return redirect(url_for('main.scan_page', mode='receive'))
    finally:
        conn.close()


# --- SCAN PAGE INTEGRATION ---

@inventory_bp.route('/api/sku/<sku>')
def lookup_sku(sku):
    """Lookup inventory item by SKU for scan page."""
    if not is_inventory_enabled():
        return jsonify({"enabled": False}), 200
    
    conn = get_db_connection()
    try:
        item = conn.execute('''
            SELECT id, sku, name, quantity, image_url, sell_price,
                   location_area, location_aisle, location_shelf, location_bin
            FROM inventory_items 
            WHERE sku = ?
        ''', (sku,)).fetchone()
        
        if item:
            return jsonify({
                "found": True,
                "item": {
                    "id": item['id'],
                    "sku": item['sku'],
                    "name": item['name'],
                    "quantity": item['quantity'],
                    "image_url": item['image_url'],
                    "sell_price": item['sell_price'],
                    "location": ' / '.join(filter(None, [
                        item['location_area'],
                        item['location_aisle'],
                        item['location_shelf'],
                        item['location_bin']
                    ]))
                }
            })
        else:
            return jsonify({"found": False})
    finally:
        conn.close()


@inventory_bp.route('/api/quick_adjust/<int:item_id>', methods=['POST'])
def quick_adjust(item_id):
    """Quick quantity adjustment from scan page (supports -1, +1, custom, OOS)."""
    if not is_inventory_enabled():
        return jsonify({"error": "Inventory not enabled"}), 400
    
    data = request.get_json() or {}
    change = int(data.get('change', 0))
    action = data.get('action', '')  # 'oos' for mark out of stock
    
    conn = get_db_connection()
    try:
        # Get current quantity
        current = conn.execute('SELECT quantity, name, sku FROM inventory_items WHERE id = ?', 
                               (item_id,)).fetchone()
        if not current:
            return jsonify({"error": "Item not found"}), 404
        
        if action == 'oos':
            # Mark as out of stock (set to 0)
            new_qty = 0
            reason = "Marked OOS from Scan"
            change = -current['quantity']  # Calculate actual change for logging
        else:
            # Normal adjustment
            new_qty = current['quantity'] + change
            if new_qty < 0:
                return jsonify({
                    "error": f"Cannot reduce by {abs(change)} - only {current['quantity']} in stock"
                }), 400
            reason = "Sold/Consumed" if change < 0 else "Received from Scan"
        
        # Update quantity
        conn.execute('''
            UPDATE inventory_items 
            SET quantity = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (new_qty, item_id))
        
        # Log transaction (only if there was a change)
        if change != 0:
            conn.execute('''
                INSERT INTO inventory_transactions 
                (inventory_item_id, quantity_change, reason, user_id)
                VALUES (?, ?, ?, ?)
            ''', (item_id, change, reason, session.get('user')))
        
        conn.commit()
        
        return jsonify({
            "success": True,
            "new_quantity": new_qty,
            "name": current['name']
        })
    except Exception as e:
        logger.error(f"Quick adjust error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()
