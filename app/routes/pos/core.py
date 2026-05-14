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


def calculate_line_total(unit_price, quantity, discount_amount=0, discount_type=None):
    """Calculate line total with optional discount."""
    base = unit_price * quantity
    
    if discount_amount and discount_type:
        if discount_type == 'percent':
            discount = base * (discount_amount / 100)
        else:  # fixed
            discount = discount_amount
        base = max(0, base - discount)
    
    return round(base, 2)


def generate_order_number(terminal_id='POS-1'):
    """Generate a unique order number in format POS-YYYYMMDD-XXX."""
    today = date.today().strftime('%Y%m%d')
    prefix = f"POS-{today}"
    
    conn = get_db_connection()
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
        conn.close()


def get_cart():
    """
    Get the current cart.
    If terminal is paired, loads from shared database storage.
    Otherwise, uses session storage.
    """
    import json
    
    try:
        # Check if terminal is paired
        pairing = session.get('pos_pairing')
        
        if pairing and pairing.get('session_code'):
            # Load from shared storage
            session_code = pairing['session_code']
            try:
                from app.services.db import get_request_db
                conn = get_request_db()
                row = conn.execute(
                    'SELECT cart_data FROM pos_terminal_sessions WHERE session_code = ?',
                    (session_code,)
                ).fetchone()
                
                if row and row['cart_data']:
                    cart = json.loads(row['cart_data'])
                    # Ensure required keys
                    cart.setdefault('items', [])
                    cart.setdefault('discount_amount', 0)
                    cart.setdefault('discount_type', None)
                    cart.setdefault('discount_reason', '')
                    return cart
            except Exception as e:
                logger.error(f"Error loading shared cart: {e}")
                # Fall through to session cart
        
        # Not paired or error - use session cart
        cart = session.get('pos_cart')
        
        # Initialize if missing or invalid type
        if cart is None or not isinstance(cart, dict):
            cart = {
                'items': [],
                'discount_amount': 0,
                'discount_type': None,
                'discount_reason': '',
            }
            session['pos_cart'] = cart
            return cart
            
        # Repair: Ensure critical keys exist
        modified = False
        if 'items' not in cart:
            cart['items'] = []
            modified = True
        
        # Ensure discount keys
        if 'discount_amount' not in cart:
            cart['discount_amount'] = 0
            modified = True
            
        if 'discount_type' not in cart:
            cart['discount_type'] = None
            modified = True
            
        if modified:
            session.modified = True
            
        return cart
    except Exception as e:
        logger.error(f"Error in get_cart: {e}")
        # Emergency fallback to prevent 500
        return {'items': [], 'discount_amount': 0, 'discount_type': None}


def save_cart(cart):
    """
    Save the cart.
    If terminal is paired, saves to shared storage and broadcasts via WebSocket.
    Otherwise, uses session storage.
    """
    import json
    
    pairing = session.get('pos_pairing')
    
    if pairing and pairing.get('session_code'):
        # Save to shared storage
        session_code = pairing['session_code']
        try:
            from app.services.db import get_db_connection
            conn = get_db_connection()
            conn.execute(
                'UPDATE pos_terminal_sessions SET cart_data = ?, last_activity = CURRENT_TIMESTAMP WHERE session_code = ?',
                (json.dumps(cart), session_code)
            )
            conn.commit()
            conn.close()
            
            # Broadcast update to all paired terminals
            try:
                from app.services.websocket import broadcast_cart_update
                broadcast_cart_update(session_code, cart)
            except Exception as e:
                logger.warning(f"Could not broadcast cart update: {e}")
                
        except Exception as e:
            logger.error(f"Error saving shared cart: {e}")
    
    # Always also save to session (for fallback/unpair scenarios)
    session['pos_cart'] = cart
    session.modified = True


def clear_cart():
    """
    Clear the current cart.
    If paired, clears shared storage and broadcasts.
    """
    import json
    
    pairing = session.get('pos_pairing')
    
    if pairing and pairing.get('session_code'):
        session_code = pairing['session_code']
        empty_cart = {'items': [], 'discount_amount': 0, 'discount_type': None, 'discount_reason': ''}
        
        try:
            from app.services.db import get_db_connection
            conn = get_db_connection()
            conn.execute(
                'UPDATE pos_terminal_sessions SET cart_data = ?, last_activity = CURRENT_TIMESTAMP WHERE session_code = ?',
                (json.dumps(empty_cart), session_code)
            )
            conn.commit()
            conn.close()
            
            # Broadcast clear to all paired terminals
            try:
                from app.services.websocket import broadcast_cart_update
                broadcast_cart_update(session_code, empty_cart)
            except Exception as e:
                logger.warning(f"Could not broadcast cart clear: {e}")
                
        except Exception as e:
            logger.error(f"Error clearing shared cart: {e}")
    
    session.pop('pos_cart', None)
    session.modified = True


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
    
    # Enforce global authentication for POS module (except exceptions above)
    if not current_user.is_authenticated:
        return redirect(url_for('main.login', next=request.url))
    
    if not is_pos_enabled():
        flash("POS module is not enabled.")
        return redirect(url_for('main.index'))
    
    # Check user role (admins bypass role check)
    if not current_user.is_admin:
        if not current_user.has_role('pos'):
            flash("You don't have access to the POS module.")
            return redirect(url_for('main.index'))
    
    # Guard: customer terminals can only access customer-safe endpoints
    pairing = session.get('pos_pairing')
    if pairing and pairing.get('terminal_type') == 'customer':
        # Endpoints that customer terminals are allowed to access
        allowed_endpoints = {
            'pos.customer_display',
            'pos.pairing_page',
            'pos.pair_terminal',
            'pos.unpair_terminal',
            'pos.terminal_status',
            'pos.api_shared_cart',
        }
        if request.endpoint and request.endpoint not in allowed_endpoints:
            return redirect(url_for('pos.customer_display'))


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
