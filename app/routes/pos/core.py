"""
POS Core Module
Helper functions, before_request hooks, settings management, and overview routes.
"""
import logging
import json
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, date
from flask import request, redirect, url_for, flash, render_template, session
from flask_login import login_required, current_user

from app.routes.pos import pos_bp
from app.services.db import get_db_connection, get_request_db
from app.services.data_manager import load_config

logger = logging.getLogger(__name__)


def is_pos_enabled():
    """Check if POS module is enabled in config."""
    config = load_config()
    return config.get('POS_ENABLED', False) if config else False


def get_pos_setting(key, default=None):
    """Get a POS setting from the database."""
    try:
        conn = get_request_db()
        # Check if table exists first
        table_check = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='pos_settings'"
        ).fetchone()
        if not table_check:
            logger.warning("pos_settings table does not exist yet - run migrations")
            return default
        
        result = conn.execute(
            'SELECT value FROM pos_settings WHERE key = ?', (key,)
        ).fetchone()
        return result['value'] if result else default
    except Exception as e:
        logger.error(f"Error getting POS setting {key}: {e}")
        return default


def set_pos_setting(key, value):
    """Set a POS setting in the database."""
    try:
        conn = get_db_connection()
        conn.execute('''
            INSERT OR REPLACE INTO pos_settings (key, value) VALUES (?, ?)
        ''', (key, str(value)))
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


