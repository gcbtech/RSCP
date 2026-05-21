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
    generate_order_number, round_money, calculate_percentage
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
            order_discount = calculate_percentage(subtotal, cart['discount_amount'])
        else:
            order_discount = cart['discount_amount']
    
    # Apply coupon discount
    coupon_discount = 0
    if cart.get('applied_coupon'):
        try:
            coupon_discount = float(cart['applied_coupon'].get('discount', 0))
        except (ValueError, TypeError):
            logger.error(f"Invalid coupon discount in session: {cart['applied_coupon']}")
            cart['applied_coupon'] = None
            from app.routes.pos.core import save_cart
            save_cart(cart)
    
    discounted_subtotal = max(0, subtotal - order_discount - coupon_discount)
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
            cash_discount_value = calculate_percentage(discounted_subtotal, cash_discount_amount)
        else:
            cash_discount_value = round_money(min(cash_discount_amount, discounted_subtotal))
    
    return render_template('pos/checkout.html',
                           cart=cart,
                           subtotal=round(subtotal, 2),
                           order_discount=round(order_discount, 2),
                           coupon_discount=round(coupon_discount, 2),
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
            order_discount = calculate_percentage(subtotal, cart['discount_amount'])
        else:
            order_discount = cart['discount_amount']
    
    # Apply coupon discount
    coupon_discount = 0
    if cart.get('applied_coupon'):
        try:
            coupon_discount = float(cart['applied_coupon'].get('discount', 0))
        except (ValueError, TypeError):
            # If invalid here, just ignore it to allow checkout to proceed
            coupon_discount = 0
            
    discounted_subtotal = max(0, subtotal - order_discount - coupon_discount)
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
                    cash_discount_applied = calculate_percentage(discounted_subtotal, cash_discount_amount)
                else:
                    cash_discount_applied = round_money(min(cash_discount_amount, discounted_subtotal))
                
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
                    split_cash_discount = calculate_percentage(cash_amount, cash_discount_amount)
                else:
                    # Apply fixed discount proportionally to cash portion
                    cash_proportion = cash_amount / total if total > 0 else 0
                    split_cash_discount = round_money(min(cash_discount_amount * cash_proportion, cash_amount))
                
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
                
                # Check for "End Sale on Out of Stock" condition
                updated_item = conn.execute('''
                    SELECT quantity, sale_enabled, sale_end_on_stock 
                    FROM inventory_items WHERE id = ?
                ''', (item['inventory_item_id'],)).fetchone()
                
                if updated_item and updated_item['quantity'] <= 0:
                    if updated_item['sale_enabled'] and updated_item['sale_end_on_stock']:
                        conn.execute('''
                            UPDATE inventory_items SET sale_enabled = 0 WHERE id = ?
                        ''', (item['inventory_item_id'],))
                        logger.info(f"Sale auto-ended for item {item['inventory_item_id']} due to stock depletion.")
        
        # Record coupon redemption if any
        if cart.get('applied_coupon'):
            coupon_id = cart['applied_coupon'].get('id')
            coupon_code = cart['applied_coupon'].get('code')
            if coupon_id:
                conn.execute('''
                    INSERT INTO pos_coupon_redemptions (
                        coupon_id, order_id, serial_used, discount_applied, redeemed_by
                    ) VALUES (?, ?, ?, ?, ?)
                ''', (coupon_id, order_id, coupon_code, round(coupon_discount, 2), current_user.id))
                
                conn.execute('''
                    UPDATE pos_coupons 
                    SET current_uses = COALESCE(current_uses, 0) + 1 
                    WHERE id = ?
                ''', (coupon_id,))
                logger.info(f"Recorded redemption of coupon ID {coupon_id} for order {order_number}")
        
        conn.commit()
        clear_cart()
        
        logger.info(f"Order {order_number} completed by {current_user.username}, total: ${total}")
        flash(f'Order {order_number} completed! Total: ${total:.2f}')
        
        # Build redirect URL with optional params for auto-print and cash drawer
        redirect_params = {}
        
        # Check if auto-print is enabled
        auto_print_enabled = get_pos_setting('AUTO_PRINT_RECEIPT', 'false') == 'true'
        if auto_print_enabled:
            redirect_params['auto_print'] = '1'
        
        # Check if cash drawer should open (only for cash or split with cash)
        if get_pos_setting('CASH_DRAWER_ENABLED', 'false') == 'true':
            has_cash = payment_method == 'cash' or (
                payment_method == 'split' and payment_details.get('cash', 0) > 0
            )
            if has_cash:
                redirect_params['open_drawer'] = '1'
        
        if auto_print_enabled:
            return redirect(url_for('pos.sales', auto_print_order=order_number, **redirect_params))
        else:
            return redirect(url_for('pos.receipt', order_number=order_number, **redirect_params))
        
    except Exception as e:
        conn.rollback()
        error_details = traceback.format_exc()
        logger.error(f"Checkout error: {e}\n{error_details}")
        flash(f'Error processing order: {str(e)}')
        return redirect(url_for('pos.checkout'))
    finally:
        conn.close()


def _prepare_receipt_data(conn, order):
    """Helper to load items, coupon, and calculate discount details for receipts."""
    # Load items and convert to list of dicts to allow custom properties
    items_rows = conn.execute('''
        SELECT oi.*, ii.addon_1, ii.addon_2, ii.sell_price as current_reg_price
        FROM pos_order_items oi
        LEFT JOIN inventory_items ii ON oi.inventory_item_id = ii.id
        WHERE oi.order_id = ?
    ''', (order['id'],)).fetchall()
    
    items = [dict(row) for row in items_rows]
    
    # Fetch coupon redemption if any
    redemption_row = conn.execute('''
        SELECT cr.*, c.name as coupon_name, c.code as coupon_code, c.discount_type as coupon_discount_type, c.discount_value as coupon_discount_value,
               c.buy_quantity, c.get_quantity, c.reward_item_id
        FROM pos_coupon_redemptions cr
        JOIN pos_coupons c ON cr.coupon_id = c.id
        WHERE cr.order_id = ?
    ''', (order['id'],)).fetchone()
    
    coupon_redemption = dict(redemption_row) if redemption_row else None
    
    # Track item coupon discounts if coupon is item-specific
    if coupon_redemption and coupon_redemption['coupon_discount_type'] in ('item_dollar', 'item_percent', 'bogo_free', 'bogo_percent', 'bogo_cross'):
        try:
            targets = conn.execute('''
                SELECT item_id FROM pos_coupon_items WHERE coupon_id = ?
            ''', (coupon_redemption['coupon_id'],)).fetchall()
            target_ids = [t['item_id'] for t in targets]
        except Exception as e:
            logger.error(f"Error loading target items for coupon: {e}")
            target_ids = []
            
        # Calculate individual item coupon discounts
        for item in items:
            item['coupon_discount'] = 0
            if coupon_redemption['coupon_discount_type'] == 'bogo_cross':
                if item['inventory_item_id'] == coupon_redemption['reward_item_id']:
                    item['coupon_discount'] = item['unit_price']
            elif item['inventory_item_id'] in target_ids:
                item_total = item['unit_price'] * item['quantity']
                if coupon_redemption['coupon_discount_type'] == 'item_dollar':
                    item['coupon_discount'] = min(coupon_redemption['coupon_discount_value'], item_total)
                elif coupon_redemption['coupon_discount_type'] == 'item_percent':
                    item['coupon_discount'] = round((item_total * coupon_redemption['coupon_discount_value']) / 100, 2)
                elif coupon_redemption['coupon_discount_type'] == 'bogo_free':
                    free_qty = item['quantity'] // (coupon_redemption['buy_quantity'] + 1)
                    item['coupon_discount'] = round(free_qty * item['unit_price'], 2)
                elif coupon_redemption['coupon_discount_type'] == 'bogo_percent':
                    discounted_qty = item['quantity'] // (coupon_redemption['buy_quantity'] + 1)
                    base_amount = discounted_qty * item['unit_price']
                    item['coupon_discount'] = round((base_amount * coupon_redemption['coupon_discount_value']) / 100, 2)
    else:
        for item in items:
            item['coupon_discount'] = 0
            
    # Calculate receipt-level summary statistics
    # True subtotal is before any item-level manual discounts
    subtotal_before_discounts = sum(item['unit_price'] * item['quantity'] for item in items)
    
    # Total item discount is the sum of manual item discounts
    total_item_discount = sum((item['unit_price'] * item['quantity']) - item['line_total'] for item in items)
    
    return items, coupon_redemption, subtotal_before_discounts, total_item_discount


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
    
    items, coupon_redemption, subtotal_before_discounts, total_item_discount = _prepare_receipt_data(conn, order)
    
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
                           coupon_redemption=coupon_redemption,
                           subtotal_before_discounts=round(subtotal_before_discounts, 2),
                           total_item_discount=round(total_item_discount, 2),
                           payment_details=payment_details,
                           config=config,
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
    
    items, coupon_redemption, subtotal_before_discounts, total_item_discount = _prepare_receipt_data(conn, order)
    
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
                           coupon_redemption=coupon_redemption,
                           subtotal_before_discounts=round(subtotal_before_discounts, 2),
                           total_item_discount=round(total_item_discount, 2),
                           payment_details=payment_details,
                           config=config,
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

