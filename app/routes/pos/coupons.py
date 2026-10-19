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

from app.services import money
from app.services.db import get_db_connection
from app.services.data_manager import load_config

logger = logging.getLogger(__name__)

# Use the existing pos_bp from core
from app.routes.pos.core import pos_bp
from app.utils.permissions import require_permission


def _money_or_none(raw):
    """Cents for an optional dollar field; blank -> None. ValueError on junk."""
    raw = (raw or '').strip()
    if not raw:
        return None
    cents = money.parse(raw)
    if cents is None:
        raise ValueError(f'invalid amount {raw!r}')
    return cents


def _coupon_value(discount_type, raw):
    """(discount_cents, discount_percent) for a coupon's value as typed: dollars
    for the *_dollar types, a percent for the others. ValueError on junk."""
    raw = '0' if raw is None or str(raw).strip() == '' else str(raw).strip()
    if (discount_type or '').endswith('_dollar'):
        cents = money.parse(raw)
        if cents is None:
            raise ValueError(f'invalid amount {raw!r}')
        return cents, None
    return None, float(raw)


def generate_serial_code(prefix="SAVE"):
    """Generate a unique serial code for serialized coupons."""
    chars = string.ascii_uppercase + string.digits
    random_part = ''.join(secrets.choice(chars) for _ in range(6))
    return f"{prefix}-{random_part}"


# ========================================
# Coupon List & Management
# ========================================

