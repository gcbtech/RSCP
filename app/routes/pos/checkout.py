"""
POS Checkout Module
Payment processing, order finalization, and receipt generation.
"""
import logging
import json
import secrets
import traceback
from datetime import datetime
from flask import request, redirect, url_for, flash, render_template, make_response
from flask_login import current_user, login_required

from app.routes.pos import pos_bp, PAYMENT_METHODS
from app.utils.permissions import require_permission
from app.routes.pos.core import (
    get_cart, save_cart, clear_cart, get_tax_rate, calculate_tax,
    generate_order_number, round_money, calculate_percentage, compute_cart_totals,
    get_pos_setting
)
from app.services.db import get_db_connection, get_request_db
from app.services.pos_ledger import allocate_payments, write_payments, order_payments

logger = logging.getLogger(__name__)


@pos_bp.route('/checkout')
def checkout():
    """Checkout page with payment options."""
    cart = get_cart()
    
    if not cart['items']:
        flash('Cart is empty.')
        return redirect(url_for('pos.sales'))
    
    # Same math as every other screen (compute_cart_totals), so the page,
    # the customer display and the recorded sale cannot disagree.
    totals = compute_cart_totals(cart)

    from app.routes.pos.core import get_pos_setting
    cash_discount_amount = float(get_pos_setting('CASH_DISCOUNT_AMOUNT', '0') or 0)
    cash_discount_type = get_pos_setting('CASH_DISCOUNT_TYPE', 'percent')

    return render_template('pos/checkout.html',
                           cart=cart,
                           subtotal=totals['subtotal'],
                           order_discount=totals['order_discount'],
                           coupon_discount=totals['coupon_discount'],
                           tax_rate=totals['tax_rate_pct'],
                           tax_amount=totals['tax_amount'],
                           total=totals['card_total'],
                           payment_methods=PAYMENT_METHODS,
                           cash_discount_enabled=totals['cash_discount_enabled'],
                           cash_discount_amount=cash_discount_amount,
                           cash_discount_type=cash_discount_type,
                           cash_discount_value=totals['cash_discount_value'],
                           # One token per rendered checkout: a double-click or a
                           # retried POST records the sale once.
                           checkout_token=secrets.token_urlsafe(16))


