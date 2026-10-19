"""
POS Email Service
Builds and sends the End of Day report email. The manual "Send Email Report"
button and the automatic daily email both come through here, and both use the
same figures as the daily report page and its print view
(pos_reports.generate_daily_report_data).

send_email() is the one SMTP sender (POS_EMAIL_* settings); the monitor's
alert emails (app/services/monitor.py) use it too.
"""
import smtplib
import ssl
import logging
from html import escape
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

from app.routes.pos.core import get_pos_setting
from app.services import money
from app.services.pos_reports import generate_daily_report_data
from app.services.security import decrypt

logger = logging.getLogger(__name__)

# A stalled SMTP server must not hang the request or the EOD job forever.
SMTP_TIMEOUT_S = 60


def _money(cents):
    """Cents -> "$1,234.56" (negatives "$-5.00", as the email always showed)."""
    return '$' + money.dollars_grouped(cents or 0)


def render_eod_email_html(report_date, data):
    """HTML body for the EOD email from the daily report data."""
    r = data['report']
    th = "padding: 8px; border-bottom: 2px solid #ddd; text-align: left;"
    td = "padding: 6px 8px; border-bottom: 1px solid #eee;"
    num = td + " text-align: right;"

    def row(label, value, style=''):
        return f"<tr><td style='{td}{style}'>{label}</td><td style='{num}{style}'>{value}</td></tr>"

    d = r['discounts']
    c = r['corrections']
    sales_rows = ''.join([
        row('Orders', r['order_count']),
        row('Gross Sales <small>(before discounts, ex. tax)</small>', _money(r['gross_sales_cents'])),
        row('&nbsp;&nbsp;Line discounts', '-' + _money(d['line_cents'])),
        row('&nbsp;&nbsp;Order discounts', '-' + _money(d['order_cents'])),
        row('&nbsp;&nbsp;Coupons', '-' + _money(d['coupon_cents'])),
        row('&nbsp;&nbsp;Cash discounts', '-' + _money(d['cash_cents'])),
        row('<strong>Net Sales</strong> <small>(ex. tax)</small>', f"<strong>{_money(r['net_sales_cents'])}</strong>"),
        row('Tax Collected', _money(r['tax_collected_cents'])),
        row('Sales Total <small>(incl. tax)</small>', _money(r['sales_total_cents'])),
        *([row(f"Sale corrections ({c['count']})", _money(c['total_cents']), ' color: #b45309;'),
           row('&nbsp;&nbsp;of which tax', _money(c['tax_cents']), ' color: #b45309;')] if c['count'] else []),
        row(f"Refunds ({r['refunds']['count']})", '-' + _money(r['refunds']['total_cents']), ' color: #dc3545;'),
        row('&nbsp;&nbsp;of which tax', '-' + _money(r['refunds']['tax_cents']), ' color: #dc3545;'),
        row('Tax (net of refunds and corrections)' if c['count'] else 'Tax (net of refunds)',
            _money(r['tax_net_cents'])),
        row('<strong>NET REVENUE</strong> <small>(incl. tax, after refunds'
            + (' and corrections' if c['count'] else '') + ')</small>',
            f"<strong>{_money(r['net_revenue_cents'])}</strong>", ' background: #f8f9fa;'),
    ])

    def corrected_cell(t):
        return f"<td style='{num}'>{_money(t['corrected_cents'])}</td>" if c['count'] else ''

    tender_rows = ''.join(
        f"<tr><td style='{td}'>{escape(t['label'])}</td><td style='{num}'>{_money(t['collected_cents'])}</td>"
        f"{corrected_cell(t)}"
        f"<td style='{num}'>-{_money(t['refunded_cents'])}</td><td style='{num}'><strong>{_money(t['net_cents'])}</strong></td></tr>"
        for t in r['tenders'])
    corrected_th = f'<th style="{th} text-align: right;">Corrections</th>' if c['count'] else ''

    ec = r['expected_cash']
    cash_rows = ''.join([
        row('Opening float(s)', _money(ec['opening_float_cents'])),
        row('+ Cash sales', _money(ec['cash_sales_cents'])),
        row('- Cash refunds', _money(ec['cash_refunds_cents'])),
        *([row('+/- Cash sale corrections', _money(ec['cash_corrections_cents']))]
          if ec['cash_corrections_cents'] else []),
        row('<strong>Expected cash</strong>', f"<strong>{_money(ec['total_cents'])}</strong>"),
    ])
    drawer_rows = ''
    for s in r['drawer_sessions']:
        if s['is_open']:
            status = f"open, expected {_money(s['expected_cash_cents'])}"
        else:
            status = (f"counted {_money(s['counted_cash_cents'])} vs expected {_money(s['expected_cash_cents'])}: "
                      f"<strong>{'over' if (s['over_short_cents'] or 0) >= 0 else 'short'} "
                      f"{_money(abs(s['over_short_cents'] or 0))}</strong>")
        drawer_rows += row(f"Drawer #{s['id']} (float {_money(s['opening_float_cents'])})", status)

    item_rows = ''.join(
        f"<tr><td style='{td}'>{escape(str(i['name']))}</td><td style='{num}'>{i['qty']}</td>"
        f"<td style='{num}'>{_money(i['total_cents'])}</td></tr>"
        for i in data['top_items'][:5])

    return f"""
    <html>
    <body style="font-family: 'Segoe UI', sans-serif; color: #333; line-height: 1.5;">
        <div style="max-width: 600px; margin: 0 auto; border: 1px solid #eee; padding: 20px; border-radius: 8px;">
            <h2 style="color: #2c3e50; margin-bottom: 5px;">End of Day Report</h2>
            <p style="color: #7f8c8d; margin-top: 0;">Date: <strong>{report_date.strftime('%B %d, %Y')}</strong></p>

            <h4 style="border-bottom: 2px solid #eee; padding-bottom: 6px;">Sales</h4>
            <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">{sales_rows}</table>

            <h4 style="border-bottom: 2px solid #eee; padding-bottom: 6px;">Tenders</h4>
            <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
                <tr><th style="{th}">Method</th><th style="{th} text-align: right;">Collected</th>{corrected_th}
                    <th style="{th} text-align: right;">Refunded</th><th style="{th} text-align: right;">Net</th></tr>
                {tender_rows}
            </table>

            <h4 style="border-bottom: 2px solid #eee; padding-bottom: 6px;">Cash</h4>
            <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">{cash_rows}{drawer_rows}</table>

            <h4 style="border-bottom: 2px solid #eee; padding-bottom: 6px;">Top Selling Items (Top 5)</h4>
            <table style="width: 100%; border-collapse: collapse; margin-bottom: 20px;">
                <tr><th style="{th}">Item</th><th style="{th} text-align: right;">Net Qty</th>
                    <th style="{th} text-align: right;">Net Sales</th></tr>
                {item_rows}
            </table>

            <p style="font-size: 0.8em; color: #999; text-align: center; margin-top: 30px;">
                Generated by RSCP Point of Sale &bull; {datetime.now().strftime('%Y-%m-%d %H:%M')}
            </p>
        </div>
    </body>
    </html>
    """


