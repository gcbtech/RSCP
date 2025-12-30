"""
POS Checkout Module
Payment processing, order finalization, and receipt generation.
"""
import logging
import json
import traceback
from datetime import datetime
from flask import request, redirect, url_for, flash, render_template, make_response
from flask_login import current_user, login_required

from app.routes.pos import pos_bp, PAYMENT_METHODS
from app.routes.pos.core import (
    get_cart, clear_cart, get_tax_rate, calculate_tax,
    generate_order_number
)
from app.services.db import get_db_connection, get_request_db

logger = logging.getLogger(__name__)


@pos_bp.route('/checkout')
@login_required
def checkout():
    """Checkout page with payment options."""
    cart = get_cart()
    
    if not cart['items']:
        flash('Cart is empty.')
        return redirect(url_for('pos.sales'))
    
    tax_rate = get_tax_rate()
    subtotal = sum(item.get('line_total', 0) for item in cart['items'])
    
    # Apply order-level discount
    order_discount = 0
    if cart.get('discount_amount') and cart.get('discount_type'):
        if cart['discount_type'] == 'percent':
            order_discount = subtotal * (cart['discount_amount'] / 100)
        else:
            order_discount = cart['discount_amount']
    
    discounted_subtotal = max(0, subtotal - order_discount)
    tax_amount = calculate_tax(discounted_subtotal)
    total = discounted_subtotal + tax_amount
    
    # Get cash discount settings to show on checkout
    from app.routes.pos.core import get_pos_setting
    cash_discount_enabled = get_pos_setting('CASH_DISCOUNT_ENABLED', 'false') == 'true'
    cash_discount_amount = float(get_pos_setting('CASH_DISCOUNT_AMOUNT', '0') or 0)
    cash_discount_type = get_pos_setting('CASH_DISCOUNT_TYPE', 'percent')
    
    # Calculate what the cash discount would be
    cash_discount_value = 0
    if cash_discount_enabled and cash_discount_amount > 0:
        if cash_discount_type == 'percent':
            cash_discount_value = round(discounted_subtotal * (cash_discount_amount / 100), 2)
        else:
            cash_discount_value = round(min(cash_discount_amount, discounted_subtotal), 2)
    
    return render_template('pos/checkout.html',
                           cart=cart,
                           subtotal=round(subtotal, 2),
                           order_discount=round(order_discount, 2),
                           tax_rate=tax_rate * 100,
                           tax_amount=round(tax_amount, 2),
                           total=round(total, 2),
                           payment_methods=PAYMENT_METHODS,
                           cash_discount_enabled=cash_discount_enabled,
                           cash_discount_amount=cash_discount_amount,
                           cash_discount_type=cash_discount_type,
                           cash_discount_value=cash_discount_value)


