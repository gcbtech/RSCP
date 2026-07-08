"""
POS Sales Module
Main sales interface routes and cart management.
"""
import logging
import json
from datetime import datetime
from flask import request, redirect, url_for, flash, render_template, jsonify, session
from flask_login import current_user, login_required

from app.routes.pos import pos_bp
from app.routes.pos.core import (
    get_cart, save_cart, clear_cart, get_inventory_item, get_tax_rate,
    calculate_tax, calculate_line_total, require_manager_for_void, allow_hold_orders,
    calculate_percentage
)
from app.services.db import get_db_connection, get_request_db

logger = logging.getLogger(__name__)


@pos_bp.route('/')
@login_required
def sales():
    """Main POS sales interface."""
    # Check if POS is locked
    if session.get('pos_locked'):
        return redirect(url_for('pos.pos_unlock'))
    
    cart = get_cart()
    tax_rate = get_tax_rate()
    
    # Calculate totals
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
    
    # Check for held orders
    held_orders = []
    if allow_hold_orders():
        try:
            conn = get_request_db()
            # Check if table exists
            table_exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='pos_held_orders'"
            ).fetchone()
            if table_exists:
                held_orders = conn.execute('''
                    SELECT id, note, created_at FROM pos_held_orders
                    WHERE operator_id = ?
                    ORDER BY created_at DESC
                ''', (current_user.id,)).fetchall()
        except Exception as e:
            logger.warning(f"Could not load held orders: {e}")
    
    return render_template('pos/sales.html',
                           cart=cart,
                           subtotal=round(subtotal, 2),
                           order_discount=round(order_discount, 2),
                           tax_rate=tax_rate * 100,  # Display as percentage
                           tax_amount=round(tax_amount, 2),
                           total=round(total, 2),
                           held_orders=held_orders,
                           allow_hold=allow_hold_orders(),
                           require_manager_void=require_manager_for_void(),
                           pos_operator=session.get('pos_operator'))


def _render_cart_ajax(cart):
    """Helper to render only the cart fragment for AJAX updates."""
    tax_rate = get_tax_rate()
    subtotal = sum(item.get('line_total', 0) for item in cart['items'])
    
    order_discount = 0
    if cart.get('discount_amount') and cart.get('discount_type'):
        if cart['discount_type'] == 'percent':
            order_discount = calculate_percentage(subtotal, cart['discount_amount'])
        else:
            order_discount = cart['discount_amount']
            
    discounted_subtotal = max(0, subtotal - order_discount)
    tax_amount = calculate_tax(discounted_subtotal)
    total = discounted_subtotal + tax_amount
    
    return render_template('pos/_cart_fragment.html',
                           cart=cart,
                           subtotal=round(subtotal, 2),
                           order_discount=round(order_discount, 2),
                           tax_rate=tax_rate * 100,
                           tax_amount=round(tax_amount, 2),
                           total=round(total, 2),
                           allow_hold=allow_hold_orders())


@pos_bp.route('/cart/add', methods=['POST'])
@login_required
def cart_add():
    """Add an item to the cart."""
    sku = request.form.get('sku', '').strip()
    quantity = int(request.form.get('quantity', 1))
    
    if not sku:
        flash('Please enter a SKU or scan an item.')
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return _render_cart_ajax(get_cart())
        return redirect(url_for('pos.sales'))
    
    # Look up the item
    item = get_inventory_item(sku)
    if not item:
        flash(f'Item not found: {sku}. Use "Add Custom Item" for non-inventory items.', 'warning')
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return _render_cart_ajax(get_cart())
        return redirect(url_for('pos.sales'))
    
    cart = get_cart()
    
    # Check how many of this item are already in cart by inventory_item_id
    # (not by input sku, since user may have searched by UPC or part number)
    existing = next((i for i in cart['items'] if i.get('inventory_item_id') == item['id']), None)
    cart_qty = existing['quantity'] if existing else 0
    total_qty = cart_qty + quantity
    
    # Check stock (warning only, don't block)
    if item['quantity'] <= 0:
        flash(f'⚠️ STOCK WARNING: "{item["name"]}" shows OUT OF STOCK in inventory!', 'warning')
    elif total_qty > item['quantity']:
        flash(f'⚠️ STOCK WARNING: Selling {total_qty} but only {item["quantity"]} in inventory!', 'warning')
    
    # Check for legacy item (warning only, allow sale)
    if item.get('is_legacy'):
        flash(f'📦 LEGACY ITEM: "{item["name"]}" is marked as discontinued. Proceed with sale if appropriate.', 'warning')
    
    # Calculate line total
    unit_price = item.get('current_price') or item.get('sell_price') or 0
    line_total = calculate_line_total(unit_price, quantity)
    
    # Check if item already in cart - if so, update quantity
    if existing:
        existing['quantity'] += quantity
        existing['line_total'] = calculate_line_total(
            existing['unit_price'], 
            existing['quantity'],
            existing.get('discount_amount', 0),
            existing.get('discount_type')
        )
    else:
        cart['items'].append({
            'inventory_item_id': item['id'],
            'sku': sku,
            'name': item['name'],
            'quantity': quantity,
            'unit_price': unit_price,
            'regular_price': item.get('regular_price', unit_price),
            'sell_price': item.get('regular_price', unit_price),  # For template price warning
            'is_on_sale': item.get('is_on_sale', False),
            'stock_quantity': item['quantity'],  # Current inventory stock level
            'discount_amount': 0,
            'discount_type': None,
            'line_total': line_total,
            'image_url': item.get('image_url'),
        })
    
    save_cart(cart)
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return _render_cart_ajax(cart)
        
    return redirect(url_for('pos.sales'))


