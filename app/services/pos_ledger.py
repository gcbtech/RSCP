"""
POS payments ledger and cash drawer sessions.

pos_payments holds one row per tender on a sale:
  amount        what that tender paid toward the sale (sums to the order total)
  tendered      cash handed over (cash rows only)
  change_given  cash handed back (cash rows only)

pos_orders.payment_details (JSON) is still written for compatibility, but
reports, receipts and drawer counts read the ledger.
"""
import json
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

REFUND_METHODS = {'cash': 'Cash', 'card': 'Card'}


def money(value, default=0.0):
    """Round half-up to cents; `default` for anything that is not a finite number."""
    try:
        d = Decimal(str(value))
    except Exception:
        return default
    if not d.is_finite():
        return default
    return float(d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def utc_now_str():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def parse_details(raw):
    try:
        details = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {}
    return details if isinstance(details, dict) else {}


def allocate_payments(method, details, total):
    """Ledger rows [(method, amount, tendered, change_given)] for a sale.

    cash  -> one cash row for the total; tendered/change as recorded
    split -> card rows as given (capped at what is left of the total), then
             one cash row for the rest: tendered = cash handed over,
             change = tendered - applied
    other -> one row of that method for the total
    """
    details = details if isinstance(details, dict) else {}
    total = money(total)
    if method == 'cash':
        tendered = money(details.get('tendered'), None)
        if tendered is None or tendered < total:
            tendered = total
        return [('cash', total, tendered, money(tendered - total))]
    if method == 'split':
        rows = []
        remaining = total
        cards = details.get('cards')
        for c in (cards if isinstance(cards, list) else []):
            c = money(c, None)
            if c is None or c <= 0:
                continue
            applied = min(c, remaining)
            rows.append(('card', applied, None, 0.0))
            remaining = money(remaining - applied)
        handed = max(money(details.get('cash', 0)), 0.0)
        if remaining > 0 or handed > 0:
            tendered = max(handed, remaining)
            rows.append(('cash', remaining, tendered, money(tendered - remaining)))
        return rows
    return [(method or 'card', total, None, 0.0)]


def write_payments(conn, order_id, rows, created_at=None):
    """Insert ledger rows for an order (created_at defaults to the order's)."""
    if created_at is None:
        row = conn.execute('SELECT created_at FROM pos_orders WHERE id = ?', (order_id,)).fetchone()
        created_at = row[0] if row and row[0] else utc_now_str()
    for method, amount, tendered, change in rows:
        conn.execute('''INSERT INTO pos_payments (order_id, method, amount, tendered, change_given, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)''', (order_id, method, amount, tendered, change, created_at))


def rebuild_order_payments(conn, order_id):
    """Re-derive an order's ledger rows from its current total (after a sale edit)."""
    order = conn.execute('SELECT payment_method, payment_details, total, created_at FROM pos_orders WHERE id = ?',
                         (order_id,)).fetchone()
    if not order:
        return
    existing = conn.execute('SELECT method, amount, tendered FROM pos_payments WHERE order_id = ? ORDER BY id',
                            (order_id,)).fetchall()
    details = parse_details(order[1])
    if existing and order[0] == 'split':
        # Keep the card amounts actually charged; cash covers the rest
        details = dict(details)
        details['cards'] = [r[1] for r in existing if r[0] == 'card']
        cash = [r[2] for r in existing if r[0] == 'cash']
        if cash:
            details['cash'] = cash[0] or 0
    conn.execute('DELETE FROM pos_payments WHERE order_id = ?', (order_id,))
    write_payments(conn, order_id, allocate_payments(order[0], details, order[2]), created_at=order[3])


def order_payments(conn, order_id):
    """Ledger rows for an order, as dicts (receipts)."""
    return [dict(r) for r in conn.execute(
        'SELECT method, amount, tendered, change_given FROM pos_payments WHERE order_id = ? ORDER BY id',
        (order_id,)).fetchall()]


def default_refund_method(order_payment_method):
    """Card sales are refunded to card; cash and split sales in cash."""
    return 'card' if order_payment_method == 'card' else 'cash'


# ---------------------------------------------------------------------------
# Cash drawer sessions
# ---------------------------------------------------------------------------

def get_open_session(conn, register_id=None):
    if register_id is None:
        row = conn.execute('''SELECT * FROM drawer_sessions WHERE closed_at IS NULL AND register_id IS NULL
                              ORDER BY opened_at DESC, id DESC LIMIT 1''').fetchone()
    else:
        row = conn.execute('''SELECT * FROM drawer_sessions WHERE closed_at IS NULL AND register_id = ?
                              ORDER BY opened_at DESC, id DESC LIMIT 1''', (register_id,)).fetchone()
    return row


def cash_movements(conn, start_utc, end_utc):
    """(cash applied to sales, cash refunded) between two UTC timestamps (inclusive)."""
    cash_in = conn.execute('''
        SELECT COALESCE(SUM(p.amount), 0) FROM pos_payments p
        JOIN pos_orders o ON o.id = p.order_id
        WHERE p.method = 'cash' AND o.status != 'held' AND o.created_at BETWEEN ? AND ?
    ''', (start_utc, end_utc)).fetchone()[0]
    cash_out = conn.execute('''
        SELECT COALESCE(SUM(r.amount), 0) FROM pos_refunds r
        LEFT JOIN pos_orders o ON o.id = r.order_id
        WHERE COALESCE(r.refund_method, CASE WHEN o.payment_method = 'card' THEN 'card' ELSE 'cash' END) = 'cash'
          AND r.created_at BETWEEN ? AND ?
    ''', (start_utc, end_utc)).fetchone()[0]
    return money(cash_in), money(cash_out)


def session_figures(conn, drawer_session):
    """Expected cash for a drawer session (live while it is open).

    expected = opening float + cash applied to sales - cash refunds, over the
    session's window. over_short = counted - expected once counted.
    """
    s = dict(drawer_session)
    end = s['closed_at'] or utc_now_str()
    cash_in, cash_out = cash_movements(conn, s['opened_at'], end)
    expected = money((s['opening_float'] or 0) + cash_in - cash_out)
    s['cash_sales'] = cash_in
    s['cash_refunds'] = cash_out
    s['is_open'] = s['closed_at'] is None
    if s['is_open']:
        s['expected_cash'] = expected
    return s


def open_session(conn, user_id, opening_float, register_id=None, notes=''):
    """Open a drawer session. Returns its id, or None if one is already open."""
    if get_open_session(conn, register_id) is not None:
        return None
    cur = conn.execute('''INSERT INTO drawer_sessions (register_id, opened_at, opened_by, opening_float, notes)
                          VALUES (?, ?, ?, ?, ?)''',
                       (register_id, utc_now_str(), user_id, money(opening_float), notes or None))
    return cur.lastrowid


def close_session(conn, session_id, user_id, counted_cash, notes=''):
    """Close an open session: store expected, counted and over/short. Returns the figures."""
    row = conn.execute('SELECT * FROM drawer_sessions WHERE id = ? AND closed_at IS NULL',
                       (session_id,)).fetchone()
    if row is None:
        return None
    closed_at = utc_now_str()
    s = dict(row)
    s['closed_at'] = closed_at
    cash_in, cash_out = cash_movements(conn, s['opened_at'], closed_at)
    expected = money((s['opening_float'] or 0) + cash_in - cash_out)
    counted = money(counted_cash)
    over_short = money(counted - expected)
    combined_notes = '\n'.join(n for n in (s.get('notes'), notes) if n) or None
    conn.execute('''UPDATE drawer_sessions
                    SET closed_at = ?, closed_by = ?, counted_cash = ?, expected_cash = ?, over_short = ?, notes = ?
                    WHERE id = ?''', (closed_at, user_id, counted, expected, over_short, combined_notes, session_id))
    return {'expected_cash': expected, 'counted_cash': counted, 'over_short': over_short,
            'cash_sales': cash_in, 'cash_refunds': cash_out}


def sessions_between(conn, start_utc, end_utc):
    """Drawer sessions that overlap a UTC window, with their figures."""
    rows = conn.execute('''
        SELECT d.*, uo.username AS opened_by_name, uc.username AS closed_by_name
        FROM drawer_sessions d
        LEFT JOIN users uo ON uo.id = d.opened_by
        LEFT JOIN users uc ON uc.id = d.closed_by
        WHERE d.opened_at <= ? AND (d.closed_at IS NULL OR d.closed_at >= ?)
        ORDER BY d.opened_at, d.id
    ''', (end_utc, start_utc)).fetchall()
    return [session_figures(conn, r) for r in rows]
