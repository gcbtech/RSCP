"""
POS pricing: money rounding, cart totals and coupon evaluation.

The single source of truth for POS arithmetic. Every screen (sales page,
cart fragment, /api/cart, customer display and scanner polls, checkout page)
and the recorded sale use these functions, so they can never disagree.

Conventions (same as the other services):
  * no Flask request/session/current_user here;
  * settings are passed in (PricingConfig), never read here, so the money
    math is a pure function of its inputs;
  * coupon evaluation takes the caller's open connection.

app.routes.pos.core / app.routes.pos.coupons re-export the old names
(round_money, calculate_tax(subtotal), compute_cart_totals(cart),
evaluate_coupon, ...) as thin wrappers that read the current settings.
"""
import logging
from collections import namedtuple
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

logger = logging.getLogger(__name__)

_CENT = Decimal('0.01')

# The settings the cart math depends on.
#   tax_rate              fraction (0.07 for 7%)
#   cash_discount_enabled bool
#   cash_discount_amount  percent (type 'percent') or dollars (type 'fixed')
#   cash_discount_type    'percent' | 'fixed'
PricingConfig = namedtuple('PricingConfig',
                           'tax_rate cash_discount_enabled cash_discount_amount cash_discount_type')
PricingConfig.__new__.__defaults__ = (0.0, False, 0.0, 'percent')


# ---------------------------------------------------------------------------
# Money helpers
# ---------------------------------------------------------------------------

def round_money(value):
    """Round a monetary value to 2 decimal places using ROUND_HALF_UP.

    Python's built-in round() uses banker's rounding (round half to even),
    which can cause penny discrepancies. This function uses traditional
    rounding where .5 always rounds up.
    """
    return float(Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP))


def calculate_tax(subtotal, rate):
    """Tax on `subtotal` at `rate` (a fraction), rounded half-up to the cent."""
    result = Decimal(str(subtotal)) * Decimal(str(rate))
    return float(result.quantize(_CENT, rounding=ROUND_HALF_UP))


def calculate_percentage(amount, percent):
    """`percent`% of `amount` using Decimal (no float drift), rounded half-up."""
    try:
        if not amount or not percent:
            return 0.0
        result = Decimal(str(amount)) * Decimal(str(percent)) / Decimal('100')
        return float(result.quantize(_CENT, rounding=ROUND_HALF_UP))
    except Exception:
        return 0.0


def calculate_line_total(unit_price, quantity, discount_amount=0, discount_type=None):
    """Line total with an optional line discount ('percent' or fixed dollars)."""
    base = unit_price * quantity

    if discount_amount and discount_type:
        if discount_type == 'percent':
            discount = calculate_percentage(base, discount_amount)
        else:  # fixed
            discount = discount_amount
        base = max(0, base - discount)

    return round_money(base)


