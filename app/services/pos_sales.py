"""
POS sales after the fact: editing and deleting recorded sales, and the
item sale-price ("sales manager") fields.

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

from app.services import inventory
from app.services.inventory import write_transaction
from app.services.pos_ledger import rebuild_order_payments
from app.services.pos_pricing import calculate_line_total, calculate_tax, round_money

logger = logging.getLogger(__name__)

REASON_ORDER_DELETED = 'Order Deleted'
REASON_EDIT_REMOVED = 'Sale Edit (item removed)'
REASON_EDIT_QTY_DOWN = 'Sale Edit (qty decreased)'
REASON_EDIT_QTY_UP = 'Sale Edit (qty increased)'


class SaleError(Exception):
    """A sale operation that was refused (message is user-facing)."""


class OrderNotFound(SaleError):
    def __init__(self):
        super().__init__('Order not found.')


class SaleHasRefunds(SaleError):
    def __init__(self):
        super().__init__('This sale has refunds and can no longer be edited. Use a refund instead.')


class InvalidSaleItems(SaleError):
    def __init__(self):
        super().__init__('Quantities must be whole numbers of at least 1 and prices cannot be negative. '
                         'Use the remove button to take an item off the sale.')


DeleteResult = namedtuple('DeleteResult', 'order_number restored_units')
EditResult = namedtuple('EditResult', 'order_number changes new_total date_rejected')

# One edited line as posted: the order-item id and its new values (raw strings)
SaleLineEdit = namedtuple('SaleLineEdit', 'item_id quantity price name')


def _audit(conn, action, user, order_number, details, ip):
    conn.execute('''
        INSERT INTO pos_audit_log (action, user_id, user_name, target_type, target_id, details, ip_address)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (action, user.id, user.username, 'order', str(order_number), json.dumps(details), ip))


def _restock(conn, item_id, units, reason, user_ref, order_number):
    """Put units back (or take them off when negative); skipped when the
    inventory item no longer exists."""
    if not item_id or not units:
        return 0
    try:
        inventory.adjust_stock(conn, item_id, units, reason, user_ref, order_number,
                               allow_negative=True)
    except inventory.ItemNotFound:
        logger.warning(f"Order {order_number}: inventory item {item_id} no longer exists; stock not changed")
        return 0
    return units


def has_refunds(conn, order_id):
    return conn.execute('SELECT 1 FROM pos_refunds WHERE order_id = ? LIMIT 1',
                        (order_id,)).fetchone() is not None


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def delete_order(conn, order_number, restore_inventory, user, ip=None):
    """Hard-delete a sale and everything hanging off it (refunds, payments,
    coupon redemptions, lines), with an audit row.

    restore_inventory puts back what actually left the shelf: units sold
    minus any a refund already restocked, matched on the inventory item id
    (the line's sku is whatever was scanned, often a UPC).
    Raises OrderNotFound. Returns DeleteResult.
    """
    with write_transaction(conn):
        order = conn.execute('SELECT id FROM pos_orders WHERE order_number = ?',
                             (order_number,)).fetchone()
        if not order:
            raise OrderNotFound()
        order_id = order['id']

        restored = 0
        if restore_inventory:
            lines = conn.execute('''
                SELECT oi.inventory_item_id, oi.quantity,
                       COALESCE((SELECT SUM(ri.quantity) FROM pos_refund_items ri
                                 WHERE ri.order_item_id = oi.id AND ri.restock_action = 'restock'), 0)
                           AS restocked
                FROM pos_order_items oi WHERE oi.order_id = ?
            ''', (order_id,)).fetchall()
            for line in lines:
                units = line['quantity'] - line['restocked']
                if units > 0:
                    restored += _restock(conn, line['inventory_item_id'], units,
                                         REASON_ORDER_DELETED, str(user.id), order_number)
            logger.info(f"Restored {restored} units to inventory for order {order_number}")

        # Foreign keys are not enforced, so nothing cascades on its own
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
        _audit(conn, 'delete_order', user, order_number,
               {'restore_inventory': restore_inventory}, ip)

    logger.info(f"Order {order_number} deleted by admin {user.username}"
                + (" with inventory restored" if restore_inventory else ""))
    return DeleteResult(order_number, restored)


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------

def sale_adjustments(conn, order, default_tax_rate=0.0):
    """The order-level adjustments of a completed sale, as recorded at checkout.

    Re-pricing an edited sale must reuse these (not today's settings):
      discount_pct    - order discount as a fraction of the subtotal, when it
                        was entered as a percentage (stored in dollars)
      discount_fixed  - order discount in dollars, when entered as dollars
      coupon          - coupon dollars redeemed on this order
      cash_discount   - cash discount dollars from payment_details
      tax_rate        - the tax rate in force at the time of sale
                        (default_tax_rate for very old orders without one)
    """
    discount_dollars = float(order['discount_amount'] or 0)
    subtotal = float(order['subtotal'] or 0)
    pct = None
    if discount_dollars and order['discount_type'] == 'percent' and subtotal > 0:
        pct = discount_dollars / subtotal
    coupon = conn.execute(
        'SELECT COALESCE(SUM(discount_applied), 0) AS c FROM pos_coupon_redemptions WHERE order_id = ?',
        (order['id'],)).fetchone()['c']
    try:
        cash_discount = float((json.loads(order['payment_details'] or '{}') or {}).get('cash_discount') or 0)
    except (ValueError, TypeError, AttributeError):
        cash_discount = 0.0
    return {
        'discount_pct': pct,
        'discount_fixed': 0.0 if pct is not None else discount_dollars,
        'coupon': float(coupon or 0),
        'cash_discount': cash_discount,
        'tax_rate': float(order['tax_rate'] if order['tax_rate'] is not None else default_tax_rate),
    }


def reprice_sale(new_subtotal, adj):
    """(order_discount, tax, total) for an edited sale; mirrors edit_sale.html's preview."""
    if adj['discount_pct'] is not None:
        order_discount = round_money(new_subtotal * adj['discount_pct'])
    else:
        order_discount = round_money(min(adj['discount_fixed'], new_subtotal))
    after = max(0.0, new_subtotal - order_discount - min(adj['coupon'], max(0.0, new_subtotal - order_discount)))
    tax = calculate_tax(after, adj['tax_rate'])
    total = round_money(max(0.0, after + tax - adj['cash_discount']))
    return order_discount, tax, total


def _parse_lines(lines):
    parsed = []
    try:
        for line in lines:
            qty = int(line.quantity)
            price = round(float(line.price), 2)
            if qty < 1 or price < 0:
                raise ValueError
            parsed.append((int(line.item_id), qty, price, line.name or ''))
    except (ValueError, TypeError):
        raise InvalidSaleItems() from None
    return parsed


def edit_sale(conn, order_id, lines, deleted_ids, user, *, new_date='', new_time='',
              default_tax_rate=0.0, ip=None):
    """Apply a manager's edit to a recorded sale.

    lines        [SaleLineEdit(item_id, quantity, price, name)] for the lines kept
    deleted_ids  order-item ids removed from the sale
    new_date/new_time  store-local date/time ('' keeps the original)

    Removed lines and quantity changes move stock (logged against the order);
    the sale is re-priced with its own discount, coupon, cash discount and
    tax rate; the payments ledger is rebuilt; an audit row lists the changes.
    Raises OrderNotFound, SaleHasRefunds, InvalidSaleItems (nothing changed).
    Returns EditResult (date_rejected: the date/time did not parse and the
    sale date was left alone).
    """
    from app.utils.helpers import local_to_utc_str, utc_to_local_dt

    with write_transaction(conn):
        order = conn.execute('SELECT * FROM pos_orders WHERE id = ?', (order_id,)).fetchone()
        if not order:
            raise OrderNotFound()
        if has_refunds(conn, order_id):
            raise SaleHasRefunds()

        old_items = {row['id']: dict(row) for row in conn.execute(
            'SELECT * FROM pos_order_items WHERE order_id = ?', (order_id,)).fetchall()}

        # Validate everything before touching stock or the order
        parsed = _parse_lines(lines)

        adj = sale_adjustments(conn, order, default_tax_rate)
        order_number = order['order_number']
        changes = []

        # 1. Removed lines go back on the shelf
        for raw_id in deleted_ids:
            if not raw_id:
                continue
            del_id = int(raw_id)
            old = old_items.get(del_id)
            if not old:
                continue
            _restock(conn, old.get('inventory_item_id'), old['quantity'], REASON_EDIT_REMOVED,
                     user.username, order_number)
            conn.execute('DELETE FROM pos_order_items WHERE id = ?', (del_id,))
            changes.append(f"Removed: {old['name']} x{old['quantity']}")

        # 2. Kept lines: quantity (stock), price, name
        new_subtotal = 0
        for item_id, new_qty, new_price, new_name in parsed:
            old = old_items.get(item_id)
            if not old:
                continue
            old_qty = old['quantity']
            old_price = old['unit_price']

            returned = old_qty - new_qty  # positive = units back on the shelf
            if returned:
                _restock(conn, old.get('inventory_item_id'), returned,
                         REASON_EDIT_QTY_DOWN if returned > 0 else REASON_EDIT_QTY_UP,
                         user.username, order_number)

            # Keep the line's own discount (it was part of what was charged)
            line_total = calculate_line_total(new_price, new_qty,
                                              old.get('discount_amount') or 0,
                                              old.get('discount_type'))
            new_subtotal += line_total
            conn.execute('''
                UPDATE pos_order_items
                SET quantity = ?, unit_price = ?, line_total = ?, name = ?
                WHERE id = ?
            ''', (new_qty, new_price, line_total, new_name, item_id))

            if new_qty != old_qty:
                changes.append(f"{old['name']}: qty {old_qty} → {new_qty}")
            if abs(new_price - old_price) > 0.001:
                changes.append(f"{old['name']}: price ${old_price:.2f} → ${new_price:.2f}")
            if new_name and new_name != old['name']:
                changes.append(f"Renamed: {old['name']} → {new_name}")

        # 3. Re-price with the sale's own adjustments. (The order discount is
        # stored in dollars even when entered as a percentage.)
        new_subtotal = round(new_subtotal, 2)
        new_discount, new_tax, new_total = reprice_sale(new_subtotal, adj)

        # 4. Date/time: entered in store-local time, stored in UTC
        new_timestamp = order['created_at']
        date_rejected = False
        if new_date:
            old_local = utc_to_local_dt(order['created_at'])
            local_time = new_time or (old_local.strftime('%H:%M:%S') if old_local else '00:00:00')
            try:
                candidate = local_to_utc_str(f"{new_date} {local_time}")
                if candidate != str(order['created_at'])[:19]:
                    new_timestamp = candidate
                    old_txt = old_local.strftime('%Y-%m-%d %H:%M:%S') if old_local else order['created_at']
                    changes.append(f"Date: {old_txt} → {new_date} {local_time}")
            except ValueError:
                date_rejected = True

        if abs(new_total - order['total']) > 0.001:
            changes.append(f"Total: ${order['total']:.2f} → ${new_total:.2f}")

        conn.execute('''
            UPDATE pos_orders
            SET subtotal = ?, discount_amount = ?, tax_amount = ?, total = ?, created_at = ?
            WHERE id = ?
        ''', (new_subtotal, new_discount, new_tax, new_total, new_timestamp, order_id))
        rebuild_order_payments(conn, order_id)

        if changes:
            _audit(conn, 'edit_sale', user, order_number,
                   {'changes': changes, 'old_total': order['total'], 'new_total': new_total}, ip)

    return EditResult(order_number, changes, new_total, date_rejected)


# ---------------------------------------------------------------------------
# Item sale prices (sales manager): sale fields only, never quantity
# ---------------------------------------------------------------------------

def start_item_sale(conn, sku, sale_price, sale_start=None, sale_end=None, end_on_stock=False):
    """Put an item on sale. Raises inventory.ItemNotFound for an unknown SKU.
    Returns the item id."""
    with write_transaction(conn):
        item = conn.execute('SELECT id FROM inventory_items WHERE sku = ?', (sku,)).fetchone()
        if not item:
            raise inventory.ItemNotFound(sku)
        conn.execute('''
            UPDATE inventory_items
            SET sale_price = ?, sale_start = ?, sale_end = ?,
                sale_enabled = 1, sale_end_on_stock = ?
            WHERE id = ?
        ''', (sale_price, sale_start or None, sale_end or None, 1 if end_on_stock else 0, item['id']))
    return item['id']


def stop_item_sale(conn, item_id):
    """Take an item off sale."""
    with write_transaction(conn):
        conn.execute('UPDATE inventory_items SET sale_enabled = 0 WHERE id = ?', (item_id,))
