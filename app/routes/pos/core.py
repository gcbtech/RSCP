"""
POS Core Module
Helper functions, before_request hooks, settings management, and overview routes.
"""
import logging
import json
from datetime import datetime
from flask import request, redirect, url_for, flash, render_template, session
from flask_login import login_required, current_user

from app.routes.pos import pos_bp
from app.utils.permissions import require_permission
from app.services.db import get_db_connection, get_request_db
from app.services.data_manager import load_config

logger = logging.getLogger(__name__)


def is_pos_enabled():
    """Check if POS module is enabled in config."""
    config = load_config()
    return config.get('POS_ENABLED', False) if config else False


def get_pos_setting(key, default=None):
    """Get a POS setting as a string ('true'/'false', '0.07', text).

    Compatibility shim over app.services.settings (the typed API is
    settings.get(key)); returns `default` when the setting was never stored.
    """
    from app.services import settings as settings_store
    try:
        stored = settings_store.stored_values()
        if stored is not None:
            if key not in stored:
                return default
            return settings_store.to_legacy_string(stored[key])
        # Database not migrated to the settings table yet
        conn = get_request_db()
        result = conn.execute(
            'SELECT value FROM pos_settings WHERE key = ?', (key,)
        ).fetchone()
        return result['value'] if result else default
    except Exception as e:
        logger.error(f"Error getting POS setting {key}: {e}")
        return default


def set_pos_setting(key, value):
    """Set a POS setting (strings like 'true'/'false' are converted to the
    setting's type). Compatibility shim over app.services.settings."""
    from app.services import settings as settings_store
    try:
        if settings_store.store_ready():
            settings_store.set_lenient({key: value})
            return True
        conn = get_db_connection()
        conn.execute('INSERT OR REPLACE INTO pos_settings (key, value) VALUES (?, ?)', (key, str(value)))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Error setting POS setting {key}: {e}")
        return False


def get_tax_rate():
    """Get the current tax rate as a float (e.g., 0.07 for 7%)."""
    rate = get_pos_setting('TAX_RATE', '0.0')
    try:
        return float(rate)
    except ValueError:
        return 0.0


# Money math lives in app.services.pos_pricing; these names stay importable
# from here (sales, checkout, tests). calculate_tax / compute_cart_totals read
# the current settings through get_tax_rate / get_pos_setting in this module.
from app.services.pos_pricing import (  # noqa: E402,F401
    round_money, calculate_percentage, calculate_line_total, PricingConfig,
)
from app.services import pos_pricing  # noqa: E402


def calculate_tax(subtotal):
    """Calculate tax amount for a given subtotal at the current tax rate."""
    return pos_pricing.calculate_tax(subtotal, get_tax_rate())


def pricing_config():
    """The current pricing settings (tax rate, cash discount) for pos_pricing."""
    enabled = get_pos_setting('CASH_DISCOUNT_ENABLED', 'false') == 'true'
    amount = 0.0
    if enabled:
        try:
            amount = float(get_pos_setting('CASH_DISCOUNT_AMOUNT', '0') or 0)
        except (TypeError, ValueError):
            amount = 0.0
    return PricingConfig(
        tax_rate=get_tax_rate(),
        cash_discount_enabled=enabled,
        cash_discount_amount=amount,
        cash_discount_type=get_pos_setting('CASH_DISCOUNT_TYPE', 'percent') if enabled else 'percent',
    )


def generate_order_number(terminal_id='POS-1', conn=None):
    """Next order number for today, format POS-YYYYMMDD-NNN.

    Call with the checkout transaction's connection (after BEGIN IMMEDIATE)
    so the read and the insert are atomic across registers.
    """
    from app.services.pos_checkout import next_order_number
    own_conn = conn is None
    if own_conn:
        conn = get_db_connection()
    try:
        return next_order_number(conn)
    finally:
        if own_conn:
            conn.close()


class ScannerTokenInvalid(Exception):
    """Raised when a request presents an unknown or revoked pairing token.

    Converted to a 401 JSON response by the blueprint errorhandler so a
    revoked scanner drops to its pairing screen instead of silently
    writing to a session-local cart.
    """


@pos_bp.errorhandler(ScannerTokenInvalid)
def _handle_scanner_token_invalid(e):
    from flask import jsonify
    return jsonify({
        'success': False,
        'revoked': True,
        'message': 'Pairing revoked. Re-pair this device to a register.'
    }), 401