def build_eod_email(report_date):
    """(subject, html, data) for a local date."""
    data = generate_daily_report_data(report_date)
    subject = f"RSCP EOD Report - {report_date.strftime('%Y-%m-%d')}"
    return subject, render_eod_email_html(report_date, data), data


def recipient_list(recipients):
    """'a@x, b@y' (commas or semicolons) -> ['a@x', 'b@y']."""
    return [r.strip() for r in (recipients or '').replace(';', ',').split(',') if r.strip()]


def send_email(subject, html, recipients, text=None):
    """Send one email through the POS SMTP settings (STARTTLS, optional login).

    `recipients` is a list or a comma-separated string. With `text`, the mail
    is multipart/alternative with a plain-text part first. Raises on any
    failure (missing host, connection, authentication, refused recipients).
    """
    host = get_pos_setting('POS_EMAIL_HOST')
    if isinstance(recipients, str):
        recipients = recipient_list(recipients)
    if not host or not recipients:
        raise ValueError('Email settings incomplete (SMTP host or recipients missing).')
    port = int(get_pos_setting('POS_EMAIL_PORT') or '587')
    user = get_pos_setting('POS_EMAIL_USER')
    password_enc = get_pos_setting('POS_EMAIL_PASSWORD')
    password = decrypt(password_enc) if password_enc else None

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = user or 'noreply@rscp.local'
    msg["To"] = ', '.join(recipients)
    if text is not None:
        msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_S) as server:
        server.starttls(context=context)
        if user and password:
            server.login(user, password)
        server.sendmail(msg["From"], recipients, msg.as_string())


def deliver_eod_email(report_date):
    """Send the EOD email. Returns (ok, message)."""
    host = get_pos_setting('POS_EMAIL_HOST')
    recipients = get_pos_setting('POS_EMAIL_RECIPIENTS')
    if not host or not recipients:
        logger.warning("POS EOD Email skipped: Missing host or recipients configuration.")
        return False, "Email settings incomplete. Please configure in Admin Panel."

    try:
        subject, html_content, _data = build_eod_email(report_date)
        to = recipient_list(recipients)
        send_email(subject, html_content, to)
        logger.info(f"POS EOD Email sent to {recipients}")
        return True, f"EOD Report sent to {len(to)} recipient(s)."
    except Exception as e:
        logger.error(f"Failed to send POS EOD Email: {e}")
        return False, f"Error sending email: {e}"


def send_eod_email(report_date):
    """Send the End of Day report email (automatic job). Returns True on success."""
    ok, _message = deliver_eod_email(report_date)
    return ok
