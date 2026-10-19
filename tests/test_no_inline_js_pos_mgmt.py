"""
POS management / report pages carry no inline JavaScript, so the
Content-Security-Policy can drop 'unsafe-inline' from script-src.

Every page (rendered as admin and as POS manager, with an order, refund,
coupon and on-sale item seeded) must have:
  - no <script> without src, except <script type="application/json"> data blocks
  - no on* event-handler attributes
  - no javascript: URLs
and every data-action / data-change / ... name it uses must be registered with
RSCP.actions() by one of the scripts the page loads.

_nav.html is stubbed out here: it belongs to the core-pages workstream and is
checked there.
"""
import json
import os
import re

import pytest
from jinja2 import ChoiceLoader, DictLoader

from tests.conftest import login
from tests.test_no_inline_js_inventory import STATIC, _registered_actions, _scan, _static_file

TRICKY_NAME = 'O\'Brien "Deluxe" <Widget> & Co'


@pytest.fixture
def seeded(fresh_app):
    """Items (one on sale, one never sold), a completed sale, a refund and a coupon."""
    from app.services.db import get_db_connection

    fresh_app.jinja_env.loader = ChoiceLoader([DictLoader({'_nav.html': ''}), fresh_app.jinja_env.loader])

    conn = get_db_connection(fresh_app.config['DATABASE'])
    item_id = conn.execute(
        "INSERT INTO inventory_items (sku, name, sell_price_cents, buy_price_cents, quantity, sale_enabled, "
        "sale_price_cents) VALUES ('NJ-POS-1', ?, 1000, 400, 5, 1, 800)", (TRICKY_NAME,)).lastrowid
    conn.execute(
        "INSERT INTO inventory_items (sku, name, sell_price_cents, buy_price_cents, quantity) "
        "VALUES ('NJ-POS-2', 'Dusty Widget', 300, 100, 9)")
    order_id = conn.execute(
        "INSERT INTO pos_orders (order_number, status, subtotal_cents, tax_rate, tax_amount_cents, total_cents, "
        "payment_method, payment_details, operator_id) "
        "VALUES ('POS-NJ-001', 'completed', 1000, 0, 0, 1000, 'cash', '{}', 1)").lastrowid
    conn.execute(
        "INSERT INTO pos_order_items (order_id, inventory_item_id, sku, name, quantity, unit_price_cents, "
        "line_total_cents) VALUES (?, ?, 'NJ-POS-1', ?, 1, 1000, 1000)", (order_id, item_id, TRICKY_NAME))
    conn.execute(
        "INSERT INTO pos_refunds (order_id, refund_type, amount_cents, reason, manager_id) "
        "VALUES (?, 'partial', 200, 'scuffed', 1)", (order_id,))
    coupon_id = conn.execute(
        "INSERT INTO pos_coupons (name, code, coupon_type, discount_type, discount_percent) "
        "VALUES (?, 'NJTEST', 'serialized', 'bogo_cross', 10)", (TRICKY_NAME,)).lastrowid
    conn.commit()
    conn.close()
    return {'coupon_id': coupon_id, 'item_id': item_id}


PAGES = [
    '/pos/management',
    '/pos/management?start_date=2020-01-01&end_date=2030-12-31',
    '/pos/management/reports/daily',
    '/pos/management/reports/daily/print',
    '/pos/management/reports/daily-items',
    '/pos/management/reports/custom',
    '/pos/management/top-sellers',
    '/pos/management/top-sellers?q=no-such-item',
    '/pos/management/reports/valuation',
    '/pos/management/stale-inventory',
    '/pos/management/reports/reorder',
    '/pos/management/refunds-report',
    '/pos/coupons',
    '/pos/coupons/create',
    '/pos/coupons/{coupon_id}/edit',
    '/pos/management/sales',
    '/pos/sales-manager',
    '/pos/management/settings',
]


def _get(client, url, seeded):
    resp = client.get(url.format(**seeded))
    assert resp.status_code == 200, (url, resp.status_code)
    return resp.get_data(as_text=True)


def _page_data(html):
    m = re.search(r'<script type="application/json" id="page-data">(.*?)</script>', html, re.S)
    assert m, 'no page-data block'
    return json.loads(m.group(1))


@pytest.mark.parametrize('user_id', [1, 3], ids=['admin', 'pos_manager'])
def test_pos_mgmt_pages_have_no_inline_js(fresh_app, fresh_client, seeded, user_id):
    client = login(fresh_client, user_id)
    failures = {}
    for url in PAGES:
        html = _get(client, url, seeded)
        scan = _scan(html)
        registered = set()
        for src in scan.script_srcs:
            path = _static_file(src)
            if path:
                assert os.path.isfile(path), (url, src)
                with open(path, encoding='utf-8') as f:
                    registered |= _registered_actions(f.read())
        missing = scan.handler_names - registered
        if missing:
            scan.problems.append('unregistered handlers: %s' % sorted(missing))
        if scan.problems:
            failures[url] = scan.problems
    assert not failures, failures


