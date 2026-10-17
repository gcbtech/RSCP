"""
POS Reports Service

One sales engine (build_sales_report) produces every money figure for the
daily report page, its thermal print view, the manual and automatic EOD
emails and the custom report. Pages add presentation extras (hourly or
per-period activity, top items) but never recompute the money.
"""
import re
from datetime import datetime, date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from app.services.db import get_db_connection
from app.utils.helpers import local_date_to_utc_range

TENDER_LABELS = {'cash': 'Cash', 'card': 'Card', 'split': 'Split (unallocated)'}
_TENDER_ORDER = ['cash', 'card']

# Refund method for refunds recorded before the ledger existed (or inserted
# without one): card sales went back to card, everything else in cash.
_REFUND_METHOD_SQL = ("COALESCE(r.refund_method, CASE WHEN o.payment_method = 'card' "
                      "THEN 'card' ELSE 'cash' END)")
_REFUND_TAX_SQL = ("COALESCE(r.tax_refunded, CASE WHEN o.total > 0 "
                   "THEN r.amount * o.tax_amount / o.total ELSE 0 END)")


def _m(value):
    """Money: round half-up to cents."""
    return float(Decimal(str(value or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def build_sales_report(start_date, end_date, conn=None):
    """The money figures for a local date range (inclusive).

    Sales are counted on the sale date, refunds on the refund date (that is
    when the money moved). Returns a dict:

      order_count        completed (non-held) sales
      gross_sales        list value of items sold, before any discount, ex. tax
      discounts          {'line', 'order', 'coupon', 'cash', 'total'}
      net_sales          gross_sales - discounts (ex. tax)
      tax_collected      tax charged on those sales
      sales_total        what customers paid (net_sales + tax_collected)
      refunds            {'count', 'total', 'tax', 'net', 'by_method': {method: amount}}
      tax_net            tax_collected - refunded tax
      net_revenue        sales_total - refunds.total (incl. tax)
      net_sales_after_refunds   net_sales - refunds.net (ex. tax)
      tenders            [{'method', 'label', 'count', 'collected', 'refunded', 'net'}]
      tender_net         {method: net}
      expected_cash      {'opening_float', 'cash_sales', 'cash_refunds', 'total'}
      drawer_sessions    drawer sessions overlapping the range, with expected/over-short
      summary            legacy keys: total_orders, gross_sales (= sales_total,
                         the "Overall Total"), total_tax, total_discounts
    """
    from app.services.pos_ledger import sessions_between

    own = conn is None
    if own:
        conn = get_db_connection()
    try:
        start_dt, _ = local_date_to_utc_range(start_date.strftime('%Y-%m-%d'))
        _, end_dt = local_date_to_utc_range(end_date.strftime('%Y-%m-%d'))
        rng = (start_dt, end_dt)

        orders = conn.execute('''
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(total), 0) AS total,
                   COALESCE(SUM(tax_amount), 0) AS tax,
                   COALESCE(SUM(discount_amount), 0) AS order_disc,
                   COALESCE(SUM(cash_discount), 0) AS cash_disc
            FROM pos_orders
            WHERE created_at BETWEEN ? AND ? AND status != 'held'
        ''', rng).fetchone()

        lines = conn.execute('''
            SELECT COALESCE(SUM(oi.unit_price * oi.quantity), 0) AS gross,
                   COALESCE(SUM(oi.unit_price * oi.quantity - oi.line_total), 0) AS line_disc
            FROM pos_order_items oi JOIN pos_orders o ON o.id = oi.order_id
            WHERE o.created_at BETWEEN ? AND ? AND o.status != 'held'
        ''', rng).fetchone()

        coupon = conn.execute('''
            SELECT COALESCE(SUM(cr.discount_applied), 0)
            FROM pos_coupon_redemptions cr JOIN pos_orders o ON o.id = cr.order_id
            WHERE o.created_at BETWEEN ? AND ? AND o.status != 'held'
        ''', rng).fetchone()[0]

        refund_rows = conn.execute(f'''
            SELECT {_REFUND_METHOD_SQL} AS method,
                   COUNT(*) AS n,
                   COALESCE(SUM(r.amount), 0) AS amount,
                   COALESCE(SUM({_REFUND_TAX_SQL}), 0) AS tax
            FROM pos_refunds r LEFT JOIN pos_orders o ON o.id = r.order_id
            WHERE r.created_at BETWEEN ? AND ?
            GROUP BY method
        ''', rng).fetchall()

        # Tenders from the ledger; a sale with no ledger rows (recorded by
        # something other than checkout) counts under its payment_method.
        tender_rows = conn.execute('''
            SELECT p.method AS method, COUNT(DISTINCT p.order_id) AS n, COALESCE(SUM(p.amount), 0) AS amount
            FROM pos_payments p JOIN pos_orders o ON o.id = p.order_id
            WHERE o.created_at BETWEEN ? AND ? AND o.status != 'held'
            GROUP BY p.method
            UNION ALL
            SELECT o.payment_method, COUNT(*), COALESCE(SUM(o.total), 0)
            FROM pos_orders o
            WHERE o.created_at BETWEEN ? AND ? AND o.status != 'held'
              AND NOT EXISTS (SELECT 1 FROM pos_payments p WHERE p.order_id = o.id)
            GROUP BY o.payment_method
        ''', rng + rng).fetchall()

        sessions = sessions_between(conn, start_dt, end_dt)
        opening_float = sum((s['opening_float'] or 0) for s in sessions
                            if start_dt <= str(s['opened_at']) <= end_dt)
    finally:
        if own:
            conn.close()

    discounts = {
        'line': _m(lines['line_disc']),
        'order': _m(orders['order_disc']),
        'coupon': _m(coupon),
        'cash': _m(orders['cash_disc']),
    }
    discounts['total'] = _m(sum(discounts.values()))
    sales_total = _m(orders['total'])
    tax_collected = _m(orders['tax'])
    net_sales = _m(sales_total - tax_collected)

    refunds_by_method = {}
    refund_count = 0
    refund_tax = 0.0
    for r in refund_rows:
        refunds_by_method[r['method']] = _m(refunds_by_method.get(r['method'], 0) + r['amount'])
        refund_count += r['n']
        refund_tax += r['tax']
    refund_total = _m(sum(refunds_by_method.values()))
    refund_tax = _m(refund_tax)
    refunds = {'count': refund_count, 'total': refund_total, 'tax': refund_tax,
               'net': _m(refund_total - refund_tax), 'by_method': refunds_by_method}

    collected, counts = {}, {}
    for t in tender_rows:
        collected[t['method']] = collected.get(t['method'], 0) + t['amount']
        counts[t['method']] = counts.get(t['method'], 0) + t['n']
    methods = [m for m in _TENDER_ORDER if m in collected or m in refunds_by_method]
    methods += sorted(m for m in set(collected) | set(refunds_by_method) if m not in methods)
    tenders = []
    for m in methods:
        c, rf = _m(collected.get(m, 0)), refunds_by_method.get(m, 0)
        tenders.append({'method': m, 'label': TENDER_LABELS.get(m, (m or '').title()),
                        'count': counts.get(m, 0), 'collected': c, 'refunded': rf, 'net': _m(c - rf)})
    tender_net = {t['method']: t['net'] for t in tenders}

    cash_sales = _m(collected.get('cash', 0))
    cash_refunds = refunds_by_method.get('cash', 0)
    expected_cash = {'opening_float': _m(opening_float), 'cash_sales': cash_sales,
                     'cash_refunds': cash_refunds,
                     'total': _m(opening_float + cash_sales - cash_refunds)}

    return {
        'start_date': start_date,
        'end_date': end_date,
        'range_days': (end_date - start_date).days + 1,
        'order_count': orders['n'],
        'gross_sales': _m(lines['gross']),
        'discounts': discounts,
        'net_sales': net_sales,
        'tax_collected': tax_collected,
        'sales_total': sales_total,
        'refunds': refunds,
        'tax_net': _m(tax_collected - refund_tax),
        'net_revenue': _m(sales_total - refund_total),
        'net_sales_after_refunds': _m(net_sales - refunds['net']),
        'tenders': tenders,
        'tender_net': tender_net,
        'expected_cash': expected_cash,
        'drawer_sessions': sessions,
        'summary': {'total_orders': orders['n'], 'gross_sales': sales_total,
                    'total_tax': tax_collected, 'total_discounts': discounts['total']},
    }


def _top_items(start_date, end_date, limit=10):
    """Top items by net sales, same figures as the Top Sellers report."""
    top_report = generate_top_sellers_data(start_date, end_date, {}, sort='revenue')
    return [{'sku': i['sku'], 'name': i['name'], 'qty': i['net_qty'], 'total': i['net_revenue']}
            for i in top_report['items'][:limit]]


def generate_daily_report_data(report_date):
    """The End of Day report for one local date: the sales engine's figures
    plus hourly activity and top items."""
    report = build_sales_report(report_date, report_date)
    conn = get_db_connection()
    try:
        start_dt, end_dt = local_date_to_utc_range(report_date.strftime('%Y-%m-%d'))
        hourly = conn.execute('''
            SELECT strftime('%H', created_at, 'localtime') as hour, COUNT(*) as count, SUM(total) as amount
            FROM pos_orders
            WHERE created_at BETWEEN ? AND ? AND status != 'held'
            GROUP BY hour
            ORDER BY hour
        ''', (start_dt, end_dt)).fetchall()
    finally:
        conn.close()

    hourly_data = [dict(h) for h in hourly]
    if hourly_data:
        active_hours = [int(h['hour']) for h in hourly_data]
        min_h, max_h = min(active_hours), max(active_hours)
        target_span = 10
        current_span = max_h - min_h + 1
        missing = target_span - current_span

        if missing > 0:
            pad_before = missing // 2
            pad_after = missing - pad_before
            min_h = max(0, min_h - pad_before)
            max_h = min(23, max_h + pad_after)
            real_span = max_h - min_h + 1
            if real_span < target_span:
                if min_h == 0:
                    max_h = min(23, min_h + target_span - 1)
                elif max_h == 23:
                    min_h = max(0, max_h - target_span + 1)

        filled_hourly = []
        hour_map = {int(h['hour']): h for h in hourly_data}
        for h in range(min_h, max_h + 1):
            if h in hour_map:
                filled_hourly.append(hour_map[h])
            else:
                filled_hourly.append({'hour': f"{h:02d}", 'count': 0, 'amount': 0.0})
        hourly_data = filled_hourly

    max_hourly_revenue = max((h['amount'] for h in hourly_data), default=0) if hourly_data else 0

    return {
        'report': report,
        'hourly': hourly_data,
        'max_hourly_revenue': max_hourly_revenue,
        'top_items': _top_items(report_date, report_date),
    }


def generate_custom_report_data(start_date, end_date):
    """
    Report for an arbitrary local date range: the sales engine's figures plus
    activity grouped by day/week/month and top items.
    """
    report = build_sales_report(start_date, end_date)
    conn = get_db_connection()
    try:
        start_dt, end_dt = _utc_bounds(start_date, end_date)

        # Determine grouping based on range length
        range_days = (end_date - start_date).days + 1
        if range_days <= 14:
            grouping = 'daily'
        elif range_days <= 60:
            grouping = 'weekly'
        else:
            grouping = 'monthly'

        if grouping == 'daily':
            activity_raw = conn.execute('''
                SELECT date(created_at, 'localtime') as period,
                       COUNT(*) as count, SUM(total) as amount
                FROM pos_orders
                WHERE created_at BETWEEN ? AND ? AND status != 'held'
                GROUP BY period ORDER BY period
            ''', (start_dt, end_dt)).fetchall()
        elif grouping == 'weekly':
            # ISO week grouping: strftime %W gives week number, combine with year
            activity_raw = conn.execute('''
                SELECT strftime('%Y-W%W', created_at, 'localtime') as period,
                       MIN(date(created_at, 'localtime')) as week_start,
                       MAX(date(created_at, 'localtime')) as week_end,
                       COUNT(*) as count, SUM(total) as amount
                FROM pos_orders
                WHERE created_at BETWEEN ? AND ? AND status != 'held'
                GROUP BY period ORDER BY period
            ''', (start_dt, end_dt)).fetchall()
        else:  # monthly
            activity_raw = conn.execute('''
                SELECT strftime('%Y-%m', created_at, 'localtime') as period,
                       COUNT(*) as count, SUM(total) as amount
                FROM pos_orders
                WHERE created_at BETWEEN ? AND ? AND status != 'held'
                GROUP BY period ORDER BY period
            ''', (start_dt, end_dt)).fetchall()
    finally:
        conn.close()

    # Format activity labels
    activity_data = []
    for row in activity_raw:
        r = dict(row)
        if grouping == 'daily':
            try:
                dt = datetime.strptime(r['period'], '%Y-%m-%d')
                r['label'] = dt.strftime('%b %d')
            except ValueError:
                r['label'] = r['period']
        elif grouping == 'weekly':
            try:
                ws = datetime.strptime(r['week_start'], '%Y-%m-%d').strftime('%b %d')
                we = datetime.strptime(r['week_end'], '%Y-%m-%d').strftime('%b %d')
                r['label'] = f"{ws} – {we}"
            except (ValueError, KeyError):
                r['label'] = r['period']
        else:  # monthly
            try:
                dt = datetime.strptime(r['period'] + '-01', '%Y-%m-%d')
                r['label'] = dt.strftime('%B %Y')
            except ValueError:
                r['label'] = r['period']
        activity_data.append(r)

    max_activity_revenue = max((a['amount'] for a in activity_data), default=0) if activity_data else 0

    return {
        'report': report,
        # kept at the top level for callers that read the custom report's summary
        'summary': report['summary'],
        'activity_data': activity_data,
        'activity_grouping': grouping,
        'max_activity_revenue': max_activity_revenue,
        # same figures as the Top Sellers report (after discounts and refunds, ex. tax)
        'top_items': _top_items(start_date, end_date),
        'range_days': range_days,
    }


# ---------------------------------------------------------------------------
# Top Sellers Report
# ---------------------------------------------------------------------------

LOCATION_FIELDS = ('area', 'aisle', 'shelf', 'bin')
LOCATION_NONE = '__none__'  # filter value meaning "no location set"

# Order items resolve to their inventory item by id, falling back to SKU when the
# original item was deleted and re-created. Refunds are attributed to the sale.
# `ol` holds each order's line total so order-level discounts (order discount,
# coupons, cash discount) and tax can be spread across lines by line value --
# that keeps item figures adding up to the order totals the Custom Report uses.
_TOP_SELLERS_FROM = '''
    FROM pos_order_items oi
    JOIN pos_orders o ON o.id = oi.order_id
    JOIN (SELECT order_id, SUM(line_total) AS lines_total
          FROM pos_order_items GROUP BY order_id) ol ON ol.order_id = o.id
    LEFT JOIN inventory_items inv ON inv.id = COALESCE(
        (SELECT x.id FROM inventory_items x WHERE x.id = oi.inventory_item_id),
        CASE WHEN oi.inventory_item_id IS NOT NULL
             THEN (SELECT y.id FROM inventory_items y WHERE y.sku = oi.sku) END)
    LEFT JOIN (
        SELECT order_item_id, SUM(quantity) AS qty, SUM(amount) AS amount
        FROM pos_refund_items GROUP BY order_item_id
    ) rf ON rf.order_item_id = oi.id
    WHERE o.created_at BETWEEN ? AND ? AND o.status != 'held'
'''

# Share of a line's list value that was actually charged (ex. tax), and its share of tax
_NET_RATIO = '(CASE WHEN ol.lines_total > 0 THEN (o.total - o.tax_amount) / ol.lines_total ELSE 0 END)'
_TAX_RATIO = '(CASE WHEN ol.lines_total > 0 THEN o.tax_amount / ol.lines_total ELSE 0 END)'

_TOP_SELLERS_KEY = '''
    CASE WHEN inv.id IS NOT NULL THEN 'i:' || inv.id
         WHEN oi.inventory_item_id IS NOT NULL THEN 's:' || oi.sku
         ELSE 'c:' || LOWER(TRIM(oi.name)) END
'''

_SKU_CATEGORY_RE = re.compile(r'^RSCP-([A-Z]{3})-', re.IGNORECASE)


def _sku_category(sku):
    m = _SKU_CATEGORY_RE.match(sku or '')
    return m.group(1).upper() if m else None


def _clean_location(value):
    value = (value or '').strip()
    return '' if value == 'None' else value


def _top_sellers_filter_sql(filters, low_threshold):
    """Build extra WHERE clauses for the Top Sellers filters. Returns (sql, params)."""
    clauses, params = [], []

    if filters.get('exclude_custom'):
        clauses.append('oi.inventory_item_id IS NOT NULL')

    if filters.get('q'):
        like = f"%{filters['q']}%"
        clauses.append('(oi.sku LIKE ? OR oi.name LIKE ? OR inv.sku LIKE ? OR inv.name LIKE ? OR inv.secondary_ids LIKE ?)')
        params += [like] * 5

    categories = filters.get('categories') or []
    if categories:
        clauses.append('(' + ' OR '.join(['COALESCE(inv.sku, oi.sku) LIKE ?'] * len(categories)) + ')')
        params += [f'RSCP-{c}-%' for c in categories]

    for field in LOCATION_FIELDS:
        value = filters.get(field)
        if value == LOCATION_NONE:
            clauses.append(f"inv.id IS NOT NULL AND COALESCE(NULLIF(TRIM(inv.location_{field}), 'None'), '') = ''")
        elif value:
            clauses.append(f'TRIM(inv.location_{field}) = ?')
            params.append(value)

    status = filters.get('item_status')
    if status == 'current':
        clauses.append('inv.id IS NOT NULL AND COALESCE(inv.is_legacy, 0) = 0')
    elif status == 'legacy':
        clauses.append('inv.is_legacy = 1')

    stock = filters.get('stock')
    if stock == 'in':
        clauses.append('inv.quantity > 0')
    elif stock == 'low':
        clauses.append('''inv.quantity > 0 AND (
            (COALESCE(inv.alert_threshold, 0) > 0 AND inv.quantity <= inv.alert_threshold)
            OR (COALESCE(inv.alert_threshold, 0) = 0 AND ? > 0 AND inv.quantity <= ?))''')
        params += [low_threshold, low_threshold]
    elif stock == 'out':
        clauses.append('inv.quantity <= 0')

    sql = ''.join(f' AND ({c})' for c in clauses)
    return sql, params


def _utc_bounds(start_date, end_date):
    start_dt, _ = local_date_to_utc_range(start_date.strftime('%Y-%m-%d'))
    _, end_dt = local_date_to_utc_range(end_date.strftime('%Y-%m-%d'))
    return start_dt, end_dt


def _top_sellers_items(conn, start_date, end_date, where_sql, where_params):
    start_dt, end_dt = _utc_bounds(start_date, end_date)
    return conn.execute(f'''
        SELECT {_TOP_SELLERS_KEY} AS item_key,
               MAX(inv.id) AS item_id,
               MAX(oi.inventory_item_id IS NOT NULL) AS from_inventory,
               COALESCE(MAX(inv.sku), MAX(oi.sku)) AS sku,
               COALESCE(MAX(inv.name), MAX(oi.name)) AS name,
               MAX(inv.location_area) AS area,
               MAX(inv.location_aisle) AS aisle,
               MAX(inv.location_shelf) AS shelf,
               MAX(inv.location_bin) AS bin,
               MAX(inv.quantity) AS stock,
               MAX(inv.buy_price) AS buy_price,
               MAX(inv.sell_price) AS list_price,
               MAX(inv.is_legacy) AS is_legacy,
               SUM(oi.quantity) AS qty_sold,
               SUM(oi.line_total) AS list_revenue,
               SUM(oi.line_total * {_NET_RATIO}) AS gross_revenue,
               SUM(oi.line_total * {_TAX_RATIO}) AS gross_tax,
               COALESCE(SUM(rf.qty), 0) AS refunded_qty,
               COALESCE(SUM(rf.amount * {_NET_RATIO}), 0) AS refunded_amount,
               COALESCE(SUM(rf.amount * {_TAX_RATIO}), 0) AS refunded_tax,
               COUNT(DISTINCT o.id) AS orders,
               MIN(o.created_at) AS first_sold,
               MAX(o.created_at) AS last_sold
        {_TOP_SELLERS_FROM} {where_sql}
        GROUP BY item_key
    ''', [start_dt, end_dt] + where_params).fetchall()


def _top_sellers_order_count(conn, start_date, end_date, where_sql, where_params):
    start_dt, end_dt = _utc_bounds(start_date, end_date)
    return conn.execute(f'''
        SELECT COUNT(DISTINCT o.id) {_TOP_SELLERS_FROM} {where_sql}
    ''', [start_dt, end_dt] + where_params).fetchone()[0]


def _top_sellers_trend(conn, start_date, end_date, where_sql, where_params):
    """Net units/revenue per day, week, or month (depending on range length)."""
    range_days = (end_date - start_date).days + 1
    if range_days <= 31:
        grouping, period_expr = 'daily', "date(o.created_at, 'localtime')"
    elif range_days <= 180:
        grouping, period_expr = 'weekly', "strftime('%Y-W%W', o.created_at, 'localtime')"
    else:
        grouping, period_expr = 'monthly', "strftime('%Y-%m', o.created_at, 'localtime')"

    start_dt, end_dt = _utc_bounds(start_date, end_date)
    rows = conn.execute(f'''
        SELECT {period_expr} AS period,
               MIN(date(o.created_at, 'localtime')) AS period_start,
               SUM(oi.quantity) - COALESCE(SUM(rf.qty), 0) AS units,
               SUM(oi.line_total * {_NET_RATIO}) - COALESCE(SUM(rf.amount * {_NET_RATIO}), 0) AS revenue
        {_TOP_SELLERS_FROM} {where_sql}
        GROUP BY period ORDER BY period
    ''', [start_dt, end_dt] + where_params).fetchall()

    by_period = {r['period']: r for r in rows}
    trend = []
    if grouping == 'daily':
        for i in range(range_days):
            d = start_date + timedelta(days=i)
            r = by_period.get(d.strftime('%Y-%m-%d'))
            trend.append({'label': d.strftime('%b %d'),
                          'units': r['units'] if r else 0,
                          'revenue': round(r['revenue'], 2) if r else 0})
    else:
        for r in rows:
            if grouping == 'weekly':
                label = 'Wk of ' + datetime.strptime(r['period_start'], '%Y-%m-%d').strftime('%b %d')
            else:
                label = datetime.strptime(r['period'] + '-01', '%Y-%m-%d').strftime('%b %Y')
            trend.append({'label': label, 'units': r['units'], 'revenue': round(r['revenue'], 2)})
    return grouping, trend


def _pct_change(current, previous):
    if not previous:
        return None
    return (current - previous) / abs(previous) * 100


TOP_SELLERS_SORTS = {
    # key: (row field, default descending)
    'qty': ('net_qty', True),
    'revenue': ('net_revenue', True),
    'profit': ('profit', True),
    'margin': ('margin', True),
    'orders': ('orders', True),
    'avg_price': ('avg_price', True),
    'per_day': ('per_day', True),
    'stock': ('stock', True),
    'days_cover': ('days_cover', False),
    'change': ('qty_change_pct', True),
    'last_sold': ('last_sold', True),
    'name': ('name', False),
}


def generate_top_sellers_data(start_date, end_date, filters, sort='qty', descending=True,
                              category_names=None, low_threshold=5):
    """
    Build the Top Sellers report for a local date range.

    Args:
        start_date, end_date (date): Local date range (inclusive).
        filters (dict): q, categories, area/aisle/shelf/bin, item_status, stock, exclude_custom.
        sort (str): Key from TOP_SELLERS_SORTS.
        descending (bool): Sort direction.
        category_names (dict): SKU category code -> display name.
        low_threshold (int): Global low-stock threshold (for the 'low' stock filter).
    Returns:
        dict with 'items' (all matching rows, sorted), 'summary', 'by_category',
        'by_location', 'trend', and 'trend_grouping'.
    """
    category_names = category_names or {}
    range_days = (end_date - start_date).days + 1
    prev_end = start_date - timedelta(days=1)
    prev_start = prev_end - timedelta(days=range_days - 1)

    where_sql, where_params = _top_sellers_filter_sql(filters, low_threshold)

    conn = get_db_connection()
    try:
        raw = _top_sellers_items(conn, start_date, end_date, where_sql, where_params)
        prev_raw = _top_sellers_items(conn, prev_start, prev_end, where_sql, where_params)
        total_orders = _top_sellers_order_count(conn, start_date, end_date, where_sql, where_params)
        prev_orders = _top_sellers_order_count(conn, prev_start, prev_end, where_sql, where_params)
        trend_grouping, trend = _top_sellers_trend(conn, start_date, end_date, where_sql, where_params)
    finally:
        conn.close()

    prev_map = {r['item_key']: (r['qty_sold'] - r['refunded_qty'], r['gross_revenue'] - r['refunded_amount'])
                for r in prev_raw}

    items = []
    for r in raw:
        d = dict(r)
        d['net_qty'] = d['qty_sold'] - d['refunded_qty']
        d['discounts'] = d['list_revenue'] - d['gross_revenue']
        d['net_revenue'] = d['gross_revenue'] - d['refunded_amount']
        has_cost = d['item_id'] is not None and (d['buy_price'] or 0) > 0
        d['cost'] = d['net_qty'] * d['buy_price'] if has_cost else None
        d['profit'] = d['net_revenue'] - d['cost'] if has_cost else None
        d['margin'] = (d['profit'] / d['net_revenue'] * 100) if has_cost and d['net_revenue'] > 0 else None
        d['avg_price'] = d['gross_revenue'] / d['qty_sold'] if d['qty_sold'] else 0
        d['per_day'] = d['net_qty'] / range_days
        d['days_cover'] = (d['stock'] / d['per_day']) if (d['stock'] or 0) > 0 and d['per_day'] > 0 else None
        d['category'] = _sku_category(d['sku'])
        d['category_name'] = category_names.get(d['category'], d['category'] or '')
        d['kind'] = 'inventory' if d['item_id'] else ('deleted' if d['from_inventory'] else 'custom')
        for f in LOCATION_FIELDS:
            d[f] = _clean_location(d[f])
        d['location'] = ' › '.join(v for v in (d['area'], d['aisle'], d['shelf'], d['bin']) if v)
        d['prev_qty'], d['prev_revenue'] = prev_map.get(d['item_key'], (0, 0))
        d['qty_change_pct'] = _pct_change(d['net_qty'], d['prev_qty'])
        d['last_sold_local'] = _utc_to_local_str(d['last_sold'])
        items.append(d)

    # Totals across every matching item (not just the rows displayed)
    net_revenue = sum(i['net_revenue'] for i in items)
    costed = [i for i in items if i['profit'] is not None]
    costed_revenue = sum(i['net_revenue'] for i in costed)
    profit = sum(i['profit'] for i in costed)
    net_units = sum(i['net_qty'] for i in items)
    prev_units = sum(q for q, _ in prev_map.values())
    prev_revenue = sum(rev for _, rev in prev_map.values())

    for i in items:
        i['share'] = (i['net_revenue'] / net_revenue * 100) if net_revenue else 0

    field, _ = TOP_SELLERS_SORTS.get(sort, TOP_SELLERS_SORTS['qty'])
    present = [i for i in items if i[field] is not None]
    missing = [i for i in items if i[field] is None]
    key = (lambda i: (i[field] or '').lower()) if field == 'name' else (lambda i: i[field])
    present.sort(key=key, reverse=descending)
    items = present + missing  # rows without a value (e.g. no cost data) always sort last

    gross_revenue = sum(i['gross_revenue'] for i in items)
    tax = sum(i['gross_tax'] for i in items)
    refunded_amount = sum(i['refunded_amount'] for i in items)
    summary = {
        'units_sold': sum(i['qty_sold'] for i in items),
        'refunded_units': sum(i['refunded_qty'] for i in items),
        'net_units': net_units,
        'list_revenue': sum(i['list_revenue'] for i in items),
        'discounts': sum(i['discounts'] for i in items),
        'gross_revenue': gross_revenue,               # after discounts, ex. tax, before refunds
        'tax': tax,
        'total_incl_tax': gross_revenue + tax,        # = Custom Report "Overall Total" when unfiltered
        'refunded_amount': refunded_amount,
        'refunded_incl_tax': refunded_amount + sum(i['refunded_tax'] for i in items),
        'net_revenue': net_revenue,
        'profit': profit,
        'margin': (profit / costed_revenue * 100) if costed_revenue > 0 else None,
        'uncosted_items': len(items) - len(costed),
        'item_count': len(items),
        'orders': total_orders,
        'units_per_order': (net_units / total_orders) if total_orders else 0,
        'units_per_day': net_units / range_days,
        'range_days': range_days,
        'prev_start': prev_start,
        'prev_end': prev_end,
        'prev_units': prev_units,
        'prev_revenue': prev_revenue,
        'prev_orders': prev_orders,
        'units_change': _pct_change(net_units, prev_units),
        'revenue_change': _pct_change(net_revenue, prev_revenue),
        'orders_change': _pct_change(total_orders, prev_orders),
    }

    return {
        'items': items,
        'summary': summary,
        'by_category': _breakdown(items, lambda i: i['category_name'] or ('Custom' if i['kind'] == 'custom' else 'Other'), net_revenue),
        'by_location': _breakdown(items, lambda i: ' › '.join(v for v in (i['area'], i['aisle']) if v) or 'No location', net_revenue)[:15],
        'trend': trend,
        'trend_grouping': trend_grouping,
    }


def _breakdown(items, key_fn, total_revenue):
    groups = {}
    for i in items:
        g = groups.setdefault(key_fn(i), {'label': key_fn(i), 'items': 0, 'units': 0, 'revenue': 0.0, 'profit': 0.0})
        g['items'] += 1
        g['units'] += i['net_qty']
        g['revenue'] += i['net_revenue']
        g['profit'] += i['profit'] or 0
    rows = sorted(groups.values(), key=lambda g: g['revenue'], reverse=True)
    for g in rows:
        g['share'] = (g['revenue'] / total_revenue * 100) if total_revenue else 0
    return rows


def _utc_to_local_str(utc_str):
    """Convert a stored UTC timestamp to a local 'YYYY-MM-DD' string."""
    if not utc_str:
        return ''
    try:
        from datetime import timezone
        dt = datetime.strptime(str(utc_str).split('.')[0], '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime('%Y-%m-%d')
    except ValueError:
        return str(utc_str)[:10]


def get_location_options():
    """Distinct non-empty location values per field, for the report filters."""
    conn = get_db_connection()
    try:
        return {
            f: [r[0] for r in conn.execute(f'''
                SELECT DISTINCT TRIM(location_{f}) FROM inventory_items
                WHERE COALESCE(NULLIF(TRIM(location_{f}), 'None'), '') != ''
                ORDER BY 1 COLLATE NOCASE
            ''')]
            for f in LOCATION_FIELDS
        }
    finally:
        conn.close()


def get_first_sale_date():
    """Local date of the earliest POS order, or None."""
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT MIN(date(created_at, 'localtime')) FROM pos_orders WHERE status != 'held'").fetchone()
        return row[0] if row else None
    finally:
        conn.close()