@pos_bp.route('/cart/add-custom', methods=['POST'])
@login_required
def cart_add_custom():
    """Add a custom (non-inventory) item to the cart."""
    name = request.form.get('name', '').strip()
    price = request.form.get('price', '0').strip()
    quantity = int(request.form.get('quantity', 1))
    
    if not name:
        flash('Please enter an item name.')
        return redirect(url_for('pos.sales'))
    
    try:
        unit_price = float(price)
    except ValueError:
        flash('Invalid price.')
        return redirect(url_for('pos.sales'))
    
    cart = get_cart()
    
    # Generate a unique custom SKU with timestamp to avoid collisions
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    custom_sku = f"CUSTOM-{timestamp}-{len(cart['items']) + 1}"
    line_total = calculate_line_total(unit_price, quantity)
    
    cart['items'].append({
        'inventory_item_id': None,  # No inventory link
        'sku': custom_sku,
        'name': name,
        'quantity': quantity,
        'unit_price': unit_price,
        'sell_price': unit_price,  # For template price warning check
        'discount_amount': 0,
        'discount_type': None,
        'line_total': line_total,
        'image_url': None,
        'is_custom': True,
    })
    
    save_cart(cart)
    flash(f'Custom item "{name}" added.')
    return redirect(url_for('pos.sales'))


@pos_bp.route('/cart/remove/<int:index>', methods=['POST'])
@login_required
def cart_remove(index):
    """Remove an item from the cart."""
    cart = get_cart()
    
    # Check if manager auth required
    if require_manager_for_void():
        # Check for manager session auth
        if not session.get('pos_manager_auth'):
            flash('Manager authentication required to void items.')
            return redirect(url_for('pos.sales'))
    
    if 0 <= index < len(cart['items']):
        removed = cart['items'].pop(index)
        flash(f'Removed: {removed["name"]}')
        save_cart(cart)
    
    return redirect(url_for('pos.sales'))


@pos_bp.route('/cart/update/<int:index>', methods=['POST'])
@login_required
def cart_update(index):
    """Update quantity or discount for a cart item."""
    cart = get_cart()
    
    if 0 <= index < len(cart['items']):
        item = cart['items'][index]
        
        # Update quantity
        quantity = request.form.get('quantity')
        if quantity:
            item['quantity'] = max(1, int(quantity))
        
        # Update discount
        discount_amount = request.form.get('discount_amount')
        discount_type = request.form.get('discount_type')
        if discount_amount is not None:
            item['discount_amount'] = float(discount_amount) if discount_amount else 0
            item['discount_type'] = discount_type if item['discount_amount'] > 0 else None
        
        # Recalculate line total
        item['line_total'] = calculate_line_total(
            item['unit_price'],
            item['quantity'],
            item.get('discount_amount', 0),
            item.get('discount_type')
        )
        
        save_cart(cart)
    
    return redirect(url_for('pos.sales'))


