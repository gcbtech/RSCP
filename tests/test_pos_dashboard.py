"""The POS management dashboard takes its money figures from the report engine
(pos_reports), so it always agrees with the End of Day, Custom and Top Sellers
reports -- including split payments, coupons, cash discounts and refunds."""
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from flask import template_rendered

from app.services import inventory as inv
from app.services import pos_checkout, pos_refunds
from app.services.db import get_db_connection
from app.services.pos_pricing import PricingConfig
from tests.conftest import login

CASHIER = SimpleNamespace(id=2, username='cashier')
MANAGER = SimpleNamespace(id=3, username='manager')
TAX10 = PricingConfig(tax_rate=0.10)
TAX10_CASH5 = PricingConfig(tax_rate=0.10, cash_discount_enabled=True, cash_discount_amount=5,
                            cash_discount_type='percent')


@pytest.fixture(autouse=True)
def denver(monkeypatch):
    """Pin the store's zone. Reports bucket days in the process's local time
    (Python's astimezone and SQLite's 'localtime'), so set TZ where the OS lets
    us (CI runs in UTC); elsewhere the machine's own zone is used."""
    if not hasattr(time, 'tzset'):
        yield
        return
    monkeypatch.setenv('TZ', 'America/Denver')
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _today():
    return date.today()


def _utc_at_local(day, hour):
    local = datetime.combine(day, datetime.min.time()).replace(hour=hour).astimezone()
    return local.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


@pytest.fixture
def conn(fresh_app):
    c = get_db_connection(fresh_app.config['DATABASE'])
    c.isolation_level = None  # as the checkout/refund routes use it
    yield c
    c.close()


def _item(conn, sku, price, buy):
    return inv.create_item(conn, {'sku': sku, 'name': f'{sku} item', 'quantity': 10,
                                  'sell_price': price, 'buy_price': buy}, user='admin')


def _cart(item_id, sku, price, qty=1, **extra):
    cart = {'items': [{'inventory_item_id': item_id, 'sku': sku, 'name': f'{sku} item', 'quantity': qty,
                       'unit_price': price, 'discount_amount': 0, 'discount_type': None,
                       'line_total': round(price * qty, 2)}],
            'discount_amount': 0, 'discount_type': None, 'discount_reason': ''}
    cart.update(extra)
    return cart


def _sell(conn, cart, method, config, **payment):
    return pos_checkout.complete_sale(conn, pos_checkout.SessionCart(cart),
                                      pos_checkout.Payment(method, **payment), CASHIER, config=config)


