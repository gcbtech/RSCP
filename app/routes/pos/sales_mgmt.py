"""
POS Sales Management
Manage active sales, discounts, and promotions.
"""
import logging
from datetime import datetime
from flask import request, redirect, url_for, flash, render_template, jsonify
from flask_login import login_required, current_user

from app.routes.pos import pos_bp
from app.services.db import get_db_connection

logger = logging.getLogger(__name__)

from app.routes.pos.coupons import require_pos_admin

@pos_bp.route('/sales-manager')
@login_required
def sales_manager():
    """Sales management dashboard."""
    if not require_pos_admin():
        flash('Access denied. POS Admin required.')
        return redirect(url_for('pos.index'))
        
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
@login_required
def add_sale():
    """Enable a sale for an item."""
    if not require_pos_admin():
        flash('Access denied. POS Admin required.')
        return redirect(url_for('pos.index'))
        
    sku = request.form.get('sku', '').strip()
    price = request.form.get('sale_price')
    start = request.form.get('sale_start')
    end = request.form.get('sale_end')
    end_on_stock = request.form.get('sale_end_on_stock') == 'on'
    
    if not sku or not price:
        flash('SKU and Price are required.')
        return redirect(url_for('pos.sales_manager'))
        
    conn = get_db_connection()
    try:
        item = conn.execute('SELECT id FROM inventory_items WHERE sku = ?', (sku,)).fetchone()
        if not item:
            flash(f'Item not found: {sku}')
            return redirect(url_for('pos.sales_manager'))
            
        conn.execute('''
            UPDATE inventory_items 
            SET sale_price = ?, sale_start = ?, sale_end = ?, 
                sale_enabled = 1, sale_end_on_stock = ?
            WHERE id = ?
        ''', (price, start or None, end or None, 1 if end_on_stock else 0, item['id']))
        conn.commit()
        
        flash(f'Sale enabled for {sku}')
    except Exception as e:
        logger.error(f"Error adding sale: {e}")
        flash(f'Error adding sale: {e}')
    finally:
        conn.close()
        
    return redirect(url_for('pos.sales_manager'))

@pos_bp.route('/sales-manager/stop/<int:item_id>', methods=['POST'])
@login_required
def stop_sale(item_id):
    """Stop a sale for an item."""
    if not require_pos_admin():
        flash('Access denied. POS Admin required.')
        return redirect(url_for('pos.index'))
        
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE inventory_items 
            SET sale_enabled = 0
            WHERE id = ?
        ''', (item_id,))
        conn.commit()
        flash('Sale stopped.')
    except Exception as e:
        logger.error(f"Error stopping sale: {e}")
        flash(f'Error stopping sale: {e}')
    finally:
        conn.close()
        
    return redirect(url_for('pos.sales_manager'))
