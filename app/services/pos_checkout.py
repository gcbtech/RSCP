"""
POS checkout: record a sale.

complete_sale() does everything from re-reading the cart to emptying it in
one write transaction (BEGIN IMMEDIATE), so concurrent registers cannot
interleave: order numbers stay unique, a single-use coupon is redeemed once,
and a resubmitted form (same checkout token) is recorded once.

A register cart is re-read INSIDE that transaction and emptied in it too:
what is sold is exactly what is cleared. An item a paired scanner adds after
the checkout page opened either changes the total/version (and the sale is
refused for review) or lands after the commit in the next, empty cart -- it
is never sold unseen or silently wiped.

No Flask request/session here: the route passes the cart source, the raw
payment inputs, the acting user and the pricing settings, and maps the
typed errors to its flashes/redirects.
"""
import json
import logging
from collections import namedtuple
from datetime import date

from app.services import inventory, money, pos_pricing, pos_registers
from app.services.pos_ledger import allocate_payments, write_payments
from app.services.pos_pricing import parse_money, parse_money_field

logger = logging.getLogger(__name__)

PAYMENT_METHODS = ('cash', 'card', 'split')


# ---------------------------------------------------------------------------
# Inputs / results
# ---------------------------------------------------------------------------

# Raw tender inputs as posted (strings or None); parsed and validated here.
#   method        'cash' | 'card' | 'split'
#   tendered      cash handed over (cash; blank = exact)
#   cash_amount   cash part of a split (blank = 0)
#   card_amounts  card parts of a split (blank entries are ignored)
Payment = namedtuple('Payment', 'method tendered cash_amount card_amounts')
Payment.__new__.__defaults__ = (None, None, ())


class RegisterCart(namedtuple('RegisterCart', 'register_id')):
    """The cart lives on a register: re-read and emptied under the lock."""


class SessionCart(namedtuple('SessionCart', 'cart')):
    """A per-session cart snapshot (no register identity); the caller
    empties the session cart after a successful sale."""


# money fields are integer cents (payment_details uses *_cents keys)
SaleResult = namedtuple('SaleResult',
                        'order_id order_number total_cents payment_method payment_details cash_discount_cents')


class CheckoutError(Exception):
    """A sale that was not recorded. `message` is the user-facing text;
    `redirect_to` is where the cashier goes next ('sales' | 'checkout' |
    'receipt')."""
    code = 'error'
    redirect_to = 'checkout'

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class UnknownPaymentMethod(CheckoutError):
    code = 'unknown_payment_method'

    def __init__(self):
        super().__init__('Unknown payment method.')


class CartEmpty(CheckoutError):
    code = 'cart_empty'
    redirect_to = 'sales'

    def __init__(self):
        super().__init__('Cart is empty.')


class AlreadyCompleted(CheckoutError):
    """The checkout token was already used: the sale exists."""
    code = 'already_completed'
    redirect_to = 'receipt'

    def __init__(self, order_number):
        super().__init__(f'Order {order_number} was already completed.')
        self.order_number = order_number


class CartChanged(CheckoutError):
    code = 'cart_changed'

    def __init__(self):
        super().__init__('The cart changed since this checkout page opened. Please review the new total.')


class CouponInvalid(CheckoutError):
    """The applied coupon no longer validates against the cart; the caller
    drops it from the cart (and only it)."""
    code = 'coupon_invalid'

    def __init__(self, reason):
        super().__init__(f"Coupon removed: {reason or 'no longer valid'} Please review the total.")
        self.reason = reason


class CouponLimitReached(CheckoutError):
    code = 'coupon_limit'

    def __init__(self):
        super().__init__('Error processing order: Coupon has reached its maximum number of uses.')


class TenderError(CheckoutError):
    """Short cash, split amounts that don't cover the total, bad amounts."""
    code = 'tender'


# ---------------------------------------------------------------------------
# Order numbers
# ---------------------------------------------------------------------------

def next_order_number(conn, today=None):
    """Next order number for today, format POS-YYYYMMDD-NNN.

    Call inside the checkout transaction so the read and the insert are
    atomic across registers. The sequence is compared numerically, so it
    keeps counting past 999.
    """
    prefix = f"POS-{(today or date.today()).strftime('%Y%m%d')}"
    row = conn.execute('''
        SELECT MAX(CAST(substr(order_number, ?) AS INTEGER)) AS seq
        FROM pos_orders WHERE order_number LIKE ?
    ''', (len(prefix) + 2, f"{prefix}-%")).fetchone()
    next_seq = (row['seq'] or 0) + 1
    return f"{prefix}-{str(next_seq).zfill(3)}"


# ---------------------------------------------------------------------------
# Tender
# ---------------------------------------------------------------------------

