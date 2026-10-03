"""
POS Reports Service
Handles generation of report data for both web view and automated emails.
"""
import json
import re
from datetime import datetime, date, timedelta
from app.services.db import get_db_connection
from app.utils.helpers import local_date_to_utc_range

def generate_daily_report_data(report_date):
    """
    Generate the daily report data dictionary for a specific date.
    Args:
        report_date (date): The local date to generate the report for.
    Returns:
        dict: Report data containing summary, refunds, payments, etc.
    """
    conn = get_db_connection()
    try:
        report_date_str = report_date.strftime('%Y-%m-%d')
        start_dt, end_dt = local_date_to_utc_range(report_date_str)

        # 1. Summary Stats
        summary = conn.execute('''
            SELECT 
                COUNT(*) as total_orders,
                COALESCE(SUM(total), 0) as gross_sales,
                COALESCE(SUM(discount_amount), 0) as total_discounts,
                COALESCE(SUM(tax_amount), 0) as total_tax
            FROM pos_orders
            WHERE created_at BETWEEN ? AND ? AND status != 'held'
        ''', (start_dt, end_dt)).fetchone()

        # 2. Refunds
        refunds_summary = conn.execute('''
            SELECT 
                COUNT(*) as count,
                COALESCE(SUM(amount), 0) as total
            FROM pos_refunds
            WHERE created_at BETWEEN ? AND ?
        ''', (start_dt, end_dt)).fetchone()
        
        net_revenue = (summary['gross_sales'] or 0) - (refunds_summary['total'] or 0)

        # 3. Payment Breakdown
        payments = conn.execute('''
            SELECT 
                payment_method, 
                COUNT(*) as count, 
                SUM(total) as total_amount,
                SUM(tax_amount) as tax_amount
            FROM pos_orders
            WHERE created_at BETWEEN ? AND ? AND status != 'held'
            GROUP BY payment_method
        ''', (start_dt, end_dt)).fetchall()

        # Fetch Refund breakdown by payment method
        refund_breakdown = conn.execute('''
            SELECT 
                o.payment_method, 
                SUM(r.amount) as refund_total
            FROM pos_refunds r
            JOIN pos_orders o ON r.order_id = o.id
            WHERE r.created_at BETWEEN ? AND ?
            GROUP BY o.payment_method
        ''', (start_dt, end_dt)).fetchall()
        
        refund_map = {r['payment_method']: (r['refund_total'] or 0) for r in refund_breakdown}

        # Calculate Totals for Summary
        total_cash = 0
        total_cash_net = 0
        total_card = 0
        total_card_net = 0
        
        # Fetch Split Payment Details for precise allocation
        split_orders = conn.execute('''
            SELECT payment_details, total, tax_amount
            FROM pos_orders
            WHERE created_at BETWEEN ? AND ? AND status != 'held' AND payment_method = 'split'
        ''', (start_dt, end_dt)).fetchall()

        split_cash_total = 0
        split_card_total = 0
        
        for sp in split_orders:
            try:
                details = json.loads(sp['payment_details'])
                cash_part = float(details.get('cash', 0))
                card_part = sum(float(x) for x in details.get('cards', []))
                
                split_cash_total += cash_part
                split_card_total += card_part
            except (ValueError, TypeError, json.JSONDecodeError):
                pass

        for p in payments:
            method = p['payment_method']
            amount = p['total_amount'] or 0
            tax = p['tax_amount'] or 0
            net = amount - tax
            
            refund_amount = refund_map.get(method, 0)
            
            if method == 'cash':
                total_cash += (amount - refund_amount)
                total_cash_net += (net - refund_amount)
            elif method == 'split':
                s_total = split_cash_total + split_card_total
                if s_total > 0:
                    s_cash_ratio = split_cash_total / s_total
                    s_card_ratio = split_card_total / s_total
                else:
                    s_cash_ratio = 0
                    s_card_ratio = 1
                
                split_tax_cash = tax * s_cash_ratio
                split_tax_card = tax * s_card_ratio
                
                s_cash_net = split_cash_total - split_tax_cash
                s_card_net = split_card_total - split_tax_card
                
                total_cash += split_cash_total
                total_cash_net += s_cash_net
                
                total_card += (split_card_total - refund_amount)
                total_card_net += (s_card_net - refund_amount)
            else:
                total_card += (amount - refund_amount)
                total_card_net += (net - refund_amount)

        # 4. Hourly Sales
        hourly = conn.execute('''
            SELECT strftime('%H', created_at, 'localtime') as hour, COUNT(*) as count, SUM(total) as amount
            FROM pos_orders
            WHERE created_at BETWEEN ? AND ? AND status != 'held'
            GROUP BY hour
            ORDER BY hour
        ''', (start_dt, end_dt)).fetchall()
        
        # 5. Top Sellers
        top_items = conn.execute('''
            SELECT oi.sku, oi.name, SUM(oi.quantity) as qty, SUM(oi.line_total) as total
            FROM pos_order_items oi
            JOIN pos_orders o ON oi.order_id = o.id
            WHERE o.created_at BETWEEN ? AND ? AND o.status != 'held'
            GROUP BY oi.sku
            ORDER BY total DESC
            LIMIT 10
        ''', (start_dt, end_dt)).fetchall()

        # Process hourly
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
            'summary': dict(summary),
            'refunds': dict(refunds_summary),
            'net_revenue': net_revenue,
            'payments': [dict(p) for p in payments],
            'hourly': hourly_data,
            'max_hourly_revenue': max_hourly_revenue,
            'top_items': [dict(i) for i in top_items],
            'total_cash': total_cash,
            'total_cash_net': total_cash_net,
            'total_card': total_card,
            'total_card_net': total_card_net
        }
    finally:
        conn.close()