def _seed_day(conn):
    """Today, 10% tax, recorded through the real checkout and refund services.

    A  cash : 2 x WIDGET $10                                     -> $22.00
    B  card : 1 x LAPTOP $50                                     -> $55.00
    C  split: 2 x CABLE $20, $24 card + $20 cash                 -> $44.00
    D  cash : 1 x GADGET $30, $5 coupon, 5% cash discount ($1.25) -> $26.25
    Refunds: all of B to card ($55.00, $5.00 tax); one CABLE of C in cash
    ($22.00, $2.00 tax); all of D in cash ($26.25, $2.50 tax).
    """
    widget = _item(conn, 'WIDGET', 10.0, 4.0)
    laptop = _item(conn, 'LAPTOP', 50.0, 30.0)
    cable = _item(conn, 'CABLE', 20.0, 5.0)
    gadget = _item(conn, 'GADGET', 30.0, 12.0)
    coupon_id = conn.execute('''INSERT INTO pos_coupons (name, code, coupon_type, discount_type, discount_value)
                                VALUES ('SAVE5', 'SAVE5', 'standard', 'order_dollar', 5.0)''').lastrowid

    a = _sell(conn, _cart(widget, 'WIDGET', 10.0, qty=2), 'cash', TAX10, tendered='30')
    b = _sell(conn, _cart(laptop, 'LAPTOP', 50.0), 'card', TAX10)
    c = _sell(conn, _cart(cable, 'CABLE', 20.0, qty=2), 'split', TAX10, cash_amount='20', card_amounts=['24'])
    d = _sell(conn, _cart(gadget, 'GADGET', 30.0,
                          applied_coupon={'id': coupon_id, 'code': 'SAVE5', 'name': 'SAVE5', 'discount': 5.0}),
              'cash', TAX10_CASH5, tendered='30')
    assert (a.total, b.total, c.total, d.total, d.cash_discount) == (22.0, 55.0, 44.0, 26.25, 1.25)

    full_b = pos_refunds.process_refund(conn, b.order_id, 'full', 'other', MANAGER)
    cable_line = conn.execute('SELECT id FROM pos_order_items WHERE order_id = ?', (c.order_id,)).fetchone()[0]
    part_c = pos_refunds.process_refund(conn, c.order_id, 'partial', 'other', MANAGER,
                                        items=[(str(cable_line), '1')])
    full_d = pos_refunds.process_refund(conn, d.order_id, 'full', 'other', MANAGER)
    assert (full_b.amount, full_b.tax_refunded, full_b.refund_method) == (55.0, 5.0, 'card')
    assert (part_c.amount, part_c.tax_refunded, part_c.refund_method) == (22.0, 2.0, 'cash')
    assert (full_d.amount, full_d.tax_refunded, full_d.refund_method) == (26.25, 2.5, 'cash')

    # Pin everything to the middle of the local day so the test never straddles midnight
    conn.execute('UPDATE pos_orders SET created_at = ?', (_utc_at_local(_today(), 12),))
    conn.execute('UPDATE pos_refunds SET created_at = ?', (_utc_at_local(_today(), 13),))
    return a, b, c, d


def _render(app, client, path, template, **params):
    captured = []

    def record(sender, template, context, **extra):
        captured.append((template.name, context))
    template_rendered.connect(record, app)
    try:
        resp = client.get(path, query_string=params)
    finally:
        template_rendered.disconnect(record, app)
    assert resp.status_code == 200
    ctx = next(c for name, c in captured if name == template)
    return ctx, resp.get_data(as_text=True)


def _page_data(html):
    import json
    import re
    m = re.search(r'<script type="application/json" id="page-data">(.*?)</script>', html, re.S)
    return json.loads(m.group(1))


def test_dashboard_matches_the_report_engine_for_a_mixed_day(fresh_app, fresh_client, conn):
    from app.services.pos_reports import build_sales_report, generate_top_sellers_data
    _seed_day(conn)
    today = _today()
    login(fresh_client, 3)

    dash, html = _render(fresh_app, fresh_client, '/pos/management', 'pos/management.html', days=1)
    with fresh_app.app_context():
        engine = build_sales_report(today, today)
        top = generate_top_sellers_data(today, today, {})
    daily, _ = _render(fresh_app, fresh_client, '/pos/management/reports/daily', 'pos/daily_report.html',
                       date=today.isoformat())
    custom, _ = _render(fresh_app, fresh_client, '/pos/management/reports/custom', 'pos/custom_report.html',
                        start=today.isoformat(), end=today.isoformat())

    # One source of truth: the dashboard shows exactly what the reports show
    assert dash['report'] == engine == daily['report'] == custom['report']
    assert dash['profit']['profit'] == top['summary']['profit']

    # ... and those numbers are right
    r = dash['report']
    assert r['order_count'] == 4
    assert r['gross_sales'] == 140.0
    assert r['discounts'] == {'line': 0.0, 'order': 0.0, 'coupon': 5.0, 'cash': 1.25, 'total': 6.25}
    assert r['net_sales'] == 133.75
    assert r['tax_collected'] == 13.50
    assert r['sales_total'] == 147.25
    assert r['refunds'] == {'count': 3, 'total': 103.25, 'tax': 9.50, 'net': 93.75,
                            'by_method': {'card': 55.0, 'cash': 48.25}}
    assert r['net_revenue'] == 44.00
    assert r['tax_net'] == 4.00
    # Split sale C counts $20 under cash and $24 under card, not as a "split" lump
    assert [(t['method'], t['count'], t['collected'], t['refunded'], t['net']) for t in r['tenders']] == [
        ('cash', 3, 68.25, 48.25, 20.0),
        ('card', 2, 79.0, 55.0, 24.0),
    ]
    assert dash['avg_order'] == 36.81            # 147.25 / 4
    # Est. profit after discounts and refunds, ex. tax (Top Sellers): WIDGET 20-8, CABLE 20-5;
    # LAPTOP and GADGET were fully refunded
    assert dash['profit']['profit'] == pytest.approx(27.0)
    assert dash['daily_sales'] == [{'day': today.isoformat(), 'orders': 4, 'revenue': 147.25}]

    for figure in ('$147.25', '$44.00', '$27.00', '$36.81', '$103.25', 'Refunds (3)', '$68.25', '$79.00'):
        assert figure in html, figure
    data = _page_data(html)
    assert data['paymentLabels'] == ['cash', 'card'] and data['paymentData'] == [68.25, 79.0]
    assert data['salesData'] == [147.25] and data['salesLabels'] == [today.isoformat()]