@pos_bp.route('/checkout/process', methods=['POST'])
@login_required
def checkout_process():
    """Process the checkout and create order."""
    cart = get_cart()
    
    if not cart['items']:
        flash('Cart is empty.')
        return redirect(url_for('pos.sales'))
    
    payment_method = request.form.get('payment_method', 'cash')
    
    # Calculate totals
    tax_rate = get_tax_rate()
    subtotal = sum(item.get('line_total', 0) for item in cart['items'])
    
    order_discount = 0
    if cart.get('discount_amount') and cart.get('discount_type'):
        if cart['discount_type'] == 'percent':
            order_discount = subtotal * (cart['discount_amount'] / 100)
        else:
            order_discount = cart['discount_amount']
    
    discounted_subtotal = max(0, subtotal - order_discount)
    tax_amount = calculate_tax(discounted_subtotal)
    total = round(discounted_subtotal + tax_amount, 2)
    
    # Apply cash discount if enabled and payment is cash
    cash_discount_applied = 0
    from app.routes.pos.core import get_pos_setting
    if payment_method == 'cash':
        cash_discount_enabled = get_pos_setting('CASH_DISCOUNT_ENABLED', 'false') == 'true'
        if cash_discount_enabled:
            cash_discount_amount = float(get_pos_setting('CASH_DISCOUNT_AMOUNT', '0') or 0)
            cash_discount_type = get_pos_setting('CASH_DISCOUNT_TYPE', 'percent')
            
            if cash_discount_amount > 0:
                if cash_discount_type == 'percent':
                    cash_discount_applied = round(discounted_subtotal * (cash_discount_amount / 100), 2)
                else:
                    cash_discount_applied = round(min(cash_discount_amount, discounted_subtotal), 2)
                
                total = round(total - cash_discount_applied, 2)
    
    # Build payment details
    payment_details = {}
    
    if payment_method == 'cash':
        tendered = float(request.form.get('tendered', total))
        change = round(tendered - total, 2)
        payment_details = {
            'tendered': tendered, 
            'change': max(0, change),
            'cash_discount': cash_discount_applied
        }
    elif payment_method == 'split':
        # Parse split payment details
        cash_amount = float(request.form.get('cash_amount', 0))
        card_amounts = request.form.getlist('card_amount')
        card_amounts = [float(a) for a in card_amounts if a]
        
        # Apply cash discount to the cash portion of split payment
        split_cash_discount = 0
        cash_discount_enabled = get_pos_setting('CASH_DISCOUNT_ENABLED', 'false') == 'true'
        if cash_discount_enabled and cash_amount > 0:
            cash_discount_amount = float(get_pos_setting('CASH_DISCOUNT_AMOUNT', '0') or 0)
            cash_discount_type = get_pos_setting('CASH_DISCOUNT_TYPE', 'percent')
            
            if cash_discount_amount > 0:
                if cash_discount_type == 'percent':
                    # Apply percentage discount to cash portion only
                    split_cash_discount = round(cash_amount * (cash_discount_amount / 100), 2)
                else:
                    # Apply fixed discount proportionally to cash portion
                    cash_proportion = cash_amount / total if total > 0 else 0
                    split_cash_discount = round(min(cash_discount_amount * cash_proportion, cash_amount), 2)
                
                total = round(total - split_cash_discount, 2)
        
        payment_details = {
            'cash': cash_amount,
            'cards': card_amounts,
            'total_tendered': cash_amount + sum(card_amounts),
            'cash_discount': split_cash_discount
        }
    
    # Generate order number
    order_number = generate_order_number()
    
    conn = get_db_connection()
    try:
        # Create order
        cursor = conn.execute('''
            INSERT INTO pos_orders (
                order_number, status, subtotal, tax_rate, tax_amount,
                discount_amount, discount_type, discount_reason, total,
                payment_method, payment_details, operator_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            order_number, 'completed', round(subtotal, 2), tax_rate, round(tax_amount, 2),
            round(order_discount, 2), cart.get('discount_type'), cart.get('discount_reason', ''),
            total, payment_method, json.dumps(payment_details), current_user.id
        ))
        order_id = cursor.lastrowid
        
        # Create order items and decrement inventory
        for item in cart['items']:
            conn.execute('''
                INSERT INTO pos_order_items (
                    order_id, inventory_item_id, sku, name, quantity,
                    unit_price, discount_amount, discount_type, line_total
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                order_id, item.get('inventory_item_id'), item['sku'], item['name'],
                item['quantity'], item['unit_price'], item.get('discount_amount', 0),
                item.get('discount_type'), item['line_total']
            ))
            
            # Decrement inventory
            if item.get('inventory_item_id'):
                conn.execute('''
                    UPDATE inventory_items 
                    SET quantity = quantity - ? 
                    WHERE id = ?
                ''', (item['quantity'], item['inventory_item_id']))
                
                # Log inventory transaction
                conn.execute('''
                    INSERT INTO inventory_transactions (
                        inventory_item_id, quantity_change, reason, user_id, source_tracking
                    ) VALUES (?, ?, ?, ?, ?)
                ''', (
                    item['inventory_item_id'], -item['quantity'], 
                    'Sold/Consumed', str(current_user.id), order_number
                ))
        
        conn.commit()
        clear_cart()
        
        logger.info(f"Order {order_number} completed by {current_user.username}, total: ${total}")
        flash(f'Order {order_number} completed! Total: ${total:.2f}')
        
        return redirect(url_for('pos.receipt', order_number=order_number))
        
    except Exception as e:
        conn.rollback()
        error_details = traceback.format_exc()
        logger.error(f"Checkout error: {e}\n{error_details}")
        flash(f'Error processing order: {str(e)}')
        return redirect(url_for('pos.checkout'))
    finally:
        conn.close()


@pos_bp.route('/receipt/<order_number>')
@login_required
def receipt(order_number):
    """View order receipt."""
    conn = get_request_db()
    
    order = conn.execute('''
        SELECT o.*, u.username as operator_name
        FROM pos_orders o
        LEFT JOIN users u ON o.operator_id = u.id
        WHERE o.order_number = ?
    ''', (order_number,)).fetchone()
    
    if not order:
        flash('Order not found.')
        return redirect(url_for('pos.sales'))
    
    items = conn.execute('''
        SELECT * FROM pos_order_items WHERE order_id = ?
    ''', (order['id'],)).fetchall()
    
    # Parse payment details
    payment_details = {}
    if order['payment_details']:
        try:
            payment_details = json.loads(order['payment_details'])
        except json.JSONDecodeError:
            pass  # Invalid payment details JSON
            
    # Get branding settings
    from app.routes.pos.core import get_pos_setting
    from app.services.data_manager import load_config
    config = load_config() or {}
    
    return render_template('pos/receipt.html',
                           order=order,
                           items=items,
                           payment_details=payment_details,
                           org_name=config.get('ORGANIZATION_NAME', ''),
                           receipt_store_name=get_pos_setting('RECEIPT_STORE_NAME', ''),
                           receipt_header=get_pos_setting('RECEIPT_HEADER', ''),
                           receipt_footer=get_pos_setting('RECEIPT_FOOTER', ''),
                           receipt_store_name_bold=get_pos_setting('RECEIPT_STORE_NAME_BOLD', 'false') == 'true',
                           receipt_store_name_italic=get_pos_setting('RECEIPT_STORE_NAME_ITALIC', 'false') == 'true',
                           receipt_header_bold=get_pos_setting('RECEIPT_HEADER_BOLD', 'false') == 'true',
                           receipt_header_italic=get_pos_setting('RECEIPT_HEADER_ITALIC', 'false') == 'true',
                           receipt_footer_bold=get_pos_setting('RECEIPT_FOOTER_BOLD', 'false') == 'true',
                           receipt_footer_italic=get_pos_setting('RECEIPT_FOOTER_ITALIC', 'false') == 'true')


