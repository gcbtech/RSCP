"""
POS refunds.

process_refund() records a refund in one write transaction (BEGIN IMMEDIATE
taken BEFORE reading what was already refunded, so a double-click or two
managers cannot both pass the caps):

  * a refunded unit is worth its line value times the order's paid ratio
    (order discount, coupon, cash discount and tax folded in), never more;
  * the refund never exceeds what is left of what was paid, and the refund
    that empties the order takes exactly what is left (no stray cent);
  * tax_refunded is the order's tax share of the amount (or exactly the tax
    not yet refunded on the last refund);
  * restocked units go back through inventory.adjust_stock.

No Flask request/session here: inputs, the approving manager and the
fallback acting user are passed in.
"""
import logging
from collections import namedtuple

from app.services import inventory
from app.services.pos_ledger import REFUND_METHODS, default_refund_method, money

logger = logging.getLogger(__name__)

REASON_REFUND_RESTOCK = 'Refund Restock'

# Reasons that restock by default (item is sellable and back on the shelf)
RESTOCK_REASONS = frozenset({'customer_changed_mind', 'wrong_item', 'price_adjustment'})
# Reasons that do not (damaged/defective, or nothing physically returned)
NO_RESTOCK_REASONS = frozenset({'defective', 'duplicate_charge', 'other'})


class RefundError(Exception):
    """A refund that was refused. `message` is user-facing; `order_number` is
    set when the manager goes back to that order's refund page."""

    def __init__(self, message, order_number=None):
        super().__init__(message)
        self.message = message
        self.order_number = order_number


class UnknownRefundMethod(RefundError):
    def __init__(self):
        super().__init__('Unknown refund method.')


class OrderNotFound(RefundError):
    def __init__(self):
        super().__init__('Order not found.')


class OverRefund(RefundError):
    """More units requested than are left to refund on a line."""

    def __init__(self, qty, remaining, order_number):
        super().__init__(f'Cannot refund {qty} of that item — only {remaining} left to refund.', order_number)
        self.qty = qty
        self.remaining = remaining


class NothingToRefund(RefundError):
    def __init__(self, order_number):
        super().__init__('No items available to refund (order may already be fully refunded).', order_number)


RefundResult = namedtuple('RefundResult',
                          'refund_id order_id order_number amount tax_refunded refund_method '
                          'status items_restocked items_damaged manager_username')


def default_restock_action(reason):
    """'restock' for reasons where the item comes back sellable, else 'none'."""
    return 'restock' if reason in RESTOCK_REASONS else 'none'


def _resolve_manager(conn, manager_username, fallback_user):
    """(manager id, manager username): the approving manager when known,
    else the acting user."""
    username = manager_username or fallback_user.username
    row = conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
    return (row['id'] if row else fallback_user.id), username


def process_refund(conn, order_id, refund_type, reason, user, *, reason_notes='',
                   refund_method=None, items=(), restock_actions=None, manager_username=None):
    """Refund (part of) a sale. Returns a RefundResult or raises a RefundError
    (nothing is written then).

    conn             a connection of its own; the transaction is begun and
                     committed/rolled back here
    refund_type      'full' (everything not yet refunded) or 'partial'
    items            partial refunds: [(order_item_id, quantity)] as posted (raw)
    restock_actions  {order_item_id as posted: 'restock' | 'damaged' | 'none'};
                     lines without one use the reason's default
    user             acting user (.id, .username); the fallback manager
    manager_username the manager who authorised the refund, when not `user`
    """
    if refund_method is not None and refund_method not in REFUND_METHODS:
        raise UnknownRefundMethod()
    restock_actions = restock_actions or {}

    conn.execute('BEGIN IMMEDIATE')
    try:
        result = _record_refund(conn, order_id, refund_type, reason, reason_notes, refund_method,
                                items, restock_actions, user, manager_username)
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    logger.info(f"Refund processed: Order {result.order_number}, Amount: ${result.amount:.2f}, "
                f"Manager: {result.manager_username}")
    return result