@pos_bp.route('/cart/discount', methods=['POST'])
@login_required
def cart_discount():
    """Apply order-level discount. Requires manager approval if threshold exceeded."""
    from app.services.data_manager import load_config
    from datetime import datetime
    
    cart = get_cart()
    config = load_config()
    
    discount_amount = request.form.get('discount_amount', 0)
    discount_type = request.form.get('discount_type', 'fixed')
    discount_reason = request.form.get('discount_reason', '')
    manager_override = request.form.get('manager_approved', '')  # Manager username if approved
    
    try:
        discount_value = float(discount_amount) if discount_amount else 0
    except ValueError:
        discount_value = 0
    
    # Check if discount exceeds thresholds
    needs_approval = False
    max_percent = config.get('POS_MAX_DISCOUNT_PERCENT', 10)
    max_amount = config.get('POS_MAX_DISCOUNT_AMOUNT', 20)
    
    if discount_value > 0:
        if discount_type == 'percent' and discount_value > max_percent:
            needs_approval = True
        elif discount_type == 'fixed' and discount_value > max_amount:
            needs_approval = True
    
    # Check if user is admin (skip approval needed)
    if current_user.is_admin:
        needs_approval = False
    
    # Check for manager override approval from session
    if needs_approval and not manager_override:
        manager_auth = session.get('pos_discount_manager_auth')
        if manager_auth:
            auth_time = manager_auth.get('timestamp', 0)
            # Valid for 2 minutes
            if datetime.now().timestamp() - auth_time < 120:
                manager_override = manager_auth.get('username')
                needs_approval = False
    
    if needs_approval and not manager_override:
        # Return error - frontend should show manager approval modal
        flash(f'Discount exceeds limit. Manager approval required (max {max_percent}% or ${max_amount}).')
        return redirect(url_for('pos.sales'))
    
    # Apply the discount
    cart['discount_amount'] = discount_value
    cart['discount_type'] = discount_type if discount_value > 0 else None
    cart['discount_reason'] = discount_reason
    if manager_override:
        cart['discount_approved_by'] = manager_override
    
    # Clear the session approval
    session.pop('pos_discount_manager_auth', None)
    
    save_cart(cart)
    if manager_override:
        flash(f'Order discount applied (approved by {manager_override}).')
    else:
        flash('Order discount applied.')
    return redirect(url_for('pos.sales'))


@pos_bp.route('/cart/clear', methods=['POST'])
@login_required
def cart_clear():
    """Clear the entire cart."""
    clear_cart()
    flash('Cart cleared.')
    return redirect(url_for('pos.sales'))


@pos_bp.route('/cart/hold', methods=['POST'])
@login_required
def cart_hold():
    """Hold the current cart for later."""
    if not allow_hold_orders():
        flash('Hold orders feature is disabled.')
        return redirect(url_for('pos.sales'))
    
    cart = get_cart()
    if not cart['items']:
        flash('Cannot hold an empty cart.')
        return redirect(url_for('pos.sales'))
    
    note = request.form.get('note', '')
    
    conn = get_db_connection()
    try:
        conn.execute('''
            INSERT INTO pos_held_orders (operator_id, cart_data, note)
            VALUES (?, ?, ?)
        ''', (current_user.id, json.dumps(cart), note))
        conn.commit()
        clear_cart()
        flash('Cart held for later.')
    except Exception as e:
        logger.error(f"Error holding cart: {e}")
        flash('Error holding cart.')
    finally:
        conn.close()
    
    return redirect(url_for('pos.sales'))


@pos_bp.route('/cart/recall/<int:held_id>', methods=['POST'])
@login_required
def cart_recall(held_id):
    """Recall a held cart."""
    conn = get_db_connection()
    try:
        result = conn.execute('''
            SELECT cart_data FROM pos_held_orders
            WHERE id = ? AND operator_id = ?
        ''', (held_id, current_user.id)).fetchone()
        
        if result:
            cart = json.loads(result['cart_data'])
            save_cart(cart)
            
            # Delete the held order
            conn.execute('DELETE FROM pos_held_orders WHERE id = ?', (held_id,))
            conn.commit()
            flash('Cart recalled.')
        else:
            flash('Held order not found.')
    except Exception as e:
        logger.error(f"Error recalling cart: {e}")
        flash('Error recalling cart.')
    finally:
        conn.close()
    
    return redirect(url_for('pos.sales'))
