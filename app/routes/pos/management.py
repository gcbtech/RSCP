"""
POS Management Module
Analytics, reports, and admin settings.
"""
import logging
import json
import csv
import io
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, date, timedelta
from flask import request, redirect, url_for, flash, render_template, jsonify, make_response
from flask_login import current_user, login_required

from app.routes.pos import pos_bp
from app.routes.pos.core import get_pos_setting, set_pos_setting, get_tax_rate
from app.services import money, pos_sales
from app.services.db import get_db_connection, get_request_db
from app.services.pos_ledger import REFUND_METHODS
from app.services.pos_order_state import STATUS_SQL, list_columns_sql, list_rows, order_state, order_view
from app.utils.permissions import require_permission

logger = logging.getLogger(__name__)


# Longest range the dashboard will chart (a day per point); wider custom ranges keep the end date.
_DASHBOARD_MAX_DAYS = 7320


def _dashboard_range():
    """(start, end, use_custom_range) from ?start_date&end_date or the ?days preset.
    Junk falls back to the default timeframe instead of failing."""
    def parse(value):
        try:
            d = date.fromisoformat(value)
        except ValueError:
            return None
        return d if 1971 <= d.year <= 2999 else None

    start = parse(request.args.get('start_date', '').strip())
    end = parse(request.args.get('end_date', '').strip())
    if start and end:
        if start > end:
            start, end = end, start
        start = max(start, end - timedelta(days=_DASHBOARD_MAX_DAYS - 1))
        return start, end, True

    try:
        days = int(request.args.get('days') or get_pos_setting('POS_DEFAULT_REPORT_TIMEFRAME', '30'))
    except (TypeError, ValueError):
        days = 30
    days = min(max(days, 1), _DASHBOARD_MAX_DAYS)
    end = date.today()
    return end - timedelta(days=days - 1), end, False


@pos_bp.route('/management')
@require_permission('pos.manage')
def management():
    """Management dashboard. Money figures come from the report engine
    (pos_reports), so they match the End of Day, Custom and Top Sellers reports."""
    from app.services.pos_reports import build_sales_report, daily_sales_series, generate_top_sellers_data

    conn = get_request_db()
    start, end, use_custom_range = _dashboard_range()

    report = build_sales_report(start, end, conn=conn)
    avg_order_cents = (money.div_round(report['sales_total_cents'], report['order_count'])
                       if report['order_count'] else 0)
    daily_sales = daily_sales_series(start, end, conn=conn)

    # Top sellers and est. profit -- same figures as the Top Sellers report ("View All" opens it with this range)
    top_report = generate_top_sellers_data(start, end, {})
    top_sellers = [{'sku': i['sku'], 'name': i['name'], 'sold': i['net_qty'], 'revenue_cents': i['net_revenue_cents']}
                   for i in top_report['items'][:10]]

    return render_template('pos/management.html',
                           report=report,
                           avg_order_cents=avg_order_cents,
                           profit=top_report['summary'],
                           daily_sales=daily_sales,
                           top_sellers=top_sellers,
                           payment_breakdown=[t for t in report['tenders'] if t['collected_cents'] > 0],
                           days=(end - start).days + 1,
                           start_date=start.isoformat(),
                           end_date=end.isoformat(),
                           use_custom_range=use_custom_range,
                           tax_rate=get_tax_rate() * 100)


@pos_bp.route('/management/sales')
@require_permission('pos.manage')
def sales_history():
    """Detailed sales history."""
    conn = get_request_db()
    
    page = int(request.args.get('page', 1))
    per_page = 50
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    status = request.args.get('status', '')
    item_query = request.args.get('item_query', '').strip()
    
    # Totals after corrections and the status derived from refunds and
    # corrections (a finished sale's own row never changes)
    sql = f'''
        SELECT DISTINCT o.*, u.username as operator_name{list_columns_sql()}
        FROM pos_orders o
        LEFT JOIN users u ON o.operator_id = u.id
    '''
    params = []
    
    if item_query:
        # Enable search by Item (SKU, Name, Secondary IDs)
        sql += " JOIN pos_order_items oi ON o.id = oi.order_id"
        
        # 1. Find matching inventory SKUs (including secondary IDs)
        # Note: We search inventory to find all relevant SKUs that might match the query
        inv_matches = conn.execute('''
            SELECT sku FROM inventory_items 
            WHERE sku LIKE ? OR name LIKE ? OR secondary_ids LIKE ?
        ''', ('%'+item_query+'%', '%'+item_query+'%', '%'+item_query+'%')).fetchall()
        
        matched_skus = [r['sku'] for r in inv_matches]
        
        # 2. Build filter for order items
        # Check if OI matches name/sku directly, OR if its sku is in the inventory matches
        if matched_skus:
            placeholders = ','.join(['?'] * len(matched_skus))
            sql += f" WHERE (oi.name LIKE ? OR oi.sku LIKE ? OR oi.sku IN ({placeholders}))"
            params.extend(['%'+item_query+'%', '%'+item_query+'%'] + matched_skus)
        else:
            sql += " WHERE (oi.name LIKE ? OR oi.sku LIKE ?)"
            params.extend(['%'+item_query+'%', '%'+item_query+'%'])
    else:
        sql += " WHERE 1=1"

    if date_from:
        sql += " AND date(o.created_at, 'localtime') >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND date(o.created_at, 'localtime') <= ?"
        params.append(date_to)
    if status:
        sql += f' AND {STATUS_SQL} = ?'
        params.append(status)
    
    # Count total (distinct orders)
    # For count, we can wrap the query or count distinct IDs
    # Simplest way given complex joins:
    count_offset = sql.find('FROM pos_orders o')
    if item_query:
        count_sql = "SELECT COUNT(DISTINCT o.id) as cnt " + sql[count_offset:]
    else:
        count_sql = "SELECT COUNT(*) as cnt " + sql[count_offset:]
        
    total = conn.execute(count_sql, params).fetchone()['cnt']
    total_pages = (total + per_page - 1) // per_page
    
    sql += ' ORDER BY o.created_at DESC LIMIT ? OFFSET ?'
    params.extend([per_page, (page - 1) * per_page])
    
    orders = list_rows(conn.execute(sql, params).fetchall())
    
    return render_template('pos/sales_history.html',
                           orders=orders,
                           page=page,
                           total_pages=total_pages,
                           total=total,
                           date_from=date_from,
                           date_to=date_to,
                           status=status,
                           item_query=item_query)