def settle_tender(payment, totals, config):
    """(total due, payment_details, cash_discount), all cents, for this tender,
    or raise TenderError. `totals` is pos_pricing.cart_totals() of the sold cart.

    The tendered amount must cover the total exactly. (The dollar version
    compared floats with a 0.01 slack, so a tender one cent short was
    sometimes accepted, depending on float rounding.)
    """
    total = totals['card_total_cents']
    if payment.method == 'cash':
        cash_discount = totals['cash_discount_cents'] if totals['cash_discount_enabled'] else 0
        total = total - cash_discount
        tendered = parse_money_field(payment.tendered, default=total)
        if tendered is None or tendered < total:
            raise TenderError(f'Cash tendered must be at least {money.fmt(total)}.')
        return total, {
            'tendered_cents': tendered,
            'change_cents': max(0, tendered - total),
            'cash_discount_cents': cash_discount,
        }, cash_discount

    if payment.method == 'split':
        cash_amount = parse_money_field(payment.cash_amount, default=0)
        card_amounts = [parse_money(a) for a in (payment.card_amounts or ()) if a]
        if cash_amount is None or any(a is None for a in card_amounts):
            raise TenderError('Invalid split payment amounts.')

        # The cash discount applies to the cash portion only
        cash_discount = pos_pricing.split_cash_discount(cash_amount, card_amounts, total, config)
        total = total - cash_discount

        total_tendered = cash_amount + sum(card_amounts)
        if total_tendered < total:
            raise TenderError(f'Split payments add up to {money.fmt(total_tendered)}, '
                              f'less than the {money.fmt(total)} total.')
        # Change can only be given in cash: cards may not exceed the total
        if sum(card_amounts) > total:
            raise TenderError(f'Card amounts add up to more than the {money.fmt(total)} total.')
        return total, {
            'cash_cents': cash_amount,
            'cards_cents': card_amounts,
            'total_tendered_cents': total_tendered,
            'change_cents': min(cash_amount, max(0, total_tendered - total)),
            'cash_discount_cents': cash_discount,
        }, cash_discount

    return total, {}, 0


# ---------------------------------------------------------------------------
# Stock
# ---------------------------------------------------------------------------

def end_sale_if_depleted(conn, item_id, quantity):
    """'End sale on out of stock': stop the item's sale price once it sells out."""
    if quantity > 0:
        return False
    row = conn.execute('SELECT sale_enabled, sale_end_on_stock FROM inventory_items WHERE id = ?',
                       (item_id,)).fetchone()
    if row and row['sale_enabled'] and row['sale_end_on_stock']:
        conn.execute('UPDATE inventory_items SET sale_enabled = 0 WHERE id = ?', (item_id,))
        logger.info(f"Sale auto-ended for item {item_id} due to stock depletion.")
        return True
    return False


def _sell_stock(conn, item_id, quantity, user_ref, order_number, order_id=None):
    """Take sold units off the shelf (a sale is never refused for stock):
    one 'sale' movement referencing the order."""
    try:
        change = inventory.adjust_stock(conn, item_id, -quantity, inventory.REASON_SALE,
                                        user_ref, order_number, ref_type=inventory.REF_ORDER,
                                        ref_id=order_id, allow_negative=True)
    except inventory.ItemNotFound:
        # Item deleted while it sat in the cart: the sale still stands
        logger.warning(f"Order {order_number}: inventory item {item_id} no longer exists; stock not changed")
        return
    end_sale_if_depleted(conn, item_id, change.new_quantity)


# ---------------------------------------------------------------------------
# The sale
# ---------------------------------------------------------------------------

def _order_number(conn, order_number_fn):
    if order_number_fn is None:
        return next_order_number(conn)
    return order_number_fn(conn)


def complete_sale(conn, cart_source, payment, user, token=None, expected_total=None,
                  expected_version=None, *, config, order_number_fn=None):
    """Record the sale of the cart; returns a SaleResult or raises a
    CheckoutError (nothing is written then).

    conn             a connection of its own (autocommit mode is fine); the
                     transaction is begun and committed/rolled back here
    cart_source      RegisterCart(register_id) or SessionCart(cart)
    payment          Payment(method, tendered, cash_amount, card_amounts), raw
    user             acting user: object with .id and .username
    token            checkout token rendered on the checkout page (one sale per token)
    expected_total   total the cashier quoted (string/number; blank = unchecked)
    expected_version register cart version the page was rendered at (blank = unchecked)
    config           pos_pricing.PricingConfig
    order_number_fn  fn(conn) -> order number (default next_order_number)
    """
    if payment.method not in PAYMENT_METHODS:
        raise UnknownPaymentMethod()
    token = (token or '').strip() or None
    register_id = cart_source.register_id if isinstance(cart_source, RegisterCart) else None

    conn.execute('BEGIN IMMEDIATE')
    try:
        result = _record_sale(conn, cart_source, register_id, payment, user, token,
                              expected_total, expected_version, config, order_number_fn)
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    logger.info(f"Order {result.order_number} completed by {user.username}, total: {money.fmt(result.total_cents)}")
    return result


