"""
POS sales-side pages carry no inline JavaScript, so the Content-Security-Policy
can drop 'unsafe-inline' from script-src.

Every register / checkout / scanner / customer-display / receipt / refund page,
and the cart fragment the register hot-swaps in (GET /pos/cart/fragment and the
X-Requested-With answer to a scan), must have:
  - no <script> without src, except <script type="application/json"> data blocks
  - no on* event-handler attributes
  - no javascript: URLs
and every data-action / data-change / ... name it uses must be registered with
RSCP.actions() by one of the scripts the page loads (for the fragments: the
scripts of the register page they are swapped into).
"""
import json
import os
import re
from html.parser import HTMLParser

import pytest

from tests.conftest import login

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
STATIC = os.path.join(ROOT, 'static')
HANDLER_ATTRS = ('data-action', 'data-change', 'data-input', 'data-submit',
                 'data-keyup', 'data-keydown', 'data-focusout')
URL_ATTRS = ('href', 'src', 'action', 'formaction', 'data')

REG = 'reg-nojs'
TRICKY_NAME = 'O\'Brien "Deluxe" <Widget> & Co'


class _Scanner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.problems = []
        self.script_srcs = []
        self.handler_names = set()
        self.confirms = []
        self._in_inline_script = False
        self._inline_body = ''

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        for name, value in attrs:
            if name.lower().startswith('on'):
                self.problems.append('<%s %s="%s">' % (tag, name, (value or '')[:60]))
            if name.lower() in URL_ATTRS and (value or '').strip().lower().startswith('javascript:'):
                self.problems.append('<%s %s="%s">' % (tag, name, value))
            if name in HANDLER_ATTRS and value:
                self.handler_names.add(value)
            if name == 'data-confirm':
                self.confirms.append(value)
        if tag == 'script':
            if a.get('src'):
                self.script_srcs.append(a['src'])
            elif (a.get('type') or '').lower() != 'application/json':
                self._in_inline_script = True
                self._inline_body = ''

    handle_startendtag = handle_starttag

    def handle_data(self, data):
        if self._in_inline_script:
            self._inline_body += data

    def handle_endtag(self, tag):
        if tag == 'script' and self._in_inline_script:
            self.problems.append('inline <script>: %s' % self._inline_body.strip()[:80])
            self._in_inline_script = False


def _registered_actions(js):
    names = set()
    for block in re.findall(r'RSCP\.actions\(\{(.*?)\n\s*\}\);', js, re.S):
        names.update(re.findall(r'(?m)^\s*(\w+)\s*:', block))
    return names


def _static_file(src):
    path = src.split('?', 1)[0]
    if not path.startswith('/static/'):
        return None
    return os.path.join(STATIC, *path[len('/static/'):].split('/'))


def _scan(html):
    scanner = _Scanner()
    scanner.feed(html)
    scanner.close()
    return scanner


def _registered_by(scan, where):
    registered = set()
    for src in scan.script_srcs:
        path = _static_file(src)
        if path:
            assert os.path.isfile(path), (where, src)
            with open(path, encoding='utf-8') as f:
                registered |= _registered_actions(f.read())
    return registered


def _page_data(html):
    m = re.search(r'<script type="application/json" id="page-data">(.*?)</script>', html, re.S)
    assert m, 'no page-data block'
    return json.loads(m.group(1))


def _db(app):
    from app.services.db import get_db_connection
    return get_db_connection(app.config['DATABASE'])


