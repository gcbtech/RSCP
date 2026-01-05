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
from app.services.db import get_db_connection, get_request_db

logger = logging.getLogger(__name__)


def require_admin(f):
    """Decorator to require admin access."""
    from functools import wraps
    
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_admin:
            flash('Admin access required.')
            return redirect(url_for('pos.sales'))
        return f(*args, **kwargs)
    return decorated_function


@pos_bp.route('/management')
@login_required
@require_admin
def management():
    """Management dashboard with analytics overview."""
    conn = get_request_db()
    
    # Date range (default: last 30 days)
    days = int(request.args.get('days', 30))
    # Use days-1 for inclusive range (e.g. days=1 is just Today)
    lookback = max(0, days - 1)
    start_date = (date.today() - timedelta(days=lookback)).strftime('%Y-%m-%d')
    
    # Summary stats
    stats = conn.execute('''
        SELECT 
            COUNT(*) as total_orders,
            COALESCE(SUM(total), 0) as total_revenue,
            COALESCE(AVG(total), 0) as avg_order,
            COALESCE(SUM(discount_amount), 0) as total_discounts,
            COALESCE(SUM(tax_amount), 0) as total_tax
        FROM pos_orders
        WHERE date(created_at, 'localtime') >= ? AND status != 'held'
    ''', (start_date,)).fetchone()
    
    # Refund stats
    refund_stats = conn.execute('''
        SELECT 
            COUNT(*) as refund_count,
            COALESCE(SUM(amount), 0) as refund_total
        FROM pos_refunds
        WHERE date(created_at, 'localtime') >= ?
    ''', (start_date,)).fetchone()
    
    # Daily sales trend
    days_data = conn.execute('''
        SELECT date(created_at, 'localtime') as day, 
               COUNT(*) as orders,
               COALESCE(SUM(total), 0) as revenue
        FROM pos_orders
        WHERE date(created_at, 'localtime') >= ? AND status != 'held'
        GROUP BY date(created_at, 'localtime')
        ORDER BY day
    ''', (start_date,)).fetchall()
    
    # Fill in missing dates with 0
    daily_sales = []
    # Create date map
    sales_map = {row['day']: row for row in days_data}
    
    for i in range(days):
        d = (date.today() - timedelta(days=lookback - i)).strftime('%Y-%m-%d')
        if d in sales_map:
            daily_sales.append(sales_map[d])
        else:
             daily_sales.append({'day': d, 'orders': 0, 'revenue': 0})
    
    # Top sellers
    top_sellers = conn.execute('''
        SELECT sku, name, SUM(quantity) as sold, SUM(line_total) as revenue
        FROM pos_order_items oi
        JOIN pos_orders o ON oi.order_id = o.id
        WHERE date(o.created_at, 'localtime') >= ? AND o.status != 'held'
        GROUP BY sku
        ORDER BY sold DESC
        LIMIT 10
    ''', (start_date,)).fetchall()
    
    # Payment method breakdown
    payment_breakdown = conn.execute('''
        SELECT payment_method, COUNT(*) as count, SUM(total) as amount
        FROM pos_orders
        WHERE date(created_at, 'localtime') >= ? AND status != 'held'
        GROUP BY payment_method
    ''', (start_date,)).fetchall()
    
    return render_template('pos/management.html',
                           stats=stats,
                           refund_stats=refund_stats,
                           daily_sales=daily_sales,
                           top_sellers=top_sellers,
                           payment_breakdown=payment_breakdown,
                           days=days,
                           tax_rate=get_tax_rate() * 100)