def test_print_page_is_standalone_and_loads_helpers(fresh_app, fresh_client, seeded):
    html = _get(login(fresh_client, 1), '/pos/management/reports/daily/print', seeded)
    common = html.index('js/rscp-common.js')
    assert common < html.index('js/pages/pos-mgmt-common.js')
    assert 'data-action="print"' in html and 'data-action="closeWindow"' in html


def test_report_controls_use_data_attributes(fresh_app, fresh_client, seeded):
    client = login(fresh_client, 1)
    html = _get(client, '/pos/management/reports/daily', seeded)
    assert 'data-change="submitForm"' in html
    assert 'data-confirm="Send EOD Email to configured recipients?"' in html
    assert ('data-action="openPopup" data-popup-name="eod_print" '
            'data-popup-features="width=350,height=600"') in html

    html = _get(client, '/pos/coupons', seeded)
    assert 'data-confirm="Delete this coupon? This cannot be undone."' in html
    html = _get(client, '/pos/sales-manager', seeded)
    assert 'data-confirm="Stop this sale?"' in html
    html = _get(client, '/pos/management/settings', seeded)
    assert 'data-action="testDrawer"' in html
    assert html.index('bootstrap.bundle.min.js') < html.index('js/pages/pos-settings.js')


def test_management_page_data(fresh_app, fresh_client, seeded):
    html = _get(login(fresh_client, 1), '/pos/management?start_date=2020-01-01&end_date=2030-12-31', seeded)
    data = _page_data(html)
    assert set(data) == {'salesData', 'salesLabels', 'paymentLabels', 'paymentData'}
    assert len(data['salesData']) == len(data['salesLabels']) >= 1
    assert all(re.fullmatch(r'\d{4}-\d{2}-\d{2}', d) for d in data['salesLabels'])
    assert data['paymentLabels'] == ['cash'] and data['paymentData'] == [10]
    assert 'data-change="handleDaysChange"' in html and 'data-action="applyCustomRange"' in html


def test_top_sellers_page_data(fresh_app, fresh_client, seeded):
    client = login(fresh_client, 1)
    html = _get(client, '/pos/management/top-sellers', seeded)
    data = _page_data(html)
    assert data['trend'] and {'label', 'revenue', 'units'} <= set(data['trend'][0])
    assert re.fullmatch(r'\d{4}-\d{2}-\d{2}', data['firstSaleDate'])
    assert 'id="trendChart"' in html

    # The script only draws when there is trend data, exactly when the canvas is rendered.
    html = _get(client, '/pos/management/top-sellers?q=no-such-item', seeded)
    assert bool(_page_data(html)['trend']) == ('id="trendChart"' in html)


def test_coupon_form_page_data(fresh_app, fresh_client, seeded):
    client = login(fresh_client, 1)
    assert _page_data(_get(client, '/pos/coupons/create', seeded)) == {
        'couponType': 'generic', 'discountType': 'order_percent'}
    assert _page_data(_get(client, '/pos/coupons/{coupon_id}/edit', seeded)) == {
        'couponType': 'serialized', 'discountType': 'bogo_cross'}


def test_tricky_names_stay_escaped(fresh_app, fresh_client, seeded):
    client = login(fresh_client, 1)
    for url in ('/pos/management', '/pos/coupons', '/pos/coupons/{coupon_id}/edit',
                '/pos/management/stale-inventory', '/pos/sales-manager'):
        assert TRICKY_NAME not in _get(client, url, seeded), url


POS_MGMT_JS = ['js/pages/' + name for name in (
    'pos-mgmt-common.js', 'pos-management.js', 'pos-top-sellers.js', 'pos-stale-inventory.js',
    'pos-coupon-form.js', 'pos-settings.js')]


@pytest.mark.parametrize('rel', POS_MGMT_JS)
def test_pos_mgmt_js_files_are_static_and_handler_free(rel):
    with open(os.path.join(STATIC, *rel.split('/')), encoding='utf-8') as f:
        js = f.read()
    assert '{{' not in js and '{%' not in js, 'Jinja inside a static JS file'
    assert not re.search(r'\son[a-z]+\s*=\s*["\'`$]', js), 'inline handler in generated HTML'
    assert 'javascript:' not in js
    assert "'use strict'" in js
    used = set(re.findall(r'data-(?:action|change|input|submit|keyup|keydown|focusout)="(\w+)"', js))
    assert used <= _registered_actions(js), used - _registered_actions(js)
