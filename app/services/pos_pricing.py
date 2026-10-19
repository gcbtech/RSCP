"""
POS pricing: cart totals and coupon evaluation, in integer cents.

The single source of truth for POS arithmetic. Every screen (sales page,
cart fragment, /api/cart, customer display and scanner polls, checkout page)
and the recorded sale use these functions, so they can never disagree.

Conventions (same as the other services):
  * money is integer cents (app/services/money.py); percentages are percent
    numbers and the tax rate a fraction;
  * no Flask request/session/current_user here;
  * settings are passed in (PricingConfig), never read here, so the money
    math is a pure function of its inputs;
  * coupon evaluation takes the caller's open connection.

Rounding (unchanged from the dollar version): line discounts, the order
discount, percent coupons and the cash discount are each rounded half-up to
the cent where they are computed; tax is computed once, on the discounted
order subtotal, and rounded half-up. With exact cents there is no float
drift between those steps any more (see money.py).

Cart dicts (session, register cart_json, held orders) hold, per line,
unit_price_cents / regular_price_cents / sell_price_cents / line_total_cents
plus discount_type ('percent' | 'fixed' | None) with discount_percent or
discount_cents; the cart has the same three discount fields for the order
discount, and applied_coupon.discount_cents. upgrade_legacy_cart() converts
a cart saved before v15 (dollars, 'discount_amount').

app.routes.pos.core / app.routes.pos.coupons re-export the old names
(calculate_tax(subtotal), compute_cart_totals(cart), evaluate_coupon, ...)
as thin wrappers that read the current settings.
"""
import logging
from collections import namedtuple
from datetime import datetime

from app.services import money

logger = logging.getLogger(__name__)

# The settings the cart math depends on.
#   tax_rate              fraction (0.07 for 7%)
#   cash_discount_enabled bool
#   cash_discount_type    'percent' | 'fixed'
#   cash_discount_percent percent number (type 'percent')
#   cash_discount_cents   cents (type 'fixed')
PricingConfig = namedtuple('PricingConfig',
                           'tax_rate cash_discount_enabled cash_discount_type '
                           'cash_discount_percent cash_discount_cents')
PricingConfig.__new__.__defaults__ = (0.0, False, 'percent', 0.0, 0)

DISCOUNT_TYPES = ('percent', 'fixed')


# ---------------------------------------------------------------------------
# Money helpers (all amounts are integer cents)
# ---------------------------------------------------------------------------

def calculate_tax(subtotal_cents, rate):
    """Tax on `subtotal_cents` at `rate` (a fraction), rounded half-up to the cent."""
    return money.apply_rate(subtotal_cents, rate)


def calculate_percentage(amount_cents, percent):
    """`percent`% of `amount_cents`, rounded half-up to the cent."""
    try:
        return money.percent_of(amount_cents, percent)
    except Exception:
        return 0


def calculate_line_total(unit_price_cents, quantity, discount_type=None, discount_percent=0,
                         discount_cents=0):
    """Line total in cents with an optional line discount: 'percent'
    (discount_percent of the line) or 'fixed' (discount_cents off the line).
    Never below zero."""
    base = int(unit_price_cents or 0) * int(quantity or 0)
    if discount_type == 'percent' and discount_percent:
        base -= calculate_percentage(base, discount_percent)
    elif discount_type == 'fixed' and discount_cents:
        base -= int(discount_cents)
    return max(0, base)


def line_total_for(item):
    """calculate_line_total for a cart / order line dict."""
    return calculate_line_total(item.get('unit_price_cents') or 0, item.get('quantity') or 0,
                                item.get('discount_type'), item.get('discount_percent') or 0,
                                item.get('discount_cents') or 0)


def parse_money(raw):
    """Cents for a money input; None if it is not a finite, non-negative amount."""
    return money.parse(raw)


def parse_money_field(raw, default=None):
    """parse_money for an optional form field: blank/missing -> default."""
    return money.parse_field(raw, default)


# ---------------------------------------------------------------------------
# Carts saved before v15 (dollars)
# ---------------------------------------------------------------------------

def _legacy_discount(d):
    """Replace a dollar-era 'discount_amount' (a percent or dollars, by
    discount_type) with discount_percent / discount_cents."""
    raw = d.pop('discount_amount', 0)
    dtype = d.get('discount_type')
    d['discount_percent'] = 0
    d['discount_cents'] = 0
    if not dtype or not raw:
        d['discount_type'] = None   # the old math ignored a discount without both
        return
    if dtype == 'percent':
        try:
            d['discount_percent'] = float(raw)
        except (TypeError, ValueError):
            d['discount_type'] = None
    else:
        d['discount_type'] = 'fixed'
        d['discount_cents'] = money.from_dollars(raw) or 0