@pos_bp.route('/receipt/<order_number>/print')
@login_required
def receipt_print(order_number):
    """Printable receipt view."""
    conn = get_request_db()
    
    order = conn.execute('''
        SELECT o.*, u.username as operator_name
        FROM pos_orders o
        LEFT JOIN users u ON o.operator_id = u.id
        WHERE o.order_number = ?
    ''', (order_number,)).fetchone()
    
    if not order:
        flash('Order not found.')
        return redirect(url_for('pos.sales'))
    
    items = conn.execute('''
        SELECT * FROM pos_order_items WHERE order_id = ?
    ''', (order['id'],)).fetchall()
    
    payment_details = {}
    if order['payment_details']:
        try:
            payment_details = json.loads(order['payment_details'])
        except json.JSONDecodeError:
            pass  # Invalid payment details JSON
            
    # Get branding settings
    from app.routes.pos.core import get_pos_setting
    from app.services.data_manager import load_config
    config = load_config() or {}
    
    return render_template('pos/receipt_print.html',
                           order=order,
                           items=items,
                           payment_details=payment_details,
                           org_name=config.get('ORGANIZATION_NAME', ''),
                           receipt_store_name=get_pos_setting('RECEIPT_STORE_NAME', ''),
                           receipt_header=get_pos_setting('RECEIPT_HEADER', ''),
                           receipt_footer=get_pos_setting('RECEIPT_FOOTER', ''),
                           receipt_store_name_bold=get_pos_setting('RECEIPT_STORE_NAME_BOLD', 'false') == 'true',
                           receipt_store_name_italic=get_pos_setting('RECEIPT_STORE_NAME_ITALIC', 'false') == 'true',
                           receipt_header_bold=get_pos_setting('RECEIPT_HEADER_BOLD', 'false') == 'true',
                           receipt_header_italic=get_pos_setting('RECEIPT_HEADER_ITALIC', 'false') == 'true',
                           receipt_footer_bold=get_pos_setting('RECEIPT_FOOTER_BOLD', 'false') == 'true',
                           receipt_footer_italic=get_pos_setting('RECEIPT_FOOTER_ITALIC', 'false') == 'true')


@pos_bp.route('/order/<order_number>/delete', methods=['POST'])
@login_required
def delete_order(order_number):
    """Delete an order (admin only)."""
    # Admin check
    if not current_user.is_admin:
        flash('Only administrators can delete orders.')
        return redirect(url_for('pos.receipt', order_number=order_number))
    
    restore_inventory = request.form.get('restore_inventory') == 'on'
    
    from app.services.db import get_db_connection
    conn = get_db_connection()
    
    try:
        # Find the order
        order = conn.execute('SELECT id FROM pos_orders WHERE order_number = ?', (order_number,)).fetchone()
        
        if not order:
            flash('Order not found.')
            return redirect(url_for('pos.management'))
        
        order_id = order['id']
        
        # If restoring inventory, get the order items and restore quantities
        if restore_inventory:
            order_items = conn.execute('''
                SELECT sku, quantity FROM pos_order_items WHERE order_id = ?
            ''', (order_id,)).fetchall()
            
            restored_count = 0
            for item in order_items:
                if item['sku']:
                    # Add quantity back to inventory
                    conn.execute('''
                        UPDATE inventory_items 
                        SET quantity = quantity + ? 
                        WHERE sku = ?
                    ''', (item['quantity'], item['sku']))
                    restored_count += item['quantity']
            
            logger = logging.getLogger(__name__)
            logger.info(f"Restored {restored_count} units to inventory for order {order_number}")
        
        # Delete associated items
        conn.execute('DELETE FROM pos_order_items WHERE order_id = ?', (order_id,))
        
        # Delete associated refunds
        conn.execute('DELETE FROM pos_refunds WHERE order_id = ?', (order_id,))
        
        # Delete the order
        conn.execute('DELETE FROM pos_orders WHERE id = ?', (order_id,))
        
        conn.commit()
        
        logger = logging.getLogger(__name__)
        logger.info(f"Order {order_number} deleted by admin {current_user.username}" + 
                   (" with inventory restored" if restore_inventory else ""))
        
        if restore_inventory:
            flash(f'Order {order_number} deleted and inventory quantities restored.')
        else:
            flash(f'Order {order_number} has been permanently deleted.')
        return redirect(url_for('pos.sales_history'))
        
    except Exception as e:
        conn.rollback()
        logger = logging.getLogger(__name__)
        logger.error(f"Error deleting order {order_number}: {e}")
        flash(f'Error deleting order: {str(e)}')
        return redirect(url_for('pos.receipt', order_number=order_number))
    finally:
        conn.close()