def _record_sale(conn, cart_source, register_id, payment, user, token,
                 expected_total, expected_version, config, order_number_fn):
    # Already recorded? (double-click, or a retry after a lost response)
    if token:
        existing = conn.execute('SELECT order_number FROM pos_orders WHERE checkout_token = ?',
                                (token,)).fetchone()
        if existing:
            raise AlreadyCompleted(existing['order_number'])

    # The cart as it is NOW, under the write lock: nothing (scanner, handheld,
    # another tab) can change it until this transaction ends.
    if register_id:
        cart, locked_version = pos_registers.read_register_cart(conn, register_id)
        if not cart['items']:
            raise CartEmpty()
        expected_version = str(expected_version or '').strip()
        if expected_version:
            try:
                if int(expected_version) != locked_version:
                    raise CartChanged()
            except ValueError:
                pass
    else:
        cart = json.loads(json.dumps(cart_source.cart))
        if not cart.get('items'):
            raise CartEmpty()

    # Re-validate the coupon against the cart as it is NOW: the stored
    # discount was computed when it was applied, items may have gone since,
    # and limits must be checked under the lock.
    coupon = cart.get('applied_coupon')
    if coupon:
        check = pos_pricing.evaluate_coupon(coupon.get('code'), cart['items'],
                                            pos_pricing.cart_subtotal(cart['items']), conn, cart=cart)
        if not check.get('valid'):
            raise CouponInvalid(check.get('error', 'no longer valid'))
        coupon['discount_cents'] = check['discount_cents']

    totals = pos_pricing.cart_totals(cart, config)
    total = totals['card_total_cents']

    # The cashier quoted the total shown on the checkout page (dollars, as
    # rendered). If the cart changed since, stop rather than record a
    # different amount. An unparseable value is ignored, as before.
    if expected_total:
        quoted = money.parse(expected_total, allow_negative=True)
        if quoted is not None and quoted != total:
            raise CartChanged()

    total, payment_details, cash_discount = settle_tender(payment, totals, config)

    order_number = _order_number(conn, order_number_fn)
    order_discount = totals['order_discount_cents']
    cursor = conn.execute('''
        INSERT INTO pos_orders (
            order_number, status, subtotal_cents, tax_rate, tax_amount_cents,
            discount_amount_cents, discount_type, discount_reason, total_cents,
            payment_method, payment_details, operator_id, checkout_token, cash_discount_cents
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        order_number, 'completed', totals['subtotal_cents'], config.tax_rate,
        totals['tax_amount_cents'], order_discount,
        cart.get('discount_type') if order_discount else None, cart.get('discount_reason', ''),
        total, payment.method, json.dumps(payment_details), user.id, token,
        cash_discount
    ))
    order_id = cursor.lastrowid

    # Payments ledger: what each tender paid toward the sale, plus cash
    # tendered and change given
    write_payments(conn, order_id, allocate_payments(payment.method, payment_details, total))

    for item in cart['items']:
        item_id = item.get('inventory_item_id')
        if item_id and not conn.execute('SELECT 1 FROM inventory_items WHERE id = ?', (item_id,)).fetchone():
            # Deleted while it sat in the cart: the sale still stands, without
            # the (now dangling) link; the line keeps its sku and name.
            logger.warning(f"Order {order_number}: inventory item {item_id} no longer exists; "
                           f"line recorded without it")
            item_id = None
        dtype = item.get('discount_type')
        conn.execute('''
            INSERT INTO pos_order_items (
                order_id, inventory_item_id, sku, name, quantity,
                unit_price_cents, discount_type, discount_percent, discount_cents, line_total_cents
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            order_id, item_id, item['sku'], item['name'],
            item['quantity'], item['unit_price_cents'], dtype,
            (item.get('discount_percent') or 0) if dtype == 'percent' else 0,
            (item.get('discount_cents') or 0) if dtype == 'fixed' else 0,
            item['line_total_cents']
        ))
        if item_id:
            _sell_stock(conn, item_id, item['quantity'], str(user.id), order_number, order_id)

    if coupon and coupon.get('id'):
        _redeem_coupon(conn, coupon, order_id, order_number, totals['coupon_discount_cents'], user)

    # Empty exactly the cart that was just sold, in the same transaction
    if register_id:
        pos_registers.write_register_cart(conn, register_id, pos_registers.empty_cart())

    return SaleResult(order_id, order_number, total, payment.method, payment_details, cash_discount)


def _redeem_coupon(conn, coupon, order_id, order_number, coupon_discount, user):
    conn.execute('''
        INSERT INTO pos_coupon_redemptions (
            coupon_id, order_id, serial_used, discount_applied_cents, redeemed_by
        ) VALUES (?, ?, ?, ?, ?)
    ''', (coupon['id'], order_id, coupon.get('code'), coupon_discount, user.id))
    used = conn.execute('''
        UPDATE pos_coupons
        SET current_uses = COALESCE(current_uses, 0) + 1
        WHERE id = ?
          AND (max_uses IS NULL OR max_uses = '' OR CAST(max_uses AS INTEGER) <= 0
               OR COALESCE(current_uses, 0) < CAST(max_uses AS INTEGER))
    ''', (coupon['id'],)).rowcount
    if not used:
        raise CouponLimitReached()
    logger.info(f"Recorded redemption of coupon ID {coupon['id']} for order {order_number}")
