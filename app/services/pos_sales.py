"""
POS sales after the fact: correcting and voiding recorded sales, and the
item sale-price ("sales manager") fields.

A finished sale is permanent (schema v23 triggers refuse UPDATE/DELETE on
the sales tables). Edit Sale and Void record a correction beside it instead,
dated when it is made (like a refund), so reports count it on that day:

  pos_sale_corrections        who, when (UTC), why, the order, and the money
                              deltas (subtotal, discount, coupon, cash
                              discount, tax, total)
  pos_sale_correction_items   per line: quantity delta, unit price after,
                              line total delta, new name, and the change to
                              the line's share of the sale (item reports)
  pos_payments                the money that changed hands because of it
                              (correction_id set; negative = paid back to
                              the customer, positive = collected)

pos_order_state.order_state() replays original + corrections + refunds; an
edit starts from that net state. Re-pricing reuses the sale's own
adjustments (sale_adjustments / reprice_sale): the coupon and cash discount
scale down with the subtotal, never up; tax at the order's stored rate.

Every stock change goes through inventory.adjust_stock (relative, logged,
never refused: allow_negative=True, as sales always were). Each operation
runs in one write transaction (inventory.write_transaction): it commits on
its own, or joins the caller's open transaction.

No Flask request/session here: the acting user (object with .id and
.username), the client IP for the audit log and the form inputs are passed
in explicitly.
"""
import json
import logging
from collections import namedtuple
from fractions import Fraction

from app.services import inventory, money
from app.services.inventory import write_transaction
from app.services.pos_ledger import REFUND_METHODS, utc_now_str
from app.services.pos_order_state import (load_order, order_state, cash_discount_of,
                                         OrderNotFound as _StateOrderNotFound)
from app.services.pos_pricing import calculate_line_total, calculate_tax

logger = logging.getLogger(__name__)

# Stock movements recorded against the order: (reason code, note detail)
REASON_ORDER_VOIDED = (inventory.REASON_SALE_VOID, 'sale voided')
REASON_EDIT_REMOVED = (inventory.REASON_SALE_EDIT, 'item removed')
REASON_EDIT_QTY_DOWN = (inventory.REASON_SALE_EDIT, 'qty decreased')
REASON_EDIT_QTY_UP = (inventory.REASON_SALE_EDIT, 'qty increased')

_MONEY_KEYS = ('subtotal', 'discount', 'coupon', 'cash_discount', 'tax', 'total')


class SaleError(Exception):
    """A sale operation that was refused (message is user-facing)."""


class OrderNotFound(SaleError):
    def __init__(self):
        super().__init__('Order not found.')


class SaleVoided(SaleError):
    def __init__(self):
        super().__init__('This sale was voided and can no longer be changed.')


class SaleHasRefunds(SaleError):
    """Void refused: part of the sale was already refunded."""

    def __init__(self):
        super().__init__("This sale has refunds, so it can't be voided. Refund what is left instead.")


class RefundedLine(SaleError):
    """An edit that would take back units (or money) already refunded."""


class BelowRefunded(SaleError):
    def __init__(self, refunded_cents):
        super().__init__(f"{money.fmt(refunded_cents)} of this sale was already refunded, so its total "
                         f"can't be corrected below that.")


class UnknownMethod(SaleError):
    def __init__(self):
        super().__init__('Unknown payment method.')


class InvalidSaleItems(SaleError):
    def __init__(self):
        super().__init__('Quantities must be whole numbers of at least 1 and prices cannot be negative. '
                         'Use the remove button to take an item off the sale.')


# money_cents: what the correction moved (negative = paid back to the
# customer); method: the tender it moved in (None when nothing moved)
EditResult = namedtuple('EditResult', 'order_number changes new_total_cents correction_id money_cents method')
# payments: {method: cents} paid back (negative)
VoidResult = namedtuple('VoidResult', 'order_number correction_id restored_units money_cents payments')

# One edited line as posted: the order-item id and its new values (raw strings)
SaleLineEdit = namedtuple('SaleLineEdit', 'item_id quantity price name')