@pytest.fixture
def pos_world(fresh_app):
    """A register (admin, X-Terminal-Id) with a held order, a cart carrying a
    coupon, a cash discount, a completed sale and an open cash drawer."""
    from app.routes.pos.core import set_pos_setting
    from app.services import pos_registers

    with fresh_app.app_context():
        set_pos_setting('ALLOW_HOLD_ORDERS', 'true')
        set_pos_setting('CASH_DISCOUNT_ENABLED', 'true')
        set_pos_setting('CASH_DISCOUNT_TYPE', 'percent')
        set_pos_setting('CASH_DISCOUNT_AMOUNT', '3')

    conn = _db(fresh_app)
    conn.execute('INSERT INTO inventory_items (sku, name, sell_price, quantity) VALUES (?, ?, ?, ?)',
                 ('NJ-AAA', TRICKY_NAME, 10.0, 50))
    conn.execute('INSERT INTO inventory_items (sku, name, sell_price, quantity) VALUES (?, ?, ?, ?)',
                 ('NJ-BBB', 'Plain item', 4.5, 50))
    item_id = conn.execute("SELECT id FROM inventory_items WHERE sku = 'NJ-AAA'").fetchone()[0]
    order_id = conn.execute('''
        INSERT INTO pos_orders (order_number, status, subtotal, tax_rate, tax_amount,
                                discount_amount, discount_type, total, payment_method,
                                payment_details, operator_id)
        VALUES ('POS-NJ-001', 'completed', 10, 0.07, 0.7, 0, NULL, 10.7, 'card', '{}', 1)
    ''').lastrowid
    conn.execute('''INSERT INTO pos_order_items (order_id, inventory_item_id, sku, name, quantity,
                                                 unit_price, line_total)
                    VALUES (?, ?, 'NJ-AAA', ?, 1, 10, 10)''', (order_id, item_id, TRICKY_NAME))
    conn.commit()
    conn.close()

    register = fresh_app.test_client()
    login(register, 1)
    headers = {'X-Terminal-Id': REG}
    register.post('/pos/api/register/hello', json={'register_id': REG, 'friendly_name': 'Front'})
    register.post('/pos/cart/add', data={'sku': 'NJ-BBB'}, headers=headers)
    register.post('/pos/cart/hold', data={'note': TRICKY_NAME}, headers=headers)
    register.post('/pos/cart/add', data={'sku': 'NJ-AAA'}, headers=headers)
    register.post('/pos/cart/add', data={'sku': 'NJ-BBB'}, headers=headers)

    conn = _db(fresh_app)
    cart, _ = pos_registers.load_register_cart(conn, REG)
    assert len(cart['items']) == 2
    cart['applied_coupon'] = {'coupon_id': 1, 'code': 'SAVE1', 'name': TRICKY_NAME, 'discount': 1.0}
    pos_registers.save_register_cart(conn, REG, cart)
    conn.close()

    manager = fresh_app.test_client()
    login(manager, 3)
    assert manager.post('/pos/drawer/open', data={'opening_float': '100'}).status_code == 302

    return {'register': register, 'headers': headers, 'manager': manager,
            'order_id': order_id, 'order_number': 'POS-NJ-001'}


def _get(client, url, headers=None):
    resp = client.get(url, headers=headers or {})
    assert resp.status_code == 200, (url, resp.status_code, resp.headers.get('Location'))
    return resp.get_data(as_text=True)


def _rendered_pages(fresh_app, fresh_client, world):
    """(label, html, registered-actions-override or None) for every page."""
    reg, hdr, mgr = world['register'], world['headers'], world['manager']
    num, oid = world['order_number'], world['order_id']
    pages = [
        ('/pos/', _get(reg, '/pos/', hdr)),
        ('/pos/checkout', _get(reg, '/pos/checkout', hdr)),
        ('/pos/preview-receipt', _get(reg, '/pos/preview-receipt', hdr)),
        ('/pos/receipt/%s' % num, _get(reg, '/pos/receipt/%s' % num, hdr)),
        ('/pos/receipt/%s/print' % num, _get(reg, '/pos/receipt/%s/print' % num, hdr)),
        ('/pos/refunds/auth', _get(mgr, '/pos/refunds/auth')),
        ('/pos/refunds', _get(mgr, '/pos/refunds')),
        ('/pos/refunds/search', _get(mgr, '/pos/refunds/search?q=NJ-AAA')),
        ('/pos/refunds/order/%s' % num, _get(mgr, '/pos/refunds/order/%s' % num)),
        ('/pos/management/edit-sale/%d' % oid, _get(mgr, '/pos/management/edit-sale/%d' % oid)),
        ('/pos/drawer', _get(mgr, '/pos/drawer')),
    ]

    cashier = fresh_app.test_client()
    login(cashier, 2)
    pages.append(('/pos/scan', _get(cashier, '/pos/scan')))
    pages.append(('/pos/customer-display', _get(fresh_client, '/pos/customer-display')))

    # Anonymous GET /pos/login is bounced by the global session guard; render it directly.
    from flask import render_template
    with fresh_app.test_request_context('/pos/login'):
        pages.append(('pos/login.html', render_template('pos/login.html')))

    # Cart fragments swapped into the register page
    pages.append(('/pos/cart/fragment', _get(reg, '/pos/cart/fragment', hdr)))
    resp = reg.post('/pos/cart/add', data={'sku': 'NJ-BBB', 'quantity': '1'},
                    headers={**hdr, 'X-Requested-With': 'XMLHttpRequest'})
    assert resp.status_code == 200
    pages.append(('POST /pos/cart/add (scan queue)', resp.get_data(as_text=True)))
    return pages