@pos_bp.route('/coupons')
@require_permission('pos.manage')
def coupons():
    """List all coupons (admin only)."""
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
@require_permission('pos.manage')
def create_coupon():
    """Create a new coupon."""
    conn = get_db_connection()
    
    if request.method == 'POST':
        try:
            name = request.form.get('name', '').strip()
            coupon_type = request.form.get('coupon_type', 'generic')
            discount_type = request.form.get('discount_type', 'order_percent')
            discount_cents, discount_percent = _coupon_value(discount_type, request.form.get('discount_value', 0))
            
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
            min_purchase = _money_or_none(request.form.get('min_purchase', ''))
            
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
                    name, code, coupon_type, discount_type, discount_cents, discount_percent,
                    buy_quantity, get_quantity, reward_item_id,
                    min_purchase_cents, max_uses, start_date, end_date,
                    cannot_combine, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                name, code, coupon_type, discount_type, discount_cents, discount_percent,
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
@require_permission('pos.manage')
def edit_coupon(coupon_id):
    """Edit an existing coupon."""
    conn = get_db_connection()
    
    coupon = conn.execute('SELECT * FROM pos_coupons WHERE id = ?', (coupon_id,)).fetchone()
    if not coupon:
        flash('Coupon not found.')
        return redirect(url_for('pos.coupons'))
    
    if request.method == 'POST':
        try:
            name = request.form.get('name', '').strip()
            discount_cents, discount_percent = _coupon_value(coupon['discount_type'],
                                                             request.form.get('discount_value', 0))
            
            # BOGO fields
            buy_quantity = int(request.form.get('buy_quantity', 1) or 1)
            get_quantity = int(request.form.get('get_quantity', 1) or 1)
            reward_item_id = request.form.get('reward_item_id') or None
            if reward_item_id:
                reward_item_id = int(reward_item_id)
            
            # Limits
            min_purchase = _money_or_none(request.form.get('min_purchase', ''))
            
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
                    name = ?, discount_cents = ?, discount_percent = ?,
                    buy_quantity = ?, get_quantity = ?, reward_item_id = ?,
                    min_purchase_cents = ?, max_uses = ?, start_date = ?, end_date = ?,
                    cannot_combine = ?
                WHERE id = ?
            ''', (
                name, discount_cents, discount_percent,
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
@require_permission('pos.manage')
def toggle_coupon(coupon_id):
    """Activate/deactivate a coupon."""
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
@require_permission('pos.manage')
def delete_coupon(coupon_id):
    """Delete a coupon (never one that has been redeemed on a sale)."""
    conn = get_db_connection()
    try:
        used = conn.execute('SELECT 1 FROM pos_coupon_redemptions WHERE coupon_id = ? LIMIT 1',
                            (coupon_id,)).fetchone()
        if used:
            flash('This coupon has been used on sales, so it is kept for their history. '
                  'Deactivate it instead.')
            return redirect(url_for('pos.coupons'))
        conn.execute('DELETE FROM pos_coupon_items WHERE coupon_id = ?', (coupon_id,))
        conn.execute('DELETE FROM pos_coupons WHERE id = ?', (coupon_id,))
        conn.commit()
    finally:
        conn.close()
    
    flash('Coupon deleted.')
    return redirect(url_for('pos.coupons'))


# ========================================
# Coupon Validation & Application API
# ========================================

# Coupon validation and discount math live in app.services.pos_pricing
from app.services.pos_pricing import (  # noqa: E402,F401
    evaluate_coupon, calculate_coupon_discount, cart_subtotal, coupon_result_json,
)


@pos_bp.route('/api/coupon/validate', methods=['POST'])
def validate_coupon():
    """Validate a coupon code and return details."""
    data = request.get_json()
    code = data.get('code', '').strip().upper()

    # ALWAYS use the authoritative server-side cart for validation.
    # Client-passed cart data can be stale after DOM hot-swaps or across
    # paired terminal sessions.  get_cart() handles all pairing/session
    # resolution and returns the canonical cart.
    from app.routes.pos.core import get_cart
    live_cart = get_cart()
    cart_items = live_cart.get('items', [])
    subtotal = cart_subtotal(cart_items)

    logger.info(f"[Coupon Validate] Validating coupon: {code}. "
                f"Live cart items count: {len(cart_items)}, subtotal: {subtotal} cents")

    conn = get_db_connection()
    try:
        # JSON edge: dollars (discount_amount, coupon.discount_value)
        return jsonify(coupon_result_json(evaluate_coupon(code, cart_items, subtotal, conn)))
    finally:
        conn.close()


@pos_bp.route('/api/coupon/apply', methods=['POST'])
def apply_coupon():
    """Apply a coupon to the cart, re-validating and recomputing the discount
    server-side. Only the coupon CODE from the client is trusted; the posted
    discount amount is ignored and re-derived from the authoritative live cart,
    so a crafted request can't apply an arbitrary (or oversized) discount.

    Evaluated under the cart lock (mutate_cart), so the discount matches the
    exact cart it is saved onto even if a scanner is adding items."""
    from app.routes.pos.core import mutate_cart
    from app.services.db import get_request_db

    data = request.get_json() or {}
    code = data.get('code', '').strip().upper()
    if not code:
        return jsonify({'success': False, 'error': 'Coupon code required.'}), 400

    def _apply(cart):
        cart_items = cart.get('items', [])
        result = evaluate_coupon(code, cart_items, cart_subtotal(cart_items), get_request_db(), cart=cart)
        if not result.get('valid'):
            return result
        coupon = result['coupon']
        cart['applied_coupon'] = {
            'id': coupon['id'],
            'code': coupon['code'],
            'name': coupon['name'],
            'discount_cents': result['discount_cents'],
            'cannot_combine': coupon.get('cannot_combine', False),
        }
        return result

    _cart, result = mutate_cart(_apply)
    if not result.get('valid'):
        return jsonify({'success': False, 'error': result.get('error', 'Invalid coupon.')}), 400

    return jsonify({'success': True, 'discount_amount': money.to_dollars(result['discount_cents'])})


@pos_bp.route('/api/coupon/remove', methods=['POST'])
def remove_coupon():
    """Remove the applied coupon from the cart."""
    from app.routes.pos.core import mutate_cart

    def _remove(cart):
        if cart.get('applied_coupon'):
            cart['applied_coupon'] = None

    mutate_cart(_remove)
    return jsonify({'success': True})
