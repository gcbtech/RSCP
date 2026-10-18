"""POS management/report pages render on the shared layout (templates/base.html)."""
import re

import pytest

from tests.conftest import login

BOOTSTRAP_CSS = 'bootstrap@5.3.0/dist/css/bootstrap.min.css'


def _db(app):
    from app.services.db import get_db_connection
    return get_db_connection(app.config['DATABASE'])


@pytest.fixture
def seeded(fresh_app):
    """One item, one completed sale of it, a refund, and a coupon."""
    conn = _db(fresh_app)
    item_id = conn.execute(
        "INSERT INTO inventory_items (sku, name, sell_price, buy_price, quantity) "
        "VALUES ('UI-SKU', 'UI <b>Widget</b>', 10, 4, 5)").lastrowid
    order_id = conn.execute(
        "INSERT INTO pos_orders (order_number, status, subtotal, tax_rate, tax_amount, total, "
        "payment_method, payment_details, operator_id) "
        "VALUES ('POS-UI-001', 'completed', 10, 0, 0, 10, 'cash', '{}', 1)").lastrowid
    conn.execute(
        "INSERT INTO pos_order_items (order_id, inventory_item_id, sku, name, quantity, unit_price, line_total) "
        "VALUES (?, ?, 'UI-SKU', 'UI <b>Widget</b>', 1, 10, 10)", (order_id, item_id))
    conn.execute(
        "INSERT INTO pos_refunds (order_id, refund_type, amount, reason, manager_id) "
        "VALUES (?, 'partial', 2, 'scuffed', 1)", (order_id,))
    coupon_id = conn.execute(
        "INSERT INTO pos_coupons (name, code, coupon_type, discount_type, discount_value) "
        "VALUES ('UI Coupon', 'UITEST', 'generic', 'order_percent', 10)").lastrowid
    conn.commit()
    conn.close()
    return {'coupon_id': coupon_id}


# (url, page-specific marker, uses Bootstrap CSS)
PAGES = [
    ('/pos/management', 'id="salesChart"', True),
    ('/pos/management/reports/daily', 'End of Day Report</h2>', True),
    ('/pos/management/reports/daily-items', 'Daily Items Sold</h2>', True),
    ('/pos/management/reports/custom', 'Custom Report</h2>', True),
    ('/pos/management/top-sellers', 'Top Sellers</h2>', True),
    ('/pos/management/reports/valuation', 'Inventory Valuation</h2>', True),
    ('/pos/management/stale-inventory', 'id="staleTable"', True),
    ('/pos/management/reports/reorder', 'Smart Reorder Report</h2>', True),
    ('/pos/management/refunds-report', 'Refunds Report</h2>', True),
    ('/pos/coupons', 'Manage Coupons</h2>', True),
    ('/pos/coupons/create', 'Create Coupon</h3>', True),
    ('/pos/coupons/{coupon_id}/edit', 'Edit Coupon</h3>', True),
    ('/pos/management/sales', 'Sales History</h2>', True),
    ('/pos/sales-manager', 'Sales Manager</h1>', True),
    ('/pos/management/settings', 'POS Settings</h2>', True),
    # Print view: deliberately standalone (thermal layout, no Bootstrap).
    ('/pos/management/reports/daily/print', 'END OF DAY REPORT', False),
]


@pytest.mark.parametrize('user_id', [1, 3], ids=['admin', 'pos_manager'])
@pytest.mark.parametrize('url,marker,uses_bootstrap', PAGES, ids=[p[0] for p in PAGES])
def test_page_renders_on_layout(fresh_app, fresh_client, seeded, user_id, url, marker, uses_bootstrap):
    login(fresh_client, user_id)
    resp = fresh_client.get(url.format(**seeded))
    assert resp.status_code == 200, url
    html = resp.get_data(as_text=True)
    assert html.count('<!DOCTYPE html>') == 1
    assert html.count('<html') == 1
    assert '<html lang="en">' in html
    assert marker in html
    if uses_bootstrap:
        assert html.count(BOOTSTRAP_CSS) == 1
        assert html.count('bootstrap.bundle.min.js') == 1
        assert html.count('bootstrap-icons@') == 1
        assert html.count('<nav') >= 1
    else:
        assert BOOTSTRAP_CSS not in html


def test_cashier_cannot_open_management_pages(fresh_app, fresh_client, seeded):
    login(fresh_client, 2)
    resp = fresh_client.get('/pos/management/reports/daily')
    assert resp.status_code in (302, 403)


def test_stale_inventory_only_loads_scripts_from_jsdelivr(fresh_app, fresh_client, seeded):
    login(fresh_client, 1)
    html = fresh_client.get('/pos/management/stale-inventory').get_data(as_text=True)
    hosts = set(re.findall(r'<script[^>]+src="https?://([^/"]+)', html))
    assert hosts == {'cdn.jsdelivr.net'}
    css_hosts = set(re.findall(r'<link[^>]+href="https?://([^/"]+)', html))
    assert css_hosts == {'cdn.jsdelivr.net'}
    # jQuery and DataTables load after the markup, jQuery first, then the init script.
    jq = html.index('npm/jquery@3.7.0')
    dt = html.index('npm/datatables.net@1.13.6/js/jquery.dataTables.min.js')
    dt_bs5 = html.index('npm/datatables.net-bs5@1.13.6/js/dataTables.bootstrap5.min.js')
    init = html.index('js/pages/pos-stale-inventory.js')
    assert html.index('id="staleTable"') < jq < dt < dt_bs5 < init
    assert 'datatables.net-bs5@1.13.6/css/dataTables.bootstrap5.min.css' in html
    # Numeric columns sort on plain numbers, not on the badge/"N days" markup.
    assert re.search(r'<td class="text-end" data-order="\d+">\s*<span class="days-stale', html)


def test_chart_pages_use_pinned_chartjs(fresh_app, fresh_client, seeded):
    login(fresh_client, 1)
    for url, page_js in (('/pos/management', 'js/pages/pos-management.js'),
                         ('/pos/management/top-sellers', 'js/pages/pos-top-sellers.js')):
        html = fresh_client.get(url).get_data(as_text=True)
        assert 'npm/chart.js"' not in html
        # The page script (which calls new Chart) loads after the pinned Chart.js.
        assert html.count('npm/chart.js@4.4.0/dist/chart.umd.min.js') == 1
        assert html.index('chart.umd.min.js') < html.index(page_js)


def test_coupon_form_uses_pinned_tom_select(fresh_app, fresh_client, seeded):
    login(fresh_client, 1)
    html = fresh_client.get('/pos/coupons/create').get_data(as_text=True)
    assert html.count('tom-select@2.2.2/dist/css/tom-select.bootstrap5.min.css') == 1
    assert html.index('tom-select.complete.min.js') < html.index('js/pages/pos-coupon-form.js')
