"""
POS Core Module
Helper functions, before_request hooks, settings management, and overview routes.
"""
import logging
import json
from datetime import datetime, date
from flask import request, redirect, url_for, flash, render_template, session

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


def calculate_tax(subtotal):
    """Calculate tax amount for a given subtotal."""
    rate = get_tax_rate()
    return round(subtotal * rate, 2)


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
    """Get the current cart from session."""
    if 'pos_cart' not in session:
        session['pos_cart'] = {
            'items': [],
            'discount_amount': 0,
            'discount_type': None,
            'discount_reason': '',
        }
    return session['pos_cart']


def save_cart(cart):
    """Save cart to session."""
    session['pos_cart'] = cart
    session.modified = True


def clear_cart():
    """Clear the current cart."""
    session.pop('pos_cart', None)
    session.modified = True


def get_inventory_item(sku):
    """Get an inventory item by SKU, UPC, or Part Number."""
    try:
        conn = get_request_db()
        
        # First try exact SKU match
        result = conn.execute('''
            SELECT id, sku, name, quantity, sell_price, buy_price, image_url
            FROM inventory_items WHERE sku = ?
        ''', (sku,)).fetchone()
        
        # If not found, search in secondary_ids (UPC, part_number)
        if not result:
            import json
            # Search by checking if the input matches UPC or part_number in secondary_ids JSON
            all_items = conn.execute('''
                SELECT id, sku, name, quantity, sell_price, buy_price, image_url, secondary_ids
                FROM inventory_items WHERE secondary_ids IS NOT NULL AND secondary_ids != '{}'
            ''').fetchall()
            
            for item in all_items:
                try:
                    if item['secondary_ids']:
                        ids = json.loads(item['secondary_ids'])
                        if ids.get('upc') == sku or ids.get('part_number') == sku:
                            result = item
                            break
                except json.JSONDecodeError:
                    continue
        
        return dict(result) if result else None
    except Exception as e:
        logger.error(f"Error getting inventory item {sku}: {e}")
        return None


def search_inventory(query, limit=10):
    """Search inventory items by SKU or name."""
    try:
        conn = get_request_db()
        search_term = f"%{query}%"
        results = conn.execute('''
            SELECT id, sku, name, quantity, sell_price, image_url
            FROM inventory_items 
            WHERE sku LIKE ? OR name LIKE ?
            ORDER BY 
                CASE WHEN sku LIKE ? THEN 0 ELSE 1 END,
                name ASC
            LIMIT ?
        ''', (search_term, search_term, f"{query}%", limit)).fetchall()
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
    from flask_login import current_user
    
    # Allow API endpoints, login, and debug
    if request.endpoint and ('api' in request.endpoint or 'login' in request.endpoint or 'debug' in request.endpoint):
        return
    
    if not is_pos_enabled():
        flash("POS module is not enabled.")
        return redirect(url_for('main.index'))
    
    # Check user role (admins bypass role check)
    if current_user.is_authenticated and not current_user.is_admin:
        if not current_user.has_role('pos'):
            flash("You don't have access to the POS module.")
            return redirect(url_for('main.index'))


@pos_bp.route('/debug')
def pos_debug():
    """Debug endpoint to check POS module status."""
    from flask_login import current_user
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
def save_email_settings():
    """Save POS email automation settings."""
    host = request.form.get('host', '').strip()
    port = request.form.get('port', '587').strip()
    user = request.form.get('user', '').strip()
    password = request.form.get('password', '').strip()
    recipients = request.form.get('recipients', '').strip()
    
    set_pos_setting('POS_EMAIL_HOST', host)
    set_pos_setting('POS_EMAIL_PORT', port)
    set_pos_setting('POS_EMAIL_USER', user)
    set_pos_setting('POS_EMAIL_RECIPIENTS', recipients)
    
    # Only update password if provided
    if password:
        set_pos_setting('POS_EMAIL_PASSWORD', password)
    
    flash("Email settings saved.")
    return redirect(url_for('admin.admin_panel') + '?tab=pos')
