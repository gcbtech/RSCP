import csv
import io
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.environ.get('RSCP_DATA_DIR') or os.path.dirname(__file__), '..')))

from app.services.db import get_db_connection

TODAY = date.today()
START = TODAY - timedelta(days=2)          # 3-day range: START..TODAY
RANGE = {'start': START.isoformat(), 'end': TODAY.isoformat()}


def utc_at_local_noon(days_ago):
    local = datetime.combine(TODAY - timedelta(days=days_ago), time(12, 0)).astimezone()
    return local.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


@pytest.fixture
def app():
    from app import create_app

    db_path = os.path.join(os.environ.get('RSCP_DATA_DIR') or os.path.dirname(__file__), 'test_top_sellers.db')
    if os.path.exists(db_path):
        os.remove(db_path)

    app = create_app(test_config={
        'TESTING': True,
        'DATABASE': db_path,
        'WTF_CSRF_ENABLED': False,
        'SECRET_KEY': 'test_key'
    })

    with app.app_context():
        from app.services.migration import run_migrations
        run_migrations()
        conn = get_db_connection()
        conn.execute('INSERT INTO users (id, username, password_hash, is_admin, roles) VALUES (1, ?, ?, 1, ?)',
                     ('admin', 'pbkdf2:sha256:somehash', '["super_admin"]'))

        items = [
            # id, sku, name, qty, area, aisle, buy (cents), legacy
            (1, 'RSCP-PRI-0000-0001', 'Laptop A', 5, 'Retail', 'Laptops', 10000, 0),
            (2, 'RSCP-SEC-0000-0002', 'Server B', 0, 'Retail', 'Servers', 20000, 1),
            (3, 'RSCP-ASC-0000-0003', 'Cable C', 50, 'Build', 'None', 100, 0),
        ]
        conn.executemany('''INSERT INTO inventory_items
            (id, sku, name, quantity, location_area, location_aisle, buy_price_cents, is_legacy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)''', items)

        def order(oid, days_ago, lines, discount=0, tax_rate=0.0):
            subtotal = sum(qty * price for *_, qty, price in lines)
            tax = round((subtotal - discount) * tax_rate)
            conn.execute('''INSERT INTO pos_orders (id, order_number, subtotal_cents, tax_rate, tax_amount_cents,
                            discount_amount_cents, total_cents, payment_method, operator_id, created_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 'cash', 1, ?)''',
                         (oid, f'ORD-{oid}', subtotal, tax_rate, tax, discount, subtotal - discount + tax,
                          utc_at_local_noon(days_ago)))
            for line_id, inv_id, sku, name, qty, price in lines:
                conn.execute('''INSERT INTO pos_order_items (id, order_id, inventory_item_id, sku, name, quantity,
                                unit_price_cents, line_total_cents) VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                             (line_id, oid, inv_id, sku, name, qty, price, qty * price))

        # $320 of items, $32 order discount, 8% tax on $288 = $23.04 -> total $311.04
        order(1, 1, [(11, 1, 'RSCP-PRI-0000-0001', 'Laptop A', 2, 15000),
                     (12, 3, 'RSCP-ASC-0000-0003', 'Cable C', 10, 200)], discount=3200, tax_rate=0.08)
        order(2, 1, [(21, 1, 'RSCP-PRI-0000-0001', 'Laptop A', 1, 15000),
                     (22, 2, 'RSCP-SEC-0000-0002', 'Server B', 1, 40000)])
        order(3, 1, [(31, None, 'CUSTOM-1', 'Mystery Box', 1, 5000)])
        # an inventory item deleted before foreign keys were enforced: schema
        # v16 cleared the dangling id, the line keeps its (non-CUSTOM) sku
        order(4, 1, [(41, None, 'RSCP-PRI-0000-0999', 'Old Thing', 1, 3000)])
        order(5, 4, [(51, 1, 'RSCP-PRI-0000-0001', 'Laptop A', 1, 15000)])      # previous period
        order(6, 10, [(61, 3, 'RSCP-ASC-0000-0003', 'Cable C', 99, 200)])       # outside both periods

        conn.execute('''INSERT INTO pos_refunds (id, order_id, refund_type, amount_cents, reason, manager_id)
                        VALUES (1, 2, 'partial', 15000, 'defective', 1)''')
        conn.execute('INSERT INTO pos_refund_items (refund_id, order_item_id, quantity, amount_cents) '
                     'VALUES (1, 21, 1, 15000)')
        conn.commit()
        conn.close()

    yield app

    if os.path.exists(db_path):
        os.remove(db_path)


@pytest.fixture
def client(app):
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = '1'
        sess['user'] = 1
    return client


def _items_section(html):
    return html.split('📦 Items')[1].split('By Location')[0]


def _get(client, **params):
    resp = client.get('/pos/management/top-sellers', query_string={**RANGE, **params})
    assert resp.status_code == 200
    return _items_section(resp.get_data(as_text=True))


def test_metrics(app):
    from app.services.pos_reports import generate_top_sellers_data
    with app.app_context():
        data = generate_top_sellers_data(START, TODAY, {}, category_names={'PRI': 'Primary'})
    by_name = {i['name']: i for i in data['items']}

    assert set(by_name) == {'Laptop A', 'Server B', 'Cable C', 'Old Thing', 'Mystery Box'}
    a = by_name['Laptop A']
    assert (a['qty_sold'], a['refunded_qty'], a['net_qty']) == (3, 1, 2)
    # Order 1's 10% discount is spread over its lines: $300 list -> $270 charged; order 2 adds $150; refund -$150
    assert a['list_revenue_cents'] == 45000 and a['gross_revenue_cents'] == pytest.approx(42000)
    assert a['discounts_cents'] == pytest.approx(3000)
    assert a['net_revenue_cents'] == pytest.approx(27000) and a['profit_cents'] == pytest.approx(7000)
    assert round(a['margin'], 1) == 25.9
    assert a['orders'] == 2 and a['prev_qty'] == 1 and a['qty_change_pct'] == 100
    assert a['category_name'] == 'Primary' and a['location'] == 'Retail › Laptops'
    assert a['days_cover'] == pytest.approx(5 / (2 / 3))

    assert by_name['Cable C']['location'] == 'Build'            # literal 'None' aisle is treated as blank
    assert by_name['Cable C']['net_revenue_cents'] == pytest.approx(1800)
    assert by_name['Old Thing']['kind'] == 'deleted' and by_name['Old Thing']['profit_cents'] is None
    assert by_name['Mystery Box']['kind'] == 'custom'

    s = data['summary']
    assert s['net_units'] == 15 and s['orders'] == 4
    assert s['discounts_cents'] == pytest.approx(3200) and s['tax_cents'] == pytest.approx(2304)
    assert s['total_incl_tax_cents'] == pytest.approx(31104 + 55000 + 5000 + 3000)
    assert s['net_revenue_cents'] == pytest.approx(91800 - 15000)
    assert s['profit_cents'] == pytest.approx(7000 + 20000 + 800) and s['uncosted_items'] == 2
    assert s['prev_units'] == 1 and s['range_days'] == 3
    assert [i['name'] for i in data['items']][:2] == ['Cable C', 'Laptop A']   # sorted by net qty


def test_totals_match_custom_report(app):
    """Unfiltered Top Sellers must reconcile with the Custom Report for the same dates."""
    from app.services.pos_reports import generate_top_sellers_data, generate_custom_report_data
    with app.app_context():
        top = generate_top_sellers_data(START, TODAY, {})
        custom = generate_custom_report_data(START, TODAY)
    s, c = top['summary'], custom['summary']
    assert s['total_incl_tax_cents'] == pytest.approx(c['gross_sales_cents'])            # "Overall Total"
    assert s['gross_revenue_cents'] == pytest.approx(c['gross_sales_cents'] - c['total_tax_cents'])
    assert s['tax_cents'] == pytest.approx(c['total_tax_cents'])
    assert s['orders'] == c['total_orders']
    # The Custom Report's item table shows the same rows and figures
    expected = sorted(((i['name'], i['net_qty'], round(i['net_revenue_cents'])) for i in top['items']),
                      key=lambda t: -t[2])[:10]
    assert [(i['name'], i['qty'], round(i['total_cents'])) for i in custom['top_items']] == expected


def test_default_page(client):
    html = _get(client)
    for name in ('Laptop A', 'Server B', 'Cable C', 'Old Thing', 'Mystery Box'):
        assert name in html


@pytest.mark.parametrize('params, expected', [
    ({'exclude_custom': '1'}, {'Laptop A', 'Server B', 'Cable C', 'Old Thing'}),
    ({'cat': 'PRI'}, {'Laptop A', 'Old Thing'}),
    ({'cat': ['SEC', 'ASC']}, {'Server B', 'Cable C'}),
    ({'aisle': 'Laptops'}, {'Laptop A'}),
    ({'aisle': '__none__'}, {'Cable C'}),
    ({'area': 'Retail'}, {'Laptop A', 'Server B'}),
    ({'item_status': 'legacy'}, {'Server B'}),
    ({'item_status': 'current'}, {'Laptop A', 'Cable C'}),
    ({'stock': 'out'}, {'Server B'}),
    ({'stock': 'in'}, {'Laptop A', 'Cable C'}),
    ({'q': 'cable'}, {'Cable C'}),
])
def test_filters(client, params, expected):
    html = _get(client, **params)
    shown = {n for n in ('Laptop A', 'Server B', 'Cable C', 'Old Thing', 'Mystery Box') if n in html}
    assert shown == expected


def test_sort_by_revenue_and_links_keep_filters(client):
    html = _get(client, sort='revenue', dir='desc', cat=['PRI', 'SEC', 'ASC'])
    order = sorted(['Server B', 'Laptop A', 'Old Thing', 'Cable C'], key=html.index)
    assert order == ['Server B', 'Laptop A', 'Old Thing', 'Cable C']
    # Clicking the active column flips direction and keeps every selected category
    links = re.findall(r'href="([^"]*sort=revenue[^"]*)"', html)
    assert len(links) == 1
    assert 'dir=asc' in links[0] and all(f'cat={c}' in links[0] for c in ('PRI', 'SEC', 'ASC'))


def test_limit(client):
    html = _get(client, limit='25')
    assert 'Showing 5 of 5' in html


def test_csv_export(client):
    resp = client.get('/pos/management/top-sellers', query_string={**RANGE, 'format': 'csv', 'limit': '25'})
    assert resp.status_code == 200 and resp.mimetype == 'text/csv'
    rows = list(csv.DictReader(io.StringIO(resp.get_data(as_text=True))))
    assert len(rows) == 5
    laptop = next(r for r in rows if r['Name'] == 'Laptop A')
    assert laptop['Net Qty'] == '2' and laptop['Est. Profit'] == '70.0' and laptop['Aisle'] == 'Laptops'
    assert laptop['Discounts'] == '30.0' and laptop['Net Sales (ex. tax)'] == '270.0'


def test_default_range_without_params(client):
    resp = client.get('/pos/management/top-sellers')
    assert resp.status_code == 200
    assert 'Laptop A' in resp.get_data(as_text=True)    # default 30-day window includes yesterday


@pytest.mark.parametrize('path', ['/pos/management/margins', '/pos/management/hourly', '/pos/management/operators'])
def test_removed_reports_are_gone(client, path):
    assert client.get(path).status_code == 404


def test_dashboard_renders_without_removed_links(client):
    resp = client.get('/pos/management')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'Margin Analysis' not in html and 'Hourly Analysis' not in html and 'Operator Performance' not in html
    assert '/pos/management/top-sellers?' in html   # "View All" carries the dashboard date range
