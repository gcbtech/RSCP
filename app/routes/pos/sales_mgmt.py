"""
POS Sales Management
Manage active sales, discounts, and promotions.
"""
import logging
from datetime import datetime
from flask import request, redirect, url_for, flash, render_template, jsonify
from flask_login import login_required, current_user

from app.routes.pos import pos_bp
from app.services import money, pos_sales
from app.services.db import get_db_connection
from app.services.inventory import ItemNotFound

logger = logging.getLogger(__name__)

from app.utils.permissions import require_permission

@pos_bp.route('/sales-manager')
@require_permission('pos.manage')
def sales_manager():
    """Sales management dashboard."""
    conn = get_db_connection()
    try:
        # Get active sales
        active_sales = conn.execute('''
            SELECT * FROM inventory_items 
            WHERE sale_enabled = 1 
            ORDER BY sale_end DESC, name ASC
        ''').fetchall()
        
        # Get scheduled sales (future)
        scheduled_sales = conn.execute('''
            SELECT * FROM inventory_items 
            WHERE sale_enabled = 1 
            AND sale_start > datetime('now', 'localtime')
            ORDER BY sale_start ASC
        ''').fetchall()
        
        return render_template('pos/sales_manager.html', 
                             active_sales=active_sales,
                             scheduled_sales=scheduled_sales)
    finally:
        conn.close()

@pos_bp.route('/sales-manager/add', methods=['POST'])
@require_permission('pos.manage')
def add_sale():
    """Enable a sale for an item."""
    sku = request.form.get('sku', '').strip()
    price = request.form.get('sale_price')
    start = request.form.get('sale_start')
    end = request.form.get('sale_end')
    end_on_stock = request.form.get('sale_end_on_stock') == 'on'
    
    if not sku or not price:
        flash('SKU and Price are required.')
        return redirect(url_for('pos.sales_manager'))
    price_cents = money.parse(price)
    if price_cents is None:
        flash('Invalid sale price.')
        return redirect(url_for('pos.sales_manager'))

    conn = get_db_connection()
    try:
        pos_sales.start_item_sale(conn, sku, price_cents, start, end, end_on_stock)
        flash(f'Sale enabled for {sku}')
    except ItemNotFound:
        flash(f'Item not found: {sku}')
    except Exception as e:
        logger.error(f"Error adding sale: {e}")
        flash(f'Error adding sale: {e}')
    finally:
        conn.close()

    return redirect(url_for('pos.sales_manager'))


@pos_bp.route('/sales-manager/stop/<int:item_id>', methods=['POST'])
@require_permission('pos.manage')
def stop_sale(item_id):
    """Stop a sale for an item."""
    conn = get_db_connection()
    try:
        pos_sales.stop_item_sale(conn, item_id)
        flash('Sale stopped.')
    except Exception as e:
        logger.error(f"Error stopping sale: {e}")
        flash(f'Error stopping sale: {e}')
    finally:
        conn.close()
        
    return redirect(url_for('pos.sales_manager'))
