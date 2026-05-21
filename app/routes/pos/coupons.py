"""
POS Coupons Module
Handles coupon creation, management, and redemption.
"""
import json
import logging
import secrets
import string
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session
from flask_login import login_required, current_user

from app.services.db import get_db_connection
from app.services.data_manager import load_config

logger = logging.getLogger(__name__)

# Use the existing pos_bp from core
from app.routes.pos.core import pos_bp


def require_pos_admin():
    """Check if current user is POS admin or super admin."""
    if not current_user.is_authenticated:
        return False
    if current_user.is_admin:
        return True
    # Check for pos_admin role
    try:
        conn = get_db_connection()
        user = conn.execute('SELECT roles FROM users WHERE id = ?', (current_user.id,)).fetchone()
        if user and user['roles']:
            roles = json.loads(user['roles']) if isinstance(user['roles'], str) else user['roles']
            return 'pos_admin' in roles
    except Exception:
        pass
    return False


def generate_serial_code(prefix="SAVE"):
    """Generate a unique serial code for serialized coupons."""
    chars = string.ascii_uppercase + string.digits
    random_part = ''.join(secrets.choice(chars) for _ in range(6))
    return f"{prefix}-{random_part}"


# ========================================
# Coupon List & Management
# ========================================

@pos_bp.route('/coupons')
@login_required
def coupons():
    """List all coupons (admin only)."""
    if not require_pos_admin():
        flash('Access denied. POS Admin required.')
        return redirect(url_for('pos.index'))
    
    conn = get_db_connection()
    
    coupons = conn.execute('''
        SELECT c.*, u.username as created_by_name,
               (SELECT COUNT(*) FROM pos_coupon_redemptions WHERE coupon_id = c.id) as redemption_count
        FROM pos_coupons c
        LEFT JOIN users u ON c.created_by = u.id
        ORDER BY c.created_at DESC
    ''').fetchall()
    
    config = load_config() or {}
    
    return render_template('pos/coupons.html', 
                           coupons=coupons,
                           config=config,
                           now=datetime.now().strftime('%Y-%m-%d'))