@pos_bp.route('/management/sales')
@login_required
@require_admin
def sales_history():
    """Detailed sales history."""
    conn = get_request_db()
    
    page = int(request.args.get('page', 1))
    per_page = 50
    date_from = request.args.get('date_from', '')
    date_to = request.args.get('date_to', '')
    status = request.args.get('status', '')
    item_query = request.args.get('item_query', '').strip()
    
    sql = '''
        SELECT DISTINCT o.*, u.username as operator_name
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
        sql += ' AND o.status = ?'
        params.append(status)
    
    # Count total (distinct orders)
    # For count, we can wrap the query or count distinct IDs
    # Simplest way given complex joins:
    count_offset = sql.find('FROM')
    if item_query:
        count_sql = "SELECT COUNT(DISTINCT o.id) as cnt " + sql[count_offset:]
    else:
        count_sql = "SELECT COUNT(*) as cnt " + sql[count_offset:]
        
    total = conn.execute(count_sql, params).fetchone()['cnt']
    total_pages = (total + per_page - 1) // per_page
    
    sql += ' ORDER BY o.created_at DESC LIMIT ? OFFSET ?'
    params.extend([per_page, (page - 1) * per_page])
    
    orders = conn.execute(sql, params).fetchall()
    
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
@login_required
@require_admin
def top_sellers():
    """Top selling items analysis."""
    conn = get_request_db()
    
    days = int(request.args.get('days', 30))
    start_date = (date.today() - timedelta(days=max(0, days - 1))).strftime('%Y-%m-%d')
    
    items = conn.execute('''
        SELECT 
            oi.sku, oi.name,
            SUM(oi.quantity) as sold,
            SUM(oi.line_total) as revenue,
            COUNT(DISTINCT o.id) as order_count,
            AVG(oi.unit_price) as avg_price
        FROM pos_order_items oi
        JOIN pos_orders o ON oi.order_id = o.id
        WHERE date(o.created_at, 'localtime') >= ? AND o.status != 'held'
        GROUP BY oi.sku
        ORDER BY sold DESC
        LIMIT 50
    ''', (start_date,)).fetchall()
    
    return render_template('pos/top_sellers.html', items=items, days=days)


@pos_bp.route('/management/margins')
@login_required
@require_admin
def margins():
    """Margin analysis for items sold."""
    conn = get_request_db()
    
    # Get config for preferred margin
    from app.services.data_manager import load_config
    config = load_config() or {}
    preferred_margin = float(config.get('PREFERRED_MARGIN_PERCENT', 30))
    
    days = int(request.args.get('days', 30))
    start_date = (date.today() - timedelta(days=max(0, days - 1))).strftime('%Y-%m-%d')
    sort = request.args.get('sort', 'high')  # high or low
    filter_type = request.args.get('filter', 'all')
    
    having_clause = ""
    params = [start_date]
    
    if filter_type == 'under_margin':
        having_clause = "HAVING margin_percent < ?"
        params.append(preferred_margin)
    
    items = conn.execute(f'''
        SELECT 
            oi.sku, oi.name,
            SUM(oi.quantity) as sold,
            SUM(oi.line_total) as revenue,
            AVG(oi.unit_price) as avg_sell_price,
            AVG(inv.buy_price) as avg_buy_price,
            AVG(oi.unit_price) - AVG(COALESCE(inv.buy_price, 0)) as margin,
            CASE WHEN AVG(oi.unit_price) > 0 
                 THEN ((AVG(oi.unit_price) - AVG(COALESCE(inv.buy_price, 0))) / AVG(oi.unit_price)) * 100 
                 ELSE 0 END as margin_percent
        FROM pos_order_items oi
        JOIN pos_orders o ON oi.order_id = o.id
        LEFT JOIN inventory_items inv ON oi.inventory_item_id = inv.id
        WHERE date(o.created_at, 'localtime') >= ? AND o.status != 'held'
        GROUP BY oi.sku
        {having_clause}
        ORDER BY margin_percent {'DESC' if sort == 'high' else 'ASC'}
        LIMIT 50
    ''', params).fetchall()
    
    return render_template('pos/margins.html', 
                           items=items, 
                           days=days, 
                           sort=sort, 
                           active_filter=filter_type,
                           preferred_margin=preferred_margin)


@pos_bp.route('/management/hourly')
@login_required
@require_admin
def hourly_analysis():
    """Sales by hour and day of week."""
    conn = get_request_db()
    
    days = int(request.args.get('days', 30))
    start_date = (date.today() - timedelta(days=max(0, days - 1))).strftime('%Y-%m-%d')
    
    # Hourly breakdown
    hourly = conn.execute('''
        SELECT 
            strftime('%H', created_at, 'localtime') as hour,
            COUNT(*) as orders,
            SUM(total) as revenue
        FROM pos_orders
        WHERE date(created_at, 'localtime') >= ? AND status != 'held'
        GROUP BY hour
        ORDER BY hour
    ''', (start_date,)).fetchall()
    
    # Day of week breakdown
    daily = conn.execute('''
        SELECT 
            strftime('%w', created_at, 'localtime') as dow,
            COUNT(*) as orders,
            SUM(total) as revenue
        FROM pos_orders
        WHERE date(created_at, 'localtime') >= ? AND status != 'held'
        GROUP BY dow
        ORDER BY dow
    ''', (start_date,)).fetchall()
    
    dow_names = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
    
    return render_template('pos/hourly.html', 
                           hourly=hourly, 
                           daily=daily, 
                           dow_names=dow_names,
                           days=days)


@pos_bp.route('/management/operators')
@login_required
@require_admin
def operator_performance():
    """Operator sales performance."""
    conn = get_request_db()
    
    days = int(request.args.get('days', 30))
    start_date = (date.today() - timedelta(days=max(0, days - 1))).strftime('%Y-%m-%d')
    
    operators = conn.execute('''
        SELECT 
            u.username,
            COUNT(o.id) as orders,
            SUM(o.total) as revenue,
            AVG(o.total) as avg_order,
            SUM(o.discount_amount) as discounts_given
        FROM pos_orders o
        JOIN users u ON o.operator_id = u.id
        WHERE date(o.created_at, 'localtime') >= ? AND o.status != 'held'
        GROUP BY o.operator_id
        ORDER BY revenue DESC
    ''', (start_date,)).fetchall()
    
    return render_template('pos/operators.html', operators=operators, days=days)


@pos_bp.route('/management/refunds-report')
@login_required
@require_admin
def refunds_report():
    """Refund rate and analysis."""
    conn = get_request_db()
    
    days = int(request.args.get('days', 30))
    start_date = (date.today() - timedelta(days=max(0, days - 1))).strftime('%Y-%m-%d')
    
    # Overall refund stats
    stats = conn.execute('''
        SELECT 
            COUNT(*) as refund_count,
            SUM(amount) as total_refunded,
            AVG(amount) as avg_refund
        FROM pos_refunds
        WHERE date(created_at) >= ?
    ''', (start_date,)).fetchone()
    
    # Refund reasons breakdown
    reasons = conn.execute('''
        SELECT reason, COUNT(*) as count, SUM(amount) as total
        FROM pos_refunds
        WHERE date(created_at, 'localtime') >= ?
        GROUP BY reason
        ORDER BY count DESC
    ''', (start_date,)).fetchall()
    
    # Recent refunds
    recent = conn.execute('''
        SELECT r.*, o.order_number, u.username as manager_name
        FROM pos_refunds r
        JOIN pos_orders o ON r.order_id = o.id
        LEFT JOIN users u ON r.manager_id = u.id
        WHERE date(r.created_at, 'localtime') >= ?
        ORDER BY r.created_at DESC
        LIMIT 25
    ''', (start_date,)).fetchall()
    
    return render_template('pos/refunds_report.html',
                           stats=stats,
                           reasons=reasons,
                           recent=recent,
                           days=days)


@pos_bp.route('/management/export')
@login_required
@require_admin
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
        orders = conn.execute('''
            SELECT o.order_number, o.created_at, o.status, o.subtotal, o.tax_amount,
                   o.discount_amount, o.total, o.payment_method, u.username
            FROM pos_orders o
            LEFT JOIN users u ON o.operator_id = u.id
            WHERE date(o.created_at) BETWEEN ? AND ?
            ORDER BY o.created_at
        ''', (date_from, date_to)).fetchall()
        for o in orders:
            writer.writerow(list(o))
    
    elif report_type == 'items':
        writer.writerow(['Order Number', 'Date', 'SKU', 'Name', 'Quantity', 'Unit Price', 'Discount', 'Line Total'])
        items = conn.execute('''
            SELECT o.order_number, o.created_at, oi.sku, oi.name, oi.quantity,
                   oi.unit_price, oi.discount_amount, oi.line_total
            FROM pos_order_items oi
            JOIN pos_orders o ON oi.order_id = o.id
            WHERE date(o.created_at) BETWEEN ? AND ?
            ORDER BY o.created_at
        ''', (date_from, date_to)).fetchall()
        for i in items:
            writer.writerow(list(i))
    
    elif report_type == 'refunds':
        writer.writerow(['Order Number', 'Date', 'Type', 'Amount', 'Reason', 'Manager', 'Items Restocked', 'Items Damaged'])
        refunds = conn.execute('''
            SELECT o.order_number, r.created_at, r.refund_type, r.amount, r.reason,
                   u.username, r.items_restocked, r.items_damaged
            FROM pos_refunds r
            JOIN pos_orders o ON r.order_id = o.id
            LEFT JOIN users u ON r.manager_id = u.id
            WHERE date(r.created_at) BETWEEN ? AND ?
            ORDER BY r.created_at
        ''', (date_from, date_to)).fetchall()
        for r in refunds:
            writer.writerow(list(r))
    
    output.seek(0)
    response = make_response(output.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = f'attachment; filename=pos_{report_type}_{date_from}_to_{date_to}.csv'
    
    return response


@pos_bp.route('/management/settings', methods=['GET', 'POST'])
@login_required
@require_admin
def settings():
    """POS settings page."""
    if request.method == 'POST':
        # Tax rate
        tax_rate = request.form.get('tax_rate', '0')
        try:
            tax_float = round(float(tax_rate) / 100, 6)  # Round to avoid floating point issues
            set_pos_setting('TAX_RATE', str(tax_float))
        except ValueError:
            flash('Invalid tax rate.')
        
        # Feature toggles
        set_pos_setting('REQUIRE_MANAGER_VOID', 'true' if request.form.get('require_manager_void') else 'false')
        set_pos_setting('ALLOW_HOLD_ORDERS', 'true' if request.form.get('allow_hold_orders') else 'false')
        
        # Cash discount
        set_pos_setting('CASH_DISCOUNT_ENABLED', 'true' if request.form.get('cash_discount_enabled') else 'false')
        set_pos_setting('CASH_DISCOUNT_AMOUNT', request.form.get('cash_discount_amount', '0'))
        set_pos_setting('CASH_DISCOUNT_TYPE', request.form.get('cash_discount_type', 'percent'))
        
        # Receipt branding
        set_pos_setting('RECEIPT_STORE_NAME', request.form.get('receipt_store_name', ''))
        set_pos_setting('RECEIPT_HEADER', request.form.get('receipt_header', ''))
        set_pos_setting('RECEIPT_FOOTER', request.form.get('receipt_footer', ''))
        
        # Receipt styling options
        set_pos_setting('RECEIPT_STORE_NAME_BOLD', 'true' if request.form.get('receipt_store_name_bold') else 'false')
        set_pos_setting('RECEIPT_STORE_NAME_ITALIC', 'true' if request.form.get('receipt_store_name_italic') else 'false')
        set_pos_setting('RECEIPT_HEADER_BOLD', 'true' if request.form.get('receipt_header_bold') else 'false')
        set_pos_setting('RECEIPT_HEADER_ITALIC', 'true' if request.form.get('receipt_header_italic') else 'false')
        set_pos_setting('RECEIPT_FOOTER_BOLD', 'true' if request.form.get('receipt_footer_bold') else 'false')
        set_pos_setting('RECEIPT_FOOTER_ITALIC', 'true' if request.form.get('receipt_footer_italic') else 'false')
        
        flash('POS settings saved.')
        return redirect(url_for('pos.settings'))
    
    return render_template('pos/settings.html',
                           tax_rate=round(get_tax_rate() * 100, 4),  # Round for display
                           require_manager_void=get_pos_setting('REQUIRE_MANAGER_VOID', 'false') == 'true',
                           allow_hold_orders=get_pos_setting('ALLOW_HOLD_ORDERS', 'true') == 'true',
                           # Cash discount
                           cash_discount_enabled=get_pos_setting('CASH_DISCOUNT_ENABLED', 'false') == 'true',
                           cash_discount_amount=get_pos_setting('CASH_DISCOUNT_AMOUNT', '0'),
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
                           receipt_footer_italic=get_pos_setting('RECEIPT_FOOTER_ITALIC', 'false') == 'true')


@pos_bp.route('/management/reports/daily')
@login_required
@require_admin
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
    
    for p in payments:
        method = p['payment_method']
        amount = p['total_amount'] or 0
        tax = p['tax_amount'] or 0
        net = amount - tax
        
        refund_amount = refund_map.get(method, 0)
        
        # Adjust for refunds
        # Note: We subtract refunds from the Gross Amount for the "Total Card/Cash" display
        # and also from the Net amount.
        
        if method == 'cash':
            total_cash += (amount - refund_amount)
            total_cash_net += (net - refund_amount)
        else:
            total_card += (amount - refund_amount)
            total_card_net += (net - refund_amount)

    # 4. Hourly Sales (Chart data) - Fix Timezone
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

    # Process hourly data to fill gaps and ensure minimum span (10h)
    hourly_data = [dict(h) for h in hourly]
    if hourly_data:
        active_hours = [int(h['hour']) for h in hourly_data]
        min_h, max_h = min(active_hours), max(active_hours)
        
        # Ensure at least 10 hours span if possible
        target_span = 10
        current_span = max_h - min_h + 1
        missing = target_span - current_span
        
        if missing > 0:
            pad_before = missing // 2
            pad_after = missing - pad_before
            min_h = max(0, min_h - pad_before)
            max_h = min(23, max_h + pad_after)
            
            # Adjust if we hit boundaries
            real_span = max_h - min_h + 1
            if real_span < target_span:
                if min_h == 0:
                    max_h = min(23, min_h + target_span - 1)
                elif max_h == 23:
                    min_h = max(0, max_h - target_span + 1)
        
        # Fill gaps
        filled_hourly = []
        hour_map = {int(h['hour']): h for h in hourly_data}
        for h in range(min_h, max_h + 1):
            if h in hour_map:
                filled_hourly.append(hour_map[h])
            else:
                filled_hourly.append({'hour': f"{h:02d}", 'count': 0, 'amount': 0.0})
        hourly_data = filled_hourly

    # Calculate max hourly revenue for graph scaling
    max_hourly_revenue = max((h['amount'] for h in hourly_data), default=0) if hourly_data else 0



    return render_template('pos/daily_report.html',
                           report_date=report_date,
                           summary=summary,
                           refunds=refunds_summary,
                           net_revenue=net_revenue,
                           payments=payments,
                           hourly=hourly_data,
                           max_hourly_revenue=max_hourly_revenue,
                           top_items=top_items,
                           total_cash=total_cash,
                           total_cash_net=total_cash_net,
                           total_card=total_card,
                           total_card_net=total_card_net)


@pos_bp.route('/management/reports/daily/print')
@login_required
@require_admin
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
    
    # Convert local date to UTC range for correct database queries
    from app.utils.helpers import local_date_to_utc_range
    start_dt, end_dt = local_date_to_utc_range(report_date_str)

    # 1. Summary Stats
    summary = conn.execute('''
        SELECT 
            COUNT(*) as total_orders,
            COALESCE(SUM(total), 0) as gross_sales,
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

    # 3. Payment Breakdown (Simplified for receipt)
    payments = conn.execute('''
        SELECT 
            payment_method, 
            SUM(total) as total_amount
        FROM pos_orders
        WHERE created_at BETWEEN ? AND ? AND status != 'held'
        GROUP BY payment_method
    ''', (start_dt, end_dt)).fetchall()
    
    # Calculate simple cash/card totals
    total_cash = 0
    total_card = 0
    
    for p in payments:
        if p['payment_method'] == 'cash':
            total_cash += p['total_amount']
        else:
            total_card += p['total_amount']

    return render_template('pos/daily_report_print.html',
                           report_date=report_date,
                           summary=summary,
                           refunds=refunds_summary,
                           net_revenue=net_revenue,
                           total_cash=total_cash,
                           total_card=total_card)


