"""
The effective (net) state of a recorded sale.

A finished sale is permanent (schema v23): its pos_orders row, lines,
payments and coupon redemption are never changed. What happened to it
afterwards is recorded beside it, each dated when it happened:

  corrections  pos_sale_corrections (+ items): Edit Sale saves and voids;
               each changes line quantities/prices and the order's money
               by recorded deltas, and the money that changed hands is a
               pos_payments row with correction_id set
  refunds      pos_refunds (+ items): goods returned and money paid back

order_state() replays them: original sale -> corrections -> refunds, giving
the lines and totals as they stand now and what is still refundable. Every
page that shows or acts on a sale (receipt, reprint, Edit Sale, refunds)
reads it, so they all agree. List pages use the SQL snippets below
(net_total_sql, STATUS_SQL), which follow the same rules.

All money is integer cents.
"""
import json

from app.services import money
from app.services.pos_ledger import default_refund_method

KIND_LABELS = {'edit': 'Sale corrected', 'void': 'Sale voided'}

# Derived status of an order `o`, for list pages (same rules as _status()).
#   voided          a void correction exists
#   held            legacy rows stored as held
#   completed       no refunds
#   refunded        every unit still on the sale has been refunded
#   partial_refund  some refunds
# Refunds recorded without refund items (very old data) keep the stored
# status.
STATUS_SQL = '''(CASE
    WHEN EXISTS (SELECT 1 FROM pos_sale_corrections vc WHERE vc.order_id = o.id AND vc.kind = 'void')
        THEN 'voided'
    WHEN o.status = 'held' THEN 'held'
    WHEN NOT EXISTS (SELECT 1 FROM pos_refunds sr WHERE sr.order_id = o.id) THEN 'completed'
    WHEN (SELECT COALESCE(SUM(sri.quantity), 0) FROM pos_refund_items sri
          JOIN pos_refunds sr2 ON sr2.id = sri.refund_id WHERE sr2.order_id = o.id) = 0
        THEN COALESCE(NULLIF(o.status, 'completed'), 'partial_refund')
    WHEN (SELECT COALESCE(SUM(sri.quantity), 0) FROM pos_refund_items sri
          JOIN pos_refunds sr2 ON sr2.id = sri.refund_id WHERE sr2.order_id = o.id)
         >= (SELECT COALESCE(SUM(soi.quantity), 0) FROM pos_order_items soi WHERE soi.order_id = o.id)
            + COALESCE((SELECT SUM(sci.qty_delta) FROM pos_sale_correction_items sci
                        JOIN pos_sale_corrections sc ON sc.id = sci.correction_id
                        WHERE sc.order_id = o.id), 0)
        THEN 'refunded'
    ELSE 'partial_refund'
END)'''


def net_total_sql(alias='o'):
    """SQL for an order's total after corrections (before refunds)."""
    return (f'({alias}.total_cents + COALESCE((SELECT SUM(nc.total_delta_cents) FROM pos_sale_corrections nc '
            f'WHERE nc.order_id = {alias}.id), 0))')


def list_columns_sql():
    """Extra SELECT columns for order lists: net_total_cents, refunded_cents,
    correction_count, refund_count and the derived status as net_status
    (list_rows() makes that the row's `status`). Use after `o.*` (orders as `o`)."""
    return f''',
        {net_total_sql('o')} AS net_total_cents,
        (SELECT COALESCE(SUM(lr.amount_cents), 0) FROM pos_refunds lr WHERE lr.order_id = o.id)
            AS refunded_cents,
        (SELECT COUNT(*) FROM pos_sale_corrections lc WHERE lc.order_id = o.id) AS correction_count,
        (SELECT COUNT(*) FROM pos_refunds lr2 WHERE lr2.order_id = o.id) AS refund_count,
        {STATUS_SQL} AS net_status'''


def list_rows(rows):
    """Order list rows as dicts whose `status` is the derived one."""
    return [dict(r, status=r['net_status']) for r in rows]


class OrderNotFound(Exception):
    pass


def load_order(conn, order_id=None, order_number=None):
    """The pos_orders row (with operator_name) by id or number, or None."""
    if order_id is not None:
        where, arg = 'o.id = ?', order_id
    else:
        where, arg = 'o.order_number = ?', order_number
    return conn.execute(f'''
        SELECT o.*, u.username AS operator_name
        FROM pos_orders o LEFT JOIN users u ON o.operator_id = u.id
        WHERE {where}
    ''', (arg,)).fetchone()