def upgrade_legacy_cart(cart):
    """Convert, in place, a cart saved before v15 (dollar floats and
    'discount_amount') to cents. Idempotent; returns True if it changed."""
    changed = False
    for item in cart.get('items') or []:
        if not isinstance(item, dict):
            continue
        for key in ('unit_price', 'regular_price', 'sell_price', 'line_total'):
            if key in item:
                value = item.pop(key)
                if f'{key}_cents' not in item:
                    cents = money.from_dollars(value)
                    if cents is None and key in ('unit_price', 'line_total'):
                        cents = 0
                    item[f'{key}_cents'] = cents
                changed = True
        if 'discount_amount' in item:
            _legacy_discount(item)
            changed = True
    if 'discount_amount' in cart:
        _legacy_discount(cart)
        changed = True
    coupon = cart.get('applied_coupon')
    if isinstance(coupon, dict) and 'discount' in coupon:
        value = coupon.pop('discount')
        coupon.setdefault('discount_cents', money.from_dollars(value) or 0)
        changed = True
    return changed


# ---------------------------------------------------------------------------
# Cart totals
# ---------------------------------------------------------------------------

def cart_subtotal(items):
    """Sum of the line totals, in cents."""
    return sum(int(item.get('line_total_cents') or 0) for item in (items or []))


def cash_discount_for(amount_cents, config):
    """The cash discount earned on `amount_cents` (0 when disabled)."""
    if not config.cash_discount_enabled:
        return 0
    if config.cash_discount_type == 'percent':
        return calculate_percentage(amount_cents, config.cash_discount_percent or 0)
    fixed = int(config.cash_discount_cents or 0)
    if fixed <= 0:
        return 0
    return min(fixed, amount_cents)


def order_discount_for(cart, subtotal_cents):
    """The order-level discount (cents) the cart's discount fields give."""
    dtype = cart.get('discount_type')
    if dtype == 'percent' and cart.get('discount_percent'):
        return calculate_percentage(subtotal_cents, cart['discount_percent'])
    if dtype == 'fixed' and cart.get('discount_cents'):
        try:
            return max(0, min(int(cart['discount_cents']), subtotal_cents))
        except (TypeError, ValueError):
            return 0
    return 0


def cart_totals(cart, config):
    """Every number a cart shows (cents), from the cart and the pricing settings.

    Includes dual cash/card pricing when a cash discount is configured.
    """
    items = cart.get('items', []) or []
    subtotal = cart_subtotal(items)
    order_discount = order_discount_for(cart, subtotal)

    try:
        coupon_discount = int((cart.get('applied_coupon') or {}).get('discount_cents', 0) or 0)
    except (TypeError, ValueError):
        coupon_discount = 0

    discounted_subtotal = max(0, subtotal - order_discount - coupon_discount)
    tax_rate = config.tax_rate
    tax_amount = calculate_tax(discounted_subtotal, tax_rate)
    card_total = discounted_subtotal + tax_amount

    cash_discount_enabled = bool(config.cash_discount_enabled)
    cash_discount_value = cash_discount_for(discounted_subtotal, config)
    cash_total = card_total - cash_discount_value

    return {
        'items': items,
        'item_count': len(items),
        'subtotal_cents': subtotal,
        'order_discount_cents': order_discount,
        'coupon_discount_cents': coupon_discount,
        'applied_coupon': cart.get('applied_coupon'),
        'tax_rate_pct': round(tax_rate * 100, 2),
        'tax_amount_cents': tax_amount,
        'card_total_cents': card_total,
        'cash_discount_enabled': cash_discount_enabled,
        'cash_discount_cents': cash_discount_value,
        'cash_total_cents': cash_total,
    }


def split_cash_discount(cash_cents, card_cents, total_cents, config):
    """Cash discount on a split payment: it applies to the cash that pays the
    sale, not the cash handed over ($100 given for a $20 cash balance earns
    the discount on $20). Fixed discounts are prorated by that cash share."""
    if not config.cash_discount_enabled or cash_cents <= 0:
        return 0
    cash_basis = min(cash_cents, max(0, total_cents - sum(card_cents)))
    if config.cash_discount_type == 'percent':
        return calculate_percentage(cash_basis, config.cash_discount_percent or 0)
    fixed = int(config.cash_discount_cents or 0)
    if fixed <= 0 or total_cents <= 0:
        return 0
    return min(money.prorate(fixed, cash_basis, total_cents), cash_basis)


# ---------------------------------------------------------------------------
# Edges: carts as the dollar JSON RSCP's page scripts read
# ---------------------------------------------------------------------------

def _discount_json(d):
    if d.get('discount_type') == 'percent':
        return d.get('discount_percent') or 0
    if d.get('discount_type') == 'fixed':
        return money.to_dollars(d.get('discount_cents') or 0)
    return 0


def item_json(item):
    """A cart line as the page JS reads it (dollars, the old field names)."""
    out = {k: v for k, v in item.items() if not k.endswith('_cents') and k != 'discount_percent'}
    for key in ('unit_price', 'regular_price', 'sell_price', 'line_total'):
        if f'{key}_cents' in item:
            out[key] = money.to_dollars(item[f'{key}_cents'])
    out['discount_amount'] = _discount_json(item)
    return out