def parse_money(raw):
    """Parse a money input; None if it is not a finite, non-negative number."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value < 0 or value == float('inf'):
        return None
    return round_money(value)


def parse_money_field(raw, default=None):
    """parse_money for an optional form field: blank/missing -> default."""
    if raw is None or str(raw).strip() == '':
        return default
    return parse_money(raw)


# ---------------------------------------------------------------------------
# Cart totals
# ---------------------------------------------------------------------------

def cart_subtotal(items):
    """Sum of the line totals (unrounded, as coupon rules have always seen it)."""
    return sum(item.get('line_total', 0) for item in (items or []))


def cash_discount_for(amount, config):
    """The cash discount earned on `amount` (0 when disabled)."""
    if not config.cash_discount_enabled:
        return 0.0
    try:
        cd_amount = float(config.cash_discount_amount or 0)
    except (TypeError, ValueError):
        cd_amount = 0.0
    if cd_amount <= 0:
        return 0.0
    if config.cash_discount_type == 'percent':
        return calculate_percentage(amount, cd_amount)
    return round_money(min(cd_amount, amount))


def cart_totals(cart, config):
    """Every number a cart shows, from the cart and the pricing settings.

    Includes dual cash/card pricing when a cash discount is configured.
    """
    items = cart.get('items', []) or []
    subtotal = round_money(cart_subtotal(items))

    order_discount = 0.0
    if cart.get('discount_amount') and cart.get('discount_type'):
        if cart['discount_type'] == 'percent':
            order_discount = calculate_percentage(subtotal, cart['discount_amount'])
        else:
            try:
                order_discount = round_money(min(float(cart['discount_amount']), subtotal))
            except (TypeError, ValueError):
                order_discount = 0.0

    try:
        coupon_discount = float((cart.get('applied_coupon') or {}).get('discount', 0) or 0)
    except (TypeError, ValueError):
        coupon_discount = 0.0

    discounted_subtotal = max(0.0, subtotal - order_discount - coupon_discount)
    tax_rate = config.tax_rate
    tax_amount = calculate_tax(discounted_subtotal, tax_rate)
    card_total = round_money(discounted_subtotal + tax_amount)

    cash_discount_enabled = bool(config.cash_discount_enabled)
    cash_discount_value = cash_discount_for(discounted_subtotal, config)
    cash_total = round_money(card_total - cash_discount_value)

    return {
        'items': items,
        'item_count': len(items),
        'subtotal': subtotal,
        'order_discount': round_money(order_discount),
        'coupon_discount': round_money(coupon_discount),
        'applied_coupon': cart.get('applied_coupon'),
        'tax_rate_pct': round(tax_rate * 100, 2),
        'tax_amount': tax_amount,
        'card_total': card_total,
        'cash_discount_enabled': cash_discount_enabled,
        'cash_discount_value': cash_discount_value,
        'cash_total': cash_total,
    }


def split_cash_discount(cash_amount, card_amounts, total, config):
    """Cash discount on a split payment: it applies to the cash that pays the
    sale, not the cash handed over ($100 given for a $20 cash balance earns
    the discount on $20). Fixed discounts are prorated by that cash share."""
    if not config.cash_discount_enabled or cash_amount <= 0:
        return 0
    try:
        cd_amount = float(config.cash_discount_amount or 0)
    except (TypeError, ValueError):
        cd_amount = 0.0
    if cd_amount <= 0:
        return 0
    cash_basis = min(cash_amount, max(0.0, total - sum(card_amounts)))
    if config.cash_discount_type == 'percent':
        return calculate_percentage(cash_basis, cd_amount)
    cash_proportion = cash_basis / total if total > 0 else 0
    return round_money(min(cd_amount * cash_proportion, cash_basis))


# ---------------------------------------------------------------------------
# Coupons
# ---------------------------------------------------------------------------

def _int_or_none(value):
    try:
        return int(value) if value is not None else None
    except (ValueError, TypeError):
        return None


def _coupon_target_ids(conn, coupon_id):
    return [r['item_id'] for r in conn.execute(
        'SELECT item_id FROM pos_coupon_items WHERE coupon_id = ?', (coupon_id,)).fetchall()]


def evaluate_coupon(code, cart_items, cart_subtotal, conn, cart=None, today=None):
    """Authoritatively validate a coupon against the given cart and compute its
    discount server-side.

    Returns a plain dict, either:
      {'valid': False, 'error': <message>}
    or:
      {'valid': True, 'coupon': {...}, 'discount_amount': <server-computed>}

    /api/coupon/validate, /api/coupon/apply and checkout all route through
    this, so the discount stored on the cart can never diverge from -- or be
    forged past -- validation. The caller owns the connection.
    `today` ('YYYY-MM-DD') defaults to the local date.
    """
    coupon = conn.execute('''
        SELECT * FROM pos_coupons WHERE code = ? AND active = 1
    ''', (code,)).fetchone()

    if not coupon:
        logger.warning(f"[Coupon Validate] Coupon not found or inactive: {code}")
        return {'valid': False, 'error': 'Invalid coupon code.'}

    now = today or datetime.now().strftime('%Y-%m-%d')

    if coupon['start_date'] and coupon['start_date'] > now:
        logger.warning(f"[Coupon Validate] Coupon {code} is not yet active (start_date: {coupon['start_date']}, now: {now})")
        return {'valid': False, 'error': 'Coupon is not yet active.'}

    if coupon['end_date'] and coupon['end_date'] < now:
        logger.warning(f"[Coupon Validate] Coupon {code} has expired (end_date: {coupon['end_date']}, now: {now})")
        return {'valid': False, 'error': 'Coupon has expired.'}

    # Usage limits (None, empty string, or 0 means unlimited uses)
    max_uses = coupon['max_uses']
    current_uses = int(coupon['current_uses'] or 0)

    if max_uses is not None and str(max_uses).strip() != "" and int(max_uses) > 0:
        if current_uses >= int(max_uses):
            logger.warning(f"[Coupon Validate] Coupon {code} reached max uses: {current_uses}/{max_uses}")
            return {'valid': False, 'error': 'Coupon has reached maximum uses.'}

    # Serialized coupons: single redemption per serial
    if coupon['coupon_type'] == 'serialized':
        redemption = conn.execute('''
            SELECT id FROM pos_coupon_redemptions WHERE coupon_id = ? AND serial_used = ?
        ''', (coupon['id'], code)).fetchone()
        if redemption:
            logger.warning(f"[Coupon Validate] Serialized coupon {code} already redeemed")
            return {'valid': False, 'error': 'This coupon has already been redeemed.'}

    # Minimum purchase for order-level coupons
    if coupon['discount_type'] in ('order_dollar', 'order_percent') and coupon['min_purchase']:
        if cart_subtotal < coupon['min_purchase']:
            logger.warning(f"[Coupon Validate] Subtotal {cart_subtotal} < min purchase {coupon['min_purchase']} for coupon {code}")
            return {
                'valid': False,
                'error': f'Minimum purchase of ${coupon["min_purchase"]:.2f} required.'
            }

    # Item-specific coupons: a target item must be in the cart
    if coupon['discount_type'] in ('item_dollar', 'item_percent', 'bogo_free', 'bogo_percent'):
        target_items = conn.execute('''
            SELECT ci.item_id, ii.sku FROM pos_coupon_items ci
            LEFT JOIN inventory_items ii ON ci.item_id = ii.id
            WHERE ci.coupon_id = ?
        ''', (coupon['id'],)).fetchall()
        target_ids = [r['item_id'] for r in target_items]
        target_skus = {r['sku'].upper() for r in target_items if r['sku']}

        logger.info(f"[Coupon Validate] Item-specific validation for coupon ID {coupon['id']}. "
                    f"Target item IDs: {target_ids}, Target SKUs: {target_skus}")

        if target_ids:
            # Match the live cart by inventory_item_id AND by SKU
            cart_item_ids = []
            cart_item_skus = set()
            for item in cart_items:
                iid = item.get('inventory_item_id')
                if iid is not None:
                    try:
                        cart_item_ids.append(int(iid))
                    except (ValueError, TypeError):
                        pass
                sku = item.get('sku')
                if sku:
                    cart_item_skus.add(str(sku).upper())

            matching_ids = set(target_ids) & set(cart_item_ids)
            matching_skus = target_skus & cart_item_skus

            logger.info(f"[Coupon Validate] Cart item IDs: {cart_item_ids}, Cart SKUs: {cart_item_skus}")
            logger.info(f"[Coupon Validate] Matching IDs: {matching_ids}, Matching SKUs: {matching_skus}")

            if not matching_ids and not matching_skus:
                logger.warning(f"[Coupon Validate] Required item for coupon {code} not in cart. "
                               f"Targets: {target_ids}/{target_skus}, Cart: {cart_item_ids}/{cart_item_skus}")
                return {
                    'valid': False,
                    'error': 'Required item for this coupon is not in cart.'
                }
            logger.info(f"[Coupon Validate] Matching target items found in cart (IDs: {matching_ids}, SKUs: {matching_skus})")

    # "Cannot combine" coupons do not stack with an order-level discount
    if coupon['cannot_combine'] and cart and cart.get('discount_amount') and cart.get('discount_type'):
        return {'valid': False, 'error': 'This coupon cannot be combined with other discounts.'}

    discount = calculate_coupon_discount(coupon, cart_items, cart_subtotal, conn)
    logger.info(f"[Coupon Validate] Coupon {code} validated successfully. Discount amount: {discount}")

    return {
        'valid': True,
        'coupon': {
            'id': coupon['id'],
            'name': coupon['name'],
            'code': coupon['code'],
            'discount_type': coupon['discount_type'],
            'discount_value': coupon['discount_value'],
            'cannot_combine': bool(coupon['cannot_combine'])
        },
        'discount_amount': discount
    }


def calculate_coupon_discount(coupon, cart_items, cart_subtotal, conn):
    """The discount a (valid) coupon gives on these cart items."""
    discount_type = coupon['discount_type']
    discount_value = coupon['discount_value']

    if discount_type == 'order_dollar':
        return min(discount_value, cart_subtotal)

    if discount_type == 'order_percent':
        return calculate_percentage(cart_subtotal, discount_value)

    if discount_type in ('item_dollar', 'item_percent'):
        target_ids = _coupon_target_ids(conn, coupon['id'])
        total_discount = 0
        for item in cart_items:
            if _int_or_none(item.get('inventory_item_id')) in target_ids:
                item_total = float(item.get('unit_price', 0)) * int(item.get('quantity', 1))
                if discount_type == 'item_dollar':
                    # Applied once per item type, not per unit
                    total_discount += min(discount_value, item_total)
                else:
                    total_discount += calculate_percentage(item_total, discount_value)
        return total_discount

    if discount_type == 'bogo_free':
        # Buy X get 1 free (same item): for every buy_qty + 1, one is free
        buy_qty = coupon['buy_quantity']
        target_ids = _coupon_target_ids(conn, coupon['id'])
        total_discount = 0
        for item in cart_items:
            if _int_or_none(item.get('inventory_item_id')) in target_ids:
                qty = int(item.get('quantity', 1))
                unit_price = float(item.get('unit_price', 0))
                total_discount += (qty // (buy_qty + 1)) * unit_price
        return total_discount

    if discount_type == 'bogo_percent':
        # Buy X get the next at Y% off
        buy_qty = coupon['buy_quantity']
        target_ids = _coupon_target_ids(conn, coupon['id'])
        total_discount = 0
        for item in cart_items:
            if _int_or_none(item.get('inventory_item_id')) in target_ids:
                qty = int(item.get('quantity', 1))
                unit_price = float(item.get('unit_price', 0))
                base_amount = (qty // (buy_qty + 1)) * unit_price
                total_discount += calculate_percentage(base_amount, discount_value)
        return round(total_discount, 2)

    if discount_type == 'bogo_cross':
        # Buy A get B free
        reward_item_id = _int_or_none(coupon['reward_item_id'])
        buy_qty = coupon['buy_quantity']
        target_ids = _coupon_target_ids(conn, coupon['id'])

        has_required = any(
            _int_or_none(item.get('inventory_item_id')) in target_ids
            and int(item.get('quantity', 1)) >= buy_qty
            for item in cart_items)
        if not has_required:
            return 0

        for item in cart_items:
            if _int_or_none(item.get('inventory_item_id')) == reward_item_id:
                return float(item.get('unit_price', 0))  # one free
        return 0

    return 0