def _audit(conn, action, user, order_number, details, ip):
    conn.execute('''
        INSERT INTO pos_audit_log (action, user_id, user_name, target_type, target_id, details, ip_address)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (action, user.id, user.username, 'order', str(order_number), json.dumps(details), ip))


def _restock(conn, item_id, units, reason, user_ref, order_number, order_id):
    """Put units back (or take them off when negative): one movement
    referencing the order. reason is one of the REASON_* (code, detail)
    pairs above. Skipped when the inventory item no longer exists."""
    if not item_id or not units:
        return 0
    code, detail = reason
    try:
        inventory.adjust_stock(conn, item_id, units, code, user_ref, f"{order_number}: {detail}",
                               ref_type=inventory.REF_ORDER, ref_id=order_id, allow_negative=True)
    except inventory.ItemNotFound:
        logger.warning(f"Order {order_number}: inventory item {item_id} no longer exists; stock not changed")
        return 0
    return units


def has_refunds(conn, order_id):
    return conn.execute('SELECT 1 FROM pos_refunds WHERE order_id = ? LIMIT 1',
                        (order_id,)).fetchone() is not None


def _state(conn, order_id=None, order_number=None):
    row = load_order(conn, order_id=order_id, order_number=order_number)
    if row is None:
        raise OrderNotFound()
    try:
        return order_state(conn, row)
    except _StateOrderNotFound:
        raise OrderNotFound() from None


# ---------------------------------------------------------------------------
# Re-pricing
# ---------------------------------------------------------------------------

def sale_adjustments(conn, order, default_tax_rate=0.0):
    """The order-level adjustments of a completed sale, as recorded at checkout.

    Re-pricing a corrected sale must reuse these (not today's settings).
    Money is cents:
      discount_pct    - (discount cents, subtotal cents) when the order discount
                        was entered as a percentage (it is stored in cents), so
                        it scales exactly with the new subtotal; else None
      discount_fixed  - order discount in cents, when entered as an amount
      coupon          - coupon cents redeemed on this order
      cash_discount   - cash discount cents (pos_orders.cash_discount_cents,
                        else payment_details)
      tax_rate        - the tax rate in force at the time of sale
                        (default_tax_rate for very old orders without one)
      orig_subtotal   - the subtotal at checkout (what coupon/cash discount scale against)
    """
    discount = order['discount_amount_cents'] or 0
    subtotal = order['subtotal_cents'] or 0
    pct = None
    if discount and order['discount_type'] == 'percent' and subtotal > 0:
        pct = (discount, subtotal)
    coupon = conn.execute(
        'SELECT COALESCE(SUM(discount_applied_cents), 0) AS c FROM pos_coupon_redemptions WHERE order_id = ?',
        (order['id'],)).fetchone()['c']
    cash_discount = cash_discount_of(order)
    return {
        'discount_pct': pct,
        'discount_fixed': 0 if pct is not None else discount,
        'coupon': int(coupon or 0),
        'cash_discount': cash_discount,
        'tax_rate': float(order['tax_rate'] if order['tax_rate'] is not None else default_tax_rate),
        'orig_subtotal': subtotal,
    }


def reprice_parts(new_subtotal, adj):
    """{'discount', 'coupon', 'cash_discount', 'tax', 'total'} in cents for a
    sale whose lines add up to new_subtotal; mirrors pos-edit-sale.js.

    The coupon and cash discount were recorded for the original sale. When
    items come off they shrink with the subtotal (never grow), so removing
    most of a sale can't leave the whole coupon on what remains.
    """
    orig = adj.get('orig_subtotal') or 0

    def scaled(cents):
        if orig > 0 and new_subtotal < orig:
            return money.prorate(cents, new_subtotal, orig)
        return cents

    coupon = scaled(adj['coupon'])
    cash_discount = scaled(adj['cash_discount'])
    if adj['discount_pct'] is not None:
        discount, base = adj['discount_pct']
        order_discount = money.prorate(new_subtotal, discount, base)
    else:
        order_discount = min(adj['discount_fixed'], new_subtotal)
    after = max(0, new_subtotal - order_discount - min(coupon, max(0, new_subtotal - order_discount)))
    tax = calculate_tax(after, adj['tax_rate'])
    total = max(0, after + tax - cash_discount)
    return {'discount': order_discount, 'coupon': coupon, 'cash_discount': cash_discount,
            'tax': tax, 'total': total}


def reprice_sale(new_subtotal, adj):
    """(order_discount, tax, total) in cents (see reprice_parts)."""
    p = reprice_parts(new_subtotal, adj)
    return p['discount'], p['tax'], p['total']


def _parse_lines(lines):
    parsed = []
    try:
        for line in lines:
            qty = int(line.quantity)
            price = money.parse(line.price)   # dollars as typed -> cents
            if qty < 1 or price is None:
                raise ValueError
            parsed.append((int(line.item_id), qty, price, (line.name or '').strip()))
    except (ValueError, TypeError):
        raise InvalidSaleItems() from None
    return parsed


# ---------------------------------------------------------------------------
# Item shares (for item reports)
# ---------------------------------------------------------------------------

def _allocate(fractions, total):
    """Round exact shares to whole cents that add up to `total` (largest
    remainder). `fractions` is {key: Fraction}."""
    if not fractions:
        return {}
    floors = {k: v.numerator // v.denominator for k, v in fractions.items()}
    extra = total - sum(floors.values())
    order = sorted(fractions, key=lambda k: (fractions[k] - floors[k], -k if isinstance(k, int) else 0),
                   reverse=True)
    out = dict(floors)
    if extra >= 0:
        for k in order[:extra]:
            out[k] += 1
    else:   # (only when the shares don't add up to total: legacy data)
        for k in order[::-1][:-extra]:
            out[k] -= 1
    return out


def _line_shares(state, new_line_totals, new_total, new_tax):
    """Per-line change to each line's share of the sale ex. tax and of its
    tax: {line id: (revenue_delta, tax_delta)}, whole cents adding up to the
    change in (total - tax) and in tax.

    A line's share is its line total times (total - tax) / sum of line
    totals, as Top Sellers spreads a sale over its lines; before a
    correction it is the checkout share plus earlier corrections' deltas."""
    orig = state['original']
    lines = state['lines']
    orig_lines_total = sum(ln['orig_line_total_cents'] for ln in lines)
    net = state['net']
    new_lines_total = sum(new_line_totals.values())

    def share(lt, amount, whole):
        return Fraction(lt * amount, whole) if whole > 0 else Fraction(0)

    rev, tax = {}, {}
    for ln in lines:
        cur_rev = share(ln['orig_line_total_cents'], orig['total_cents'] - orig['tax_cents'], orig_lines_total) \
            + ln['revenue_delta_cents']
        cur_tax = share(ln['orig_line_total_cents'], orig['tax_cents'], orig_lines_total) + ln['tax_delta_cents']
        lt = new_line_totals[ln['id']]
        rev[ln['id']] = share(lt, new_total - new_tax, new_lines_total) - cur_rev
        tax[ln['id']] = share(lt, new_tax, new_lines_total) - cur_tax
    rev_target = (new_total - new_tax) - (net['total_cents'] - net['tax_cents'])
    tax_target = new_tax - net['tax_cents']
    if orig_lines_total <= 0:   # nothing was spread at checkout: spread exactly what is there now
        rev_target = money.div_round(sum(rev.values()).numerator, sum(rev.values()).denominator)
        tax_target = money.div_round(sum(tax.values()).numerator, sum(tax.values()).denominator)
    rev_cents = _allocate(rev, rev_target)
    tax_cents = _allocate(tax, tax_target)
    return {k: (rev_cents[k], tax_cents[k]) for k in rev}


def _insert_correction(conn, state, kind, user, reason, method, deltas, created_at):
    cur = conn.execute('''
        INSERT INTO pos_sale_corrections (
            order_id, kind, user_id, user_name, reason, method,
            subtotal_delta_cents, discount_delta_cents, coupon_delta_cents, cash_discount_delta_cents,
            tax_delta_cents, total_delta_cents, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (state['order_id'], kind, user.id, user.username, reason or None, method,
          deltas['subtotal'], deltas['discount'], deltas['coupon'], deltas['cash_discount'],
          deltas['tax'], deltas['total'], created_at))
    return cur.lastrowid


def _insert_item(conn, correction_id, line_id, qty_delta, unit_price, lt_delta, name, shares):
    rev, tax = shares
    conn.execute('''
        INSERT INTO pos_sale_correction_items (
            correction_id, order_item_id, qty_delta, unit_price_cents, line_total_delta_cents, name,
            revenue_delta_cents, tax_delta_cents
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (correction_id, line_id, qty_delta, unit_price, lt_delta, name, rev, tax))


def _insert_payment(conn, order_id, method, amount, correction_id, created_at):
    conn.execute('''INSERT INTO pos_payments (order_id, method, amount_cents, tendered_cents,
                                              change_given_cents, created_at, correction_id)
                    VALUES (?, ?, ?, NULL, 0, ?, ?)''', (order_id, method, amount, created_at, correction_id))


# ---------------------------------------------------------------------------
# Edit (a correction)
# ---------------------------------------------------------------------------

def edit_sale(conn, order_id, lines, deleted_ids, user, *, method=None, reason='',
              default_tax_rate=0.0, ip=None):
    """Record a manager's correction of a recorded sale. The sale itself is
    not changed.

    lines        [SaleLineEdit(item_id, quantity, price, name)] for the lines kept
                 (lines not posted stay as they are)
    deleted_ids  order-item ids removed from the sale
    method       'cash' | 'card': how a difference is paid back / collected
                 (default: card sales to card, others cash)
    reason       free-text note kept with the correction

    Starts from the sale's net state (earlier corrections and refunds
    applied). Quantity changes move stock (logged against the order); the
    sale is re-priced with its own discount, coupon, cash discount and tax
    rate; a difference in the total is a payments-ledger row dated now; an
    audit row lists the changes. Nothing is recorded when nothing changed.
    Raises OrderNotFound, SaleVoided, InvalidSaleItems, RefundedLine,
    BelowRefunded, UnknownMethod (nothing written).
    Returns EditResult.
    """
    if method is not None and method not in REFUND_METHODS:
        raise UnknownMethod()

    with write_transaction(conn):
        state = _state(conn, order_id=order_id)
        if state['voided']:
            raise SaleVoided()
        order = state['order']
        order_number = order['order_number']

        # Validate everything before touching anything
        parsed = _parse_lines(lines)
        by_id = {ln['id']: ln for ln in state['lines']}
        target = {ln['id']: (ln['quantity'], ln['unit_price_cents'], ln['name']) for ln in state['lines']}
        removed = set()
        for raw_id in deleted_ids:   # a double-posted removal counts once
            try:
                line = by_id.get(int(raw_id))
            except (TypeError, ValueError):
                continue
            if line is not None and line['quantity'] > 0:
                removed.add(line['id'])
                target[line['id']] = (0, line['unit_price_cents'], line['name'])
        for item_id, qty, price, name in parsed:
            line = by_id.get(item_id)
            if line is None or item_id in removed or line['quantity'] <= 0:
                continue
            target[item_id] = (qty, price, name or line['name'])

        for line_id, (qty, price, _name) in target.items():
            line = by_id[line_id]
            if line['refunded_qty'] and qty < line['refunded_qty']:
                raise RefundedLine(f"{line['refunded_qty']} of {line['name']} already refunded, so it can't go "
                                   f"below {line['refunded_qty']}.")
            if line['refunded_qty'] and price != line['unit_price_cents']:
                raise RefundedLine(f"{line['name']} has refunded units, so its price can't be changed.")

        new_lt = {}
        for ln in state['lines']:
            qty, price, _name = target[ln['id']]
            if qty == ln['quantity'] and price == ln['unit_price_cents']:
                new_lt[ln['id']] = ln['line_total_cents']
            elif qty <= 0:
                new_lt[ln['id']] = 0
            else:   # the line keeps its own discount (it was part of what was charged)
                new_lt[ln['id']] = calculate_line_total(price, qty, ln['discount_type'], ln['discount_percent'],
                                                        ln['discount_cents'])

        old_subtotal = sum(ln['line_total_cents'] for ln in state['lines'])
        new_subtotal = sum(new_lt.values())
        adj = sale_adjustments(conn, order, default_tax_rate)
        before, after = reprice_parts(old_subtotal, adj), reprice_parts(new_subtotal, adj)
        deltas = {k: after[k] - before[k] for k in _MONEY_KEYS if k != 'subtotal'}
        deltas['subtotal'] = new_subtotal - old_subtotal
        net = state['net']
        new_total = net['total_cents'] + deltas['total']
        new_tax = net['tax_cents'] + deltas['tax']

        changes = []
        for ln in state['lines']:
            qty, price, name = target[ln['id']]
            if ln['id'] in removed:
                changes.append(f"Removed: {ln['name']} x{ln['quantity']}")
                continue
            if qty != ln['quantity']:
                changes.append(f"{ln['name']}: qty {ln['quantity']} → {qty}")
            if price != ln['unit_price_cents']:
                changes.append(f"{ln['name']}: price {money.fmt(ln['unit_price_cents'])} → {money.fmt(price)}")
            if name != ln['name']:
                changes.append(f"Renamed: {ln['name']} → {name}")
        if not changes:
            return EditResult(order_number, [], net['total_cents'], None, 0, None)

        if new_total < state['refunded_cents']:
            raise BelowRefunded(state['refunded_cents'])
        if new_total != net['total_cents']:
            changes.append(f"Total: {money.fmt(net['total_cents'])} → {money.fmt(new_total)}")

        now = utc_now_str()
        moved = deltas['total']
        tender = (method or state['default_method']) if moved else None
        correction_id = _insert_correction(conn, state, 'edit', user, reason, tender, deltas, now)

        shares = _line_shares(state, new_lt, new_total, new_tax)
        for ln in state['lines']:
            qty, price, name = target[ln['id']]
            qty_delta = qty - ln['quantity']
            lt_delta = new_lt[ln['id']] - ln['line_total_cents']
            renamed = name if name != ln['name'] else None
            if qty_delta or price != ln['unit_price_cents'] or lt_delta or renamed or any(shares[ln['id']]):
                _insert_item(conn, correction_id, ln['id'], qty_delta, price, lt_delta, renamed, shares[ln['id']])
            if qty_delta:   # positive = more units sold: they leave the shelf
                reason_code = (REASON_EDIT_REMOVED if ln['id'] in removed
                               else REASON_EDIT_QTY_UP if qty_delta > 0 else REASON_EDIT_QTY_DOWN)
                _restock(conn, ln['inventory_item_id'], -qty_delta, reason_code, user.username,
                         order_number, state['order_id'])

        if moved:
            _insert_payment(conn, state['order_id'], tender, moved, correction_id, now)

        # the audit log is a human-readable record: totals in dollars, as always
        _audit(conn, 'edit_sale', user, order_number,
               {'changes': changes, 'correction_id': correction_id,
                'old_total': money.to_dollars(net['total_cents']), 'new_total': money.to_dollars(new_total),
                'money': money.to_dollars(moved), 'method': tender, 'reason': reason or ''}, ip)

    logger.info(f"Sale {order_number} corrected by {user.username} (correction {correction_id}): "
                f"{money.fmt(moved)} {tender or ''}")
    return EditResult(order_number, changes, new_total, correction_id, moved, tender)


# ---------------------------------------------------------------------------
# Void (a correction reversing the whole sale)
# ---------------------------------------------------------------------------

def void_payments(state):
    """{method: cents} a void pays back (negative), per tender actually taken
    (checkout and earlier corrections). Any part of the total the ledger
    does not cover (sales recorded without one) goes back in the sale's
    default refund method."""
    by_method = {m: c for m, c in state['payments_by_method'].items() if c}
    gap = state['net']['total_cents'] - sum(by_method.values())
    if gap:
        by_method[state['default_method']] = by_method.get(state['default_method'], 0) + gap
    return {m: -c for m, c in by_method.items() if c}


def void_order(conn, order_number, restore_inventory, user, *, reason='', ip=None):
    """Void a sale: one correction taking every line off it and paying back
    what was taken, per tender. The sale stays in the history, shown as
    voided; a voided sale can't be edited, refunded or voided again.

    restore_inventory puts the units back on the shelf (sale_void movements
    referencing the order). Coupon uses go back to the coupon.
    Raises OrderNotFound, SaleVoided, SaleHasRefunds (refund the rest
    instead). Returns VoidResult.
    """
    with write_transaction(conn):
        state = _state(conn, order_number=order_number)
        if state['voided']:
            raise SaleVoided()
        if state['has_refunds']:
            raise SaleHasRefunds()
        order_id = state['order_id']
        net = state['net']
        deltas = {k: -net[f'{k}_cents'] for k in _MONEY_KEYS}
        payments = void_payments(state)
        method = next(iter(payments)) if len(payments) == 1 else ('split' if payments else None)

        now = utc_now_str()
        correction_id = _insert_correction(conn, state, 'void', user, reason, method, deltas, now)
        shares = _line_shares(state, {ln['id']: 0 for ln in state['lines']}, 0, 0)
        restored = 0
        for ln in state['lines']:
            if ln['quantity'] or ln['line_total_cents'] or any(shares[ln['id']]):
                _insert_item(conn, correction_id, ln['id'], -ln['quantity'], ln['unit_price_cents'],
                             -ln['line_total_cents'], None, shares[ln['id']])
            if restore_inventory and ln['quantity'] > 0:
                restored += _restock(conn, ln['inventory_item_id'], ln['quantity'], REASON_ORDER_VOIDED,
                                     str(user.id), order_number, order_id)
        for m, cents in payments.items():
            _insert_payment(conn, order_id, m, cents, correction_id, now)

        # The sale didn't happen: its coupon use goes back
        for r in conn.execute('SELECT coupon_id FROM pos_coupon_redemptions WHERE order_id = ?',
                              (order_id,)).fetchall():
            conn.execute('''UPDATE pos_coupons SET current_uses = MAX(COALESCE(current_uses, 0) - 1, 0)
                            WHERE id = ?''', (r['coupon_id'],))

        _audit(conn, 'void_order', user, order_number,
               {'correction_id': correction_id, 'restore_inventory': bool(restore_inventory),
                'total': money.to_dollars(net['total_cents']),
                'paid_back': {m: money.to_dollars(c) for m, c in payments.items()}, 'reason': reason or ''}, ip)

    logger.info(f"Order {order_number} voided by {user.username}"
                + (f", {restored} unit(s) restored to stock" if restore_inventory else ""))
    return VoidResult(order_number, correction_id, restored, sum(payments.values()), payments)


# ---------------------------------------------------------------------------
# Item sale prices (sales manager): sale fields only, never quantity
# ---------------------------------------------------------------------------

def start_item_sale(conn, sku, sale_price_cents, sale_start=None, sale_end=None, end_on_stock=False):
    """Put an item on sale (price in cents). Raises inventory.ItemNotFound for
    an unknown SKU. Returns the item id."""
    with write_transaction(conn):
        item = conn.execute('SELECT id FROM inventory_items WHERE sku = ?', (sku,)).fetchone()
        if not item:
            raise inventory.ItemNotFound(sku)
        conn.execute('''
            UPDATE inventory_items
            SET sale_price_cents = ?, sale_start = ?, sale_end = ?,
                sale_enabled = 1, sale_end_on_stock = ?
            WHERE id = ?
        ''', (sale_price_cents, sale_start or None, sale_end or None, 1 if end_on_stock else 0, item['id']))
    return item['id']


def stop_item_sale(conn, item_id):
    """Take an item off sale."""
    with write_transaction(conn):
        conn.execute('UPDATE inventory_items SET sale_enabled = 0 WHERE id = ?', (item_id,))
