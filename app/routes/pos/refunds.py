"""
POS Refunds Module
Manager-authenticated refund processing.
"""
import logging
import json
from datetime import datetime, timedelta
from flask import request, redirect, url_for, flash, render_template, session
from flask_login import current_user, login_required

from app.routes.pos import pos_bp, REFUND_REASONS
from app.services.db import get_db_connection, get_request_db

logger = logging.getLogger(__name__)


def require_manager_auth(f):
    """Decorator to require manager authentication for refunds."""
    from functools import wraps
    
    @wraps(f)
    def decorated_function(*args, **kwargs):
        from app.utils.permissions import has_permission
        # Check if user is admin or pos manager
        if not has_permission(current_user, 'pos.manage'):
            # Check for temporary manager session
            manager_auth = session.get('pos_refund_manager_auth')
            if not manager_auth:
                flash('Manager authentication required.')
                return redirect(url_for('pos.refund_auth'))
            
            # Check if auth has expired (5 minute timeout)
            auth_time = manager_auth.get('timestamp', 0)
            if datetime.now().timestamp() - auth_time > 300:
                session.pop('pos_refund_manager_auth', None)
                flash('Manager session expired. Please re-authenticate.')
                return redirect(url_for('pos.refund_auth'))
        
        return f(*args, **kwargs)
    return decorated_function


@pos_bp.route('/refunds')
@login_required
@require_manager_auth
def refunds():
    """Refund search and management interface."""
    conn = get_request_db()
    
    # Date filtering
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    
    sql = '''
        SELECT o.*, u.username as operator_name,
               (SELECT COUNT(*) FROM pos_refunds r WHERE r.order_id = o.id) as refund_count
        FROM pos_orders o
        LEFT JOIN users u ON o.operator_id = u.id
        WHERE 1=1
    '''
    params = []
    
    if date_from:
        sql += " AND date(o.created_at, 'localtime') >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND date(o.created_at, 'localtime') <= ?"
        params.append(date_to)
    
    sql += ' ORDER BY o.created_at DESC LIMIT 50'
    
    recent_orders = conn.execute(sql, params).fetchall()
    
    return render_template('pos/refunds.html',
                           orders=recent_orders,
                           date_from=date_from,
                           date_to=date_to,
                           refund_reasons=REFUND_REASONS)


@pos_bp.route('/refunds/auth', methods=['GET', 'POST'])
@login_required
def refund_auth():
    """Manager authentication for refunds."""
    if request.method == 'POST':
        from werkzeug.security import check_password_hash
        from app.services.auth import User
        from app.utils.permissions import has_permission
        
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        pin = request.form.get('pin', '')
        badge_id = request.form.get('badge_id', '')
        
        # If badge_id provided, find user by badge
        user_obj = None
        if badge_id and not username:
            from app.services.db import get_request_db
            conn = get_request_db()
            row = conn.execute("SELECT id FROM users WHERE badge_id = ?", (badge_id,)).fetchone()
            if row:
                user_obj = User.get(row['id'])
        elif username:
            user_obj = User.get_by_username(username)
            
        if user_obj and has_permission(user_obj, 'pos.manage'):
            # Check password, PIN, or Badge
            from app.services.db import get_request_db
            conn = get_request_db()
            pwd_row = conn.execute("SELECT password_hash, pin_hash, badge_id FROM users WHERE id = ?", (user_obj.id,)).fetchone()
            
            authenticated = False
            if pwd_row:
                if password and check_password_hash(pwd_row['password_hash'], password):
                    authenticated = True
                elif pin and pwd_row['pin_hash'] and check_password_hash(pwd_row['pin_hash'], pin):
                    authenticated = True
                elif badge_id and pwd_row['badge_id'] == badge_id:
                    authenticated = True
            
            if authenticated:
                session['pos_refund_manager_auth'] = {
                    'username': user_obj.username,
                    'timestamp': datetime.now().timestamp()
                }
                flash(f'Manager {user_obj.username} authenticated.')
                return redirect(url_for('pos.refunds'))
        
        flash('Invalid manager credentials.')
    
    return render_template('pos/refund_auth.html')