@pos_bp.route('/coupons/create', methods=['GET', 'POST'])
@login_required
def create_coupon():
    """Create a new coupon."""
    if not require_pos_admin():
        flash('Access denied. POS Admin required.')
        return redirect(url_for('pos.index'))
    
    conn = get_db_connection()
    
    if request.method == 'POST':
        try:
            name = request.form.get('name', '').strip()
            coupon_type = request.form.get('coupon_type', 'generic')
            discount_type = request.form.get('discount_type', 'order_percent')
            discount_value = float(request.form.get('discount_value', 0))
            
            # Generate or use provided code
            if coupon_type == 'serialized':
                code = generate_serial_code()
            else:
                code = request.form.get('code', '').strip().upper()
                if not code:
                    flash('Code is required for generic coupons.')
                    return redirect(url_for('pos.create_coupon'))
            
            # Check for duplicate code
            existing = conn.execute('SELECT id FROM pos_coupons WHERE code = ?', (code,)).fetchone()
            if existing:
                flash(f'Coupon code "{code}" already exists.')
                return redirect(url_for('pos.create_coupon'))
            
            # BOGO fields
            buy_quantity = int(request.form.get('buy_quantity', 1) or 1)
            get_quantity = int(request.form.get('get_quantity', 1) or 1)
            reward_item_id = request.form.get('reward_item_id') or None
            if reward_item_id:
                reward_item_id = int(reward_item_id)
            
            # Limits
            min_purchase = request.form.get('min_purchase', '').strip()
            min_purchase = float(min_purchase) if min_purchase else None
            
            max_uses = request.form.get('max_uses', '').strip()
            max_uses = int(max_uses) if max_uses else None
            
            # Dates
            start_date = request.form.get('start_date', '').strip() or None
            end_date = request.form.get('end_date', '').strip() or None
            
            # Flags
            cannot_combine = request.form.get('cannot_combine') == 'on'
            
            # Insert coupon
            cursor = conn.execute('''
                INSERT INTO pos_coupons (
                    name, code, coupon_type, discount_type, discount_value,
                    buy_quantity, get_quantity, reward_item_id,
                    min_purchase, max_uses, start_date, end_date,
                    cannot_combine, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                name, code, coupon_type, discount_type, discount_value,
                buy_quantity, get_quantity, reward_item_id,
                min_purchase, max_uses, start_date, end_date,
                1 if cannot_combine else 0, current_user.id
            ))
            conn.commit()
            coupon_id = cursor.lastrowid
            
            # Add target items for item-specific coupons
            target_items = request.form.getlist('target_items')
            if target_items and discount_type in ('item_dollar', 'item_percent', 'bogo_free', 'bogo_percent'):
                for item_id in target_items:
                    conn.execute('''
                        INSERT INTO pos_coupon_items (coupon_id, item_id)
                        VALUES (?, ?)
                    ''', (coupon_id, int(item_id)))
                conn.commit()
            
            flash(f'Coupon "{name}" created successfully! Code: {code}')
            return redirect(url_for('pos.coupons'))
            
        except Exception as e:
            logger.error(f"Error creating coupon: {e}")
            flash(f'Error creating coupon: {str(e)}')
            return redirect(url_for('pos.create_coupon'))
    
    # GET - show form
    items = conn.execute('''
        SELECT id, sku, name FROM inventory_items 
        WHERE COALESCE(is_legacy, 0) = 0
        ORDER BY name
    ''').fetchall()
    
    config = load_config() or {}
    
    return render_template('pos/coupon_form.html',
                           coupon=None,
                           items=items,
                           config=config,
                           edit_mode=False)


@pos_bp.route('/coupons/<int:coupon_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_coupon(coupon_id):
    """Edit an existing coupon."""
    if not require_pos_admin():
        flash('Access denied. POS Admin required.')
        return redirect(url_for('pos.index'))
    
    conn = get_db_connection()
    
    coupon = conn.execute('SELECT * FROM pos_coupons WHERE id = ?', (coupon_id,)).fetchone()
    if not coupon:
        flash('Coupon not found.')
        return redirect(url_for('pos.coupons'))
    
    if request.method == 'POST':
        try:
            name = request.form.get('name', '').strip()
            discount_value = float(request.form.get('discount_value', 0))
            
            # BOGO fields
            buy_quantity = int(request.form.get('buy_quantity', 1) or 1)
            get_quantity = int(request.form.get('get_quantity', 1) or 1)
            reward_item_id = request.form.get('reward_item_id') or None
            if reward_item_id:
                reward_item_id = int(reward_item_id)
            
            # Limits
            min_purchase = request.form.get('min_purchase', '').strip()
            min_purchase = float(min_purchase) if min_purchase else None
            
            max_uses = request.form.get('max_uses', '').strip()
            max_uses = int(max_uses) if max_uses else None
            
            # Dates
            start_date = request.form.get('start_date', '').strip() or None
            end_date = request.form.get('end_date', '').strip() or None
            
            # Flags
            cannot_combine = request.form.get('cannot_combine') == 'on'
            
            # Update coupon (can't change code or type after creation)
            conn.execute('''
                UPDATE pos_coupons SET
                    name = ?, discount_value = ?,
                    buy_quantity = ?, get_quantity = ?, reward_item_id = ?,
                    min_purchase = ?, max_uses = ?, start_date = ?, end_date = ?,
                    cannot_combine = ?
                WHERE id = ?
            ''', (
                name, discount_value,
                buy_quantity, get_quantity, reward_item_id,
                min_purchase, max_uses, start_date, end_date,
                1 if cannot_combine else 0, coupon_id
            ))
            
            # Update target items
            conn.execute('DELETE FROM pos_coupon_items WHERE coupon_id = ?', (coupon_id,))
            target_items = request.form.getlist('target_items')
            if target_items:
                for item_id in target_items:
                    conn.execute('''
                        INSERT INTO pos_coupon_items (coupon_id, item_id)
                        VALUES (?, ?)
                    ''', (coupon_id, int(item_id)))
            
            conn.commit()
            flash(f'Coupon "{name}" updated successfully!')
            return redirect(url_for('pos.coupons'))
            
        except Exception as e:
            logger.error(f"Error updating coupon: {e}")
            flash(f'Error updating coupon: {str(e)}')
    
    # GET - show form
    items = conn.execute('''
        SELECT id, sku, name FROM inventory_items 
        WHERE COALESCE(is_legacy, 0) = 0
        ORDER BY name
    ''').fetchall()
    
    # Get currently linked items
    linked_items = conn.execute('''
        SELECT item_id FROM pos_coupon_items WHERE coupon_id = ?
    ''', (coupon_id,)).fetchall()
    linked_item_ids = [r['item_id'] for r in linked_items]
    
    config = load_config() or {}
    
    return render_template('pos/coupon_form.html',
                           coupon=coupon,
                           items=items,
                           linked_item_ids=linked_item_ids,
                           config=config,
                           edit_mode=True)


@pos_bp.route('/coupons/<int:coupon_id>/toggle', methods=['POST'])
@login_required
def toggle_coupon(coupon_id):
    """Activate/deactivate a coupon."""
    if not require_pos_admin():
        return jsonify({'error': 'Access denied'}), 403
    
    conn = get_db_connection()
    
    coupon = conn.execute('SELECT active FROM pos_coupons WHERE id = ?', (coupon_id,)).fetchone()
    if not coupon:
        flash('Coupon not found.')
        return redirect(url_for('pos.coupons'))
    
    new_status = 0 if coupon['active'] else 1
    conn.execute('UPDATE pos_coupons SET active = ? WHERE id = ?', (new_status, coupon_id))
    conn.commit()
    
    flash(f'Coupon {"activated" if new_status else "deactivated"}.')
    return redirect(url_for('pos.coupons'))


@pos_bp.route('/coupons/<int:coupon_id>/delete', methods=['POST'])
@login_required
def delete_coupon(coupon_id):
    """Delete a coupon."""
    if not require_pos_admin():
        return jsonify({'error': 'Access denied'}), 403
    
    conn = get_db_connection()
    
    conn.execute('DELETE FROM pos_coupon_items WHERE coupon_id = ?', (coupon_id,))
    conn.execute('DELETE FROM pos_coupons WHERE id = ?', (coupon_id,))
    conn.commit()
    
    flash('Coupon deleted.')
    return redirect(url_for('pos.coupons'))


# ========================================
# Coupon Validation & Application API
# ========================================

@pos_bp.route('/api/coupon/validate', methods=['POST'])
@login_required
def validate_coupon():
    """Validate a coupon code and return details."""
    data = request.get_json()
    code = data.get('code', '').strip().upper()
    cart_items = data.get('cart_items', [])
    cart_subtotal = float(data.get('cart_subtotal', 0))
    
    conn = get_db_connection()
    
    # Find coupon
    coupon = conn.execute('''
        SELECT * FROM pos_coupons WHERE code = ? AND active = 1
    ''', (code,)).fetchone()
    
    if not coupon:
        return jsonify({'valid': False, 'error': 'Invalid coupon code.'})
    
    now = datetime.now().strftime('%Y-%m-%d')
    
    # Check dates
    if coupon['start_date'] and coupon['start_date'] > now:
        return jsonify({'valid': False, 'error': 'Coupon is not yet active.'})
    
    if coupon['end_date'] and coupon['end_date'] < now:
        return jsonify({'valid': False, 'error': 'Coupon has expired.'})
    
    # Check usage limits
    if coupon['max_uses'] and coupon['current_uses'] >= coupon['max_uses']:
        return jsonify({'valid': False, 'error': 'Coupon has reached maximum uses.'})
    
    # For serialized coupons, check if already redeemed
    if coupon['coupon_type'] == 'serialized':
        redemption = conn.execute('''
            SELECT id FROM pos_coupon_redemptions WHERE coupon_id = ? AND serial_used = ?
        ''', (coupon['id'], code)).fetchone()
        if redemption:
            return jsonify({'valid': False, 'error': 'This coupon has already been redeemed.'})
    
    # Check minimum purchase for order-level coupons
    if coupon['discount_type'] in ('order_dollar', 'order_percent') and coupon['min_purchase']:
        if cart_subtotal < coupon['min_purchase']:
            return jsonify({
                'valid': False, 
                'error': f'Minimum purchase of ${coupon["min_purchase"]:.2f} required.'
            })
    
    # For item-specific coupons, check if item is in cart
    if coupon['discount_type'] in ('item_dollar', 'item_percent', 'bogo_free', 'bogo_percent'):
        target_items = conn.execute('''
            SELECT item_id FROM pos_coupon_items WHERE coupon_id = ?
        ''', (coupon['id'],)).fetchall()
        target_ids = [r['item_id'] for r in target_items]
        
        if target_ids:
            cart_item_ids = []
            for item in cart_items:
                iid = item.get('inventory_item_id')
                if iid is not None:
                    try:
                        cart_item_ids.append(int(iid))
                    except (ValueError, TypeError):
                        pass
            matching = set(target_ids) & set(cart_item_ids)
            if not matching:
                return jsonify({
                    'valid': False, 
                    'error': 'Required item for this coupon is not in cart.'
                })
    
    # Calculate discount
    discount = calculate_coupon_discount(coupon, cart_items, cart_subtotal, conn)
    
    return jsonify({
        'valid': True,
        'coupon': {
            'id': coupon['id'],
            'name': coupon['name'],
            'code': coupon['code'],
            'discount_type': coupon['discount_type'],
            'discount_value': coupon['discount_value'],
            'cannot_combine': bool(coupon['cannot_combine'])
        },
        'discount_amount': discount
    })


def calculate_coupon_discount(coupon, cart_items, cart_subtotal, conn):
    """Calculate the discount amount for a coupon."""
    discount_type = coupon['discount_type']
    discount_value = coupon['discount_value']
    
    if discount_type == 'order_dollar':
        return min(discount_value, cart_subtotal)
    
    elif discount_type == 'order_percent':
        from app.routes.pos.core import calculate_percentage
        return calculate_percentage(cart_subtotal, discount_value)
    
    elif discount_type in ('item_dollar', 'item_percent'):
        # Get target items
        target_items = conn.execute('''
            SELECT item_id FROM pos_coupon_items WHERE coupon_id = ?
        ''', (coupon['id'],)).fetchall()
        target_ids = [r['item_id'] for r in target_items]
        
        total_discount = 0
        for item in cart_items:
            item_id = item.get('inventory_item_id')
            try:
                item_id = int(item_id) if item_id is not None else None
            except (ValueError, TypeError):
                item_id = None
            if item_id in target_ids:
                item_total = float(item.get('unit_price', 0)) * int(item.get('quantity', 1))
                if discount_type == 'item_dollar':
                    # Apply once per item type, not per quantity
                    total_discount += min(discount_value, item_total)
                else:
                    from app.routes.pos.core import calculate_percentage
                    total_discount += calculate_percentage(item_total, discount_value)
        
        return total_discount
    
    elif discount_type == 'bogo_free':
        # Buy X get 1 free (same item)
        buy_qty = coupon['buy_quantity']
        target_items = conn.execute('''
            SELECT item_id FROM pos_coupon_items WHERE coupon_id = ?
        ''', (coupon['id'],)).fetchall()
        target_ids = [r['item_id'] for r in target_items]
        
        total_discount = 0
        for item in cart_items:
            item_id = item.get('inventory_item_id')
            try:
                item_id = int(item_id) if item_id is not None else None
            except (ValueError, TypeError):
                item_id = None
            if item_id in target_ids:
                qty = int(item.get('quantity', 1))
                unit_price = float(item.get('unit_price', 0))
                # For every buy_qty + 1, one is free
                free_items = qty // (buy_qty + 1)
                total_discount += free_items * unit_price
        
        return total_discount
    
    elif discount_type == 'bogo_percent':
        # Buy X get next at Y% off
        buy_qty = coupon['buy_quantity']
        target_items = conn.execute('''
            SELECT item_id FROM pos_coupon_items WHERE coupon_id = ?
        ''', (coupon['id'],)).fetchall()
        target_ids = [r['item_id'] for r in target_items]
        
        total_discount = 0
        for item in cart_items:
            item_id = item.get('inventory_item_id')
            try:
                item_id = int(item_id) if item_id is not None else None
            except (ValueError, TypeError):
                item_id = None
            if item_id in target_ids:
                qty = int(item.get('quantity', 1))
                unit_price = float(item.get('unit_price', 0))
                # For every buy_qty + 1, one gets discount
                discounted_items = qty // (buy_qty + 1)
                base_amount = discounted_items * unit_price
                from app.routes.pos.core import calculate_percentage
                total_discount += calculate_percentage(base_amount, discount_value)
        
        return round(total_discount, 2)
    
    elif discount_type == 'bogo_cross':
        # Buy A get B free
        reward_item_id = coupon['reward_item_id']
        try:
            reward_item_id = int(reward_item_id) if reward_item_id is not None else None
        except (ValueError, TypeError):
            reward_item_id = None
        buy_qty = coupon['buy_quantity']
        
        target_items = conn.execute('''
            SELECT item_id FROM pos_coupon_items WHERE coupon_id = ?
        ''', (coupon['id'],)).fetchall()
        target_ids = [r['item_id'] for r in target_items]
        
        # Check if required item is in cart with enough quantity
        has_required = False
        for item in cart_items:
            item_id = item.get('inventory_item_id')
            try:
                item_id = int(item_id) if item_id is not None else None
            except (ValueError, TypeError):
                item_id = None
            if item_id in target_ids:
                if int(item.get('quantity', 1)) >= buy_qty:
                    has_required = True
                    break
        
        if not has_required:
            return 0
        
        # Find reward item in cart
        for item in cart_items:
            item_id = item.get('inventory_item_id')
            try:
                item_id = int(item_id) if item_id is not None else None
            except (ValueError, TypeError):
                item_id = None
            if item_id == reward_item_id:
                return float(item.get('unit_price', 0))  # One free
        
        return 0
    
    return 0


@pos_bp.route('/api/coupon/apply', methods=['POST'])
@login_required
def apply_coupon():
    """Apply a validated coupon to the cart session."""
    from app.routes.pos.core import get_cart, save_cart
    
    data = request.get_json()
    cart = get_cart()
    
    cart['applied_coupon'] = {
        'id': data.get('coupon_id'),
        'code': data.get('code'),
        'name': data.get('name'),
        'discount': data.get('discount', 0)
    }
    save_cart(cart)
    
    return jsonify({'success': True})


@pos_bp.route('/api/coupon/remove', methods=['POST'])
@login_required
def remove_coupon():
    """Remove applied coupon from cart session."""
    from app.routes.pos.core import get_cart, save_cart
    
    cart = get_cart()
    if 'applied_coupon' in cart:
        cart['applied_coupon'] = None
        save_cart(cart)
    
    return jsonify({'success': True})