@pos_bp.route('/management/reports/valuation')
@login_required
@require_admin
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
    items = conn.execute('SELECT sku, quantity, buy_price, sell_price FROM inventory_items').fetchall()
    
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
                'cost_value': 0,
                'retail_value': 0
            }
            
        qty = item['quantity'] or 0
        cost = (item['buy_price'] or 0) * qty
        retail = (item['sell_price'] or 0) * qty
        
        category_data[cat_code]['count'] += 1
        category_data[cat_code]['qty'] += qty
        category_data[cat_code]['cost_value'] += cost
        category_data[cat_code]['retail_value'] += retail
        
        total_cost += cost
        total_retail += retail
        total_items += 1
        
    # Calculate totals
    potential_profit = total_retail - total_cost
    margin_percent = (potential_profit / total_retail * 100) if total_retail > 0 else 0
    
    sorted_cats = sorted(category_data.values(), key=lambda x: x['code'])
    
    return render_template('pos/valuation_report.html',
                           categories=sorted_cats,
                           total_cost=total_cost,
                           total_retail=total_retail,
                           potential_profit=potential_profit,
                           margin_percent=margin_percent,
                           total_items=total_items,
                           today=date.today())


@pos_bp.route('/management/reports/reorder')
@login_required
@require_admin
def reorder_report():
    """Smart Reorder Report (POS Module)."""
    conn = get_request_db()
    
    # 1. Calculate Sales Velocity (Avg Daily Sales in last 30 days)
    days_lookback = 30
    start_date = (date.today() - timedelta(days=days_lookback)).strftime('%Y-%m-%d')
    
    velocity_query = '''
        SELECT 
            oi.sku, 
            SUM(oi.quantity) as total_sold
        FROM pos_order_items oi
        JOIN pos_orders o ON oi.order_id = o.id
        WHERE o.created_at >= ? AND o.status != 'held'
        GROUP BY oi.sku
    '''
    sales_data = conn.execute(velocity_query, (start_date,)).fetchall()
    velocity_map = {row['sku']: (row['total_sold'] / days_lookback) for row in sales_data}
    
    # 2. Get Current Inventory
    inventory_items = conn.execute('''
        SELECT id, sku, name, quantity, supplier, buy_price 
        FROM inventory_items 
        WHERE quantity > 0 OR sku IN (SELECT sku FROM pos_order_items)
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
                'cost_est': suggested_order * (item['buy_price'] or 0)
            })
            
    recommendations.sort(key=lambda x: x['days_cover'])
    
    return render_template('pos/reorder_report.html',
                           recommendations=recommendations,
                           today=date.today())


@pos_bp.route('/management/reports/daily/email', methods=['POST'])
@login_required
@require_admin
def send_eod_email():
    """Send Detailed End of Day Report via Email."""
    # 1. Get Settings
    host = get_pos_setting('POS_EMAIL_HOST')
    port = get_pos_setting('POS_EMAIL_PORT', '587')
    user = get_pos_setting('POS_EMAIL_USER')
    from app.services.security import decrypt
    password = decrypt(get_pos_setting('POS_EMAIL_PASSWORD'))
    recipients = get_pos_setting('POS_EMAIL_RECIPIENTS') # Comma separated
    
    if not all([host, user, password, recipients]):
        flash("Email settings incomplete. Please configure in Admin Panel.")
        return redirect(url_for('pos.daily_report'))
        
    # 2. Get Date
    report_date_str = request.form.get('date') or request.args.get('date') or date.today().strftime('%Y-%m-%d')
    try:
        report_date = datetime.strptime(report_date_str, '%Y-%m-%d').date()
    except ValueError:
        report_date = date.today()
        report_date_str = report_date.strftime('%Y-%m-%d')

    # Convert to UTC range
    from app.utils.helpers import local_date_to_utc_range
    start_dt, end_dt = local_date_to_utc_range(report_date_str)
        
    # 3. Fetch Data (Detailed)
    conn = get_request_db()
    
    # A. Summary
    summary = conn.execute('''
        SELECT 
            COUNT(*) as total_orders,
            COALESCE(SUM(total), 0) as gross_sales,
            COALESCE(SUM(tax_amount), 0) as total_tax
        FROM pos_orders
        WHERE created_at BETWEEN ? AND ? AND status != 'held'
    ''', (start_dt, end_dt)).fetchone()
    
    # B. Refunds
    refunds = conn.execute('''
        SELECT COUNT(*) as count, COALESCE(SUM(amount), 0) as total
        FROM pos_refunds WHERE created_at BETWEEN ? AND ?
    ''', (start_dt, end_dt)).fetchone()
    
    net_revenue = (summary['gross_sales'] or 0) - (refunds['total'] or 0)
    
    # C. Payments Breakdown
    payments = conn.execute('''
        SELECT 
            payment_method, 
            COUNT(*) as count,
            SUM(total) as total_amount
        FROM pos_orders
        WHERE created_at BETWEEN ? AND ? AND status != 'held'
        GROUP BY payment_method
    ''', (start_dt, end_dt)).fetchall()

    # Calculate Cash/Card totals for summary
    total_cash = sum(p['total_amount'] for p in payments if p['payment_method'] == 'cash')
    total_card = sum(p['total_amount'] for p in payments if p['payment_method'] != 'cash')

    # D. Top Items (Limit 5 for email)
    top_items = conn.execute('''
        SELECT oi.name, SUM(oi.quantity) as qty, SUM(oi.line_total) as total
        FROM pos_order_items oi
        JOIN pos_orders o ON oi.order_id = o.id
        WHERE o.created_at BETWEEN ? AND ? AND o.status != 'held'
        GROUP BY oi.sku
        ORDER BY total DESC
        LIMIT 5
    ''', (start_dt, end_dt)).fetchall()

    # 4. Build Email Body (HTML)
    # Styles
    th_style = "padding: 8px; border-bottom: 2px solid #ddd; text-align: left;"
    td_style = "padding: 8px; border-bottom: 1px solid #eee;"
    td_num = "padding: 8px; border-bottom: 1px solid #eee; text-align: right;"
    
    # Tables Construction
    payment_rows = ""
    for p in payments:
        payment_rows += f"<tr><td style='{td_style}'>{p['payment_method'].upper()}</td><td style='{td_num}'>${p['total_amount']:,.2f}</td></tr>"
        
    item_rows = ""
    for i in top_items:
        item_rows += f"<tr><td style='{td_style}'>{i['name']}</td><td style='{td_num}'>{i['qty']}</td><td style='{td_num}'>${i['total']:,.2f}</td></tr>"

    html_content = f"""
    <html>
    <body style="font-family: 'Segoe UI', sans-serif; color: #333; line-height: 1.5;">
        <div style="max-width: 600px; margin: 0 auto; border: 1px solid #eee; padding: 20px; border-radius: 8px;">
            <h2 style="color: #2c3e50; margin-bottom: 5px;">📊 End of Day Report</h2>
            <p style="color: #7f8c8d; margin-top: 0;">Date: <strong>{report_date.strftime('%B %d, %Y')}</strong></p>
            
            <hr style="border: 0; border-top: 1px solid #eee; margin: 20px 0;">

            <!-- Financial Summary -->
            <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
                <tr>
                    <td style="padding: 10px;"><strong>Gross Sales</strong></td>
                    <td style="padding: 10px; text-align: right; font-size: 1.2em;">${summary['gross_sales']:,.2f}</td>
                </tr>
                <tr>
                    <td style="padding: 10px; color: #7f8c8d;">Tax Collected</td>
                    <td style="padding: 10px; text-align: right; color: #7f8c8d;">${summary['total_tax']:,.2f}</td>
                </tr>
                <tr>
                    <td style="padding: 10px; color: #dc3545;">Refunds ({refunds['count']})</td>
                    <td style="padding: 10px; text-align: right; color: #dc3545;">-${refunds['total']:,.2f}</td>
                </tr>
                <tr style="background-color: #f8f9fa;">
                    <td style="padding: 15px; border-top: 2px solid #2c3e50;"><strong>NET REVENUE</strong></td>
                    <td style="padding: 15px; border-top: 2px solid #2c3e50; text-align: right; font-size: 1.4em; color: #198754;">
                        <strong>${net_revenue:,.2f}</strong>
                    </td>
                </tr>
            </table>

            <div style="display: flex; gap: 10px; margin-bottom: 30px;">
                <div style="flex: 1; background: #f8f9fa; padding: 10px; border-radius: 4px; text-align: center;">
                    <small style="color: #7f8c8d;">CASH</small><br>
                    <strong>${total_cash:,.2f}</strong>
                </div>
                <div style="flex: 1; background: #f8f9fa; padding: 10px; border-radius: 4px; text-align: center;">
                    <small style="color: #7f8c8d;">CARD</small><br>
                    <strong>${total_card:,.2f}</strong>
                </div>
                <div style="flex: 1; background: #f8f9fa; padding: 10px; border-radius: 4px; text-align: center;">
                    <small style="color: #7f8c8d;">ORDERS</small><br>
                    <strong>{summary['total_orders']}</strong>
                </div>
            </div>

            <!-- Payment Methods -->
            <h4 style="border-bottom: 2px solid #eee; padding-bottom: 10px;">💳 Payment Breakdown</h4>
            <table style="width: 100%; border-collapse: collapse; margin-bottom: 30px;">
                {payment_rows}
            </table>

            <!-- Top Items -->
            <h4 style="border-bottom: 2px solid #eee; padding-bottom: 10px;">🏆 Top Selling Items (Top 5)</h4>
            <table style="width: 100%; border-collapse: collapse; margin-bottom: 30px;">
                <tr>
                    <th style="{th_style}">Item</th>
                    <th style="{th_style} text-align: right;">Qty</th>
                    <th style="{th_style} text-align: right;">Total</th>
                </tr>
                {item_rows}
            </table>

            <p style="font-size: 0.8em; color: #999; text-align: center; margin-top: 30px;">
                Generated by RSCP Point of Sale • {datetime.now().strftime('%Y-%m-%d %H:%M')}
            </p>
        </div>
    </body>
    </html>
    """
    
    # 5. Send Email
    try:
        msg = MIMEMultipart()
        msg['Subject'] = f"RSCP EOD Report - {report_date.strftime('%Y-%m-%d')}"
        msg['From'] = user
        msg['To'] = recipients
        msg.attach(MIMEText(html_content, 'html'))
        
        context = ssl.create_default_context()
        with smtplib.SMTP(host, int(port)) as server:
            server.starttls(context=context)
            server.login(user, password)
            server.send_message(msg)
            
        flash(f"Detailed EOD Report sent to {len(recipients.split(','))} recipients.")
        
    except Exception as e:
        logger.error(f"Failed to send email: {e}")
        flash(f"Error sending email: {str(e)}")
        
    return redirect(url_for('pos.daily_report', date=report_date_str))


@pos_bp.route('/management/reports/daily-items')
@login_required
@require_admin
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
    elif sort == 'revenue_desc': order_clause = "total DESC"
    elif sort == 'revenue_asc': order_clause = "total ASC"
    elif sort == 'name_asc': order_clause = "oi.name ASC"
    elif sort == 'name_desc': order_clause = "oi.name DESC"
    elif sort == 'sku_asc': order_clause = "oi.sku ASC"
    elif sort == 'sku_desc': order_clause = "oi.sku DESC"
    elif sort == 'price_asc': order_clause = "avg_price ASC"
    elif sort == 'price_desc': order_clause = "avg_price DESC"

    # 3. Query
    # Prioritize Inventory SKU (RSCP) over Order SKU (which might be UPC/Part)
    query = f'''
        SELECT 
            COALESCE(inv.sku, oi.sku) as sku,
            oi.name, 
            SUM(oi.quantity) as qty, 
            SUM(oi.line_total) as total,
            AVG(oi.unit_price) as avg_price,
            MAX(inv.quantity) as current_stock,
            MAX(inv.location_aisle) as aisle
        FROM pos_order_items oi
        JOIN pos_orders o ON oi.order_id = o.id
        LEFT JOIN inventory_items inv ON oi.inventory_item_id = inv.id
        WHERE o.created_at BETWEEN ? AND ? 
          AND o.status != 'held'
        GROUP BY COALESCE(inv.sku, oi.sku)
        ORDER BY {order_clause}
    '''
    
    rows = conn.execute(query, (start_dt, end_dt)).fetchall()
    
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
            'total': r['total'],
            'avg_price': r['avg_price'],
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


