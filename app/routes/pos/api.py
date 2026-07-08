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
    """Get current cart state with authoritative totals."""
    from app.routes.pos.core import get_cart, compute_cart_totals

    totals = compute_cart_totals(get_cart())
    # Backward-compatible aliases for older consumers
    totals['tax_rate'] = totals['tax_rate_pct']
    totals['total'] = totals['card_total']
    return jsonify(totals)