@pos_bp.route('/management/top-sellers')
@require_permission('pos.manage')
def top_sellers():
    """Top selling items with date range, SKU/location filters, and detailed metrics."""
    from app.routes.inventory import CATEGORY_CODES
    from app.services.data_manager import load_config
    from app.services.pos_reports import (generate_top_sellers_data, get_location_options, get_first_sale_date,
                                          TOP_SELLERS_SORTS, LOCATION_FIELDS, LOCATION_NONE)
    args = request.args
    today = date.today()

    # Date range: explicit start/end (same as the Custom Report), else the last N days
    try:
        start_date = datetime.strptime(args.get('start', ''), '%Y-%m-%d').date()
        end_date = datetime.strptime(args.get('end', ''), '%Y-%m-%d').date()
    except ValueError:
        try:
            days = max(1, int(args.get('days') or get_pos_setting('POS_DEFAULT_REPORT_TIMEFRAME', '30')))
        except ValueError:
            days = 30
        end_date = today
        start_date = today - timedelta(days=days - 1)
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    location_options = get_location_options()
    filters = {
        'q': args.get('q', '').strip(),
        'categories': [c for c in args.getlist('cat') if c in CATEGORY_CODES],
        'item_status': args.get('item_status') if args.get('item_status') in ('current', 'legacy') else 'all',
        'stock': args.get('stock') if args.get('stock') in ('in', 'low', 'out') else 'any',
        'exclude_custom': args.get('exclude_custom') == '1',
    }
    for field in LOCATION_FIELDS:
        value = args.get(field, '')
        filters[field] = value if value == LOCATION_NONE or value in location_options[field] else ''

    sort = args.get('sort') if args.get('sort') in TOP_SELLERS_SORTS else 'qty'
    direction = args.get('dir') if args.get('dir') in ('asc', 'desc') else ('desc' if TOP_SELLERS_SORTS[sort][1] else 'asc')
    try:
        limit = int(args.get('limit', 50))
    except ValueError:
        limit = 50
    if limit not in TOP_SELLERS_LIMITS:
        limit = 50

    config = load_config() or {}
    report = generate_top_sellers_data(
        start_date, end_date, filters, sort=sort, descending=(direction == 'desc'),
        category_names=CATEGORY_CODES, low_threshold=int(config.get('LOW_STOCK_THRESHOLD', 5)))

    if args.get('format') == 'csv':
        return _top_sellers_csv(report['items'], start_date, end_date)

    # Sortable column headers keep the current filters and toggle direction
    base_args = args.to_dict(flat=False)
    base_args.pop('format', None)
    sort_links = {}
    for key, (_, desc_default) in TOP_SELLERS_SORTS.items():
        if key == sort:
            new_dir = 'asc' if direction == 'desc' else 'desc'
        else:
            new_dir = 'desc' if desc_default else 'asc'
        sort_links[key] = url_for('pos.top_sellers', **{**base_args, 'sort': key, 'dir': new_dir})

    return render_template('pos/top_sellers.html',
                           start_date=start_date,
                           end_date=end_date,
                           first_sale_date=get_first_sale_date(),
                           filters=filters,
                           sort=sort,
                           direction=direction,
                           limit=limit,
                           limits=TOP_SELLERS_LIMITS,
                           category_codes=CATEGORY_CODES,
                           location_options=location_options,
                           location_none=LOCATION_NONE,
                           sort_links=sort_links,
                           csv_url=url_for('pos.top_sellers', **{**base_args, 'format': 'csv'}),
                           shown_items=report['items'][:limit] if limit else report['items'],
                           # chart data for the page script, in dollars
                           trend_json=[{'label': t['label'], 'units': t['units'],
                                        'revenue': money.to_dollars(t['revenue_cents'])}
                                       for t in report['trend']],
                           **report)


TOP_SELLERS_LIMITS = (25, 50, 100, 250, 0)  # 0 = all


