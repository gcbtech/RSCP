"""
Inventory Alerts Module
Routes for stock alert management.
"""
import logging
from flask import request, redirect, url_for, flash, render_template
from flask_login import login_required, current_user
from app.utils.permissions import require_permission

from app.routes.inventory import inventory_bp
from app.services.db import get_db_connection
from app.services import inventory
from app.services.data_manager import load_config, save_config

logger = logging.getLogger(__name__)


@inventory_bp.route('/alerts')
@require_permission('inventory.manage')
def alerts_page():
    """Manage stock alerts for all items."""
    conn = get_db_connection()
    try:
        items = conn.execute('''
            SELECT id, name, sku, quantity, alert_enabled, alert_threshold
            FROM inventory_items
            ORDER BY name ASC
        ''').fetchall()
        
        conf = load_config()
        low_threshold = int(conf.get('LOW_STOCK_THRESHOLD', 5))
        
        return render_template('inventory/alerts.html', items=items, low_threshold=low_threshold)
    finally:
        conn.close()


@inventory_bp.route('/alerts/config', methods=['POST'])
@require_permission('inventory.manage')
def save_alerts_config():
    """Save global alert configuration."""
    try:
        threshold = int(request.form.get('low_stock_threshold', 5))
        if threshold < 0: threshold = 0
        
        if save_config({'LOW_STOCK_THRESHOLD': threshold}):
            flash(f"Global low stock threshold set to {threshold}.")
        else:
            flash("Error saving configuration.")
            
    except ValueError:
        flash("Invalid threshold value.")
    except Exception as e:
        logger.error(f"Error saving alert config: {e}")
        flash(f"Error: {e}")
        
    return redirect(url_for('inventory.alerts_page'))


@inventory_bp.route('/alerts/bulk', methods=['POST'])
@require_permission('inventory.manage')
def save_alerts_bulk():
    """Bulk save alert settings for all items."""
    conn = get_db_connection()
    try:
        items = conn.execute('SELECT id FROM inventory_items').fetchall()
        settings = {
            item['id']: (f"alert_{item['id']}" in request.form,
                         int(request.form.get(f"threshold_{item['id']}", 0) or 0))
            for item in items
        }
        inventory.save_alert_settings(conn, settings)
        flash("Alert settings saved.")
    except Exception as e:
        logger.error(f"Error saving alert settings: {e}")
        flash(f"Error: {e}")
    finally:
        conn.close()
    
    return redirect(url_for('inventory.alerts_page'))
