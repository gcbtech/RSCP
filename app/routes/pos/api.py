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
    data = request.get_json() or {}
    username = data.get('username', '')
    password = data.get('password', '')
    pin = data.get('pin', '')
    
    users = load_users()
    user_data = users.get(username)
    
    if not user_data or not user_data.get('is_admin'):
        return jsonify({'valid': False, 'error': 'User is not a manager'})
    
    authenticated = False
    
    if password and check_password_hash(user_data['password_hash'], password):
        authenticated = True
    elif pin and user_data.get('pin_hash'):
        if check_password_hash(user_data['pin_hash'], pin):
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
    from app.routes.pos.core import get_cart, get_tax_rate, calculate_tax
    
    cart = get_cart()
    tax_rate = get_tax_rate()
    
    subtotal = sum(item.get('line_total', 0) for item in cart['items'])
    
    # Apply order-level discount
    order_discount = 0
    if cart.get('discount_amount') and cart.get('discount_type'):
        if cart['discount_type'] == 'percent':
            order_discount = subtotal * (cart['discount_amount'] / 100)
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
