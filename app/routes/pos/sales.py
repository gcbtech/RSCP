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
    get_cart, mutate_cart, current_cart_version, get_inventory_item,
    calculate_line_total, line_total_for, require_manager_for_void, allow_hold_orders,
    compute_cart_totals
)
from app.services import money
from app.services.pos_pricing import totals_json
from app.services.pos_registers import new_line_id, find_line
from app.services.db import get_request_db

logger = logging.getLogger(__name__)


@pos_bp.route('/')
def sales():
    """Main POS sales interface."""
    cart = get_cart()
    totals = compute_cart_totals(cart)

    # Check for held orders (store-wide so a cart held on a handheld
    # scanner can be recalled at any register)
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
                    SELECT h.id, h.note, h.created_at, u.username AS held_by
                    FROM pos_held_orders h
                    LEFT JOIN users u ON u.id = h.operator_id
                    ORDER BY h.created_at DESC
                ''').fetchall()
        except Exception as e:
            logger.warning(f"Could not load held orders: {e}")

    # Gentle reminder for managers when no cash drawer session is open
    # (selling works either way)
    drawer_banner = False
    try:
        from app.utils.permissions import has_permission
        if has_permission(current_user, 'pos.manage'):
            from app.services.pos_ledger import get_open_session
            drawer_banner = get_open_session(get_request_db()) is None
    except Exception as e:
        logger.warning(f"Could not check drawer session: {e}")

    return render_template('pos/sales.html',
                           drawer_banner=drawer_banner,
                           cart=cart,
                           cart_version=current_cart_version(),
                           subtotal_cents=totals['subtotal_cents'],
                           order_discount_cents=totals['order_discount_cents'],
                           tax_rate=totals['tax_rate_pct'],
                           tax_amount_cents=totals['tax_amount_cents'],
                           total_cents=totals['card_total_cents'],
                           held_orders=held_orders,
                           allow_hold=allow_hold_orders(),
                           require_manager_void=require_manager_for_void())


def _render_cart_ajax(cart):
    """Helper to render only the cart fragment for AJAX updates."""
    totals = compute_cart_totals(cart)
    return render_template('pos/_cart_fragment.html',
                           cart=cart,
                           cart_version=current_cart_version(),
                           subtotal_cents=totals['subtotal_cents'],
                           order_discount_cents=totals['order_discount_cents'],
                           tax_rate=totals['tax_rate_pct'],
                           tax_amount_cents=totals['tax_amount_cents'],
                           total_cents=totals['card_total_cents'],
                           allow_hold=allow_hold_orders())


def _parse_discount(raw, discount_type):
    """A posted discount: a percent number for 'percent', cents for 'fixed'
    (typed in dollars). Blank or junk is 0, as before."""
    if not raw:
        return 0
    if discount_type == 'percent':
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 0
        return value if value == value and abs(value) != float('inf') else 0
    cents = money.parse(raw, allow_negative=True)
    return 0 if cents is None else cents


def _discount_fields(value, discount_type):
    """Cart / line discount fields for a parsed discount (see _parse_discount)."""
    if not value or value <= 0:
        return {'discount_type': None, 'discount_percent': 0, 'discount_cents': 0}
    if discount_type == 'percent':
        return {'discount_type': 'percent', 'discount_percent': value, 'discount_cents': 0}
    return {'discount_type': 'fixed', 'discount_percent': 0, 'discount_cents': int(value)}


def _wants_json():
    """Scanner clients ask for JSON instead of redirects/fragment HTML."""
    return request.headers.get('X-Response-Format') == 'json'


def _reject_line_discount(cart, message):
    flash(message)
    if _wants_json():
        return _cart_json(cart, success=False, error='discount_rejected', status=403)
    return redirect(url_for('pos.sales'))


def _cart_json(cart, success=True, error=None, status=200, extra=None):
    """JSON cart state for scanner clients: totals, the cart_version they
    describe, and drained flash messages."""
    from flask import get_flashed_messages
    payload = totals_json(compute_cart_totals(cart))   # JSON edge: dollars
    payload['success'] = success
    payload['version'] = current_cart_version()
    if error:
        payload['error'] = error
    if extra:
        payload.update(extra)
    # Drain flashes (stock warnings etc.) into the JSON response so they
    # surface on the scanner instead of piling up in its session.
    payload['messages'] = [
        {'category': cat, 'text': msg}
        for cat, msg in get_flashed_messages(with_categories=True)
    ]
    return jsonify(payload), status


def _respond(cart, success=True, error=None, status=200):
    """Answer a cart action in whatever form the client asked for."""
    if _wants_json():
        return _cart_json(cart, success=success, error=error, status=status)
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return _render_cart_ajax(cart)
    return redirect(url_for('pos.sales'))


def _client_cart_version():
    """The cart_version the client rendered from (optional), or None."""
    raw = request.form.get('cart_version')
    if raw is None or str(raw).strip() == '':
        return None
    try:
        version = int(raw)
    except (TypeError, ValueError):
        return None
    return version if version >= 0 else None  # -1 = client never synced


def _line_gone(cart):
    """The target line no longer exists (removed on another device, or the
    sale was completed/cleared). Never act on a different line -- report it
    and hand back the current cart.

    Lines are addressed by line_id, so a client acting from a stale
    cart_version is safe whenever its line still exists; the version only
    tells us (and the client) that it was looking at an old cart."""
    flash('That item was already removed from the cart (it changed on another device).', 'warning')
    if _wants_json():
        client_version = _client_cart_version()
        stale = client_version is not None and client_version != current_cart_version()
        return _cart_json(cart, success=False, error='line_not_found', status=409,
                          extra={'stale': stale})
    return redirect(url_for('pos.sales'))


@pos_bp.route('/cart/fragment')
def cart_fragment():
    """Rendered cart fragment for the register page's poll-driven refresh."""
    return _render_cart_ajax(get_cart())


@pos_bp.route('/cart/add', methods=['POST'])
def cart_add():
    """Add an item to the cart."""
    sku = request.form.get('sku', '').strip()
    # Adding is always a positive action; guard against negative/zero/garbage
    # quantities that would otherwise understate the sale and, at checkout,
    # INCREASE inventory (quantity - negative). Use cart/update to reduce a line.
    try:
        quantity = int(request.form.get('quantity', 1))
    except (TypeError, ValueError):
        quantity = 1
    if quantity < 1:
        quantity = 1

    if not sku:
        flash('Please enter a SKU or scan an item.')
        return _respond(get_cart(), success=False, error='missing_sku', status=400)

    # Look up the item (outside the cart lock: the lock covers only the cart)
    item = get_inventory_item(sku)
    if not item:
        flash(f'Item not found: {sku}. Use "Add Custom Item" for non-inventory items.', 'warning')
        return _respond(get_cart(), success=False, error='not_found', status=404)

    unit_price = item.get('current_price_cents') or item.get('sell_price_cents') or 0

    def _add(cart):
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

        # Item already in cart - add to that line's quantity
        if existing:
            existing['quantity'] += quantity
            existing['line_total_cents'] = line_total_for(existing)
            return existing['line_id']

        line_id = new_line_id()
        cart['items'].append({
            'line_id': line_id,
            'inventory_item_id': item['id'],
            'sku': sku,
            'name': item['name'],
            'quantity': quantity,
            'unit_price_cents': unit_price,
            'regular_price_cents': item.get('regular_price_cents', unit_price),
            'sell_price_cents': item.get('regular_price_cents', unit_price),  # For template price warning
            'is_on_sale': item.get('is_on_sale', False),
            'stock_quantity': item['quantity'],  # Current inventory stock level
            'discount_type': None,
            'discount_percent': 0,
            'discount_cents': 0,
            'line_total_cents': calculate_line_total(unit_price, quantity),
            'image_url': item.get('image_url'),
        })
        return line_id

    cart, _line_id = mutate_cart(_add)
    return _respond(cart)


@pos_bp.route('/cart/add-custom', methods=['POST'])
def cart_add_custom():
    """Add a custom (non-inventory) item to the cart."""
    name = request.form.get('name', '').strip()
    price = request.form.get('price', '0').strip()
    try:
        quantity = int(request.form.get('quantity', 1))
    except (TypeError, ValueError):
        quantity = 1
    if quantity < 1:
        quantity = 1

    if not name:
        flash('Please enter an item name.')
        return redirect(url_for('pos.sales'))

    unit_price = money.parse(price, allow_negative=True)
    if unit_price is None:
        flash('Invalid price.')
        return redirect(url_for('pos.sales'))
    if unit_price < 0:
        flash('Price cannot be negative.')
        return redirect(url_for('pos.sales'))

    line_id = new_line_id()

    def _add_custom(cart):
        # Unique custom SKU (timestamp + part of the line id) so concurrent
        # custom items never collide
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        cart['items'].append({
            'line_id': line_id,
            'inventory_item_id': None,  # No inventory link
            'sku': f"CUSTOM-{timestamp}-{line_id[1:5].upper()}",
            'name': name,
            'quantity': quantity,
            'unit_price_cents': unit_price,
            'sell_price_cents': unit_price,  # For template price warning check
            'discount_type': None,
            'discount_percent': 0,
            'discount_cents': 0,
            'line_total_cents': calculate_line_total(unit_price, quantity),
            'image_url': None,
            'is_custom': True,
        })

    mutate_cart(_add_custom)
    flash(f'Custom item "{name}" added.')
    return redirect(url_for('pos.sales'))


@pos_bp.route('/cart/remove/<line_id>', methods=['POST'])
def cart_remove(line_id):
    """Remove one cart line, addressed by its stable line_id."""
    # Check if manager auth required
    if require_manager_for_void():
        # Check for manager session auth
        if not session.get('pos_manager_auth'):
            flash('Manager authentication required to void items.')
            if _wants_json():
                return _cart_json(get_cart(), success=False, error='manager_required', status=403)
            return redirect(url_for('pos.sales'))

    def _remove(cart):
        line = find_line(cart, line_id)
        if line is not None:
            cart['items'] = [i for i in cart['items'] if i is not line]
        return line

    cart, removed = mutate_cart(_remove)
    if removed is None:
        return _line_gone(cart)
    flash(f'Removed: {removed["name"]}')
    return _respond(cart)


@pos_bp.route('/cart/update/<line_id>', methods=['POST'])
def cart_update(line_id):
    """Update quantity or discount for one cart line (by line_id).

    quantity sets an absolute quantity (the register's qty box);
    quantity_delta adjusts it relative to the CURRENT value (the scanner's
    +/- buttons), so a concurrent add on the other device isn't overwritten.
    """
    quantity = None
    raw_qty = request.form.get('quantity')
    if raw_qty:
        try:
            quantity = max(1, int(raw_qty))
        except (TypeError, ValueError):
            quantity = None
    delta = None
    raw_delta = request.form.get('quantity_delta')
    if raw_delta:
        try:
            delta = int(raw_delta)
        except (TypeError, ValueError):
            delta = None

    # Discount: validated up front (the limits don't depend on the cart)
    discount = None
    discount_amount = request.form.get('discount_amount')
    if discount_amount is not None:
        discount_type = request.form.get('discount_type')
        if discount_type not in ('percent', 'fixed'):
            discount_type = 'fixed'
        # a percent, or dollars (-> cents) for a fixed discount
        line_discount = _parse_discount(discount_amount, discount_type)
        if line_discount < 0 or (discount_type == 'percent' and line_discount > 100):
            return _reject_line_discount(get_cart(), 'Invalid discount amount.')
        # Same limits as order-level discounts: beyond them, managers only.
        from app.services.data_manager import load_config
        from app.utils.permissions import has_permission
        conf = load_config()
        over_limit = ((discount_type == 'percent' and line_discount > conf.get('POS_MAX_DISCOUNT_PERCENT', 10)) or
                      (discount_type == 'fixed' and line_discount > conf.get('POS_MAX_DISCOUNT_CENTS', 2000)))
        if over_limit and not has_permission(current_user, 'pos.manage'):
            return _reject_line_discount(get_cart(), 'Discount exceeds limit. Manager approval required.')
        discount = _discount_fields(line_discount, discount_type)

    def _update(cart):
        item = find_line(cart, line_id)
        if item is None:
            return None
        if quantity is not None:
            item['quantity'] = quantity
        elif delta:
            item['quantity'] = max(1, int(item.get('quantity', 1)) + delta)
        if discount is not None:
            item.update(discount)
        item['line_total_cents'] = line_total_for(item)
        return item

    cart, updated = mutate_cart(_update)
    if updated is None:
        return _line_gone(cart)
    return _respond(cart)


@pos_bp.route('/cart/discount', methods=['POST'])
def cart_discount():
    """Apply order-level discount. Requires manager approval if threshold exceeded."""
    from app.services.data_manager import load_config
    from datetime import datetime

    config = load_config()

    discount_amount = request.form.get('discount_amount', 0)
    discount_type = request.form.get('discount_type', 'fixed')
    discount_reason = request.form.get('discount_reason', '')
    # Approval comes only from a manager check recorded server-side in the
    # session (/pos/verify-manager). A client-supplied "manager_approved"
    # field used to be accepted here, which let any request skip approval.
    manager_override = ''

    if discount_type not in ('percent', 'fixed'):
        discount_type = 'fixed'
    # a percent, or dollars (-> cents) for a fixed discount
    discount_value = _parse_discount(discount_amount, discount_type)
    if discount_value < 0 or (discount_type == 'percent' and discount_value > 100):
        flash('Invalid discount amount.')
        return redirect(url_for('pos.sales'))

    # Check if discount exceeds thresholds
    needs_approval = False
    max_percent = config.get('POS_MAX_DISCOUNT_PERCENT', 10)
    max_cents = config.get('POS_MAX_DISCOUNT_CENTS', 2000)
    max_amount = money.to_dollars(max_cents)   # shown as before ("$20.0")

    if discount_value > 0:
        if discount_type == 'percent' and discount_value > max_percent:
            needs_approval = True
        elif discount_type == 'fixed' and discount_value > max_cents:
            needs_approval = True

    # Admins and POS managers approve their own discounts
    from app.utils.permissions import has_permission
    if has_permission(current_user, 'pos.manage'):
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

    def _apply(cart):
        if discount_value > 0 and (cart.get('applied_coupon') or {}).get('cannot_combine'):
            return False
        cart.update(_discount_fields(discount_value, discount_type))
        cart['discount_reason'] = discount_reason
        if manager_override:
            cart['discount_approved_by'] = manager_override
        return True

    _cart, applied = mutate_cart(_apply)
    if not applied:
        flash('The applied coupon cannot be combined with other discounts.')
        return redirect(url_for('pos.sales'))

    # Clear the session approval
    session.pop('pos_discount_manager_auth', None)

    if manager_override:
        flash(f'Order discount applied (approved by {manager_override}).')
    else:
        flash('Order discount applied.')
    return redirect(url_for('pos.sales'))


@pos_bp.route('/cart/clear', methods=['POST'])
def cart_clear():
    """Clear the entire cart.

    If the client says which cart_version it was looking at and the cart has
    changed since (e.g. the customer's scanner just added an item), nothing
    is cleared: the user reviews the cart and confirms again.
    """
    from app.services import pos_registers
    empty = pos_registers.empty_cart()

    def _clear(c):
        c.clear()
        c.update(empty)

    try:
        cart, _ = mutate_cart(_clear, expected_version=_client_cart_version())
    except pos_registers.CartVersionMismatch as e:
        flash('The cart changed on another device. Review it, then clear again if needed.', 'warning')
        if _wants_json():
            return _cart_json(e.cart, success=False, error='cart_changed', status=409)
        return redirect(url_for('pos.sales'))
    flash('Cart cleared.')
    if _wants_json():
        return _cart_json(cart)
    return redirect(url_for('pos.sales'))


@pos_bp.route('/cart/hold', methods=['POST'])
def cart_hold():
    """Hold the current cart for later recall at any register.

    The hold record and the clear happen in one transaction under the cart
    lock, so an item a paired scanner adds at the same moment is either in
    the held order or left in the (new) cart -- never dropped.
    """
    if not allow_hold_orders():
        flash('Hold orders feature is disabled.')
        if _wants_json():
            return _cart_json(get_cart(), success=False, error='holds_disabled', status=400)
        return redirect(url_for('pos.sales'))

    note = request.form.get('note', '')
    from app.services import pos_registers

    def _hold(cart):
        if not cart['items']:
            return False
        get_request_db().execute('''
            INSERT INTO pos_held_orders (operator_id, cart_data, note)
            VALUES (?, ?, ?)
        ''', (current_user.id, json.dumps(cart), note))
        cart.clear()
        cart.update(pos_registers.empty_cart())
        return True

    try:
        cart, held = mutate_cart(_hold)
    except Exception as e:
        logger.error(f"Error holding cart: {e}")
        flash('Error holding cart.')
        if _wants_json():
            return _cart_json(get_cart(), success=False, error='hold_failed', status=500)
        return redirect(url_for('pos.sales'))

    if not held:
        flash('Cannot hold an empty cart.')
        if _wants_json():
            return _cart_json(cart, success=False, error='empty_cart', status=400)
        return redirect(url_for('pos.sales'))

    flash('Cart held for later.')
    if _wants_json():
        return _cart_json(cart)
    return redirect(url_for('pos.sales'))


@pos_bp.route('/cart/recall/<int:held_id>', methods=['POST'])
def cart_recall(held_id):
    """Recall a held cart into the (empty) current cart.

    Refused while the current cart has items, so recalling can't silently
    discard a sale in progress (including items a paired scanner just added).
    The held row is deleted in the same transaction that loads it.
    """
    def _recall(cart):
        if cart['items']:
            return 'busy'
        conn = get_request_db()
        row = conn.execute('SELECT cart_data FROM pos_held_orders WHERE id = ?',
                           (held_id,)).fetchone()
        if not row:
            return 'missing'
        held = json.loads(row['cart_data'])
        if not isinstance(held, dict):
            raise ValueError('corrupt held order')
        conn.execute('DELETE FROM pos_held_orders WHERE id = ?', (held_id,))
        cart.clear()
        cart.update(held)
        return 'ok'

    try:
        _cart, outcome = mutate_cart(_recall)
    except Exception as e:
        logger.error(f"Error recalling cart: {e}")
        flash('Error recalling cart.')
        return redirect(url_for('pos.sales'))

    if outcome == 'ok':
        flash('Cart recalled.')
    elif outcome == 'busy':
        flash('Finish, hold or clear the current sale before recalling another order.', 'warning')
    else:
        flash('Held order not found.')
    return redirect(url_for('pos.sales'))
