"""POS sales/register pages render on the shared layout (templates/base.html).

Each page must come out as ONE well-formed document: a single doctype,
<html lang="en">, Bootstrap CSS exactly once where the page uses it, and the
element ids its JavaScript relies on. Thermal receipt prints never load
Bootstrap (80mm paper layout) and stay standalone.
"""
import pytest

from tests.conftest import login

BOOTSTRAP_CSS = 'bootstrap@5.3.0/dist/css/bootstrap.min.css'
BOOTSTRAP_JS = 'bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js'
NAV_MARKER = 'js/nav.js'  # loaded by _nav.html; defines window.rscpPoll


def _db(app):
    from app.services.db import get_db_connection
    return get_db_connection(app.config['DATABASE'])


@pytest.fixture
def order(fresh_app):
    """A completed one-line sale: (order_id, order_number)."""
    conn = _db(fresh_app)
    item = conn.execute('INSERT INTO inventory_items (sku, name, sell_price, quantity) '
                        "VALUES ('GADGET', 'GADGET item', 100, 5)").lastrowid
    order_id = conn.execute('''
        INSERT INTO pos_orders (order_number, status, subtotal, tax_rate, tax_amount,
                                discount_amount, discount_type, total, payment_method,
                                payment_details, operator_id)
        VALUES ('POS-T-001', 'completed', 100, 0.07, 7, 0, NULL, 107, 'card', '{}', 1)
    ''').lastrowid
    conn.execute('''INSERT INTO pos_order_items (order_id, inventory_item_id, sku, name, quantity,
                                                 unit_price, line_total)
                    VALUES (?, ?, 'GADGET', 'GADGET item', 1, 100, 100)''', (order_id, item))
    conn.commit()
    conn.close()
    return order_id, 'POS-T-001'


def _put_cart(fresh_app, client):
    conn = _db(fresh_app)
    item = conn.execute('INSERT INTO inventory_items (sku, name, sell_price, quantity) '
                        "VALUES ('WIDGET', 'WIDGET item', 10, 10)").lastrowid
    conn.commit()
    conn.close()
    with client.session_transaction() as sess:
        sess['pos_cart'] = {
            'items': [{'inventory_item_id': item, 'sku': 'WIDGET', 'name': 'WIDGET item',
                       'quantity': 1, 'unit_price': 10.0, 'discount_amount': 0,
                       'discount_type': None, 'line_total': 10.0}],
            'discount_amount': 0, 'discount_type': None, 'discount_reason': '',
        }


def _page(client, url):
    resp = client.get(url)
    assert resp.status_code == 200, (url, resp.status_code, resp.headers.get('Location'))
    return resp.get_data(as_text=True)


def _assert_layout(html, marker, bootstrap=True, nav=True, bootstrap_js=True):
    assert html.count('<!DOCTYPE html>') == 1
    assert html.count('<html lang="en">') == 1
    assert html.count('<html') == 1
    assert html.count('</html>') == 1
    assert html.count('<body') == 1
    assert marker in html, marker
    if bootstrap:
        assert html.count(BOOTSTRAP_CSS) == 1
        assert 'js/rscp-common.js' in html
        assert html.count('js/rscp-common.js') == 1
    else:
        assert BOOTSTRAP_CSS not in html
    assert (NAV_MARKER in html) == nav
    assert html.count(BOOTSTRAP_JS) == (1 if bootstrap_js else 0)


# --- register / cashier pages ---------------------------------------------

def test_sales_page(fresh_client):
    html = _page(login(fresh_client, 2), '/pos/')
    _assert_layout(html, 'id="posCartContainer"')
    assert 'id="flashMessagesContainer"' in html
    # Page script (static/js/pages/pos-sales.js) runs after the nav (rscpPoll) and the Bootstrap bundle
    assert html.index(NAV_MARKER) < html.index('js/pages/pos-sales.js')
    assert html.index(BOOTSTRAP_JS) < html.index('js/pages/pos-sales.js')


def test_checkout_page(fresh_app, fresh_client):
    login(fresh_client, 2)
    _put_cart(fresh_app, fresh_client)
    _assert_layout(_page(fresh_client, '/pos/checkout'), 'id="tenderedAmount"')


