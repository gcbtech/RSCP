"""
POS API Module
AJAX endpoints for cart operations and item search.
"""
import logging
from flask import request, jsonify, session
from flask_login import current_user, login_required
from werkzeug.security import check_password_hash

from app.routes.pos import pos_bp
from app.routes.pos.core import search_inventory, get_inventory_item
from app.services.auth import load_users

logger = logging.getLogger(__name__)


@pos_bp.route('/api/search')
@login_required
def api_search():
    """Search inventory items by SKU or name."""
    query = request.args.get('q', '').strip()
    
    if len(query) < 2:
        return jsonify({'items': []})
    
    items = search_inventory(query, limit=10)
    
    # Add stock status
    for item in items:
        if item['quantity'] <= 0:
            item['stock_status'] = 'out'
        elif item['quantity'] <= 5:
            item['stock_status'] = 'low'
        else:
            item['stock_status'] = 'ok'
    
    return jsonify({'items': items})


@pos_bp.route('/api/item/<sku>')
@login_required
def api_item(sku):
    """Get item details by SKU."""
    item = get_inventory_item(sku)
    
    if not item:
        return jsonify({'error': 'Item not found'}), 404
    
    # Add stock status
    if item['quantity'] <= 0:
        item['stock_status'] = 'out'
        item['stock_warning'] = 'Item shows out of stock in inventory'
    elif item['quantity'] <= 5:
        item['stock_status'] = 'low'
        item['stock_warning'] = f'Low stock: only {item["quantity"]} remaining'
    else:
        item['stock_status'] = 'ok'
        item['stock_warning'] = None
    
    return jsonify(item)


@pos_bp.route('/api/validate-manager', methods=['POST'])
@login_required
def api_validate_manager():
    """Validate manager credentials for void/refund operations."""
    from app.services.auth import User
    from app.utils.permissions import has_permission
    
    data = request.get_json() or {}
    username = data.get('username', '')
    password = data.get('password', '')
    pin = data.get('pin', '')
    
    user_obj = User.get_by_username(username)
    if not user_obj or not has_permission(user_obj, 'pos.manage'):
        return jsonify({'valid': False, 'error': 'User is not a manager'})
    
    # Check credentials
    from app.services.db import get_request_db
    conn = get_request_db()
    pwd_row = conn.execute("SELECT password_hash, pin_hash FROM users WHERE id = ?", (user_obj.id,)).fetchone()
    
    authenticated = False
    if pwd_row:
        if password and check_password_hash(pwd_row['password_hash'], password):
            authenticated = True
        elif pin and pwd_row['pin_hash'] and check_password_hash(pwd_row['pin_hash'], pin):
            authenticated = True
    
    if authenticated:
        # Set temporary manager auth for void operations
        from datetime import datetime
        session['pos_manager_auth'] = {
            'username': username,
            'timestamp': datetime.now().timestamp()
        }
        session.modified = True
        return jsonify({'valid': True})
    
    return jsonify({'valid': False, 'error': 'Invalid credentials'})


@pos_bp.route('/api/cart')
@login_required
def api_cart():
    """Get current cart state."""
    from app.routes.pos.core import get_cart, get_tax_rate, calculate_tax, calculate_percentage
    
    cart = get_cart()
    tax_rate = get_tax_rate()
    
    subtotal = sum(item.get('line_total', 0) for item in cart['items'])
    
    # Apply order-level discount
    order_discount = 0
    if cart.get('discount_amount') and cart.get('discount_type'):
        if cart['discount_type'] == 'percent':
            order_discount = calculate_percentage(subtotal, cart['discount_amount'])
        else:
            order_discount = cart['discount_amount']
    
    discounted_subtotal = max(0, subtotal - order_discount)
    tax_amount = calculate_tax(discounted_subtotal)
    total = discounted_subtotal + tax_amount
    
    return jsonify({
        'items': cart['items'],
        'item_count': len(cart['items']),
        'subtotal': round(subtotal, 2),
        'order_discount': round(order_discount, 2),
        'tax_rate': tax_rate * 100,
        'tax_amount': round(tax_amount, 2),
        'total': round(total, 2)
    })


@pos_bp.route('/api/shared-cart/<session_code>')
@login_required
def api_shared_cart(session_code):
    """Get cart for a paired terminal session with computed totals."""
    import json
    from app.services.db import get_request_db
    from app.routes.pos.core import get_tax_rate, get_pos_setting, calculate_tax, round_money, calculate_percentage
    
    conn = get_request_db()
    row = conn.execute(
        'SELECT cart_data FROM pos_terminal_sessions WHERE session_code = ?',
        (session_code,)
    ).fetchone()
    
    if not row:
        return jsonify({'success': False, 'message': 'Session not found'}), 404
    
    try:
        cart = json.loads(row['cart_data'])
    except:
        cart = {'items': []}
    
    items = cart.get('items', [])
    subtotal = sum(item.get('line_total', 0) for item in items)
    
    # Order discount
    order_discount = 0
    if cart.get('discount_amount') and cart.get('discount_type'):
        if cart['discount_type'] == 'percent':
            order_discount = calculate_percentage(subtotal, cart['discount_amount'])
        else:
            order_discount = cart['discount_amount']
    
    # Coupon discount
    coupon_discount = (cart.get('applied_coupon') or {}).get('discount', 0) or 0
    
    discounted_subtotal = max(0, subtotal - order_discount - coupon_discount)
    tax_rate = get_tax_rate()
    tax_amount = calculate_tax(discounted_subtotal)
    card_total = round(discounted_subtotal + tax_amount, 2)
    
    # Cash discount
    cash_discount_enabled = get_pos_setting('CASH_DISCOUNT_ENABLED', 'false') == 'true'
    cash_discount_value = 0
    if cash_discount_enabled:
        cd_amount = float(get_pos_setting('CASH_DISCOUNT_AMOUNT', '0') or 0)
        cd_type = get_pos_setting('CASH_DISCOUNT_TYPE', 'percent')
        if cd_amount > 0:
            if cd_type == 'percent':
                cash_discount_value = calculate_percentage(discounted_subtotal, cd_amount)
            else:
                cash_discount_value = round_money(min(cd_amount, discounted_subtotal))
    
    cash_total = round(card_total - cash_discount_value, 2)
    
    return jsonify({
        'success': True,
        'cart': {
            'items': items,
            'subtotal': round(subtotal, 2),
            'order_discount': round(order_discount, 2),
            'coupon_discount': round(coupon_discount, 2),
            'tax_rate_pct': round(tax_rate * 100, 2),
            'tax_amount': tax_amount,
            'card_total': card_total,
            'cash_discount_enabled': cash_discount_enabled,
            'cash_discount_value': cash_discount_value,
            'cash_total': cash_total
        }
    })