def _top_sellers_csv(items, start_date, end_date):
    """Export every matching Top Sellers row (ignores the display limit)."""
    def num(value, digits=2):
        return '' if value is None else round(value, digits)

    def dollars(cents):
        # (fractional) cents -> the dollar number the CSV has always shown
        return '' if cents is None else round(cents / 100, 2)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Rank', 'SKU', 'Name', 'Category', 'Type', 'Area', 'Aisle', 'Shelf', 'Bin',
                     'Qty Sold', 'Qty Refunded', 'Net Qty', 'Item Prices', 'Discounts', 'Sales (ex. tax)',
                     'Refunded', 'Net Sales (ex. tax)',
                     'Sales Share %', 'Avg Price', 'Unit Cost', 'Est. Profit', 'Margin %', 'Orders',
                     'Units/Day', 'Stock', 'Days of Cover', 'Prev Period Net Qty', 'Qty Change %', 'Last Sold'])
    for rank, i in enumerate(items, 1):
        writer.writerow([rank, i['sku'], i['name'], i['category_name'], i['kind'],
                         i['area'], i['aisle'], i['shelf'], i['bin'],
                         i['qty_sold'], i['refunded_qty'], i['net_qty'],
                         dollars(i['list_revenue_cents']), dollars(i['discounts_cents']),
                         dollars(i['gross_revenue_cents']),
                         dollars(i['refunded_amount_cents']), dollars(i['net_revenue_cents']),
                         num(i['share'], 1), dollars(i['avg_price_cents']), dollars(i['buy_price_cents']),
                         dollars(i['profit_cents']), num(i['margin'], 1), i['orders'], num(i['per_day']),
                         '' if i['stock'] is None else i['stock'], num(i['days_cover'], 0),
                         i['prev_qty'], num(i['qty_change_pct'], 1), i['last_sold_local']])

    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = f'attachment; filename=top_sellers_{start_date}_{end_date}.csv'
    return response


@pos_bp.route('/management/refunds-report')
@require_permission('pos.manage')
def refunds_report():
    """Refund rate and analysis."""
    conn = get_request_db()
    
    default_days = get_pos_setting('POS_DEFAULT_REPORT_TIMEFRAME', '30')
    
    # Support custom date range OR days preset
    date_from = request.args.get('date_from', '').strip()
    date_to = request.args.get('date_to', '').strip()
    
    if date_from or date_to:
        # Custom date range mode
        if not date_from:
            date_from = (date.today() - timedelta(days=365)).strftime('%Y-%m-%d')
        if not date_to:
            date_to = date.today().strftime('%Y-%m-%d')
        start_date = date_from
        end_date = date_to
        days = 0  # Indicates custom range
        use_custom_range = True
    else:
        # Days preset mode
        days = int(request.args.get('days', default_days))
        start_date = (date.today() - timedelta(days=max(0, days - 1))).strftime('%Y-%m-%d')
        end_date = date.today().strftime('%Y-%m-%d')
        use_custom_range = False
    
    # Overall refund stats
    stats = conn.execute('''
        SELECT 
            COUNT(*) as refund_count,
            SUM(amount_cents) as total_refunded_cents,
            AVG(amount_cents) as avg_refund_cents
        FROM pos_refunds
        WHERE date(created_at, 'localtime') BETWEEN ? AND ?
    ''', (start_date, end_date)).fetchone()
    
    # Refund reasons breakdown
    reasons = conn.execute('''
        SELECT reason, COUNT(*) as count, SUM(amount_cents) as total_cents
        FROM pos_refunds
        WHERE date(created_at, 'localtime') BETWEEN ? AND ?
        GROUP BY reason
        ORDER BY count DESC
    ''', (start_date, end_date)).fetchall()
    
    # Recent refunds
    recent = conn.execute('''
        SELECT r.*, o.order_number, u.username as manager_name
        FROM pos_refunds r
        JOIN pos_orders o ON r.order_id = o.id
        LEFT JOIN users u ON r.manager_id = u.id
        WHERE date(r.created_at, 'localtime') BETWEEN ? AND ?
        ORDER BY r.created_at DESC
        LIMIT 50
    ''', (start_date, end_date)).fetchall()
    
    return render_template('pos/refunds_report.html',
                           stats=stats,
                           reasons=reasons,
                           recent=recent,
                           days=days,
                           date_from=date_from if use_custom_range else '',
                           date_to=date_to if use_custom_range else '',
                           use_custom_range=use_custom_range)


def _csv_dollars(cents):
    """Cents -> the dollar number these CSVs have always carried (19.99, 10.0)."""
    return money.to_dollars(cents)


def _cash_discount_form_value():
    """The settings form's single cash discount box: the percent, or the
    fixed amount in dollars (as the old float setting rendered: '3.0')."""
    from app.services import settings as settings_store
    if get_pos_setting('CASH_DISCOUNT_TYPE', 'percent') == 'fixed':
        return str(money.to_dollars(settings_store.get('CASH_DISCOUNT_CENTS') or 0))
    return str(float(settings_store.get('CASH_DISCOUNT_PERCENT') or 0))