def test_scan_page_is_full_screen(fresh_client):
    html = _page(login(fresh_client, 2), '/pos/scan')
    _assert_layout(html, 'id="scannerApp"', nav=False, bootstrap_js=False)
    assert 'user-scalable=no' in html


def test_customer_display_is_full_screen(fresh_client):
    html = _page(fresh_client, '/pos/customer-display')  # public kiosk
    _assert_layout(html, 'id="activeDisplay"', nav=False, bootstrap_js=False)
    assert 'id="cartItems"' in html
    assert '| Customer Display</title>' in html


def test_customer_display_self_heals(fresh_client):
    """The display gets the app version (to reload after updates) and its
    script guards the poll loop against hung requests and login redirects."""
    import json, os, re
    html = _page(fresh_client, '/pos/customer-display')
    data = json.loads(re.search(r'id="page-data">(.*?)</script>', html, re.S).group(1))
    assert data['appVersion']
    js_path = os.path.join(os.path.dirname(__file__), '..', 'static', 'js', 'pages', 'pos-customer-display.js')
    with open(js_path, encoding='utf-8') as f:
        js = f.read()
    for needle in ('AbortController', "redirect: 'manual'", 'opaqueredirect', 'setInterval(watchdog'):
        assert needle in js, needle


def test_lock_screen_is_gone(fresh_client):
    login(fresh_client, 2)
    assert fresh_client.get('/pos/unlock').status_code == 404
    assert fresh_client.post('/pos/lock').status_code in (400, 404)  # 400 = CSRF before routing


def test_pos_login(fresh_app):
    # Rendered directly: anonymous GET /pos/login is currently bounced to
    # /login by the global session guard (the path is not on its allowlist),
    # and a logged-in GET redirects to the register.
    from flask import render_template
    with fresh_app.test_request_context('/pos/login'):
        html = render_template('pos/login.html')
    _assert_layout(html, 'id="badgeInput"', nav=False, bootstrap_js=False)
    assert '<title>RSCP | POS Login</title>' in html


# --- receipts ---------------------------------------------------------------

def test_receipt_page(fresh_client, order):
    html = _page(login(fresh_client, 2), f'/pos/receipt/{order[1]}')
    _assert_layout(html, order[1])
    assert f'| Receipt {order[1]}</title>' in html


def test_receipt_print_stays_standalone(fresh_client, order):
    html = _page(login(fresh_client, 2), f'/pos/receipt/{order[1]}/print')
    # window.print() runs from static/js/pages/pos-receipt-print.js
    _assert_layout(html, 'js/pages/pos-receipt-print.js', bootstrap=False, nav=False, bootstrap_js=False)


def test_receipt_preview_stays_standalone(fresh_app, fresh_client):
    login(fresh_client, 2)
    _put_cart(fresh_app, fresh_client)
    html = _page(fresh_client, '/pos/preview-receipt')
    _assert_layout(html, 'PREVIEW', bootstrap=False, nav=False, bootstrap_js=False)


# --- refunds / manager pages ------------------------------------------------

def test_refund_auth_is_full_screen(fresh_client):
    html = _page(login(fresh_client, 2), '/pos/refunds/auth')
    _assert_layout(html, 'Manager Authentication', nav=False, bootstrap_js=False)


def test_refunds_page(fresh_client, order):
    _assert_layout(_page(login(fresh_client, 3), '/pos/refunds'), 'id="btnToday"')


def test_refund_search_page(fresh_client, order):
    html = _page(login(fresh_client, 3), '/pos/refunds/search?q=GADGET')
    _assert_layout(html, '<title>RSCP | Refund Search</title>')


def test_refund_order_page(fresh_client, order):
    html = _page(login(fresh_client, 3), f'/pos/refunds/order/{order[1]}')
    _assert_layout(html, 'id="refundForm"')


def test_edit_sale_page(fresh_client, order):
    html = _page(login(fresh_client, 3), f'/pos/management/edit-sale/{order[0]}')
    _assert_layout(html, 'id="editTotal"')


def test_drawer_page(fresh_client):
    html = _page(login(fresh_client, 3), '/pos/drawer')
    _assert_layout(html, '<title>RSCP | Cash Drawer</title>')