@pos_bp.route('/checkout/process', methods=['POST'])
def checkout_process():
    """Record the sale.

    Everything from re-validating the coupon to decrementing stock happens in
    one write transaction (BEGIN IMMEDIATE), so concurrent registers cannot
    interleave: order numbers stay unique, a single-use coupon is redeemed
    once, and a resubmitted form (same checkout_token) is recorded once.
    """
    cart = get_cart()

    if not cart['items']:
        flash('Cart is empty.')
        return redirect(url_for('pos.sales'))

    payment_method = request.form.get('payment_method', 'cash')
    if payment_method not in PAYMENT_METHODS:
        flash('Unknown payment method.')
        return redirect(url_for('pos.checkout'))
    checkout_token = (request.form.get('checkout_token') or '').strip() or None

    conn = get_db_connection()
    conn.isolation_level = None
    try:
        conn.execute('BEGIN IMMEDIATE')

        # Already recorded? (double-click, or a retry after a lost response)
        if checkout_token:
            existing = conn.execute('SELECT order_number FROM pos_orders WHERE checkout_token = ?',
                                    (checkout_token,)).fetchone()
            if existing:
                conn.execute('ROLLBACK')
                flash(f"Order {existing['order_number']} was already completed.")
                return redirect(url_for('pos.receipt', order_number=existing['order_number']))

        # Re-validate the coupon against the cart as it is NOW. The discount
        # stored on the cart was computed when the coupon was applied; items
        # may have been removed since, and limits must be checked under the lock.
        coupon = cart.get('applied_coupon')
        if coupon:
            from app.routes.pos.coupons import evaluate_coupon
            subtotal_now = sum(item.get('line_total', 0) for item in cart['items'])
            result = evaluate_coupon(coupon.get('code'), cart['items'], subtotal_now, conn, cart=cart)
            if not result.get('valid'):
                conn.execute('ROLLBACK')
                cart['applied_coupon'] = None
                save_cart(cart)
                flash(f"Coupon removed: {result.get('error', 'no longer valid')} Please review the total.")
                return redirect(url_for('pos.checkout'))
            coupon['discount'] = result['discount_amount']

        totals = compute_cart_totals(cart)
        subtotal = totals['subtotal']
        order_discount = totals['order_discount']
        coupon_discount = totals['coupon_discount']
        tax_amount = totals['tax_amount']
        tax_rate = get_tax_rate()
        total = totals['card_total']

        # The cashier quoted the total shown on the checkout page. If the cart
        # changed since (e.g. a paired scanner added an item, or the coupon
        # was recomputed), stop rather than record a different amount.
        expected = request.form.get('expected_total')
        if expected:
            try:
                if abs(float(expected) - total) > 0.005:
                    conn.execute('ROLLBACK')
                    flash('The cart changed since this checkout page opened. Please review the new total.')
                    return redirect(url_for('pos.checkout'))
            except ValueError:
                pass

        payment_details = {}
        cash_discount_applied = 0.0
        if payment_method == 'cash':
            cash_discount_applied = totals['cash_discount_value'] if totals['cash_discount_enabled'] else 0
            total = round_money(total - cash_discount_applied)
            tendered = _money_field('tendered', default=total)
            if tendered is None or tendered < total - 0.01:
                conn.execute('ROLLBACK')
                flash(f'Cash tendered must be at least ${total:.2f}.')
                return redirect(url_for('pos.checkout'))
            payment_details = {
                'tendered': tendered,
                'change': round_money(max(0, tendered - total)),
                'cash_discount': cash_discount_applied,
            }
        elif payment_method == 'split':
            cash_amount = _money_field('cash_amount', default=0)
            card_amounts = [_money_value(a) for a in request.form.getlist('card_amount') if a]
            if cash_amount is None or any(a is None for a in card_amounts):
                conn.execute('ROLLBACK')
                flash('Invalid split payment amounts.')
                return redirect(url_for('pos.checkout'))

            # Cash discount applies to the cash portion only
            split_cash_discount = 0
            if totals['cash_discount_enabled'] and cash_amount > 0:
                cd_amount = float(get_pos_setting('CASH_DISCOUNT_AMOUNT', '0') or 0)
                cd_type = get_pos_setting('CASH_DISCOUNT_TYPE', 'percent')
                if cd_amount > 0:
                    if cd_type == 'percent':
                        split_cash_discount = calculate_percentage(cash_amount, cd_amount)
                    else:
                        cash_proportion = cash_amount / total if total > 0 else 0
                        split_cash_discount = round_money(min(cd_amount * cash_proportion, cash_amount))
                    total = round_money(total - split_cash_discount)

            total_tendered = round_money(cash_amount + sum(card_amounts))
            if total_tendered < total - 0.01:
                conn.execute('ROLLBACK')
                flash(f'Split payments add up to ${total_tendered:.2f}, less than the ${total:.2f} total.')
                return redirect(url_for('pos.checkout'))
            # Change can only be given in cash: cards may not exceed the total
            if round_money(sum(card_amounts)) > total + 0.005:
                conn.execute('ROLLBACK')
                flash(f'Card amounts add up to more than the ${total:.2f} total.')
                return redirect(url_for('pos.checkout'))
            cash_discount_applied = split_cash_discount
            payment_details = {
                'cash': cash_amount,
                'cards': card_amounts,
                'total_tendered': total_tendered,
                'change': round_money(min(cash_amount, max(0, total_tendered - total))),
                'cash_discount': split_cash_discount,
            }

        order_number = generate_order_number(conn=conn)

        # Create order
        cursor = conn.execute('''
            INSERT INTO pos_orders (
                order_number, status, subtotal, tax_rate, tax_amount,
                discount_amount, discount_type, discount_reason, total,
                payment_method, payment_details, operator_id, checkout_token, cash_discount
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            order_number, 'completed', round(subtotal, 2), tax_rate, round(tax_amount, 2),
            round(order_discount, 2), cart.get('discount_type') if order_discount else None,
            cart.get('discount_reason', ''),
            total, payment_method, json.dumps(payment_details), current_user.id, checkout_token,
            round_money(cash_discount_applied)
        ))
        order_id = cursor.lastrowid

        # Payments ledger: what each tender paid toward the sale, plus cash
        # tendered and change given
        write_payments(conn, order_id, allocate_payments(payment_method, payment_details, total))

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
                
                used = conn.execute('''
                    UPDATE pos_coupons
                    SET current_uses = COALESCE(current_uses, 0) + 1
                    WHERE id = ?
                      AND (max_uses IS NULL OR max_uses = '' OR CAST(max_uses AS INTEGER) <= 0
                           OR COALESCE(current_uses, 0) < CAST(max_uses AS INTEGER))
                ''', (coupon_id,)).rowcount
                if not used:
                    raise ValueError('Coupon has reached its maximum number of uses.')
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
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        error_details = traceback.format_exc()
        logger.error(f"Checkout error: {e}\n{error_details}")
        flash(f'Error processing order: {str(e)}')
        return redirect(url_for('pos.checkout'))
    finally:
        conn.close()


def _money_value(raw):
    """Parse a money form value; None if it is not a finite, non-negative number."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value < 0 or value == float('inf'):
        return None
    return round_money(value)


def _money_field(name, default=None):
    raw = request.form.get(name)
    if raw is None or str(raw).strip() == '':
        return default
    return _money_value(raw)


def _prepare_receipt_data(conn, order):
    """Everything a receipt shows, from what was stored at the time of sale.

    Discounts come from the order itself: line discounts from each line's
    stored line_total, the coupon from pos_coupon_redemptions.discount_applied,
    the cash discount from pos_orders.cash_discount -- never from today's
    coupon or settings definitions. Tenders come from the payments ledger.
    """
    items = [dict(row) for row in conn.execute('''
        SELECT oi.*, ii.addon_1, ii.addon_2, ii.sell_price as current_reg_price
        FROM pos_order_items oi
        LEFT JOIN inventory_items ii ON oi.inventory_item_id = ii.id
        WHERE oi.order_id = ?
    ''', (order['id'],)).fetchall()]

    redemption_row = conn.execute('''
        SELECT cr.*, c.name as coupon_name, COALESCE(cr.serial_used, c.code) as coupon_code
        FROM pos_coupon_redemptions cr
        LEFT JOIN pos_coupons c ON cr.coupon_id = c.id
        WHERE cr.order_id = ?
    ''', (order['id'],)).fetchone()
    coupon_redemption = dict(redemption_row) if redemption_row else None

    payment_details = {}
    if order['payment_details']:
        try:
            payment_details = json.loads(order['payment_details']) or {}
        except (json.JSONDecodeError, TypeError):
            payment_details = {}  # Invalid payment details JSON

    try:
        cash_discount = float(order['cash_discount'] or 0)
    except (IndexError, KeyError, TypeError, ValueError):
        cash_discount = 0.0
    if not cash_discount and isinstance(payment_details, dict):
        try:
            cash_discount = float(payment_details.get('cash_discount') or 0)
        except (TypeError, ValueError):
            cash_discount = 0.0

    # Subtotal before any line discount, and the line discounts as stored
    subtotal_before_discounts = sum(item['unit_price'] * item['quantity'] for item in items)
    total_item_discount = sum((item['unit_price'] * item['quantity']) - item['line_total'] for item in items)

    return {
        'items': items,
        'coupon_redemption': coupon_redemption,
        'subtotal_before_discounts': round(subtotal_before_discounts, 2),
        'total_item_discount': round(total_item_discount, 2),
        'cash_discount': round(cash_discount, 2),
        'payment_details': payment_details,
        'payments': order_payments(conn, order['id']),
    }


def _receipt_branding():
    from app.services.data_manager import load_config
    config = load_config() or {}
    return dict(
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
        receipt_footer_italic=get_pos_setting('RECEIPT_FOOTER_ITALIC', 'false') == 'true',
    )


def _load_receipt_order(conn, order_number):
    return conn.execute('''
        SELECT o.*, u.username as operator_name
        FROM pos_orders o
        LEFT JOIN users u ON o.operator_id = u.id
        WHERE o.order_number = ?
    ''', (order_number,)).fetchone()


@pos_bp.route('/receipt/<order_number>')
def receipt(order_number):
    """View order receipt."""
    conn = get_request_db()
    order = _load_receipt_order(conn, order_number)
    if not order:
        flash('Order not found.')
        return redirect(url_for('pos.sales'))
    return render_template('pos/receipt.html', order=order,
                           **_prepare_receipt_data(conn, order), **_receipt_branding())


@pos_bp.route('/receipt/<order_number>/print')
def receipt_print(order_number):
    """Printable receipt view."""
    conn = get_request_db()
    order = _load_receipt_order(conn, order_number)
    if not order:
        flash('Order not found.')
        return redirect(url_for('pos.sales'))
    return render_template('pos/receipt_print.html', order=order,
                           **_prepare_receipt_data(conn, order), **_receipt_branding())


@pos_bp.route('/order/<order_number>/delete', methods=['POST'])
@require_permission('admin.manage')
def delete_order(order_number):
    """Delete an order (admin only)."""
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
        
        # If restoring inventory, put back what actually left the shelf: the
        # units sold minus any a refund already restocked. Match on the
        # inventory item id (the order line's sku is whatever code was
        # scanned, often a UPC, so matching on it silently restored nothing).
        if restore_inventory:
            order_items = conn.execute('''
                SELECT oi.inventory_item_id, oi.quantity,
                       COALESCE((SELECT SUM(ri.quantity) FROM pos_refund_items ri
                                 WHERE ri.order_item_id = oi.id AND ri.restock_action = 'restock'), 0)
                           AS restocked
                FROM pos_order_items oi WHERE oi.order_id = ?
            ''', (order_id,)).fetchall()

            restored_count = 0
            for item in order_items:
                units = item['quantity'] - item['restocked']
                if item['inventory_item_id'] and units > 0:
                    conn.execute('UPDATE inventory_items SET quantity = quantity + ? WHERE id = ?',
                                 (units, item['inventory_item_id']))
                    conn.execute('''
                        INSERT INTO inventory_transactions
                            (inventory_item_id, quantity_change, reason, user_id, source_tracking)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (item['inventory_item_id'], units, 'Order Deleted', str(current_user.id), order_number))
                    restored_count += units

            logger = logging.getLogger(__name__)
            logger.info(f"Restored {restored_count} units to inventory for order {order_number}")

        # Delete everything that hangs off the order (foreign keys are not
        # enforced, so nothing cascades on its own)
        conn.execute('''DELETE FROM pos_refund_items
                        WHERE refund_id IN (SELECT id FROM pos_refunds WHERE order_id = ?)''', (order_id,))
        conn.execute('DELETE FROM pos_refunds WHERE order_id = ?', (order_id,))
        conn.execute('DELETE FROM pos_payments WHERE order_id = ?', (order_id,))
        for r in conn.execute('SELECT coupon_id FROM pos_coupon_redemptions WHERE order_id = ?',
                              (order_id,)).fetchall():
            conn.execute('''UPDATE pos_coupons SET current_uses = MAX(COALESCE(current_uses, 0) - 1, 0)
                            WHERE id = ?''', (r['coupon_id'],))
        conn.execute('DELETE FROM pos_coupon_redemptions WHERE order_id = ?', (order_id,))
        conn.execute('DELETE FROM pos_order_items WHERE order_id = ?', (order_id,))
        conn.execute('DELETE FROM pos_orders WHERE id = ?', (order_id,))

        # Audit trail: a hard delete otherwise leaves no record at all
        conn.execute('''
            INSERT INTO pos_audit_log (action, user_id, user_name, target_type, target_id, details, ip_address)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', ('delete_order', current_user.id, current_user.username, 'order', order_number,
              json.dumps({'restore_inventory': restore_inventory}), request.remote_addr))

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