@pos_bp.route('/management/export')
@require_permission('pos.manage')
def export_data():
    """Export sales data to CSV."""
    report_type = request.args.get('type', 'orders')
    date_from = request.args.get('date_from', (date.today() - timedelta(days=30)).strftime('%Y-%m-%d'))
    date_to = request.args.get('date_to', date.today().strftime('%Y-%m-%d'))
    
    conn = get_request_db()
    output = io.StringIO()
    writer = csv.writer(output)
    
    if report_type == 'orders':
        writer.writerow(['Order Number', 'Date', 'Status', 'Subtotal', 'Tax', 'Discount', 'Total', 'Payment', 'Operator'])
        orders = conn.execute(f'''
            SELECT o.order_number, o.created_at, {STATUS_SQL} AS status, o.subtotal_cents, o.tax_amount_cents,
                   o.discount_amount_cents, o.total_cents, o.payment_method, u.username
            FROM pos_orders o
            LEFT JOIN users u ON o.operator_id = u.id
            WHERE date(o.created_at) BETWEEN ? AND ?
            ORDER BY o.created_at
        ''', (date_from, date_to)).fetchall()
        for o in orders:
            o = list(o)
            o[3:7] = [_csv_dollars(v) for v in o[3:7]]
            writer.writerow(o)
    
    elif report_type == 'items':
        writer.writerow(['Order Number', 'Date', 'SKU', 'Name', 'Quantity', 'Unit Price', 'Discount', 'Line Total'])
        items = conn.execute('''
            SELECT o.order_number, o.created_at, oi.sku, oi.name, oi.quantity,
                   oi.unit_price_cents, oi.discount_type, oi.discount_percent, oi.discount_cents,
                   oi.line_total_cents
            FROM pos_order_items oi
            JOIN pos_orders o ON oi.order_id = o.id
            WHERE date(o.created_at) BETWEEN ? AND ?
            ORDER BY o.created_at
        ''', (date_from, date_to)).fetchall()
        for i in items:
            # Discount column as entered: a percent, or dollars
            if i['discount_type'] == 'percent':
                discount = float(i['discount_percent'] or 0)
            elif i['discount_type'] == 'fixed':
                discount = _csv_dollars(i['discount_cents'] or 0)
            else:
                discount = 0.0
            writer.writerow([i['order_number'], i['created_at'], i['sku'], i['name'], i['quantity'],
                             _csv_dollars(i['unit_price_cents']), discount, _csv_dollars(i['line_total_cents'])])
    
    elif report_type == 'refunds':
        writer.writerow(['Order Number', 'Date', 'Type', 'Amount', 'Reason', 'Manager', 'Items Restocked', 'Items Damaged'])
        refunds = conn.execute('''
            SELECT o.order_number, r.created_at, r.refund_type, r.amount_cents, r.reason,
                   u.username, r.items_restocked, r.items_damaged
            FROM pos_refunds r
            JOIN pos_orders o ON r.order_id = o.id
            LEFT JOIN users u ON r.manager_id = u.id
            WHERE date(r.created_at) BETWEEN ? AND ?
            ORDER BY r.created_at
        ''', (date_from, date_to)).fetchall()
        for r in refunds:
            r = list(r)
            r[3] = _csv_dollars(r[3])
            writer.writerow(r)
    
    output.seek(0)
    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = f'attachment; filename=pos_{report_type}_{date_from}_to_{date_to}.csv'
    
    return response


@pos_bp.route('/management/settings', methods=['GET', 'POST'])
@require_permission('pos.manage')
def settings():
    """POS settings page. The form posts to the generic settings route
    (POST /admin/settings/save, group 'pos'); a POST here is handled the same way."""
    if request.method == 'POST':
        from app.routes.settings import save_settings_from_request
        return save_settings_from_request('pos')

    # Get available serial ports for dropdown
    try:
        from app.services.hardware import list_serial_ports
        available_ports = list_serial_ports()
    except:
        available_ports = []
    
    return render_template('pos/settings.html',
                           tax_rate=round(get_tax_rate() * 100, 4),  # Round for display
                           pos_default_report_timeframe=get_pos_setting('POS_DEFAULT_REPORT_TIMEFRAME', '30'),
                           require_manager_void=get_pos_setting('REQUIRE_MANAGER_VOID', 'false') == 'true',
                           allow_hold_orders=get_pos_setting('ALLOW_HOLD_ORDERS', 'true') == 'true',
                           # Cash discount
                           cash_discount_enabled=get_pos_setting('CASH_DISCOUNT_ENABLED', 'false') == 'true',
                           cash_discount_amount=_cash_discount_form_value(),
                           cash_discount_type=get_pos_setting('CASH_DISCOUNT_TYPE', 'percent'),
                           # Receipt branding
                           receipt_store_name=get_pos_setting('RECEIPT_STORE_NAME', ''),
                           receipt_header=get_pos_setting('RECEIPT_HEADER', ''),
                           receipt_footer=get_pos_setting('RECEIPT_FOOTER', ''),
                           # Receipt styling
                           receipt_store_name_bold=get_pos_setting('RECEIPT_STORE_NAME_BOLD', 'false') == 'true',
                           receipt_store_name_italic=get_pos_setting('RECEIPT_STORE_NAME_ITALIC', 'false') == 'true',
                           receipt_header_bold=get_pos_setting('RECEIPT_HEADER_BOLD', 'false') == 'true',
                           receipt_header_italic=get_pos_setting('RECEIPT_HEADER_ITALIC', 'false') == 'true',
                           receipt_footer_bold=get_pos_setting('RECEIPT_FOOTER_BOLD', 'false') == 'true',
                           receipt_footer_italic=get_pos_setting('RECEIPT_FOOTER_ITALIC', 'false') == 'true',
                           # Hardware settings
                           cash_drawer_enabled=get_pos_setting('CASH_DRAWER_ENABLED', 'false') == 'true',
                           cash_drawer_port=get_pos_setting('CASH_DRAWER_PORT', ''),
                           auto_print_receipt=get_pos_setting('AUTO_PRINT_RECEIPT', 'false') == 'true',
                           # Email automation
                           pos_email_host=get_pos_setting('POS_EMAIL_HOST', ''),
                           pos_email_port=get_pos_setting('POS_EMAIL_PORT', '587'),
                           pos_email_user=get_pos_setting('POS_EMAIL_USER', ''),
                           pos_email_password_set=bool(get_pos_setting('POS_EMAIL_PASSWORD', '')),
                           pos_email_recipients=get_pos_setting('POS_EMAIL_RECIPIENTS', ''),
                           pos_auto_email_enabled=get_pos_setting('POS_AUTO_EMAIL_ENABLED', 'false') == 'true',
                           pos_auto_email_time=get_pos_setting('POS_AUTO_EMAIL_TIME', ''),
                           available_ports=available_ports)


