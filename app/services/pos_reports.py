"""
POS Reports Service

One sales engine (build_sales_report) produces every money figure for the
daily report page, its thermal print view, the manual and automatic EOD
emails, the custom report and the management dashboard. Pages add presentation extras (hourly or
per-period activity, top items) but never recompute the money.

Sales count on the sale date; refunds and sale corrections (Edit Sale saves
and voids, schema v23) on the date they were made, which is when the money
moved. A finished sale never changes, so a day's figures never change when
a sale from that day is corrected later.

Money is integer cents (keys end in _cents); sums of stored cents are exact,
so nothing is re-rounded. The Top Sellers figures spread order-level
discounts and tax over lines pro rata; those shares are fractional cents
(floats from SQL) and are rounded only for display.
"""
import re
from datetime import datetime, date, timedelta
from app.services.db import get_db_connection
from app.utils.helpers import local_date_to_utc_range

TENDER_LABELS = {'cash': 'Cash', 'card': 'Card', 'split': 'Split (unallocated)'}
_TENDER_ORDER = ['cash', 'card']

# Refund method for refunds recorded before the ledger existed (or inserted
# without one): card sales went back to card, everything else in cash.
_REFUND_METHOD_SQL = ("COALESCE(r.refund_method, CASE WHEN o.payment_method = 'card' "
                      "THEN 'card' ELSE 'cash' END)")
_REFUND_TAX_SQL = ("COALESCE(r.tax_refunded_cents, CASE WHEN o.total_cents > 0 "
                   "THEN CAST(ROUND(r.amount_cents * 1.0 * o.tax_amount_cents / o.total_cents) AS INTEGER) "
                   "ELSE 0 END)")


def _m(value):
    """A SQL sum of cents as an int (NULL -> 0)."""
    return int(value or 0)