@pos_bp.route('/verify-manager', methods=['POST'])
@login_required
def verify_manager():
    """API endpoint to verify manager PIN/Badge for discount approval.
    
    Returns JSON with success status and manager username.
    """
    from werkzeug.security import check_password_hash
    from app.services.auth import User
    from app.utils.permissions import has_permission
    from flask import jsonify
    
    data = request.get_json() or {}
    pin = data.get('pin', '')
    badge_id = data.get('badge_id', '')
    
    if not pin and not badge_id:
        return jsonify({'success': False, 'error': 'PIN or Badge ID required'}), 400
    
    # Get all users from database to check their credentials
    from app.services.db import get_request_db
    conn = get_request_db()
    rows = conn.execute("SELECT id, username, password_hash, pin_hash, badge_id FROM users").fetchall()
    
    for r in rows:
        user_obj = User.get(r['id'])
        if not user_obj or not has_permission(user_obj, 'pos.manage'):
            continue
        
        authenticated = False
        
        # Check PIN
        if pin and r['pin_hash']:
            if check_password_hash(r['pin_hash'], pin):
                authenticated = True
        
        # Check Badge ID
        if badge_id and r['badge_id'] == badge_id:
            authenticated = True
        
        if authenticated:
            # Store manager approval in session for this discount
            session['pos_discount_manager_auth'] = {
                'username': r['username'],
                'timestamp': datetime.now().timestamp()
            }
            return jsonify({
                'success': True,
                'manager': r['username']
            })
    
    return jsonify({'success': False, 'error': 'Invalid manager credentials'}), 401


@pos_bp.route('/refunds/search')
@login_required
@require_manager_auth
def refund_search():
    """Search orders for refund."""
    query = request.args.get('q', '')
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    
    conn = get_request_db()
    
    sql = '''
        SELECT o.*, u.username as operator_name
        FROM pos_orders o
        LEFT JOIN users u ON o.operator_id = u.id
        WHERE 1=1
    '''
    params = []
    
    if query:
        sql += ''' AND (o.order_number LIKE ? OR EXISTS (
            SELECT 1 FROM pos_order_items i WHERE i.order_id = o.id AND (i.sku LIKE ? OR i.name LIKE ?)
        ))'''
        search_term = f'%{query}%'
        params.extend([search_term, search_term, search_term])
    
    if date_from:
        sql += ' AND date(o.created_at) >= ?'
        params.append(date_from)
    
    if date_to:
        sql += ' AND date(o.created_at) <= ?'
        params.append(date_to)
    
    sql += ' ORDER BY o.created_at DESC LIMIT 50'
    
    orders = conn.execute(sql, params).fetchall()
    
    return render_template('pos/refund_search.html',
                           orders=orders,
                           query=query,
                           date_from=date_from,
                           date_to=date_to)


@pos_bp.route('/refunds/order/<order_number>')
@login_required
@require_manager_auth
def refund_order(order_number):
    """View order details for refund."""
    conn = get_request_db()
    
    order = conn.execute('''
        SELECT o.*, u.username as operator_name
        FROM pos_orders o
        LEFT JOIN users u ON o.operator_id = u.id
        WHERE o.order_number = ?
    ''', (order_number,)).fetchone()
    
    if not order:
        flash('Order not found.')
        return redirect(url_for('pos.refunds'))
    
    items = conn.execute('''
        SELECT oi.*, 
               COALESCE((SELECT SUM(ri.quantity) FROM pos_refund_items ri 
                         JOIN pos_refunds r ON ri.refund_id = r.id 
                         WHERE ri.order_item_id = oi.id), 0) as refunded_qty
        FROM pos_order_items oi
        WHERE oi.order_id = ?
    ''', (order['id'],)).fetchall()
    
    refunds = conn.execute('''
        SELECT r.*, u.username as manager_name
        FROM pos_refunds r
        LEFT JOIN users u ON r.manager_id = u.id
        WHERE r.order_id = ?
        ORDER BY r.created_at DESC
    ''', (order['id'],)).fetchall()
    
    payment_details = {}
    if order['payment_details']:
        try:
            payment_details = json.loads(order['payment_details'])
        except json.JSONDecodeError:
            pass  # Invalid payment details JSON
    
    return render_template('pos/refund_order.html',
                           order=order,
                           items=items,
                           refunds=refunds,
                           payment_details=payment_details,
                           refund_reasons=REFUND_REASONS)


