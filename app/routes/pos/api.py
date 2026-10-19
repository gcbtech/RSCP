"""
POS API Module
AJAX endpoints for cart operations and item search.
"""
import logging
from flask import request, jsonify, session
from flask_login import current_user, login_required

from app.routes.pos import pos_bp
from app.routes.pos.core import search_inventory, get_inventory_item

logger = logging.getLogger(__name__)


@pos_bp.route('/api/search')
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
def api_validate_manager():
    """Approve voids: a manager's username + password (JSON). On success the
    approval is kept in the session for cart_remove."""
    from app.utils.permissions import (check_manager_password, manager_auth_throttled,
                                       record_manager_auth_failure, record_manager_auth_success)

    data = request.get_json(silent=True) or {}
    username = data.get('username') if isinstance(data.get('username'), str) else ''
    password = data.get('password') if isinstance(data.get('password'), str) else ''
    username = username.strip()
    if not username or not password:
        return jsonify({'valid': False, 'error': 'Manager username and password required'}), 400

    if manager_auth_throttled(username):
        return jsonify({'valid': False, 'error': 'Too many failed attempts. Wait 5 minutes.'}), 429

    # One generic error for every failure, so the endpoint does not reveal
    # which usernames are managers.
    manager = check_manager_password(username, password)
    if manager is None:
        record_manager_auth_failure(username)
        return jsonify({'valid': False, 'error': 'Invalid credentials'})

    record_manager_auth_success(username)
    # Set temporary manager auth for void operations
    from datetime import datetime
    session['pos_manager_auth'] = {
        'username': manager.username,
        'timestamp': datetime.now().timestamp()
    }
    session.modified = True
    return jsonify({'valid': True})


@pos_bp.route('/api/cart')
def api_cart():
    """Get current cart state with authoritative totals."""
    from app.routes.pos.core import get_cart, compute_cart_totals

    totals = compute_cart_totals(get_cart())
    # Backward-compatible aliases for older consumers
    totals['tax_rate'] = totals['tax_rate_pct']
    totals['total'] = totals['card_total']
    return jsonify(totals)