def resolve_register_id():
    """Determine which register's cart this request targets.

    Priority:
      1. X-Pairing-Token header - a paired staff scanner acting on its
         register's cart (raises ScannerTokenInvalid if revoked/unknown)
      2. session['slaved_register_id'] - a logged-in handheld running the
         full POS page "slaved" to a register (set by /api/scanner/enter-slave
         after validating its scanner token). Makes every cart operation,
         including plain form posts and checkout, target the register's cart
         with no per-request header needed.
      3. X-Terminal-Id header - the register page's own AJAX calls
      4. session['terminal_id'] - register form posts (populated by
         /pos/api/register/hello when the sales page loads)

    Returns None when the request has no register identity yet (a fresh
    browser before the sales page JS has said hello); callers fall back
    to a per-session cart.
    """
    token = request.headers.get('X-Pairing-Token') if request else None
    if token:
        from app.services import pos_registers
        peripheral = pos_registers.resolve_peripheral(get_request_db(), token)
        if not peripheral or peripheral['role'] != 'scanner':
            raise ScannerTokenInvalid()
        return peripheral['register_id']

    slaved = session.get('slaved_register_id')
    if slaved:
        return slaved

    terminal_id = request.headers.get('X-Terminal-Id') if request else None
    return terminal_id or session.get('terminal_id') or None


def get_cart():
    """Get the current cart (read-only snapshot).

    The authoritative cart lives on the register (pos_registers row,
    resolved via resolve_register_id). Requests with no register identity
    fall back to a session-local cart so a bare browser still works.

    The version the snapshot was read at is left on g.cart_version (None for
    session carts) so pages can render it for stale-action detection.

    Never mutate the result and save it back: use mutate_cart().
    """
    from flask import g
    from app.services import pos_registers

    g.cart_version = None
    register_id = resolve_register_id()
    if register_id:
        try:
            cart, version = pos_registers.load_register_cart(get_request_db(), register_id)
            g.cart_version = version
            return cart
        except Exception as e:
            logger.error(f"Error loading register cart for {register_id}: {e}")
            # Fall through to the session cart rather than 500 mid-sale

    return _session_cart()


def _session_cart():
    from app.services import pos_registers

    cart = session.get('pos_cart')
    if cart is None or not isinstance(cart, dict):
        cart = pos_registers.empty_cart()
        session['pos_cart'] = cart
        return cart

    before = json.dumps(cart, sort_keys=True)
    pos_registers.normalize_cart(cart)
    if json.dumps(cart, sort_keys=True) != before:
        session['pos_cart'] = cart
        session.modified = True
    return cart


def current_cart_version():
    """Version of the cart most recently read/written in this request
    (None for session carts or before any read)."""
    from flask import g
    return g.get('cart_version')


def _refreshing_coupon(fn, conn):
    """Wrap a cart change so an applied coupon is re-evaluated against the
    resulting cart, in the same transaction.

    The cart stores the coupon's dollar discount from when it was applied;
    without this, removing an item (or a scanner adding one) left the old
    amount on screen while checkout recomputed it, so checkout kept refusing
    with "the cart changed". A coupon that no longer qualifies is removed
    with a message saying why.
    """
    def wrapped(cart):
        result = fn(cart)
        coupon = cart.get('applied_coupon')
        if coupon and coupon.get('code'):
            items = cart.get('items') or []
            subtotal = sum(item.get('line_total', 0) for item in items)
            check = pos_pricing.evaluate_coupon(coupon['code'], items, subtotal, conn, cart=cart)
            if check.get('valid'):
                coupon['discount'] = check['discount_amount']
            else:
                cart['applied_coupon'] = None
                flash(f"Coupon {coupon['code']} removed: {check.get('error', 'no longer applies')}")
        return result
    return wrapped