def _record_refund(conn, order_id, refund_type, reason, reason_notes, refund_method,
                   items, restock_actions, user, manager_username):
    order = conn.execute('SELECT * FROM pos_orders WHERE id = ?', (order_id,)).fetchone()
    if not order:
        raise OrderNotFound()
    order_id = order['id']
    order_number = order['order_number']

    # What of each line is ALREADY refunded, and the dollars already refunded
    lines = conn.execute('''
        SELECT oi.id, oi.quantity, oi.line_total, oi.inventory_item_id,
               COALESCE((SELECT SUM(ri.quantity) FROM pos_refund_items ri
                         JOIN pos_refunds r ON ri.refund_id = r.id
                         WHERE ri.order_item_id = oi.id), 0) AS refunded_qty
        FROM pos_order_items oi
        WHERE oi.order_id = ?
    ''', (order_id,)).fetchall()
    lines_by_id = {row['id']: row for row in lines}
    already = conn.execute('''
        SELECT COALESCE(SUM(amount), 0) AS t,
               COALESCE(SUM(COALESCE(tax_refunded, CASE WHEN ? > 0 THEN amount * ? / ? ELSE 0 END)), 0) AS tax
        FROM pos_refunds WHERE order_id = ?
    ''', (order['total'], order['tax_amount'], order['total'], order_id)).fetchone()
    already_refunded_total = already['t']
    already_tax_refunded = already['tax']
    remaining_paid = round(order['total'] - already_refunded_total, 2)

    # What the customer actually paid per $1 of line value
    lines_total = sum(r['line_total'] for r in lines)
    paid_ratio = (order['total'] / lines_total) if lines_total > 0 else 0

    default_restock = default_restock_action(reason)
    refund_amount = 0
    to_refund = []
    items_restocked = 0
    items_damaged = 0

    def _add(line, qty, action):
        nonlocal items_restocked, items_damaged
        per_unit = (line['line_total'] / line['quantity']) if line['quantity'] else 0
        value = round(per_unit * qty, 2)
        to_refund.append({
            'order_item_id': line['id'],
            'quantity': qty,
            'amount': value,  # list value; reports apply the paid/tax ratios
            'restock_action': action,
            'inventory_item_id': line['inventory_item_id'],
        })
        if action == 'restock':
            items_restocked += qty
        elif action == 'damaged':
            items_damaged += qty
        return value

    if refund_type == 'full':
        # Everything NOT already refunded; the amount is what is left of the
        # total, so repeated full refunds can't exceed what was paid.
        refund_amount = round(order['total'] - already_refunded_total, 2)
        for line in lines:
            remaining = line['quantity'] - line['refunded_qty']
            if remaining <= 0:
                continue
            _add(line, remaining, restock_actions.get(str(line['id']), default_restock))
    else:
        for raw_id, raw_qty in items:
            try:
                qty = int(raw_qty or 0)
            except (TypeError, ValueError):
                qty = 0
            if qty <= 0:
                continue
            line = lines_by_id.get(int(raw_id)) if str(raw_id).isdigit() else None
            if not line:
                continue
            remaining = line['quantity'] - line['refunded_qty']
            if qty > remaining:
                # Refuse rather than silently clamp, so the manager sees
                # exactly what is refundable.
                raise OverRefund(qty, remaining, order_number)
            value = _add(line, qty, restock_actions.get(str(raw_id), default_restock))
            refund_amount += value * paid_ratio

        # Never more than is left of what was paid; returning every remaining
        # unit refunds exactly what is left.
        units_left = sum(r['quantity'] - r['refunded_qty'] for r in lines)
        if sum(i['quantity'] for i in to_refund) >= units_left:
            refund_amount = remaining_paid
        refund_amount = round(min(refund_amount, remaining_paid), 2)

    if not to_refund or refund_amount <= 0:
        raise NothingToRefund(order_number)

    refund_amount = money(refund_amount)
    tax_left = max(0.0, money(order['tax_amount'] - already_tax_refunded))
    if refund_amount >= remaining_paid:
        tax_refunded = tax_left
    elif order['total'] > 0:
        tax_refunded = money(refund_amount * order['tax_amount'] / order['total'])
    else:
        tax_refunded = 0.0
    tax_refunded = min(tax_refunded, tax_left, refund_amount)
    refund_method = refund_method or default_refund_method(order['payment_method'])

    manager_id, manager_name = _resolve_manager(conn, manager_username, user)

    cursor = conn.execute('''
        INSERT INTO pos_refunds (
            order_id, refund_type, amount, reason, reason_notes,
            items_restocked, items_damaged, manager_id, refund_method, tax_refunded
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (order_id, refund_type, round(refund_amount, 2), reason, reason_notes,
          items_restocked, items_damaged, manager_id, refund_method, tax_refunded))
    refund_id = cursor.lastrowid

    for item in to_refund:
        conn.execute('''
            INSERT INTO pos_refund_items (refund_id, order_item_id, quantity, amount, restock_action)
            VALUES (?, ?, ?, ?, ?)
        ''', (refund_id, item['order_item_id'], item['quantity'], item['amount'], item['restock_action']))
        if item['inventory_item_id'] and item['restock_action'] == 'restock':
            try:
                inventory.adjust_stock(conn, item['inventory_item_id'], item['quantity'],
                                       REASON_REFUND_RESTOCK, str(manager_id), allow_negative=True)
            except inventory.ItemNotFound:
                logger.warning(f"Refund on {order_number}: inventory item {item['inventory_item_id']} "
                               f"no longer exists; not restocked")

    # Fully refunded once no unit is left to refund
    units_left = sum(r['quantity'] - r['refunded_qty'] for r in lines)
    fully_refunded = refund_type == 'full' or sum(i['quantity'] for i in to_refund) >= units_left
    status = 'refunded' if fully_refunded else 'partial_refund'
    conn.execute('UPDATE pos_orders SET status = ? WHERE id = ?', (status, order_id))

    return RefundResult(refund_id, order_id, order_number, refund_amount, tax_refunded, refund_method,
                        status, items_restocked, items_damaged, manager_name)