@pos_bp.route('/management/reports/daily')
@require_permission('pos.manage')
def daily_report():
    """End of Day / Daily Report."""

    conn = get_request_db()
    
    # Date selection (default today)
    report_date_str = request.args.get('date', date.today().strftime('%Y-%m-%d'))
    try:
        report_date = datetime.strptime(report_date_str, '%Y-%m-%d').date()
    except ValueError:
        report_date = date.today()
        report_date_str = report_date.strftime('%Y-%m-%d')
    
    # Convert local date to UTC range for correct database queries
    from app.utils.helpers import local_date_to_utc_range
    # (start_dt, end_dt not needed here as service handles it, but kept if used elsewhere or implicit)
    
    from app.services.pos_reports import generate_daily_report_data
    report_data = generate_daily_report_data(report_date)
    
    return render_template('pos/daily_report.html',
                           report_date=report_date,
                           **report_data)


@pos_bp.route('/management/reports/custom')
@require_permission('pos.manage')
def custom_report():
    """Custom Report with arbitrary date range."""
    # Default: current month start → today
    today = date.today()
    default_start = today.replace(day=1).strftime('%Y-%m-%d')
    default_end = today.strftime('%Y-%m-%d')
    
    start_str = request.args.get('start', default_start)
    end_str = request.args.get('end', default_end)
    
    try:
        start_date = datetime.strptime(start_str, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_str, '%Y-%m-%d').date()
    except ValueError:
        start_date = today.replace(day=1)
        end_date = today
        start_str = start_date.strftime('%Y-%m-%d')
        end_str = end_date.strftime('%Y-%m-%d')
    
    # Ensure start <= end
    if start_date > end_date:
        start_date, end_date = end_date, start_date
        start_str = start_date.strftime('%Y-%m-%d')
        end_str = end_date.strftime('%Y-%m-%d')
    
    from app.services.pos_reports import generate_custom_report_data
    report_data = generate_custom_report_data(start_date, end_date)
    
    return render_template('pos/custom_report.html',
                           start_date=start_date,
                           end_date=end_date,
                           **report_data)

@pos_bp.route('/management/reports/daily/print')
@require_permission('pos.manage')
def daily_report_print():
    """Printable Thermal Summary for End of Day."""
    conn = get_request_db()
    
    # Date selection (default today)
    report_date_str = request.args.get('date', date.today().strftime('%Y-%m-%d'))
    try:
        report_date = datetime.strptime(report_date_str, '%Y-%m-%d').date()
    except ValueError:
        report_date = date.today()
        report_date_str = report_date.strftime('%Y-%m-%d')
    
    from app.services.pos_reports import generate_daily_report_data
    report_data = generate_daily_report_data(report_date)

    return render_template('pos/daily_report_print.html',
                           report_date=report_date,
                           **report_data)