def build_sales_report(start_date, end_date, conn=None):
    """The money figures (cents) for a local date range (inclusive).

    Sales are counted on the sale date, refunds and sale corrections on the
    date they were made (that is when the money moved). Returns a dict:

      order_count              completed (non-held) sales
      gross_sales_cents        list value of items sold, before any discount, ex. tax
      discounts                {'line_cents', 'order_cents', 'coupon_cents', 'cash_cents', 'total_cents'}
      net_sales_cents          sales_total - tax_collected (ex. tax)
      tax_collected_cents      tax charged on those sales
      sales_total_cents        what customers paid
      refunds                  {'count', 'total_cents', 'tax_cents', 'net_cents',
                                'by_method_cents': {method: cents}}
      corrections              {'count', 'voids', 'subtotal_cents', 'discount_cents', 'coupon_cents',
                                'cash_discount_cents', 'tax_cents', 'total_cents', 'net_cents',
                                'by_method_cents'}: sale corrections made in the range, signed
                               (negative = taken off sales / paid back)
      tax_net_cents            tax_collected + corrections tax - refunded tax
      net_revenue_cents        sales_total + corrections total - refunds total (incl. tax)
      net_sales_after_refunds_cents   net_sales + corrections net - refunds net (ex. tax)
      tenders                  [{'method', 'label', 'count', 'collected_cents', 'corrected_cents',
                                 'refunded_cents', 'net_cents'}]
      tender_net_cents         {method: net cents}
      expected_cash            {'opening_float_cents', 'cash_sales_cents', 'cash_refunds_cents',
                                'cash_corrections_cents', 'total_cents'}
      drawer_sessions          drawer sessions overlapping the range, with expected/over-short
      summary                  legacy keys: total_orders, gross_sales_cents (= sales_total,
                               the "Overall Total"), total_tax_cents, total_discounts_cents
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
                   COALESCE(SUM(total_cents), 0) AS total,
                   COALESCE(SUM(tax_amount_cents), 0) AS tax,
                   COALESCE(SUM(discount_amount_cents), 0) AS order_disc,
                   COALESCE(SUM(cash_discount_cents), 0) AS cash_disc
            FROM pos_orders
            WHERE created_at BETWEEN ? AND ? AND status != 'held'
        ''', rng).fetchone()

        lines = conn.execute('''
            SELECT COALESCE(SUM(oi.unit_price_cents * oi.quantity), 0) AS gross,
                   COALESCE(SUM(oi.unit_price_cents * oi.quantity - oi.line_total_cents), 0) AS line_disc
            FROM pos_order_items oi JOIN pos_orders o ON o.id = oi.order_id
            WHERE o.created_at BETWEEN ? AND ? AND o.status != 'held'
        ''', rng).fetchone()

        coupon = conn.execute('''
            SELECT COALESCE(SUM(cr.discount_applied_cents), 0)
            FROM pos_coupon_redemptions cr JOIN pos_orders o ON o.id = cr.order_id
            WHERE o.created_at BETWEEN ? AND ? AND o.status != 'held'
        ''', rng).fetchone()[0]

        refund_rows = conn.execute(f'''
            SELECT {_REFUND_METHOD_SQL} AS method,
                   COUNT(*) AS n,
                   COALESCE(SUM(r.amount_cents), 0) AS amount,
                   COALESCE(SUM({_REFUND_TAX_SQL}), 0) AS tax
            FROM pos_refunds r LEFT JOIN pos_orders o ON o.id = r.order_id
            WHERE r.created_at BETWEEN ? AND ?
            GROUP BY method
        ''', rng).fetchall()

        corr = conn.execute('''
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(CASE WHEN kind = 'void' THEN 1 ELSE 0 END), 0) AS voids,
                   COALESCE(SUM(subtotal_delta_cents), 0) AS subtotal,
                   COALESCE(SUM(discount_delta_cents), 0) AS discount,
                   COALESCE(SUM(coupon_delta_cents), 0) AS coupon,
                   COALESCE(SUM(cash_discount_delta_cents), 0) AS cash_disc,
                   COALESCE(SUM(tax_delta_cents), 0) AS tax,
                   COALESCE(SUM(total_delta_cents), 0) AS total
            FROM pos_sale_corrections
            WHERE created_at BETWEEN ? AND ?
        ''', rng).fetchone()

        # Money moved by corrections, by tender, when it moved
        corr_tenders = conn.execute('''
            SELECT p.method AS method, COALESCE(SUM(p.amount_cents), 0) AS amount
            FROM pos_payments p
            WHERE p.correction_id IS NOT NULL AND p.created_at BETWEEN ? AND ?
            GROUP BY p.method
        ''', rng).fetchall()

        # Tenders from the ledger; a sale with no ledger rows (recorded by
        # something other than checkout) counts under its payment_method.
        tender_rows = conn.execute('''
            SELECT p.method AS method, COUNT(DISTINCT p.order_id) AS n, COALESCE(SUM(p.amount_cents), 0) AS amount
            FROM pos_payments p JOIN pos_orders o ON o.id = p.order_id
            WHERE o.created_at BETWEEN ? AND ? AND o.status != 'held' AND p.correction_id IS NULL
            GROUP BY p.method
            UNION ALL
            SELECT o.payment_method, COUNT(*), COALESCE(SUM(o.total_cents), 0)
            FROM pos_orders o
            WHERE o.created_at BETWEEN ? AND ? AND o.status != 'held'
              AND NOT EXISTS (SELECT 1 FROM pos_payments p WHERE p.order_id = o.id AND p.correction_id IS NULL)
            GROUP BY o.payment_method
        ''', rng + rng).fetchall()

        sessions = sessions_between(conn, start_dt, end_dt)
        opening_float = sum((s['opening_float_cents'] or 0) for s in sessions
                            if start_dt <= str(s['opened_at']) <= end_dt)
    finally:
        if own:
            conn.close()

    discounts = {
        'line_cents': _m(lines['line_disc']),
        'order_cents': _m(orders['order_disc']),
        'coupon_cents': _m(coupon),
        'cash_cents': _m(orders['cash_disc']),
    }
    discounts['total_cents'] = sum(discounts.values())
    sales_total = _m(orders['total'])
    tax_collected = _m(orders['tax'])
    net_sales = sales_total - tax_collected

    refunds_by_method = {}
    refund_count = 0
    refund_tax = 0
    for r in refund_rows:
        refunds_by_method[r['method']] = refunds_by_method.get(r['method'], 0) + _m(r['amount'])
        refund_count += r['n']
        refund_tax += _m(r['tax'])
    refund_total = sum(refunds_by_method.values())
    refunds = {'count': refund_count, 'total_cents': refund_total, 'tax_cents': refund_tax,
               'net_cents': refund_total - refund_tax, 'by_method_cents': refunds_by_method}

    corrected = {}
    for t in corr_tenders:
        corrected[t['method']] = corrected.get(t['method'], 0) + _m(t['amount'])
    corrections = {
        'count': corr['n'], 'voids': corr['voids'],
        'subtotal_cents': _m(corr['subtotal']), 'discount_cents': _m(corr['discount']),
        'coupon_cents': _m(corr['coupon']), 'cash_discount_cents': _m(corr['cash_disc']),
        'tax_cents': _m(corr['tax']), 'total_cents': _m(corr['total']),
        'net_cents': _m(corr['total']) - _m(corr['tax']),
        'by_method_cents': corrected,
    }

    collected, counts = {}, {}
    for t in tender_rows:
        collected[t['method']] = collected.get(t['method'], 0) + _m(t['amount'])
        counts[t['method']] = counts.get(t['method'], 0) + t['n']
    seen = set(collected) | set(refunds_by_method) | set(corrected)
    methods = [m for m in _TENDER_ORDER if m in seen]
    methods += sorted(m for m in seen if m not in methods)
    tenders = []
    for m in methods:
        c, cr, rf = collected.get(m, 0), corrected.get(m, 0), refunds_by_method.get(m, 0)
        tenders.append({'method': m, 'label': TENDER_LABELS.get(m, (m or '').title()),
                        'count': counts.get(m, 0), 'collected_cents': c, 'corrected_cents': cr,
                        'refunded_cents': rf, 'net_cents': c + cr - rf})
    tender_net = {t['method']: t['net_cents'] for t in tenders}

    cash_sales = collected.get('cash', 0)
    cash_refunds = refunds_by_method.get('cash', 0)
    cash_corrections = corrected.get('cash', 0)
    expected_cash = {'opening_float_cents': opening_float, 'cash_sales_cents': cash_sales,
                     'cash_refunds_cents': cash_refunds, 'cash_corrections_cents': cash_corrections,
                     'total_cents': opening_float + cash_sales - cash_refunds + cash_corrections}

    return {
        'start_date': start_date,
        'end_date': end_date,
        'range_days': (end_date - start_date).days + 1,
        'order_count': orders['n'],
        'gross_sales_cents': _m(lines['gross']),
        'discounts': discounts,
        'net_sales_cents': net_sales,
        'tax_collected_cents': tax_collected,
        'sales_total_cents': sales_total,
        'refunds': refunds,
        'corrections': corrections,
        'tax_net_cents': tax_collected + corrections['tax_cents'] - refund_tax,
        'net_revenue_cents': sales_total + corrections['total_cents'] - refund_total,
        'net_sales_after_refunds_cents': net_sales + corrections['net_cents'] - refunds['net_cents'],
        'tenders': tenders,
        'tender_net_cents': tender_net,
        'expected_cash': expected_cash,
        'drawer_sessions': sessions,
        'summary': {'total_orders': orders['n'], 'gross_sales_cents': sales_total,
                    'total_tax_cents': tax_collected, 'total_discounts_cents': discounts['total_cents']},
    }


def daily_sales_series(start_date, end_date, conn=None):
    """Orders and sales total (incl. tax, before refunds) for every local date
    in the range, zero-filled, with the sale corrections made that day:
    [{'day': 'YYYY-MM-DD', 'orders', 'revenue_cents', 'corrections_cents'}].

    Same figures as build_sales_report, so the days add up to its
    order_count, sales_total and corrections total for the range.
    """
    own = conn is None
    if own:
        conn = get_db_connection()
    try:
        start_dt, end_dt = _utc_bounds(start_date, end_date)
        rows = conn.execute('''
            SELECT date(created_at, 'localtime') AS day, COUNT(*) AS orders,
                   COALESCE(SUM(total_cents), 0) AS revenue
            FROM pos_orders
            WHERE created_at BETWEEN ? AND ? AND status != 'held'
            GROUP BY day
        ''', (start_dt, end_dt)).fetchall()
        corr_rows = conn.execute('''
            SELECT date(created_at, 'localtime') AS day, COALESCE(SUM(total_delta_cents), 0) AS total
            FROM pos_sale_corrections
            WHERE created_at BETWEEN ? AND ?
            GROUP BY day
        ''', (start_dt, end_dt)).fetchall()
    finally:
        if own:
            conn.close()

    by_day = {r['day']: r for r in rows}
    corr_by_day = {r['day']: _m(r['total']) for r in corr_rows}
    series = []
    for i in range((end_date - start_date).days + 1):
        day = (start_date + timedelta(days=i)).strftime('%Y-%m-%d')
        r = by_day.get(day)
        series.append({'day': day, 'orders': r['orders'] if r else 0,
                       'revenue_cents': _m(r['revenue']) if r else 0,
                       'corrections_cents': corr_by_day.get(day, 0)})
    return series


def _top_items(start_date, end_date, limit=10):
    """Top items by net sales, same figures as the Top Sellers report."""
    top_report = generate_top_sellers_data(start_date, end_date, {}, sort='revenue')
    return [{'sku': i['sku'], 'name': i['name'], 'qty': i['net_qty'], 'total_cents': i['net_revenue_cents']}
            for i in top_report['items'][:limit]]


def generate_daily_report_data(report_date):
    """The End of Day report for one local date: the sales engine's figures
    plus hourly activity and top items."""
    report = build_sales_report(report_date, report_date)
    conn = get_db_connection()
    try:
        start_dt, end_dt = local_date_to_utc_range(report_date.strftime('%Y-%m-%d'))
        hourly = conn.execute('''
            SELECT strftime('%H', created_at, 'localtime') as hour, COUNT(*) as count,
                   SUM(total_cents) as amount_cents
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
                filled_hourly.append({'hour': f"{h:02d}", 'count': 0, 'amount_cents': 0})
        hourly_data = filled_hourly

    max_hourly_revenue = max((h['amount_cents'] for h in hourly_data), default=0) if hourly_data else 0

    return {
        'report': report,
        'hourly': hourly_data,
        'max_hourly_revenue_cents': max_hourly_revenue,
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
                       COUNT(*) as count, SUM(total_cents) as amount_cents
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
                       COUNT(*) as count, SUM(total_cents) as amount_cents
                FROM pos_orders
                WHERE created_at BETWEEN ? AND ? AND status != 'held'
                GROUP BY period ORDER BY period
            ''', (start_dt, end_dt)).fetchall()
        else:  # monthly
            activity_raw = conn.execute('''
                SELECT strftime('%Y-%m', created_at, 'localtime') as period,
                       COUNT(*) as count, SUM(total_cents) as amount_cents
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

    max_activity_revenue = max((a['amount_cents'] for a in activity_data), default=0) if activity_data else 0

    return {
        'report': report,
        # kept at the top level for callers that read the custom report's summary
        'summary': report['summary'],
        'activity_data': activity_data,
        'activity_grouping': grouping,
        'max_activity_revenue_cents': max_activity_revenue,
        # same figures as the Top Sellers report (after discounts and refunds, ex. tax)
        'top_items': _top_items(start_date, end_date),
        'range_days': range_days,
    }


# ---------------------------------------------------------------------------
# Top Sellers Report
# ---------------------------------------------------------------------------

LOCATION_FIELDS = ('area', 'aisle', 'shelf', 'bin')
LOCATION_NONE = '__none__'  # filter value meaning "no location set"

# A line came from inventory (not a custom item) when it has an item id, or --
# for lines whose item was deleted before foreign keys were enforced (schema
# v16 set those dangling ids to NULL) -- when its SKU is not a CUSTOM-... one.
_FROM_INVENTORY = "(oi.inventory_item_id IS NOT NULL OR oi.sku NOT LIKE 'CUSTOM-%')"

# Top Sellers works on "facts": one row per sold line, on the sale date, and
# one row per line a sale correction changed, on the correction date
# (corrections count on the day they are made, like everywhere else).
#   f.qty / f.list_cents   units and line value (corrections: signed deltas)
#   f.rev / f.tax          the line's share of the sale ex. tax and of its tax:
#                          for a sale, its line total spread pro rata over the
#                          order total -- that keeps item figures adding up to
#                          the order totals the Custom Report uses; for a
#                          correction, the shares it recorded per line
#   f.rf_*                 refunds of the line, attributed to the sale (by the
#                          sale as it stands now, corrections applied)
# Order items resolve to their inventory item by id, falling back to SKU when
# the original item was deleted and re-created.
# * 1.0: these ratios are fractions (SQLite would truncate INTEGER / INTEGER).
_NET_RATIO = ('(CASE WHEN ol.lines_total > 0 THEN (o.total_cents - o.tax_amount_cents) * 1.0 / ol.lines_total '
              'ELSE 0 END)')
_TAX_RATIO = '(CASE WHEN ol.lines_total > 0 THEN o.tax_amount_cents * 1.0 / ol.lines_total ELSE 0 END)'
_NOW_LINES = '(ol.lines_total + COALESCE(cn.lines_d, 0))'
_NET_RATIO_NOW = (f'(CASE WHEN {_NOW_LINES} > 0 THEN ((o.total_cents + COALESCE(cn.total_d, 0)) '
                  f'- (o.tax_amount_cents + COALESCE(cn.tax_d, 0))) * 1.0 / {_NOW_LINES} ELSE 0 END)')
_TAX_RATIO_NOW = (f'(CASE WHEN {_NOW_LINES} > 0 THEN (o.tax_amount_cents + COALESCE(cn.tax_d, 0)) * 1.0 '
                  f'/ {_NOW_LINES} ELSE 0 END)')

_TOP_SELLERS_FACTS = f'''
    SELECT oi.id AS line_id, o.id AS order_id, o.created_at AS at, 1 AS is_sale,
           oi.quantity AS qty, oi.line_total_cents AS list_cents,
           oi.line_total_cents * {_NET_RATIO} AS rev,
           oi.line_total_cents * {_TAX_RATIO} AS tax,
           COALESCE(rf.qty, 0) AS rf_qty,
           COALESCE(rf.amount, 0) * {_NET_RATIO_NOW} AS rf_rev,
           COALESCE(rf.amount, 0) * {_TAX_RATIO_NOW} AS rf_tax
    FROM pos_order_items oi
    JOIN pos_orders o ON o.id = oi.order_id
    JOIN (SELECT order_id, SUM(line_total_cents) AS lines_total
          FROM pos_order_items GROUP BY order_id) ol ON ol.order_id = o.id
    LEFT JOIN (SELECT c.order_id, SUM(c.total_delta_cents) AS total_d, SUM(c.tax_delta_cents) AS tax_d,
                      SUM((SELECT COALESCE(SUM(ci.line_total_delta_cents), 0) FROM pos_sale_correction_items ci
                           WHERE ci.correction_id = c.id)) AS lines_d
               FROM pos_sale_corrections c GROUP BY c.order_id) cn ON cn.order_id = o.id
    LEFT JOIN (
        SELECT order_item_id, SUM(quantity) AS qty, SUM(amount_cents) AS amount
        FROM pos_refund_items GROUP BY order_item_id
    ) rf ON rf.order_item_id = oi.id
    WHERE o.created_at BETWEEN ? AND ? AND o.status != 'held'
    UNION ALL
    SELECT ci.order_item_id, c.order_id, c.created_at, 0,
           ci.qty_delta, ci.line_total_delta_cents,
           ci.revenue_delta_cents * 1.0, ci.tax_delta_cents * 1.0,
           0, 0.0, 0.0
    FROM pos_sale_correction_items ci
    JOIN pos_sale_corrections c ON c.id = ci.correction_id
    JOIN pos_orders co ON co.id = c.order_id
    WHERE c.created_at BETWEEN ? AND ? AND co.status != 'held'
'''

_TOP_SELLERS_FROM = '''
    FROM (''' + _TOP_SELLERS_FACTS + ''') f
    JOIN pos_order_items oi ON oi.id = f.line_id
    LEFT JOIN inventory_items inv ON inv.id = COALESCE(
        (SELECT x.id FROM inventory_items x WHERE x.id = oi.inventory_item_id),
        CASE WHEN ''' + _FROM_INVENTORY + '''
             THEN (SELECT y.id FROM inventory_items y WHERE y.sku = oi.sku) END)
    WHERE 1 = 1
'''

_TOP_SELLERS_KEY = '''
    CASE WHEN inv.id IS NOT NULL THEN 'i:' || inv.id
         WHEN ''' + _FROM_INVENTORY + ''' THEN 's:' || oi.sku
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
        clauses.append(_FROM_INVENTORY)

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
               MAX({_FROM_INVENTORY}) AS from_inventory,
               COALESCE(MAX(inv.sku), MAX(oi.sku)) AS sku,
               COALESCE(MAX(inv.name), MAX(oi.name)) AS name,
               MAX(inv.location_area) AS area,
               MAX(inv.location_aisle) AS aisle,
               MAX(inv.location_shelf) AS shelf,
               MAX(inv.location_bin) AS bin,
               MAX(inv.quantity) AS stock,
               MAX(inv.buy_price_cents) AS buy_price_cents,
               MAX(inv.sell_price_cents) AS list_price_cents,
               MAX(inv.is_legacy) AS is_legacy,
               SUM(f.qty) AS qty_sold,
               SUM(f.list_cents) AS list_revenue_cents,
               SUM(f.rev) AS gross_revenue_cents,
               SUM(f.tax) AS gross_tax_cents,
               COALESCE(SUM(f.rf_qty), 0) AS refunded_qty,
               COALESCE(SUM(f.rf_rev), 0) AS refunded_amount_cents,
               COALESCE(SUM(f.rf_tax), 0) AS refunded_tax_cents,
               COUNT(DISTINCT CASE WHEN f.is_sale = 1 THEN f.order_id END) AS orders,
               MIN(CASE WHEN f.is_sale = 1 THEN f.at END) AS first_sold,
               MAX(CASE WHEN f.is_sale = 1 THEN f.at END) AS last_sold
        {_TOP_SELLERS_FROM} {where_sql}
        GROUP BY item_key
    ''', [start_dt, end_dt, start_dt, end_dt] + where_params).fetchall()


def _top_sellers_order_count(conn, start_date, end_date, where_sql, where_params):
    start_dt, end_dt = _utc_bounds(start_date, end_date)
    return conn.execute(f'''
        SELECT COUNT(DISTINCT CASE WHEN f.is_sale = 1 THEN f.order_id END) {_TOP_SELLERS_FROM} {where_sql}
    ''', [start_dt, end_dt, start_dt, end_dt] + where_params).fetchone()[0]


def _top_sellers_trend(conn, start_date, end_date, where_sql, where_params):
    """Net units/revenue per day, week, or month (depending on range length)."""
    range_days = (end_date - start_date).days + 1
    if range_days <= 31:
        grouping, period_expr = 'daily', "date(f.at, 'localtime')"
    elif range_days <= 180:
        grouping, period_expr = 'weekly', "strftime('%Y-W%W', f.at, 'localtime')"
    else:
        grouping, period_expr = 'monthly', "strftime('%Y-%m', f.at, 'localtime')"

    start_dt, end_dt = _utc_bounds(start_date, end_date)
    rows = conn.execute(f'''
        SELECT {period_expr} AS period,
               MIN(date(f.at, 'localtime')) AS period_start,
               SUM(f.qty) - COALESCE(SUM(f.rf_qty), 0) AS units,
               SUM(f.rev) - COALESCE(SUM(f.rf_rev), 0) AS revenue
        {_TOP_SELLERS_FROM} {where_sql}
        GROUP BY period ORDER BY period
    ''', [start_dt, end_dt, start_dt, end_dt] + where_params).fetchall()

    by_period = {r['period']: r for r in rows}
    trend = []
    if grouping == 'daily':
        for i in range(range_days):
            d = start_date + timedelta(days=i)
            r = by_period.get(d.strftime('%Y-%m-%d'))
            trend.append({'label': d.strftime('%b %d'),
                          'units': r['units'] if r else 0,
                          'revenue_cents': round(r['revenue']) if r else 0})
    else:
        for r in rows:
            if grouping == 'weekly':
                label = 'Wk of ' + datetime.strptime(r['period_start'], '%Y-%m-%d').strftime('%b %d')
            else:
                label = datetime.strptime(r['period'] + '-01', '%Y-%m-%d').strftime('%b %Y')
            trend.append({'label': label, 'units': r['units'], 'revenue_cents': round(r['revenue'])})
    return grouping, trend


def _pct_change(current, previous):
    if not previous:
        return None
    return (current - previous) / abs(previous) * 100


TOP_SELLERS_SORTS = {
    # key: (row field, default descending)
    'qty': ('net_qty', True),
    'revenue': ('net_revenue_cents', True),
    'profit': ('profit_cents', True),
    'margin': ('margin', True),
    'orders': ('orders', True),
    'avg_price': ('avg_price_cents', True),
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

    prev_map = {r['item_key']: (r['qty_sold'] - r['refunded_qty'],
                                r['gross_revenue_cents'] - r['refunded_amount_cents'])
                for r in prev_raw}

    items = []
    for r in raw:
        d = dict(r)
        d['net_qty'] = d['qty_sold'] - d['refunded_qty']
        d['discounts_cents'] = d['list_revenue_cents'] - d['gross_revenue_cents']
        d['net_revenue_cents'] = d['gross_revenue_cents'] - d['refunded_amount_cents']
        has_cost = d['item_id'] is not None and (d['buy_price_cents'] or 0) > 0
        d['cost_cents'] = d['net_qty'] * d['buy_price_cents'] if has_cost else None
        d['profit_cents'] = d['net_revenue_cents'] - d['cost_cents'] if has_cost else None
        d['margin'] = ((d['profit_cents'] / d['net_revenue_cents'] * 100)
                       if has_cost and d['net_revenue_cents'] > 0 else None)
        d['avg_price_cents'] = d['gross_revenue_cents'] / d['qty_sold'] if d['qty_sold'] else 0
        d['per_day'] = d['net_qty'] / range_days
        d['days_cover'] = (d['stock'] / d['per_day']) if (d['stock'] or 0) > 0 and d['per_day'] > 0 else None
        d['category'] = _sku_category(d['sku'])
        d['category_name'] = category_names.get(d['category'], d['category'] or '')
        d['kind'] = 'inventory' if d['item_id'] else ('deleted' if d['from_inventory'] else 'custom')
        for f in LOCATION_FIELDS:
            d[f] = _clean_location(d[f])
        d['location'] = ' › '.join(v for v in (d['area'], d['aisle'], d['shelf'], d['bin']) if v)
        d['prev_qty'], d['prev_revenue_cents'] = prev_map.get(d['item_key'], (0, 0))
        d['qty_change_pct'] = _pct_change(d['net_qty'], d['prev_qty'])
        d['last_sold_local'] = _utc_to_local_str(d['last_sold'])
        items.append(d)

    # Totals across every matching item (not just the rows displayed)
    net_revenue = sum(i['net_revenue_cents'] for i in items)
    costed = [i for i in items if i['profit_cents'] is not None]
    costed_revenue = sum(i['net_revenue_cents'] for i in costed)
    profit = sum(i['profit_cents'] for i in costed)
    net_units = sum(i['net_qty'] for i in items)
    prev_units = sum(q for q, _ in prev_map.values())
    prev_revenue = sum(rev for _, rev in prev_map.values())

    for i in items:
        i['share'] = (i['net_revenue_cents'] / net_revenue * 100) if net_revenue else 0

    field, _ = TOP_SELLERS_SORTS.get(sort, TOP_SELLERS_SORTS['qty'])
    present = [i for i in items if i[field] is not None]
    missing = [i for i in items if i[field] is None]
    key = (lambda i: (i[field] or '').lower()) if field == 'name' else (lambda i: i[field])
    present.sort(key=key, reverse=descending)
    items = present + missing  # rows without a value (e.g. no cost data) always sort last

    gross_revenue = sum(i['gross_revenue_cents'] for i in items)
    tax = sum(i['gross_tax_cents'] for i in items)
    refunded_amount = sum(i['refunded_amount_cents'] for i in items)
    summary = {
        'units_sold': sum(i['qty_sold'] for i in items),
        'refunded_units': sum(i['refunded_qty'] for i in items),
        'net_units': net_units,
        'list_revenue_cents': sum(i['list_revenue_cents'] for i in items),
        'discounts_cents': sum(i['discounts_cents'] for i in items),
        'gross_revenue_cents': gross_revenue,         # after discounts, ex. tax, before refunds
        'tax_cents': tax,
        'total_incl_tax_cents': gross_revenue + tax,  # = Custom Report "Overall Total" when unfiltered
        'refunded_amount_cents': refunded_amount,
        'refunded_incl_tax_cents': refunded_amount + sum(i['refunded_tax_cents'] for i in items),
        'net_revenue_cents': net_revenue,
        'profit_cents': profit,
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
        'prev_revenue_cents': prev_revenue,
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
        g = groups.setdefault(key_fn(i), {'label': key_fn(i), 'items': 0, 'units': 0,
                                          'revenue_cents': 0, 'profit_cents': 0})
        g['items'] += 1
        g['units'] += i['net_qty']
        g['revenue_cents'] += i['net_revenue_cents']
        g['profit_cents'] += i['profit_cents'] or 0
    rows = sorted(groups.values(), key=lambda g: g['revenue_cents'], reverse=True)
    for g in rows:
        g['share'] = (g['revenue_cents'] / total_revenue * 100) if total_revenue else 0
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