def test_dashboard_range_and_daily_series_add_up_to_the_engine(fresh_app, fresh_client, conn):
    from app.services.pos_reports import build_sales_report, daily_sales_series
    _seed_day(conn)
    today = _today()
    widget = conn.execute("SELECT id FROM inventory_items WHERE sku = 'WIDGET'").fetchone()[0]
    earlier = _sell(conn, _cart(widget, 'WIDGET', 10.0), 'card', TAX10)
    conn.execute('UPDATE pos_orders SET created_at = ? WHERE id = ?',
                 (_utc_at_local(today - timedelta(days=3), 23), earlier.order_id))
    held = conn.execute('''INSERT INTO pos_orders (order_number, status, subtotal, tax_rate, tax_amount,
                                                   total, payment_method, operator_id)
                           VALUES ('HELD-1', 'held', 100, 0, 0, 100, 'cash', 2)''').lastrowid
    conn.execute('UPDATE pos_orders SET created_at = ? WHERE id = ?', (_utc_at_local(today, 12), held))
    login(fresh_client, 3)

    start = today - timedelta(days=6)
    for params in ({'days': 7}, {'start_date': start.isoformat(), 'end_date': today.isoformat()},
                   {'start_date': today.isoformat(), 'end_date': start.isoformat()}):   # reversed: swapped
        dash, _ = _render(fresh_app, fresh_client, '/pos/management', 'pos/management.html', **params)
        with fresh_app.app_context():
            engine = build_sales_report(start, today)
        assert dash['report'] == engine
        assert (dash['start_date'], dash['end_date'], dash['days']) == (start.isoformat(), today.isoformat(), 7)
        assert engine['order_count'] == 5 and engine['sales_total'] == 158.25   # + $11.00 three days ago
        series = dash['daily_sales']
        assert [s['day'] for s in series] == [(start + timedelta(days=i)).isoformat() for i in range(7)]
        assert sum(s['orders'] for s in series) == engine['order_count']
        assert round(sum(s['revenue'] for s in series), 2) == engine['sales_total']
        assert series[3] == {'day': (today - timedelta(days=3)).isoformat(), 'orders': 1, 'revenue': 11.0}

    with fresh_app.app_context():
        assert daily_sales_series(today, today) == [{'day': today.isoformat(), 'orders': 4, 'revenue': 147.25}]


@pytest.mark.parametrize('query', [
    'days=abc', 'days=0', 'days=-5', 'days=99999999999',
    'start_date=junk&end_date=2026-01-01', 'start_date=2026-01-01',
    'start_date=0001-01-01&end_date=9999-12-31', 'start_date=1900-01-01&end_date=2026-01-01',
])
def test_dashboard_junk_ranges_do_not_500(fresh_client, query):
    login(fresh_client, 1)
    assert fresh_client.get(f'/pos/management?{query}').status_code == 200