@pos_bp.route('/management/reports/valuation')
@require_permission('pos.manage')
def valuation_report():
    """Inventory Valuation Report (POS Module)."""
    # Check if inventory is enabled via config
    from app.services.data_manager import load_config
    config = load_config()
    if not config.get('INVENTORY_ENABLED', False):
        flash('Inventory module is disabled.')
        return redirect(url_for('pos.management'))
        
    conn = get_request_db()
    
    # Query all items
    items = conn.execute('SELECT sku, quantity, buy_price_cents, sell_price_cents FROM inventory_items '
                         'WHERE COALESCE(is_legacy, 0) = 0').fetchall()
    
    category_data = {}
    
    # Define CATEGORY_CODES locally or import if available
    CATEGORY_CODES = {
        'PRI': 'Primary',
        'SEC': 'Secondary',
        'ASC': 'Accessories',
        'ATO': 'Automatic',
    }
    
    total_cost = 0
    total_retail = 0
    total_items = 0
    
    for item in items:
        sku = item['sku'] or ''
        parts = sku.split('-')
        
        # Derive Category from SKU (RSCP-CAT-...)
        if len(parts) >= 2 and parts[0] == 'RSCP':
            cat_code = parts[1]
        else:
            cat_code = 'Uncategorized'
            
        cat_desc = CATEGORY_CODES.get(cat_code, 'Custom / Other')
        
        if cat_code not in category_data:
            category_data[cat_code] = {
                'code': cat_code,
                'description': cat_desc,
                'count': 0,
                'qty': 0,
                'cost_value_cents': 0,
                'retail_value_cents': 0
            }
            
        qty = item['quantity'] or 0
        cost = (item['buy_price_cents'] or 0) * qty
        retail = (item['sell_price_cents'] or 0) * qty
        
        category_data[cat_code]['count'] += 1
        category_data[cat_code]['qty'] += qty
        category_data[cat_code]['cost_value_cents'] += cost
        category_data[cat_code]['retail_value_cents'] += retail
        
        total_cost += cost
        total_retail += retail
        total_items += 1
        
    # Calculate totals
    potential_profit = total_retail - total_cost
    margin_percent = (potential_profit / total_retail * 100) if total_retail > 0 else 0
    
    sorted_cats = sorted(category_data.values(), key=lambda x: x['code'])
    
    return render_template('pos/valuation_report.html',
                           categories=sorted_cats,
                           total_cost_cents=total_cost,
                           total_retail_cents=total_retail,
                           potential_profit_cents=potential_profit,
                           margin_percent=margin_percent,
                           total_items=total_items,
                           today=date.today())


@pos_bp.route('/management/reports/reorder')
@require_permission('pos.manage')
def reorder_report():
    """Smart Reorder Report (POS Module)."""
    conn = get_request_db()
    
    # 1. Calculate Sales Velocity (Avg Daily Sales in last 30 days)
    days_lookback = 30
    start_date = (date.today() - timedelta(days=days_lookback)).strftime('%Y-%m-%d')
    
    # Units sold, plus quantity changes from sale corrections made in the window
    velocity_query = '''
        SELECT sku, SUM(qty) as total_sold FROM (
            SELECT oi.sku AS sku, oi.quantity AS qty
            FROM pos_order_items oi
            JOIN pos_orders o ON oi.order_id = o.id
            WHERE o.created_at >= ? AND o.status != 'held'
            UNION ALL
            SELECT oi.sku, ci.qty_delta
            FROM pos_sale_correction_items ci
            JOIN pos_sale_corrections c ON c.id = ci.correction_id
            JOIN pos_order_items oi ON oi.id = ci.order_item_id
            WHERE c.created_at >= ?
        )
        GROUP BY sku
    '''
    sales_data = conn.execute(velocity_query, (start_date, start_date)).fetchall()
    velocity_map = {row['sku']: (row['total_sold'] / days_lookback) for row in sales_data}
    
    # 2. Get Current Inventory
    inventory_items = conn.execute('''
        SELECT id, sku, name, quantity, supplier, buy_price_cents
        FROM inventory_items 
        WHERE COALESCE(is_legacy, 0) = 0
          AND (quantity > 0 OR sku IN (SELECT sku FROM pos_order_items))
    ''').fetchall()
    
    recommendations = []
    target_days = 30
    
    for item in inventory_items:
        sku = item['sku']
        qty = item['quantity'] or 0
        velocity = velocity_map.get(sku, 0)
        
        if qty == 0 and velocity == 0:
            continue
            
        if velocity > 0:
            days_cover = qty / velocity
        else:
            days_cover = 999
            
        is_low_stock = days_cover < 14
        
        if is_low_stock:
            needed = (velocity * target_days) - qty
            suggested_order = max(1, round(needed))
        else:
            suggested_order = 0
            
        if suggested_order > 0:
            recommendations.append({
                'sku': sku,
                'name': item['name'],
                'supplier': item['supplier'] or 'N/A',
                'qty': qty,
                'velocity': velocity,
                'days_cover': days_cover,
                'suggested': suggested_order,
                'cost_est_cents': suggested_order * (item['buy_price_cents'] or 0)
            })
            
    recommendations.sort(key=lambda x: x['days_cover'])
    
    return render_template('pos/reorder_report.html',
                           recommendations=recommendations,
                           today=date.today())


@pos_bp.route('/management/reports/daily/email', methods=['POST'])
@require_permission('pos.manage')
def send_eod_email():
    """Send the End of Day Report via Email (same email as the automatic one)."""
    report_date_str = request.form.get('date') or request.args.get('date') or date.today().strftime('%Y-%m-%d')
    try:
        report_date = datetime.strptime(report_date_str, '%Y-%m-%d').date()
    except ValueError:
        report_date = date.today()
        report_date_str = report_date.strftime('%Y-%m-%d')

    from app.services.pos_email import deliver_eod_email
    ok, message = deliver_eod_email(report_date)
    flash(message)
    return redirect(url_for('pos.daily_report', date=report_date_str))