def cash_discount_of(order):
    """The sale's cash discount in cents as recorded at checkout."""
    cash = order['cash_discount_cents'] or 0
    if not cash:
        try:
            details = json.loads(order['payment_details'] or '{}') or {}
            cash = int(details.get('cash_discount_cents') or 0) if isinstance(details, dict) else 0
        except (ValueError, TypeError):
            cash = 0
    return cash


def _status(order, voided, refunds, refunded_units, units):
    if voided:
        return 'voided'
    if order['status'] == 'held':
        return 'held'
    if not refunds:
        return 'completed'
    if refunded_units == 0:
        return order['status'] if order['status'] not in (None, 'completed') else 'partial_refund'
    return 'refunded' if refunded_units >= units else 'partial_refund'


def order_state(conn, order):
    """The sale as it stands now. `order` is a pos_orders row (or its id).

    Returns a dict (templates read it with dot access):
      order            the stored order row (dict, as at checkout)
      original         {'subtotal_cents', 'discount_cents', 'coupon_cents',
                        'cash_discount_cents', 'tax_cents', 'total_cents'} at checkout
      net              the same keys after every correction (before refunds)
      lines            one dict per order line (see below), in order
      live_lines       the lines still on the sale (quantity > 0)
      corrections      [{'id', 'kind', 'label', 'user_name', 'reason', 'method',
                         '*_delta_cents', 'created_at', 'items': [...],
                         'payments': [...]}] oldest first
      refunds          [{... pos_refunds row ..., 'manager_name', 'items': [...]}] oldest first
      sale_payments    the checkout's payments ledger rows
      payments_by_method  {method: net cents taken} over all ledger rows
      refunded_cents / refunded_tax_cents   refunded so far
      refundable_cents what is left of what was paid (net total - refunded)
      units / refunded_units / refundable_units
      voided, void     the void correction (or None)
      status           completed | partial_refund | refunded | voided (| held)
      subtotal_before_discounts_cents / item_discount_cents   for the net lines
      default_method   tender money goes back in by default (card sales -> card)

    Each line: id, inventory_item_id, sku, name (latest), discount_type,
    discount_percent, discount_cents, orig_quantity, orig_unit_price_cents,
    orig_line_total_cents, quantity, unit_price_cents, line_total_cents (net),
    refunded_qty, refunded_amount_cents (list value), refundable_qty,
    revenue_delta_cents / tax_delta_cents (sum over corrections).
    """
    if not hasattr(order, 'keys'):
        row = load_order(conn, order_id=order)
        if row is None:
            raise OrderNotFound()
        order = row
    order = dict(order)
    order_id = order['id']

    lines = []
    by_id = {}
    for r in conn.execute('SELECT * FROM pos_order_items WHERE order_id = ? ORDER BY id', (order_id,)):
        line = {
            'id': r['id'], 'inventory_item_id': r['inventory_item_id'], 'sku': r['sku'],
            'name': r['name'], 'original_name': r['name'],
            'discount_type': r['discount_type'], 'discount_percent': r['discount_percent'] or 0,
            'discount_cents': r['discount_cents'] or 0,
            'orig_quantity': r['quantity'], 'orig_unit_price_cents': r['unit_price_cents'],
            'orig_line_total_cents': r['line_total_cents'],
            'quantity': r['quantity'], 'unit_price_cents': r['unit_price_cents'],
            'line_total_cents': r['line_total_cents'],
            'refunded_qty': 0, 'refunded_amount_cents': 0,
            'revenue_delta_cents': 0, 'tax_delta_cents': 0,
        }
        lines.append(line)
        by_id[line['id']] = line

    coupon = conn.execute('SELECT COALESCE(SUM(discount_applied_cents), 0) FROM pos_coupon_redemptions '
                          'WHERE order_id = ?', (order_id,)).fetchone()[0] or 0
    original = {
        'subtotal_cents': order['subtotal_cents'] or 0,
        'discount_cents': order['discount_amount_cents'] or 0,
        'coupon_cents': int(coupon),
        'cash_discount_cents': cash_discount_of(order),
        'tax_cents': order['tax_amount_cents'] or 0,
        'total_cents': order['total_cents'] or 0,
    }
    net = dict(original)

    payments = [dict(p) for p in conn.execute(
        'SELECT id, method, amount_cents, tendered_cents, change_given_cents, created_at, correction_id '
        'FROM pos_payments WHERE order_id = ? ORDER BY id', (order_id,))]

    corrections = []
    void = None
    for c in conn.execute('SELECT * FROM pos_sale_corrections WHERE order_id = ? ORDER BY id', (order_id,)):
        c = dict(c)
        c['label'] = KIND_LABELS.get(c['kind'], c['kind'])
        c['items'] = []
        for ci in conn.execute('SELECT * FROM pos_sale_correction_items WHERE correction_id = ? ORDER BY id',
                               (c['id'],)):
            ci = dict(ci)
            line = by_id.get(ci['order_item_id'])
            if line is not None:
                ci['before_quantity'] = line['quantity']
                ci['before_unit_price_cents'] = line['unit_price_cents']
                ci['before_name'] = line['name']
                line['quantity'] += ci['qty_delta']
                line['unit_price_cents'] = ci['unit_price_cents']
                line['line_total_cents'] += ci['line_total_delta_cents']
                line['revenue_delta_cents'] += ci['revenue_delta_cents']
                line['tax_delta_cents'] += ci['tax_delta_cents']
                if ci['name']:
                    line['name'] = ci['name']
                ci['sku'] = line['sku']
                ci['line_name'] = line['name']
                ci['after_quantity'] = line['quantity']
            c['items'].append(ci)
        for key in ('subtotal', 'discount', 'coupon', 'cash_discount', 'tax', 'total'):
            net[f'{key}_cents'] += c[f'{key}_delta_cents']
        c['payments'] = [p for p in payments if p['correction_id'] == c['id']]
        c['money_cents'] = sum(p['amount_cents'] for p in c['payments'])
        if c['kind'] == 'void':
            void = c
        corrections.append(c)

    refunds = []
    refunded = refunded_tax = 0
    for r in conn.execute('''SELECT r.*, u.username AS manager_name FROM pos_refunds r
                             LEFT JOIN users u ON u.id = r.manager_id
                             WHERE r.order_id = ? ORDER BY r.created_at, r.id''', (order_id,)):
        r = dict(r)
        r['items'] = []
        for ri in conn.execute('SELECT * FROM pos_refund_items WHERE refund_id = ? ORDER BY id', (r['id'],)):
            ri = dict(ri)
            line = by_id.get(ri['order_item_id'])
            if line is not None:
                line['refunded_qty'] += ri['quantity']
                line['refunded_amount_cents'] += ri['amount_cents'] or 0
                ri['name'] = line['name']
                ri['sku'] = line['sku']
            r['items'].append(ri)
        amount = r['amount_cents'] or 0
        refunded += amount
        if r['tax_refunded_cents'] is not None:
            refunded_tax += r['tax_refunded_cents']
        elif net['total_cents'] > 0:   # (not written since schema v8) the order's tax share
            refunded_tax += money.prorate(amount, net['tax_cents'], net['total_cents'])
        refunds.append(r)

    units = refunded_units = 0
    for line in lines:
        line['refundable_qty'] = max(0, line['quantity'] - line['refunded_qty'])
        units += line['quantity']
        refunded_units += line['refunded_qty']

    by_method = {}
    for p in payments:
        by_method[p['method']] = by_method.get(p['method'], 0) + (p['amount_cents'] or 0)

    voided = void is not None
    live = [ln for ln in lines if ln['quantity'] > 0]
    return {
        'order': order,
        'order_id': order_id,
        'order_number': order['order_number'],
        'original': original,
        'net': net,
        'lines': lines,
        'live_lines': live,
        'corrections': corrections,
        'refunds': refunds,
        'sale_payments': [p for p in payments if p['correction_id'] is None],
        'payments': payments,
        'payments_by_method': by_method,
        'refunded_cents': refunded,
        'refunded_tax_cents': refunded_tax,
        'refundable_cents': max(0, net['total_cents'] - refunded),
        'units': units,
        'refunded_units': refunded_units,
        'refundable_units': sum(ln['refundable_qty'] for ln in lines),
        'voided': voided,
        'void': void,
        'has_refunds': bool(refunds),
        'has_corrections': bool(corrections),
        'status': _status(order, voided, refunds, refunded_units, units),
        'subtotal_before_discounts_cents': sum(ln['unit_price_cents'] * ln['quantity'] for ln in lines),
        'item_discount_cents': sum(ln['unit_price_cents'] * ln['quantity'] - ln['line_total_cents']
                                   for ln in lines),
        'default_method': default_refund_method(order['payment_method']),
    }


def order_view(state):
    """The order row with its money fields replaced by the net figures and
    the derived status, for templates that show "the order" (refund page,
    Edit Sale). The original stays in state['order']."""
    view = dict(state['order'])
    net = state['net']
    view.update({
        'subtotal_cents': net['subtotal_cents'],
        'discount_amount_cents': net['discount_cents'],
        'tax_amount_cents': net['tax_cents'],
        'total_cents': net['total_cents'],
        'cash_discount_cents': net['cash_discount_cents'],
        'status': state['status'],
        'refundable_cents': state['refundable_cents'],
    })
    return view
