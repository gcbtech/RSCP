"""
POS Reports Service
Handles generation of report data for both web view and automated emails.
"""
import json
from datetime import datetime, date
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