@pos_bp.route('/management/reports/daily-items')
@require_permission('pos.manage')
def daily_items_report():
    """Report of all items sold on a specific day."""
    conn = get_request_db()
    
    # 1. Date Selection
    report_date_str = request.args.get('date', date.today().strftime('%Y-%m-%d'))
    try:
        report_date = datetime.strptime(report_date_str, '%Y-%m-%d').date()
    except ValueError:
        report_date = date.today()
        report_date_str = report_date.strftime('%Y-%m-%d')
        
    # Helper for UTC conversion
    from app.utils.helpers import local_date_to_utc_range
    start_dt, end_dt = local_date_to_utc_range(report_date_str)
    
    # 2. Sorting
    sort = request.args.get('sort', 'qty_desc')
    order_clause = "qty DESC"
    
    # SQL Sorts
    if sort == 'qty_asc': order_clause = "qty ASC"
    elif sort == 'revenue_desc': order_clause = "total_cents DESC"
    elif sort == 'revenue_asc': order_clause = "total_cents ASC"
    elif sort == 'name_asc': order_clause = "oi.name ASC"
    elif sort == 'name_desc': order_clause = "oi.name DESC"
    elif sort == 'sku_asc': order_clause = "oi.sku ASC"
    elif sort == 'sku_desc': order_clause = "oi.sku DESC"
    elif sort == 'price_asc': order_clause = "avg_price_cents ASC"
    elif sort == 'price_desc': order_clause = "avg_price_cents DESC"

    # 3. Query
    # Prioritize Inventory SKU (RSCP) over Order SKU (which might be UPC/Part)
    # Lines sold that day, plus the changes sale corrections made that day
    # (corrections count on the day they are made)
    query = f'''
        SELECT
            COALESCE(inv.sku, oi.sku) as sku,
            oi.name,
            SUM(f.qty) as qty,
            SUM(f.total_cents) as total_cents,
            AVG(CASE WHEN f.is_sale = 1 THEN oi.unit_price_cents END) as avg_price_cents,
            MAX(inv.quantity) as current_stock,
            MAX(inv.location_aisle) as aisle
        FROM (
            SELECT oi2.id AS line_id, oi2.quantity AS qty, oi2.line_total_cents AS total_cents, 1 AS is_sale
            FROM pos_order_items oi2
            JOIN pos_orders o ON oi2.order_id = o.id
            WHERE o.created_at BETWEEN ? AND ?
              AND o.status != 'held'
            UNION ALL
            SELECT ci.order_item_id, ci.qty_delta, ci.line_total_delta_cents, 0
            FROM pos_sale_correction_items ci
            JOIN pos_sale_corrections c ON c.id = ci.correction_id
            WHERE c.created_at BETWEEN ? AND ?
        ) f
        JOIN pos_order_items oi ON oi.id = f.line_id
        LEFT JOIN inventory_items inv ON oi.inventory_item_id = inv.id
        GROUP BY COALESCE(inv.sku, oi.sku)
        ORDER BY {order_clause}
    '''

    rows = conn.execute(query, (start_dt, end_dt, start_dt, end_dt)).fetchall()
    
    # 4. Process Category logic (Python side) & Format
    items = []
    
    # Define CATEGORY_CODES locally
    CATEGORY_CODES = {
        'PRI': 'Primary',
        'SEC': 'Secondary',
        'ASC': 'Accessories',
        'ATO': 'Automatic',
    }
    
    for r in rows:
        sku = r['sku'] or ''
        
        # Display Logic: "Custom-#" for custom items
        if sku.lower().startswith('custom'):
             sku = 'Custom-#'
             
        parts = sku.split('-')
        cat_code = parts[1] if len(parts) >= 2 and parts[0] == 'RSCP' else 'Other'
        category = CATEGORY_CODES.get(cat_code, cat_code)
        
        items.append({
            'sku': r['sku'],
            'name': r['name'],
            'qty': r['qty'],
            'total_cents': r['total_cents'],
            'avg_price_cents': r['avg_price_cents'],
            'current_stock': r['current_stock'] if r['current_stock'] is not None else 0,
            'category': category
        })

    # Python Sorts (Computed fields)
    if sort == 'cat_asc':
        items.sort(key=lambda x: x['category'])
    elif sort == 'cat_desc':
        items.sort(key=lambda x: x['category'], reverse=True)
        
    return render_template('pos/daily_items.html', 
                           items=items, 
                           report_date=report_date,
                           sort=sort)


def _sale_adjustments(conn, order):
    """The sale's own order-level adjustments (pos_sales.sale_adjustments)."""
    return pos_sales.sale_adjustments(conn, order, default_tax_rate=get_tax_rate())


# (order_discount, tax, total) for an edited sale; mirrors pos-edit-sale.js
_reprice_sale = pos_sales.reprice_sale