FRAGMENTS = ('/pos/cart/fragment', 'POST /pos/cart/add (scan queue)')


def test_pos_sales_pages_have_no_inline_js(fresh_app, fresh_client, pos_world):
    failures = {}
    register_actions = None
    for label, html in _rendered_pages(fresh_app, fresh_client, pos_world):
        scan = _scan(html)
        if label in FRAGMENTS:
            assert '<!DOCTYPE' not in html and not scan.script_srcs, label
            registered = register_actions  # fragments run inside the register page
        else:
            registered = _registered_by(scan, label)
            if label == '/pos/':
                register_actions = registered
        missing = scan.handler_names - registered
        if missing:
            scan.problems.append('unregistered handlers: %s' % sorted(missing))
        if scan.problems:
            failures[label] = scan.problems
    assert not failures, failures


def test_register_page_wiring(pos_world):
    reg, hdr = pos_world['register'], pos_world['headers']
    html = _get(reg, '/pos/', hdr)
    assert _page_data(html) == {'cartAddUrl': '/pos/cart/add'}
    scan = _scan(html)
    assert any(src.endswith('/js/pages/pos-sales.js') for src in scan.script_srcs)
    # Cart controls (also in every hot-swapped fragment)
    for marker in ('data-confirm="Clear entire cart?"', 'data-change="submitForm"',
                   'data-action="holdOrder"', 'data-action="applyCouponFromModal"',
                   'data-action="removeCoupon"', 'data-keydown="couponModalKeydown"',
                   'name="cart_version"', '/pos/cart/remove/'):
        assert marker in html, marker
    assert TRICKY_NAME not in html  # never raw in markup
    fragment = _get(reg, '/pos/cart/fragment', hdr)
    assert 'data-change="submitForm"' in fragment and 'name="cart_version"' in fragment


def test_checkout_page_data(pos_world):
    reg, hdr = pos_world['register'], pos_world['headers']
    html = _get(reg, '/pos/checkout', hdr)
    data = _page_data(html)
    assert data['cashDiscountEnabled'] is True
    assert data['cashDiscountType'] == 'percent'
    assert data['cashDiscountAmount'] == pytest.approx(3.0)
    assert isinstance(data['total'], float) and data['total'] > 0
    assert 0 < data['cashDiscountValue'] < data['total']
    assert 'name="expected_total" value="%.2f"' % data['total'] in html
    assert 'data-action="validateCheckout"' in html


def test_edit_sale_page_data(pos_world):
    html = _get(pos_world['manager'], '/pos/management/edit-sale/%d' % pos_world['order_id'])
    data = _page_data(html)
    assert isinstance(data['taxRatePercent'], float)
    assert {'discount_pct', 'discount_fixed', 'coupon', 'tax_rate', 'cash_discount'} <= set(data['adjustments'])


def test_confirm_texts_keep_their_line_breaks(pos_world):
    html = _get(pos_world['register'], '/pos/receipt/%s' % pos_world['order_number'], pos_world['headers'])
    assert ('⚠️ PERMANENTLY DELETE this order?\n\nThis action cannot be undone.'
            in _scan(html).confirms)
    html = _get(pos_world['manager'], '/pos/refunds/order/%s' % pos_world['order_number'])
    assert 'Process FULL refund of $ 10.70?' in _scan(html).confirms


POS_JS = sorted('js/pages/' + name for name in os.listdir(os.path.join(STATIC, 'js', 'pages'))
                if name.startswith('pos-') and name.endswith('.js'))


def test_pos_js_files_exist():
    for name in ('sales', 'scan', 'customer-display', 'checkout', 'receipt', 'receipt-print',
                 'receipt-preview', 'login', 'refunds', 'edit-sale'):
        assert 'js/pages/pos-%s.js' % name in POS_JS, name


@pytest.mark.parametrize('rel', POS_JS)
def test_pos_js_files_are_static_and_handler_free(rel):
    with open(os.path.join(STATIC, *rel.split('/')), encoding='utf-8') as f:
        js = f.read()
    assert '{{' not in js and '{%' not in js, 'Jinja inside a static JS file'
    # HTML built in JS (innerHTML templates) must not carry inline handlers either.
    assert not re.search(r'\son[a-z]+\s*=\s*["\'`$]', js), 'inline handler in generated HTML'
    assert 'javascript:' not in js
    # Every data-action name in generated HTML is registered in the same file.
    used = set(re.findall(r'data-(?:action|change|input|submit|keyup|keydown|focusout)="(\w+)"', js))
    assert used <= _registered_actions(js), used - _registered_actions(js)
