"""
POS payments ledger and cash drawer sessions.

pos_payments holds one row per tender on a sale (all integer cents):
  amount_cents        what that tender paid toward the sale (sums to the order total)
  tendered_cents      cash handed over (cash rows only)
  change_given_cents  cash handed back (cash rows only)
  correction_id       set on rows a later sale correction wrote (schema v23):
                      money paid back (negative) or collected, dated when the
                      correction was made. Rows are never changed or deleted.

pos_orders.payment_details (JSON) is still written for compatibility, but
reports, receipts and drawer counts read the ledger. Its money keys are
cents too: tendered_cents, change_cents, cash_discount_cents, cash_cents,
cards_cents, total_tendered_cents (schema v15 converted older rows).
"""
import json
from datetime import datetime, timezone

REFUND_METHODS = {'cash': 'Cash', 'card': 'Card'}


def cents(value, default=0):
    """An int of cents from a stored cents value; `default` for anything else."""
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def utc_now_str():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def parse_details(raw):
    try:
        details = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {}
    return details if isinstance(details, dict) else {}


def allocate_payments(method, details, total):
    """Ledger rows [(method, amount, tendered, change_given)] in cents for a sale.

    cash  -> one cash row for the total; tendered/change as recorded
    split -> card rows as given (capped at what is left of the total), then
             one cash row for the rest: tendered = cash handed over,
             change = tendered - applied
    other -> one row of that method for the total
    """
    details = details if isinstance(details, dict) else {}
    total = cents(total)
    if method == 'cash':
        tendered = cents(details.get('tendered_cents'), None)
        if tendered is None or tendered < total:
            tendered = total
        return [('cash', total, tendered, tendered - total)]
    if method == 'split':
        rows = []
        remaining = total
        cards = details.get('cards_cents')
        for c in (cards if isinstance(cards, list) else []):
            c = cents(c, None)
            if c is None or c <= 0:
                continue
            applied = min(c, remaining)
            rows.append(('card', applied, None, 0))
            remaining -= applied
        handed = max(cents(details.get('cash_cents', 0)), 0)
        if remaining > 0 or handed > 0:
            tendered = max(handed, remaining)
            rows.append(('cash', remaining, tendered, tendered - remaining))
        return rows
    return [(method or 'card', total, None, 0)]


def write_payments(conn, order_id, rows, created_at=None):
    """Insert ledger rows for an order (created_at defaults to the order's)."""
    if created_at is None:
        row = conn.execute('SELECT created_at FROM pos_orders WHERE id = ?', (order_id,)).fetchone()
        created_at = row[0] if row and row[0] else utc_now_str()
    for method, amount, tendered, change in rows:
        conn.execute('''INSERT INTO pos_payments (order_id, method, amount_cents, tendered_cents,
                                                  change_given_cents, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)''', (order_id, method, amount, tendered, change, created_at))


def order_payments(conn, order_id):
    """The checkout's ledger rows for an order, as dicts (receipts); rows of
    later corrections are not included (pos_order_state lists them)."""
    return [dict(r) for r in conn.execute(
        'SELECT method, amount_cents, tendered_cents, change_given_cents FROM pos_payments '
        'WHERE order_id = ? AND correction_id IS NULL ORDER BY id',
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
    """(cash applied to sales, cash refunded, cash moved by sale corrections)
    in cents between two UTC timestamps (inclusive).

    Sales count at the sale's time; refunds and corrections when they were
    made. Corrections are signed: negative = paid back to the customer out
    of the drawer, positive = collected."""
    cash_in = conn.execute('''
        SELECT COALESCE(SUM(p.amount_cents), 0) FROM pos_payments p
        JOIN pos_orders o ON o.id = p.order_id
        WHERE p.method = 'cash' AND p.correction_id IS NULL AND o.status != 'held'
          AND o.created_at BETWEEN ? AND ?
    ''', (start_utc, end_utc)).fetchone()[0]
    cash_out = conn.execute('''
        SELECT COALESCE(SUM(r.amount_cents), 0) FROM pos_refunds r
        LEFT JOIN pos_orders o ON o.id = r.order_id
        WHERE COALESCE(r.refund_method, CASE WHEN o.payment_method = 'card' THEN 'card' ELSE 'cash' END) = 'cash'
          AND r.created_at BETWEEN ? AND ?
    ''', (start_utc, end_utc)).fetchone()[0]
    corrections = conn.execute('''
        SELECT COALESCE(SUM(p.amount_cents), 0) FROM pos_payments p
        WHERE p.method = 'cash' AND p.correction_id IS NOT NULL AND p.created_at BETWEEN ? AND ?
    ''', (start_utc, end_utc)).fetchone()[0]
    return int(cash_in), int(cash_out), int(corrections)


def session_figures(conn, drawer_session):
    """Expected cash for a drawer session (live while it is open).

    expected = opening float + cash applied to sales - cash refunds
    + cash moved by sale corrections, over the session's window.
    over_short = counted - expected once counted. All cents.
    """
    s = dict(drawer_session)
    end = s['closed_at'] or utc_now_str()
    cash_in, cash_out, corrections = cash_movements(conn, s['opened_at'], end)
    expected = (s['opening_float_cents'] or 0) + cash_in - cash_out + corrections
    s['cash_sales_cents'] = cash_in
    s['cash_refunds_cents'] = cash_out
    s['cash_corrections_cents'] = corrections
    s['is_open'] = s['closed_at'] is None
    if s['is_open']:
        s['expected_cash_cents'] = expected
    return s


def open_session(conn, user_id, opening_float_cents, register_id=None, notes=''):
    """Open a drawer session. Returns its id, or None if one is already open."""
    if get_open_session(conn, register_id) is not None:
        return None
    cur = conn.execute('''INSERT INTO drawer_sessions (register_id, opened_at, opened_by, opening_float_cents, notes)
                          VALUES (?, ?, ?, ?, ?)''',
                       (register_id, utc_now_str(), user_id, cents(opening_float_cents), notes or None))
    return cur.lastrowid


def close_session(conn, session_id, user_id, counted_cash_cents, notes=''):
    """Close an open session: store expected, counted and over/short (cents). Returns the figures."""
    row = conn.execute('SELECT * FROM drawer_sessions WHERE id = ? AND closed_at IS NULL',
                       (session_id,)).fetchone()
    if row is None:
        return None
    closed_at = utc_now_str()
    s = dict(row)
    s['closed_at'] = closed_at
    cash_in, cash_out, corrections = cash_movements(conn, s['opened_at'], closed_at)
    expected = (s['opening_float_cents'] or 0) + cash_in - cash_out + corrections
    counted = cents(counted_cash_cents)
    over_short = counted - expected
    combined_notes = '\n'.join(n for n in (s.get('notes'), notes) if n) or None
    conn.execute('''UPDATE drawer_sessions
                    SET closed_at = ?, closed_by = ?, counted_cash_cents = ?, expected_cash_cents = ?,
                        over_short_cents = ?, notes = ?
                    WHERE id = ?''', (closed_at, user_id, counted, expected, over_short, combined_notes, session_id))
    return {'expected_cash_cents': expected, 'counted_cash_cents': counted, 'over_short_cents': over_short,
            'cash_sales_cents': cash_in, 'cash_refunds_cents': cash_out,
            'cash_corrections_cents': corrections}


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