def generate_custom_report_data(start_date, end_date):
    """
    Generate report data for an arbitrary date range.
    Args:
        start_date (date): Start of range (local date).
        end_date (date): End of range (local date).
    Returns:
        dict: Report data with activity grouped by day/week/month.
    """
    from datetime import timedelta
    
    conn = get_db_connection()
    try:
        # Convert date range to UTC
        start_dt, _ = local_date_to_utc_range(start_date.strftime('%Y-%m-%d'))
        _, end_dt = local_date_to_utc_range(end_date.strftime('%Y-%m-%d'))
        
        # Determine grouping based on range length
        range_days = (end_date - start_date).days + 1
        if range_days <= 14:
            grouping = 'daily'
        elif range_days <= 60:
            grouping = 'weekly'
        else:
            grouping = 'monthly'

        # 1. Summary Stats
        summary = conn.execute('''
            SELECT 
                COUNT(*) as total_orders,
                COALESCE(SUM(total), 0) as gross_sales,
                COALESCE(SUM(discount_amount), 0) as total_discounts,
                COALESCE(SUM(tax_amount), 0) as total_tax
            FROM pos_orders
            WHERE created_at BETWEEN ? AND ? AND status != 'held'
        ''', (start_dt, end_dt)).fetchone()

        # 2. Refunds
        refunds_summary = conn.execute('''
            SELECT 
                COUNT(*) as count,
                COALESCE(SUM(amount), 0) as total
            FROM pos_refunds
            WHERE created_at BETWEEN ? AND ?
        ''', (start_dt, end_dt)).fetchone()
        
        net_revenue = (summary['gross_sales'] or 0) - (refunds_summary['total'] or 0)

        # 3. Payment Breakdown
        payments = conn.execute('''
            SELECT 
                payment_method, 
                COUNT(*) as count, 
                SUM(total) as total_amount,
                SUM(tax_amount) as tax_amount
            FROM pos_orders
            WHERE created_at BETWEEN ? AND ? AND status != 'held'
            GROUP BY payment_method
        ''', (start_dt, end_dt)).fetchall()

        # Refund breakdown by payment method
        refund_breakdown = conn.execute('''
            SELECT 
                o.payment_method, 
                SUM(r.amount) as refund_total
            FROM pos_refunds r
            JOIN pos_orders o ON r.order_id = o.id
            WHERE r.created_at BETWEEN ? AND ?
            GROUP BY o.payment_method
        ''', (start_dt, end_dt)).fetchall()
        
        refund_map = {r['payment_method']: (r['refund_total'] or 0) for r in refund_breakdown}

        # Calculate Cash/Card Totals (same logic as daily report)
        total_cash = 0
        total_cash_net = 0
        total_card = 0
        total_card_net = 0
        
        split_orders = conn.execute('''
            SELECT payment_details, total, tax_amount
            FROM pos_orders
            WHERE created_at BETWEEN ? AND ? AND status != 'held' AND payment_method = 'split'
        ''', (start_dt, end_dt)).fetchall()

        split_cash_total = 0
        split_card_total = 0
        
        for sp in split_orders:
            try:
                details = json.loads(sp['payment_details'])
                split_cash_total += float(details.get('cash', 0))
                split_card_total += sum(float(x) for x in details.get('cards', []))
            except (ValueError, TypeError, json.JSONDecodeError):
                pass

        for p in payments:
            method = p['payment_method']
            amount = p['total_amount'] or 0
            tax = p['tax_amount'] or 0
            net = amount - tax
            refund_amount = refund_map.get(method, 0)
            
            if method == 'cash':
                total_cash += (amount - refund_amount)
                total_cash_net += (net - refund_amount)
            elif method == 'split':
                s_total = split_cash_total + split_card_total
                if s_total > 0:
                    s_cash_ratio = split_cash_total / s_total
                    s_card_ratio = split_card_total / s_total
                else:
                    s_cash_ratio = 0
                    s_card_ratio = 1
                
                split_tax_cash = tax * s_cash_ratio
                split_tax_card = tax * s_card_ratio
                
                total_cash += split_cash_total
                total_cash_net += (split_cash_total - split_tax_cash)
                total_card += (split_card_total - refund_amount)
                total_card_net += (split_card_total - split_tax_card - refund_amount)
            else:
                total_card += (amount - refund_amount)
                total_card_net += (net - refund_amount)

        # 4. Activity Data (replaces hourly for custom report)
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

        # 5. Top Sellers
        top_items = conn.execute('''
            SELECT oi.sku, oi.name, SUM(oi.quantity) as qty, SUM(oi.line_total) as total
            FROM pos_order_items oi
            JOIN pos_orders o ON oi.order_id = o.id
            WHERE o.created_at BETWEEN ? AND ? AND o.status != 'held'
            GROUP BY oi.sku
            ORDER BY total DESC
            LIMIT 10
        ''', (start_dt, end_dt)).fetchall()

        return {
            'summary': dict(summary),
            'refunds': dict(refunds_summary),
            'net_revenue': net_revenue,
            'payments': [dict(p) for p in payments],
            'activity_data': activity_data,
            'activity_grouping': grouping,
            'max_activity_revenue': max_activity_revenue,
            'top_items': [dict(i) for i in top_items],
            'total_cash': total_cash,
            'total_cash_net': total_cash_net,
            'total_card': total_card,
            'total_card_net': total_card_net,
            'range_days': range_days
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Top Sellers Report
# ---------------------------------------------------------------------------

LOCATION_FIELDS = ('area', 'aisle', 'shelf', 'bin')
LOCATION_NONE = '__none__'  # filter value meaning "no location set"

# Order items resolve to their inventory item by id, falling back to SKU when the
# original item was deleted and re-created. Refunds are attributed to the sale.
_TOP_SELLERS_FROM = '''
    FROM pos_order_items oi
    JOIN pos_orders o ON o.id = oi.order_id
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

    if not filters.get('include_custom'):
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
               SUM(oi.line_total) AS gross_revenue,
               COALESCE(SUM(rf.qty), 0) AS refunded_qty,
               COALESCE(SUM(rf.amount), 0) AS refunded_amount,
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
               SUM(oi.line_total) - COALESCE(SUM(rf.amount), 0) AS revenue
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
        filters (dict): q, categories, area/aisle/shelf/bin, item_status, stock, include_custom.
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

    summary = {
        'units_sold': sum(i['qty_sold'] for i in items),
        'refunded_units': sum(i['refunded_qty'] for i in items),
        'net_units': net_units,
        'gross_revenue': sum(i['gross_revenue'] for i in items),
        'refunded_amount': sum(i['refunded_amount'] for i in items),
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