def round_money(value):
    """Round a monetary value to 2 decimal places using ROUND_HALF_UP.
    
    Python's built-in round() uses banker's rounding (round half to even),
    which can cause penny discrepancies. This function uses traditional
    rounding where .5 always rounds up.
    """
    return float(Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def calculate_tax(subtotal):
    """Calculate tax amount for a given subtotal."""
    rate = get_tax_rate()
    result = Decimal(str(subtotal)) * Decimal(str(rate))
    return float(result.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def calculate_percentage(amount, percent):
    """Calculate percentage using Decimal to avoid floating point precision loss and round with ROUND_HALF_UP."""
    try:
        if not amount or not percent:
            return 0.0
        result = Decimal(str(amount)) * Decimal(str(percent)) / Decimal('100')
        return float(result.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
    except Exception:
        return 0.0


def calculate_line_total(unit_price, quantity, discount_amount=0, discount_type=None):
    """Calculate line total with optional discount."""
    base = unit_price * quantity
    
    if discount_amount and discount_type:
        if discount_type == 'percent':
            discount = calculate_percentage(base, discount_amount)
        else:  # fixed
            discount = discount_amount
        base = max(0, base - discount)
    
    return round_money(base)


def generate_order_number(terminal_id='POS-1'):
    """Generate a unique order number in format POS-YYYYMMDD-XXX."""
    today = date.today().strftime('%Y%m%d')
    prefix = f"POS-{today}"
    
    close_conn = False
    try:
        from flask import has_app_context
        if has_app_context():
            conn = get_request_db()
        else:
            conn = get_db_connection()
            close_conn = True
    except ImportError:
        conn = get_db_connection()
        close_conn = True
        
    try:
        # Find the highest order number for today
        result = conn.execute('''
            SELECT order_number FROM pos_orders 
            WHERE order_number LIKE ? 
            ORDER BY order_number DESC LIMIT 1
        ''', (f"{prefix}-%",)).fetchone()
        
        if result:
            try:
                # Extract the sequence number
                last_seq = int(result['order_number'].split('-')[-1])
                next_seq = last_seq + 1
            except (ValueError, IndexError):
                next_seq = 1
        else:
            next_seq = 1
        
        return f"{prefix}-{str(next_seq).zfill(3)}"
    finally:
        if close_conn:
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
      2. X-Terminal-Id header - the register page's own AJAX calls
      3. session['terminal_id'] - register form posts (populated by
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

    terminal_id = request.headers.get('X-Terminal-Id') if request else None
    return terminal_id or session.get('terminal_id') or None


def get_cart():
    """Get the current cart.

    The authoritative cart lives on the register (pos_registers row,
    resolved via resolve_register_id). Requests with no register identity
    fall back to a session-local cart so a bare browser still works.
    """
    from app.services import pos_registers

    register_id = resolve_register_id()
    if register_id:
        try:
            cart, _version = pos_registers.load_register_cart(get_request_db(), register_id)
            return cart
        except Exception as e:
            logger.error(f"Error loading register cart for {register_id}: {e}")
            # Fall through to the session cart rather than 500 mid-sale

    cart = session.get('pos_cart')
    if cart is None or not isinstance(cart, dict):
        cart = pos_registers.empty_cart()
        session['pos_cart'] = cart
        return cart

    modified = False
    for key, default in (('items', []), ('discount_amount', 0),
                         ('discount_type', None), ('discount_reason', '')):
        if key not in cart:
            cart[key] = default
            modified = True
    if modified:
        session.modified = True
    return cart


def save_cart(cart):
    """Save the cart to its single authoritative home.

    Register-identified requests write the register row, bumping
    cart_version — which paired displays and scanners poll — so one save
    is the whole synchronization story. Otherwise the session cart is used.
    """
    from app.services import pos_registers

    register_id = resolve_register_id()
    if register_id:
        try:
            pos_registers.save_register_cart(get_request_db(), register_id, cart)
            return
        except Exception as e:
            logger.error(f"Error saving register cart for {register_id}: {e}")

    session['pos_cart'] = cart
    session.modified = True


def clear_cart():
    """Clear the current cart (bumps the version so peripherals refresh)."""
    from app.services import pos_registers

    save_cart(pos_registers.empty_cart())
    session.pop('pos_cart', None)
    session.modified = True


def compute_cart_totals(cart):
    """Single source of truth for cart math.

    Used by the sales page, the cart fragment, /api/cart, peripheral
    polling (customer displays and scanners), and checkout parity so
    every screen shows identical numbers — including dual cash/card
    pricing when a cash discount is configured.
    """
    items = cart.get('items', []) or []
    subtotal = round_money(sum(item.get('line_total', 0) for item in items))

    order_discount = 0.0
    if cart.get('discount_amount') and cart.get('discount_type'):
        if cart['discount_type'] == 'percent':
            order_discount = calculate_percentage(subtotal, cart['discount_amount'])
        else:
            try:
                order_discount = round_money(min(float(cart['discount_amount']), subtotal))
            except (TypeError, ValueError):
                order_discount = 0.0

    try:
        coupon_discount = float((cart.get('applied_coupon') or {}).get('discount', 0) or 0)
    except (TypeError, ValueError):
        coupon_discount = 0.0

    discounted_subtotal = max(0.0, subtotal - order_discount - coupon_discount)
    tax_rate = get_tax_rate()
    tax_amount = calculate_tax(discounted_subtotal)
    card_total = round_money(discounted_subtotal + tax_amount)

    cash_discount_enabled = get_pos_setting('CASH_DISCOUNT_ENABLED', 'false') == 'true'
    cash_discount_value = 0.0
    if cash_discount_enabled:
        try:
            cd_amount = float(get_pos_setting('CASH_DISCOUNT_AMOUNT', '0') or 0)
        except (TypeError, ValueError):
            cd_amount = 0.0
        cd_type = get_pos_setting('CASH_DISCOUNT_TYPE', 'percent')
        if cd_amount > 0:
            if cd_type == 'percent':
                cash_discount_value = calculate_percentage(discounted_subtotal, cd_amount)
            else:
                cash_discount_value = round_money(min(cd_amount, discounted_subtotal))
    cash_total = round_money(card_total - cash_discount_value)

    return {
        'items': items,
        'item_count': len(items),
        'subtotal': subtotal,
        'order_discount': round_money(order_discount),
        'coupon_discount': round_money(coupon_discount),
        'applied_coupon': cart.get('applied_coupon'),
        'tax_rate_pct': round(tax_rate * 100, 2),
        'tax_amount': tax_amount,
        'card_total': card_total,
        'cash_discount_enabled': cash_discount_enabled,
        'cash_discount_value': cash_discount_value,
        'cash_total': cash_total,
    }


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


@pos_bp.before_request
def check_pos_enabled():
    """Redirect if POS module is disabled or user lacks role."""
    
    # Allow API endpoints and login to handle their own auth/publicity
    # Removed 'debug' from skip list - it should be protected
    if request.endpoint and ('api' in request.endpoint or 'login' in request.endpoint):
        return
        
    # Support checking raw URL path for API endpoints and public customer displays
    if request.path and ('/api/' in request.path or '/customer-display' in request.path):
        return
    
    # Enforce global authentication for POS module (except exceptions above)
    from flask import current_app
    if not current_app.config.get('TESTING') and not current_user.is_authenticated:
        return redirect(url_for('auth.login', next=request.url))
    
    if not current_app.config.get('TESTING') and not is_pos_enabled():
        flash("POS module is not enabled.")
        return redirect(url_for('main.index'))
    
    # Check user role (admins bypass role check)
    if not current_app.config.get('TESTING') and not current_user.is_admin:
        from app.utils.permissions import has_permission
        if not has_permission(current_user, 'pos.view'):
            flash("You don't have access to the POS module.")
            return redirect(url_for('main.index'))


@pos_bp.route('/debug')
@login_required
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
# POS Settings Routes
# =============================================================================

@pos_bp.route('/settings/save_tax_rate', methods=['POST'])
@login_required
def save_tax_rate():
    """Save the tax rate setting."""
    try:
        tax_rate = float(request.form.get('tax_rate', 0)) / 100  # Convert percent to decimal
        set_pos_setting('TAX_RATE', tax_rate)
        flash(f"Tax rate saved: {tax_rate * 100:.2f}%")
    except Exception as e:
        flash(f"Error saving tax rate: {e}")
    return redirect(url_for('admin.admin_panel') + '?tab=pos')


@pos_bp.route('/settings/save_cash_discount', methods=['POST'])
@login_required
def save_cash_discount():
    """Save cash discount settings."""
    enabled = 'true' if request.form.get('enabled') == 'on' else 'false'
    discount_type = request.form.get('type', 'percent')
    try:
        amount = float(request.form.get('amount', 0))
    except ValueError:
        amount = 0
    
    set_pos_setting('CASH_DISCOUNT_ENABLED', enabled)
    set_pos_setting('CASH_DISCOUNT_TYPE', discount_type)
    set_pos_setting('CASH_DISCOUNT_AMOUNT', amount)
    
    flash(f"Cash discount settings saved.")
    return redirect(url_for('admin.admin_panel') + '?tab=pos')


@pos_bp.route('/settings/save_receipt', methods=['POST'])
@login_required
def save_receipt():
    """Save receipt settings."""
    store_name = request.form.get('store_name', '').strip()
    header = request.form.get('header', '').strip()
    footer = request.form.get('footer', '').strip()
    
    set_pos_setting('RECEIPT_STORE_NAME', store_name)
    set_pos_setting('RECEIPT_HEADER', header)
    set_pos_setting('RECEIPT_FOOTER', footer)
    
    flash("Receipt settings saved.")
    return redirect(url_for('admin.admin_panel') + '?tab=pos')


@pos_bp.route('/settings/save_email', methods=['POST'])
@login_required
def save_email_settings():
    """Save POS email automation settings."""
    host = request.form.get('host', '').strip()
    port = request.form.get('port', '587').strip()
    user = request.form.get('user', '').strip()
    password = request.form.get('password', '').strip()
    recipients = request.form.get('recipients', '').strip()
    
    # Auto-Email Settings
    auto_enabled = 'true' if request.form.get('auto_email_enabled') == 'on' else 'false'
    auto_time = request.form.get('auto_email_time', '').strip()
    
    set_pos_setting('POS_EMAIL_HOST', host)
    set_pos_setting('POS_EMAIL_PORT', port)
    set_pos_setting('POS_EMAIL_USER', user)
    set_pos_setting('POS_EMAIL_RECIPIENTS', recipients)
    set_pos_setting('POS_AUTO_EMAIL_ENABLED', auto_enabled)
    set_pos_setting('POS_AUTO_EMAIL_TIME', auto_time)
    
    # Only update password if provided
    if password:
        from app.services.security import encrypt
        set_pos_setting('POS_EMAIL_PASSWORD', encrypt(password))
    
    flash("Email settings saved.")
    return redirect(url_for('admin.admin_panel') + '?tab=pos')


# =============================================================================
# Hardware API Routes
# =============================================================================

@pos_bp.route('/hardware/open_drawer', methods=['POST'])
@login_required
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
@login_required
def list_ports():
    """List available serial ports for cash drawer configuration."""
    from flask import jsonify
    from app.services.hardware import list_serial_ports
    
    ports = list_serial_ports()
    return jsonify({'ports': ports})


@pos_bp.route('/hardware/test_drawer', methods=['POST'])
@login_required
def test_drawer():
    """Test the cash drawer by opening it on specified port."""
    from flask import jsonify
    from app.services.hardware import open_cash_drawer
    
    port = request.form.get('port') or request.json.get('port') if request.is_json else request.form.get('port')
    if not port:
        return jsonify({'success': False, 'message': 'No port specified'}), 400
    
    result = open_cash_drawer(port)
    return jsonify(result)