def mutate_cart(fn, expected_version=None):
    """THE way to change the cart: fn(cart) mutates it in place (and may
    return a value). Returns (cart, fn_result).

    Register carts: load -> fn -> save inside one BEGIN IMMEDIATE transaction
    (pos_registers.mutate_register_cart), so concurrent changes from the
    register, a paired scanner or a slaved handheld -- in any worker
    process -- serialize and none is lost; cart_version is bumped once.

    expected_version (register carts only) makes the change all-or-nothing:
    pos_registers.CartVersionMismatch is raised if the cart moved on.

    Session carts: fn runs on a copy that replaces the session cart only if
    fn succeeds. fn may write other tables through get_request_db(); in both
    cases those writes commit atomically with the cart change.
    """
    import copy
    from flask import g
    from app.services import pos_registers

    conn = get_request_db()
    fn = _refreshing_coupon(fn, conn)
    register_id = resolve_register_id()
    if register_id:
        try:
            cart, version, result = pos_registers.mutate_register_cart(
                conn, register_id, fn, expected_version=expected_version)
        except pos_registers.CartVersionMismatch as e:
            g.cart_version = e.version
            raise
        g.cart_version = version
        return cart, result

    g.cart_version = None
    cart = copy.deepcopy(_session_cart())
    pos_registers.begin_immediate(conn)
    try:
        result = fn(cart)
        pos_registers.normalize_cart(cart)
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    session['pos_cart'] = cart
    session.modified = True
    return cart, result


def save_cart(cart):
    """Replace the whole cart (last write wins).

    Only for callers that genuinely mean "the cart is now exactly this"
    (e.g. recalling a held order). Anything that changes part of the cart
    must use mutate_cart() so it can't overwrite a concurrent change.
    """
    replacement = json.loads(json.dumps(cart))

    def _replace(c):
        c.clear()
        c.update(replacement)

    return mutate_cart(_replace)[0]


def clear_cart():
    """Empty the cart (bumps the version so peripherals refresh)."""
    from app.services import pos_registers

    empty = pos_registers.empty_cart()

    def _clear(c):
        c.clear()
        c.update(empty)

    cart, _ = mutate_cart(_clear)
    session.pop('pos_cart', None)
    session.modified = True
    return cart


def compute_cart_totals(cart):
    """Single source of truth for cart math (pos_pricing.cart_totals at the
    current settings).

    Used by the sales page, the cart fragment, /api/cart, peripheral
    polling (customer displays and scanners), and checkout parity so
    every screen shows identical numbers -- including dual cash/card
    pricing when a cash discount is configured.
    """
    return pos_pricing.cart_totals(cart, pricing_config())


@pos_bp.route('/preview-receipt')
def preview_receipt():
    """Printable PREVIEW / pick-list of the current cart, BEFORE checkout.

    Deliberately NOT a receipt: no order is created and nothing is committed
    (no inventory decrement, no payment, no order number). It exists so bulk
    customers can print the in-progress order and physically check items off.
    The template makes the preview status unmistakable.
    """
    cart = get_cart()
    if not cart.get('items'):
        flash('Cart is empty — nothing to preview.')
        return redirect(url_for('pos.sales'))

    totals = compute_cart_totals(cart)
    config = load_config() or {}

    operator_name = None
    if current_user.is_authenticated:
        operator_name = getattr(current_user, 'username', None)
    operator_name = operator_name or session.get('operator_name') or (current_user.username if current_user.is_authenticated else '')

    return render_template(
        'pos/receipt_preview.html',
        cart=cart,
        totals=totals,
        config=config,
        org_name=config.get('ORGANIZATION_NAME', ''),
        receipt_store_name=get_pos_setting('RECEIPT_STORE_NAME', ''),
        receipt_store_name_bold=get_pos_setting('RECEIPT_STORE_NAME_BOLD', 'false') == 'true',
        receipt_store_name_italic=get_pos_setting('RECEIPT_STORE_NAME_ITALIC', 'false') == 'true',
        operator_name=operator_name,
        generated_at=datetime.now().strftime('%Y-%m-%d %I:%M %p'),
    )


from app.routes.inventory import get_inventory_item


def search_inventory(query, limit=10):
    """Search inventory items by SKU or name (Tokenized Fuzzy Search)."""
    try:
        conn = get_request_db()
        
        tokens = query.strip().split()
        if not tokens:
            return []
            
        search_conditions = []
        search_params = []
        
        for token in tokens:
            pattern = f'%{token}%'
            search_conditions.append('(sku LIKE ? OR name LIKE ? OR secondary_ids LIKE ?)')
            search_params.extend([pattern, pattern, pattern])
            
        where_clause = " AND ".join(search_conditions)
        
        sql = f'''
            SELECT id, sku, name, quantity, sell_price, image_url
            FROM inventory_items 
            WHERE COALESCE(is_legacy, 0) = 0 AND {where_clause}
            ORDER BY name ASC
            LIMIT ?
        '''
        
        results = conn.execute(sql, search_params + [limit]).fetchall()
        return [dict(r) for r in results]
    except Exception as e:
        logger.error(f"Error searching inventory: {e}")
        return []