@pos_bp.route('/management/edit-sale/<int:order_id>')
@require_permission('pos.manage')
def edit_sale(order_id):
    """Correct a sale (managers). The page starts from the sale as it stands
    now (earlier corrections and refunds applied); saving records a
    correction, the sale itself never changes."""
    conn = get_request_db()

    order = conn.execute('''
        SELECT o.*, u.username as operator_name
        FROM pos_orders o
        LEFT JOIN users u ON o.operator_id = u.id
        WHERE o.id = ?
    ''', (order_id,)).fetchone()

    if not order:
        flash("Order not found.")
        return redirect(url_for('pos.sales_history'))

    state = order_state(conn, order)
    if state['voided']:
        flash("This sale was voided and can no longer be changed.")
        return redirect(url_for('pos.sales_history'))

    adj = _sale_adjustments(conn, order)
    return render_template('pos/edit_sale.html',
                           order=order_view(state),
                           state=state,
                           items=state['live_lines'],
                           tax_rate=adj['tax_rate'] * 100,
                           adjustments=adj,
                           adjustments_json=_adjustments_json(adj, state),
                           refund_methods=REFUND_METHODS)


def _adjustments_json(adj, state):
    """sale_adjustments() as the dollar JSON pos-edit-sale.js's preview reads
    (discount_pct as a fraction of the subtotal), plus the sale's current
    total and the amounts by which its stored tax/total differ from what
    re-pricing its current lines gives (zero for every sale recorded by
    checkout), so the preview shows exactly what saving records."""
    pct = adj['discount_pct']
    lines_now = sum(ln['line_total_cents'] for ln in state['lines'])
    repriced = pos_sales.reprice_parts(lines_now, adj)
    return {
        'discount_pct': (pct[0] / pct[1]) if pct else None,
        'discount_fixed': money.to_dollars(adj['discount_fixed']),
        'coupon': money.to_dollars(adj['coupon']),
        'cash_discount': money.to_dollars(adj['cash_discount']),
        'tax_rate': adj['tax_rate'],
        'orig_subtotal': money.to_dollars(adj['orig_subtotal']),
        'current_total': money.to_dollars(state['net']['total_cents']),
        'tax_offset': money.to_dollars(state['net']['tax_cents'] - repriced['tax']),
        'total_offset': money.to_dollars(state['net']['total_cents'] - repriced['total']),
        'refunded': money.to_dollars(state['refunded_cents']),
    }


@pos_bp.route('/management/edit-sale/<int:order_id>', methods=['POST'])
@require_permission('pos.manage')
def edit_sale_post(order_id):
    """Save a correction (managers): pos_sales.edit_sale."""
    form = request.form
    item_ids = form.getlist('item_id')
    quantities = form.getlist('item_quantity')
    prices = form.getlist('item_price')
    names = form.getlist('item_name')

    def _at(values, i):
        return values[i] if i < len(values) else None

    lines = [pos_sales.SaleLineEdit(item_ids[i], _at(quantities, i), _at(prices, i), _at(names, i) or '')
             for i in range(len(item_ids))]

    method = form.get('correction_method') or None
    conn = get_request_db()
    try:
        result = pos_sales.edit_sale(
            conn, order_id, lines, form.getlist('deleted_items'), current_user,
            method=method, reason=(form.get('reason') or '').strip()[:500],
            default_tax_rate=get_tax_rate(), ip=request.remote_addr)
    except (pos_sales.InvalidSaleItems, pos_sales.RefundedLine, pos_sales.BelowRefunded,
            pos_sales.UnknownMethod) as e:
        flash(str(e))
        return redirect(url_for('pos.edit_sale', order_id=order_id))
    except pos_sales.SaleError as e:  # not found / voided
        flash(str(e))
        return redirect(url_for('pos.sales_history'))
    if conn.in_transaction:
        conn.commit()

    if not result.changes:
        flash(f"No changes to sale {result.order_number}.")
        return redirect(url_for('pos.sales_history'))
    message = f"Sale {result.order_number} corrected: {len(result.changes)} change(s)."
    label = REFUND_METHODS.get(result.method, result.method)
    if result.money_cents < 0:
        message += f" Pay the customer back {money.fmt(-result.money_cents)} ({label})."
    elif result.money_cents > 0:
        message += f" Collect {money.fmt(result.money_cents)} from the customer ({label})."
    flash(message)
    return redirect(url_for('pos.sales_history'))


@pos_bp.route('/management/stale-inventory')
@require_permission('pos.manage')
def stale_inventory():
    """Stale Inventory Report."""
    conn = get_request_db()
    
    # Calculate stale metrics:
    # days_stale = Current Date - Max(Last Sold Date, Added Date)
    # stale_score = days_stale * current_quantity
    
    items = conn.execute('''
        SELECT 
            i.id, i.sku, i.name, i.quantity as current_stock, i.created_at as added_date,
            SUM(oi.quantity) as lifetime_sold,
            MAX(o.created_at) as last_sold_date,
            CAST(julianday('now', 'localtime') - julianday(COALESCE(MAX(o.created_at), i.created_at)) AS INTEGER) as days_stale,
            (CAST(julianday('now', 'localtime') - julianday(COALESCE(MAX(o.created_at), i.created_at)) AS INTEGER) * i.quantity) as stale_score
        FROM inventory_items i
        LEFT JOIN pos_order_items oi ON i.id = oi.inventory_item_id
        LEFT JOIN pos_orders o ON oi.order_id = o.id AND o.status != 'held'
        WHERE i.quantity > 0 AND COALESCE(i.is_legacy, 0) = 0
        GROUP BY i.id
        ORDER BY stale_score DESC
    ''').fetchall()
    
    return render_template('pos/stale_inventory.html', items=items)