@pos_bp.route('/refunds/process', methods=['POST'])
@login_required
@require_manager_auth
def refund_process():
    """Process a refund."""
    order_id = request.form.get('order_id')
    refund_type = request.form.get('refund_type', 'partial')
    reason = request.form.get('reason', 'other')
    reason_notes = request.form.get('reason_notes', '')
    
    if not order_id:
        flash('Invalid order.')
        return redirect(url_for('pos.refunds'))
    
    conn = get_db_connection()
    try:
        # Get order
        order = conn.execute('SELECT * FROM pos_orders WHERE id = ?', (order_id,)).fetchone()
        if not order:
            flash('Order not found.')
            return redirect(url_for('pos.refunds'))
        
        # Calculate refund amount and process items
        refund_amount = 0
        items_to_refund = []
        items_restocked = 0
        items_damaged = 0
        
        # Reasons that should automatically restock inventory  
        # (item is still sellable and going back to inventory)
        RESTOCK_REASONS = {'customer_changed_mind', 'wrong_item', 'price_adjustment'}
        # Reasons that should NOT restock (item is damaged/defective or no physical return)
        NO_RESTOCK_REASONS = {'defective', 'duplicate_charge', 'other'}
        
        # Auto-determine default restock action based on reason
        if reason in RESTOCK_REASONS:
            default_restock = 'restock'
        elif reason in NO_RESTOCK_REASONS:
            default_restock = 'none'  # 'defective' marking handled separately if needed
        else:
            default_restock = 'none'
        
        if refund_type == 'full':
            # Full refund - all items
            items = conn.execute('SELECT * FROM pos_order_items WHERE order_id = ?', (order_id,)).fetchall()
            refund_amount = order['total']
            for item in items:
                restock_action = request.form.get(f'restock_{item["id"]}', default_restock)
                items_to_refund.append({
                    'order_item_id': item['id'],
                    'quantity': item['quantity'],
                    'amount': item['line_total'],
                    'restock_action': restock_action,
                    'inventory_item_id': item['inventory_item_id']
                })
                if restock_action == 'restock':
                    items_restocked += item['quantity']
                elif restock_action == 'damaged':
                    items_damaged += item['quantity']
        else:
            # Partial refund - selected items
            item_ids = request.form.getlist('item_id')
            for item_id in item_ids:
                qty = int(request.form.get(f'quantity_{item_id}', 0))
                if qty > 0:
                    item = conn.execute('SELECT * FROM pos_order_items WHERE id = ?', (item_id,)).fetchone()
                    if item:
                        item_refund = (item['line_total'] / item['quantity']) * qty
                        restock_action = request.form.get(f'restock_{item_id}', default_restock)
                        items_to_refund.append({
                            'order_item_id': item['id'],
                            'quantity': qty,
                            'amount': item_refund,
                            'restock_action': restock_action,
                            'inventory_item_id': item['inventory_item_id']
                        })
                        refund_amount += item_refund
                        if restock_action == 'restock':
                            items_restocked += qty
                        elif restock_action == 'damaged':
                            items_damaged += qty
        
        if refund_amount <= 0:
            flash('No items selected for refund.')
            return redirect(url_for('pos.refund_order', order_number=order['order_number']))
        
        # Get manager info
        manager_auth = session.get('pos_refund_manager_auth', {})
        manager_username = manager_auth.get('username', current_user.username)
        manager = conn.execute('SELECT id FROM users WHERE username = ?', (manager_username,)).fetchone()
        manager_id = manager['id'] if manager else current_user.id
        
        # Create refund record
        cursor = conn.execute('''
            INSERT INTO pos_refunds (
                order_id, refund_type, amount, reason, reason_notes,
                items_restocked, items_damaged, manager_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            order_id, refund_type, round(refund_amount, 2), reason, reason_notes,
            items_restocked, items_damaged, manager_id
        ))
        refund_id = cursor.lastrowid
        
        # Create refund item records and handle restocking
        for item in items_to_refund:
            conn.execute('''
                INSERT INTO pos_refund_items (refund_id, order_item_id, quantity, amount, restock_action)
                VALUES (?, ?, ?, ?, ?)
            ''', (refund_id, item['order_item_id'], item['quantity'], item['amount'], item['restock_action']))
            
            # Handle inventory
            if item['inventory_item_id'] and item['restock_action'] == 'restock':
                conn.execute('''
                    UPDATE inventory_items SET quantity = quantity + ? WHERE id = ?
                ''', (item['quantity'], item['inventory_item_id']))
                
                conn.execute('''
                    INSERT INTO inventory_transactions (
                        inventory_item_id, quantity_change, reason, user_id
                    ) VALUES (?, ?, ?, ?)
                ''', (item['inventory_item_id'], item['quantity'], 'Refund Restock', str(manager_id)))
        
        # Update order status
        new_status = 'refunded' if refund_type == 'full' else 'partial_refund'
        conn.execute('UPDATE pos_orders SET status = ? WHERE id = ?', (new_status, order_id))
        
        conn.commit()
        
        logger.info(f"Refund processed: Order {order['order_number']}, Amount: ${refund_amount:.2f}, Manager: {manager_username}")
        flash(f'Refund of ${refund_amount:.2f} processed successfully.')
        
        return redirect(url_for('pos.refund_order', order_number=order['order_number']))
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Refund error: {e}")
        flash('Error processing refund.')
        return redirect(url_for('pos.refunds'))
    finally:
        conn.close()
