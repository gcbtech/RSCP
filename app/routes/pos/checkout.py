"""
POS Checkout Module
Payment processing, order finalization, and receipt generation.
"""
import logging
import json
import secrets
import traceback
from datetime import datetime
from flask import request, redirect, url_for, flash, render_template, make_response, session
from flask_login import current_user, login_required

from app.routes.pos import pos_bp, PAYMENT_METHODS
from app.utils.permissions import require_permission
from app.routes.pos.core import (
    get_cart, mutate_cart, current_cart_version, resolve_register_id,
    generate_order_number, compute_cart_totals, pricing_config, get_pos_setting
)
from app.services import pos_checkout, pos_sales
from app.services.db import get_db_connection, get_request_db
from app.services.pos_ledger import order_payments
from app.services.pos_pricing import parse_money, parse_money_field

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
                           cart_version=current_cart_version(),
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
    """Record the sale (app.services.pos_checkout.complete_sale: one write
    transaction from re-reading the cart to emptying it) and map the outcome
    to flashes/redirects."""
    register_id = resolve_register_id()
    cart = get_cart()

    if not cart['items']:
        flash('Cart is empty.')
        return redirect(url_for('pos.sales'))

    payment_method = request.form.get('payment_method', 'cash')
    if payment_method not in PAYMENT_METHODS:
        flash('Unknown payment method.')
        return redirect(url_for('pos.checkout'))

    payment = pos_checkout.Payment(
        method=payment_method,
        tendered=request.form.get('tendered'),
        cash_amount=request.form.get('cash_amount'),
        card_amounts=request.form.getlist('card_amount'),
    )
    cart_source = (pos_checkout.RegisterCart(register_id) if register_id
                   else pos_checkout.SessionCart(cart))

    conn = get_db_connection()
    conn.isolation_level = None
    try:
        sale = pos_checkout.complete_sale(
            conn, cart_source, payment, current_user,
            token=request.form.get('checkout_token'),
            expected_total=request.form.get('expected_total'),
            expected_version=request.form.get('cart_version'),
            config=pricing_config(),
            # looked up at call time so tests can intercept it
            order_number_fn=lambda c: generate_order_number(conn=c),
        )
    except pos_checkout.CheckoutError as e:
        if isinstance(e, pos_checkout.CouponInvalid):
            # Drop only the coupon; never write back the checkout snapshot
            mutate_cart(lambda c: c.update(applied_coupon=None) if c.get('applied_coupon') else None)
        if isinstance(e, pos_checkout.CouponLimitReached):
            logger.error(f"Checkout error: {e}")
        flash(e.message)
        if e.redirect_to == 'receipt':
            return redirect(url_for('pos.receipt', order_number=e.order_number))
        return redirect(url_for('pos.sales' if e.redirect_to == 'sales' else 'pos.checkout'))
    except Exception as e:
        logger.error(f"Checkout error: {e}\n{traceback.format_exc()}")
        flash(f'Error processing order: {str(e)}')
        return redirect(url_for('pos.checkout'))
    finally:
        conn.close()

    if not register_id:
        session.pop('pos_cart', None)
        session.modified = True

    order_number = sale.order_number
    flash(f'Order {order_number} completed! Total: ${sale.total:.2f}')

    # Redirect with optional auto-print and cash-drawer flags
    redirect_params = {}
    auto_print_enabled = get_pos_setting('AUTO_PRINT_RECEIPT', 'false') == 'true'
    if auto_print_enabled:
        redirect_params['auto_print'] = '1'

    # Open the drawer only for cash, or a split with cash
    if get_pos_setting('CASH_DRAWER_ENABLED', 'false') == 'true':
        has_cash = sale.payment_method == 'cash' or (
            sale.payment_method == 'split' and sale.payment_details.get('cash', 0) > 0
        )
        if has_cash:
            redirect_params['open_drawer'] = '1'

    if auto_print_enabled:
        return redirect(url_for('pos.sales', auto_print_order=order_number, **redirect_params))
    return redirect(url_for('pos.receipt', order_number=order_number, **redirect_params))


# Form money parsing now lives in pos_pricing (kept here for importers)
_money_value = parse_money


def _money_field(name, default=None):
    return parse_money_field(request.form.get(name), default)


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
    """Delete an order (admin only): pos_sales.delete_order."""
    restore_inventory = request.form.get('restore_inventory') == 'on'
    conn = get_db_connection()
    try:
        pos_sales.delete_order(conn, order_number, restore_inventory, current_user,
                               ip=request.remote_addr)
    except pos_sales.OrderNotFound:
        flash('Order not found.')
        return redirect(url_for('pos.management'))
    except Exception as e:
        logger.error(f"Error deleting order {order_number}: {e}")
        flash(f'Error deleting order: {str(e)}')
        return redirect(url_for('pos.receipt', order_number=order_number))
    finally:
        conn.close()

    if restore_inventory:
        flash(f'Order {order_number} deleted and inventory quantities restored.')
    else:
        flash(f'Order {order_number} has been permanently deleted.')
    return redirect(url_for('pos.sales_history'))