def coupon_json(coupon):
    """An applied coupon as the page JS reads it ('discount' in dollars)."""
    if not coupon:
        return coupon
    out = {k: v for k, v in coupon.items() if not k.endswith('_cents') and k != 'discount_percent'}
    if 'discount_cents' in coupon:
        out['discount'] = money.to_dollars(coupon['discount_cents'])
    return out


def totals_json(totals):
    """cart_totals() as the dollar JSON /api/cart, scanner responses and the
    peripheral polls have always returned (same keys, same number format)."""
    return {
        'items': [item_json(i) for i in totals['items']],
        'item_count': totals['item_count'],
        'subtotal': money.to_dollars(totals['subtotal_cents']),
        'order_discount': money.to_dollars(totals['order_discount_cents']),
        'coupon_discount': money.to_dollars(totals['coupon_discount_cents']),
        'applied_coupon': coupon_json(totals['applied_coupon']),
        'tax_rate_pct': totals['tax_rate_pct'],
        'tax_amount': money.to_dollars(totals['tax_amount_cents']),
        'card_total': money.to_dollars(totals['card_total_cents']),
        'cash_discount_enabled': totals['cash_discount_enabled'],
        'cash_discount_value': money.to_dollars(totals['cash_discount_cents']),
        'cash_total': money.to_dollars(totals['cash_total_cents']),
    }


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
      {'valid': True, 'coupon': {...}, 'discount_cents': <server-computed>}

    `cart_subtotal` is the cart's subtotal in cents.

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
    min_purchase = coupon['min_purchase_cents']
    if coupon['discount_type'] in ('order_dollar', 'order_percent') and min_purchase:
        if cart_subtotal < min_purchase:
            logger.warning(f"[Coupon Validate] Subtotal {cart_subtotal}c < min purchase {min_purchase}c for coupon {code}")
            return {
                'valid': False,
                'error': f'Minimum purchase of {money.fmt(min_purchase)} required.'
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
    if coupon['cannot_combine'] and cart and cart.get('discount_type') and (
            cart.get('discount_percent') or cart.get('discount_cents')):
        return {'valid': False, 'error': 'This coupon cannot be combined with other discounts.'}

    discount = calculate_coupon_discount(coupon, cart_items, cart_subtotal, conn)
    logger.info(f"[Coupon Validate] Coupon {code} validated successfully. Discount: {discount} cents")

    return {
        'valid': True,
        'coupon': {
            'id': coupon['id'],
            'name': coupon['name'],
            'code': coupon['code'],
            'discount_type': coupon['discount_type'],
            'discount_cents': coupon['discount_cents'],
            'discount_percent': coupon['discount_percent'],
            'cannot_combine': bool(coupon['cannot_combine'])
        },
        'discount_cents': discount
    }


def coupon_result_json(result):
    """evaluate_coupon()'s result as the dollar JSON the sales page reads."""
    if not result.get('valid'):
        return result
    c = result['coupon']
    value = (c.get('discount_percent') if (c.get('discount_type') or '').endswith('percent')
             else money.to_dollars(c.get('discount_cents') or 0))
    coupon = {k: v for k, v in c.items() if k not in ('discount_cents', 'discount_percent')}
    coupon['discount_value'] = value
    return {'valid': True, 'coupon': coupon,
            'discount_amount': money.to_dollars(result['discount_cents'])}


def calculate_coupon_discount(coupon, cart_items, cart_subtotal, conn):
    """The discount (cents) a (valid) coupon gives on these cart items."""
    discount_type = coupon['discount_type']
    discount_cents = int(coupon['discount_cents'] or 0)
    discount_percent = coupon['discount_percent'] or 0

    if discount_type == 'order_dollar':
        return min(discount_cents, cart_subtotal)

    if discount_type == 'order_percent':
        return calculate_percentage(cart_subtotal, discount_percent)

    if discount_type in ('item_dollar', 'item_percent'):
        target_ids = _coupon_target_ids(conn, coupon['id'])
        total_discount = 0
        for item in cart_items:
            if _int_or_none(item.get('inventory_item_id')) in target_ids:
                item_total = int(item.get('unit_price_cents') or 0) * int(item.get('quantity', 1))
                if discount_type == 'item_dollar':
                    # Applied once per item type, not per unit
                    total_discount += min(discount_cents, item_total)
                else:
                    total_discount += calculate_percentage(item_total, discount_percent)
        return total_discount

    if discount_type == 'bogo_free':
        # Buy X get 1 free (same item): for every buy_qty + 1, one is free
        buy_qty = coupon['buy_quantity']
        target_ids = _coupon_target_ids(conn, coupon['id'])
        total_discount = 0
        for item in cart_items:
            if _int_or_none(item.get('inventory_item_id')) in target_ids:
                qty = int(item.get('quantity', 1))
                unit_price = int(item.get('unit_price_cents') or 0)
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
                unit_price = int(item.get('unit_price_cents') or 0)
                base_amount = (qty // (buy_qty + 1)) * unit_price
                total_discount += calculate_percentage(base_amount, discount_percent)
        return total_discount

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
                return int(item.get('unit_price_cents') or 0)  # one free
        return 0

    return 0