def require_manager_for_void():
    """Check if manager auth is required to void items."""
    setting = get_pos_setting('REQUIRE_MANAGER_VOID', 'false')
    return setting.lower() == 'true'


def allow_hold_orders():
    """Check if hold/recall orders feature is enabled."""
    setting = get_pos_setting('ALLOW_HOLD_ORDERS', 'true')
    return setting.lower() == 'true'


@pos_bp.route('/debug')
@require_permission('pos.manage')  # managers only
def pos_debug():
    """Debug endpoint to check POS module status."""
    # current_user imported globally now
    import traceback
    
    debug_info = {
        'pos_enabled': is_pos_enabled(),
        'user_authenticated': current_user.is_authenticated if hasattr(current_user, 'is_authenticated') else False,
        'tables_exist': {},
        'errors': []
    }
    
    try:
        conn = get_request_db()
        
        # Check each POS table
        tables = ['pos_settings', 'pos_orders', 'pos_order_items', 'pos_refunds', 'pos_refund_items', 'pos_held_orders']
        for table in tables:
            result = conn.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
            ).fetchone()
            debug_info['tables_exist'][table] = result is not None
        
        # Try to get tax rate
        debug_info['tax_rate'] = get_tax_rate()
        
    except Exception as e:
        debug_info['errors'].append(f"DB Check Error: {str(e)}\n{traceback.format_exc()}")
    
    return f"<pre>{json.dumps(debug_info, indent=2)}</pre>"


def log_pos_action(action, user_id=None, user_name=None, target_type=None, target_id=None, details=None):
    """Log a POS action to the audit trail."""
    try:
        from flask import request as flask_request
        conn = get_db_connection()
        
        ip_address = flask_request.remote_addr if flask_request else None
        
        conn.execute('''
            INSERT INTO pos_audit_log (action, user_id, user_name, target_type, target_id, details, ip_address)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (action, user_id, user_name, target_type, target_id, 
              json.dumps(details) if details else None, ip_address))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        logger.error(f"Audit log error: {e}")
        return False


# =============================================================================
# Hardware API Routes
# =============================================================================

@pos_bp.route('/hardware/open_drawer', methods=['POST'])
def open_drawer():
    """Open the cash drawer via serial port."""
    from flask import jsonify
    from app.services.hardware import open_cash_drawer
    
    # Check if cash drawer is enabled
    enabled = get_pos_setting('CASH_DRAWER_ENABLED', 'false') == 'true'
    if not enabled:
        return jsonify({'success': False, 'message': 'Cash drawer is not enabled'}), 400
    
    # 1. Try to get port from request (Client-side override)
    port = request.form.get('port') or (request.json.get('port') if request.is_json else None)
    
    # 2. Fallback to global setting (Legacy support)
    if not port:
        port = get_pos_setting('CASH_DRAWER_PORT', '')
        
    if not port:
        return jsonify({'success': False, 'message': 'Cash drawer port not configured'}), 400
    
    result = open_cash_drawer(port)
    return jsonify(result)


@pos_bp.route('/hardware/list_ports', methods=['GET'])
@require_permission('pos.manage')  # managers only
def list_ports():
    """List available serial ports for cash drawer configuration."""
    from flask import jsonify
    from app.services.hardware import list_serial_ports
    
    ports = list_serial_ports()
    return jsonify({'ports': ports})


@pos_bp.route('/hardware/test_drawer', methods=['POST'])
@require_permission('pos.manage')  # managers only
def test_drawer():
    """Test the cash drawer by opening it on specified port."""
    from flask import jsonify
    from app.services.hardware import open_cash_drawer
    
    port = request.form.get('port') or request.json.get('port') if request.is_json else request.form.get('port')
    if not port:
        return jsonify({'success': False, 'message': 'No port specified'}), 400
    
    result = open_cash_drawer(port)
    return jsonify(result)
