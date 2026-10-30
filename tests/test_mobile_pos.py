"""Phone UI, POS (/m/pos...): the register runs the desktop cart code, checkout
runs the desktop checkout, and the phone pages wrap them."""
import re

import pytest

from tests.conftest import login

PASSWORD = 'Correct-Horse-1'   # every FRESH_USERS account (conftest)


@pytest.fixture
def shop(fresh_app):
    from app.services import inventory
    from app.services.db import get_db_connection
    with fresh_app.app_context():
        conn = get_db_connection()
        fresh_app.ids = {
            'ram': inventory.create_item(conn, {'sku': 'RAM-16', 'name': 'Kingston 16GB SODIMM', 'quantity': 5,
                                                'sell_price_cents': 5499, 'location_area': 'Retail'}, user='admin'),
            'mouse': inventory.create_item(conn, {'sku': 'MSE-1', 'name': 'Wireless Mouse', 'quantity': 1,
                                                  'sell_price_cents': 1499, 'location_area': 'Retail',
                                                  'upc': '0123'}, user='admin'),
        }
        conn.close()
    return fresh_app


def act(client, action, item=None, **form):
    url = f'/m/pos/cart/{action}' + (f'/{item}' if item else '')
    resp = client.post(url, data=form, headers={'X-Response-Format': 'json'})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


def line_ids(html):
    return re.findall(r'data-line="([^"]+)"', html)


def qty(app, item_id):
    from app.services.db import get_db_connection
    with app.app_context():
        conn = get_db_connection()
        try:
            return conn.execute('SELECT quantity FROM inventory_items WHERE id = ?', (item_id,)).fetchone()[0]
        finally:
            conn.close()


def test_register_page(shop, fresh_client):
    login(fresh_client, 2)
    html = fresh_client.get('/m/pos').get_data(as_text=True)
    assert 'id="mScanCapture"' in html and 'm/pos.js' in html
    assert '"actionUrl": "/m/pos/cart/ACTION"' in html and '"helloUrl": "/pos/api/register/hello"' in html
    assert 'This phone' in html
    # Until pos.js has said hello the server doesn't know which cart this is
    assert 'Loading the cart' in html
    cart = fresh_client.get('/m/pos/cart').get_json()
    assert 'Cart is empty' in cart['html'] and cart['count'] == 0 and cart['linked'] is None
    # Once the register is known, the page renders its cart straight away
    fresh_client.post('/pos/api/register/hello', json={'register_id': 'term_test_phone'})
    assert 'Cart is empty' in fresh_client.get('/m/pos').get_data(as_text=True)


def test_scan_adds_and_buttons_change_the_line(shop, fresh_client):
    login(fresh_client, 2)
    data = act(fresh_client, 'add', sku='RAM-16')
    assert data['ok'] and data['count'] == 1
    assert 'Kingston 16GB SODIMM' in data['html'] and '$54.99' in data['foot_html']
    again = act(fresh_client, 'add', sku='RAM-16')
    assert again['count'] == 2 and len(line_ids(again['html'])) == 2      # one line: - and + carry its id
    line = line_ids(again['html'])[0]
    assert act(fresh_client, 'update', line, quantity_delta='1')['count'] == 3
    assert act(fresh_client, 'update', line, quantity_delta='-1')['count'] == 2
    by_upc = act(fresh_client, 'add', sku='0123')
    assert by_upc['ok'] and 'Wireless Mouse' in by_upc['html']
    removed = act(fresh_client, 'remove', line)
    assert removed['ok'] and 'Kingston' not in removed['html'] and removed['count'] == 1
    assert qty(shop, shop.ids['ram']) == 5          # nothing leaves stock until the sale


def test_unknown_code_and_stock_warning(shop, fresh_client):
    login(fresh_client, 2)
    missing = act(fresh_client, 'add', sku='NOPE')
    assert not missing['ok'] and missing['error'] == 'not_found'
    act(fresh_client, 'add', sku='MSE-1')
    over = act(fresh_client, 'add', sku='MSE-1')
    assert over['ok'] and any('STOCK WARNING' in m['text'] for m in over['messages'])
    assert 'Only 1 in stock' in over['html']


def test_void_needs_a_manager_when_the_store_says_so(shop, fresh_client):
    from app.routes.pos.core import set_pos_setting
    with shop.app_context():
        set_pos_setting('REQUIRE_MANAGER_VOID', 'true')
    login(fresh_client, 2)
    line = line_ids(act(fresh_client, 'add', sku='RAM-16')['html'])[0]
    refused = act(fresh_client, 'remove', line)
    assert not refused['ok'] and refused['error'] == 'manager_required' and refused['messages'] == []
    fresh_client.post('/pos/api/validate-manager', json={'username': 'manager', 'password': PASSWORD})
    assert act(fresh_client, 'remove', line)['ok']


def test_custom_item(shop, fresh_client):
    login(fresh_client, 2)
    ok = act(fresh_client, 'custom', name='Labour', price='25.00', quantity='2')
    assert ok['ok'] and 'Labour' in ok['html'] and '$50.00' in ok['foot_html']
    bad = act(fresh_client, 'custom', name='Labour', price='abc')
    assert not bad['ok'] and any('Invalid price' in m['text'] for m in bad['messages'])


def test_discount_limits_and_manager_approval(shop, fresh_client):
    login(fresh_client, 2)
    act(fresh_client, 'add', sku='RAM-16')
    small = act(fresh_client, 'discount', discount_amount='5', discount_type='percent', discount_reason='Dent')
    assert small['ok'] and 'Off $2.75' in small['foot_html']
    big = act(fresh_client, 'discount', discount_amount='50', discount_type='percent')
    assert not big['ok'] and big['error'] == 'manager_required'
    fresh_client.post('/pos/verify-manager', json={'username': 'manager', 'password': PASSWORD})
    approved = act(fresh_client, 'discount', discount_amount='50', discount_type='percent')
    assert approved['ok'] and any('approved by manager' in m['text'] for m in approved['messages'])


def test_hold_then_recall(shop, fresh_client):
    login(fresh_client, 2)
    act(fresh_client, 'add', sku='RAM-16')
    held = act(fresh_client, 'hold', note='Bob')
    assert held['ok'] and held['count'] == 0 and 'Bob' in held['held_html']
    held_id = re.search(r'data-id="(\d+)"', held['held_html']).group(1)
    act(fresh_client, 'add', sku='MSE-1')
    busy = act(fresh_client, 'recall', held_id)           # a sale is in progress
    assert not busy['ok'] and 'Wireless Mouse' in busy['html']
    act(fresh_client, 'clear')
    back = act(fresh_client, 'recall', held_id)
    assert back['ok'] and 'Kingston' in back['html'] and 'No held orders' in back['held_html']


def test_unknown_action_and_access(shop, fresh_client, monkeypatch):
    login(fresh_client, 2)
    assert fresh_client.post('/m/pos/cart/explode').status_code == 404
    assert fresh_client.post('/m/pos/cart/recall/abc').status_code == 404
    import app.routes.mobile as mobile
    monkeypatch.setattr(mobile, 'load_config', lambda *a, **k: {'INVENTORY_ENABLED': True})   # POS off
    assert fresh_client.get('/m/pos').status_code == 403
    assert fresh_client.post('/m/pos/cart/add', data={'sku': 'RAM-16'}).status_code == 403
    assert fresh_client.get('/m/pos/checkout').status_code == 403


def test_cash_sale_end_to_end(shop, fresh_client):
    login(fresh_client, 2)
    assert fresh_client.get('/m/pos/checkout').headers['Location'].endswith('/m/pos')   # empty cart
    act(fresh_client, 'add', sku='RAM-16')
    act(fresh_client, 'add', sku='RAM-16')
    page = fresh_client.get('/m/pos/checkout').get_data(as_text=True)
    token = re.search(r'name="checkout_token" value="([^"]+)"', page).group(1)
    expected = re.search(r'name="expected_total" value="([^"]+)"', page).group(1)
    assert 'Complete sale' in page and 'Kingston 16GB SODIMM ×2' in page

    short = fresh_client.post('/m/pos/checkout', data={'payment_method': 'cash', 'tendered': '1.00',
                                                        'checkout_token': token, 'expected_total': expected})
    assert short.headers['Location'].endswith('/m/pos/checkout')
    assert 'Cash tendered must be at least' in fresh_client.get('/m/pos/checkout').get_data(as_text=True)

    page = fresh_client.get('/m/pos/checkout').get_data(as_text=True)
    token = re.search(r'name="checkout_token" value="([^"]+)"', page).group(1)
    done = fresh_client.post('/m/pos/checkout', data={'payment_method': 'cash', 'tendered': '200.00',
                                                       'checkout_token': token, 'expected_total': expected})
    receipt_url = done.headers['Location']
    assert '/m/pos/receipt/' in receipt_url
    receipt = fresh_client.get(receipt_url).get_data(as_text=True)
    assert 'completed!' in receipt and 'Kingston 16GB SODIMM ×2' in receipt and 'Change' in receipt
    assert qty(shop, shop.ids['ram']) == 3
    assert fresh_client.get('/m/pos/cart').get_json()['count'] == 0

    # the same token again records nothing more
    fresh_client.post('/m/pos/checkout', data={'payment_method': 'cash', 'tendered': '200.00',
                                                'checkout_token': token, 'expected_total': expected})
    assert qty(shop, shop.ids['ram']) == 3


def test_card_sale_and_missing_receipt(shop, fresh_client):
    login(fresh_client, 2)
    act(fresh_client, 'add', sku='MSE-1')
    page = fresh_client.get('/m/pos/checkout').get_data(as_text=True)
    token = re.search(r'name="checkout_token" value="([^"]+)"', page).group(1)
    expected = re.search(r'name="expected_total" value="([^"]+)"', page).group(1)
    done = fresh_client.post('/m/pos/checkout', data={'payment_method': 'card', 'checkout_token': token,
                                                       'expected_total': expected})
    assert '/m/pos/receipt/' in done.headers['Location']
    assert qty(shop, shop.ids['mouse']) == 0
    assert fresh_client.get('/m/pos/receipt/NOPE-1').headers['Location'].endswith('/m/pos')


def test_handheld_rings_into_the_registers_cart(shop, fresh_app):
    """Pair the phone to a register (as the desktop register's Pair a Device
    box does), then items the phone scans land in that register's cart."""
    register = login(fresh_app.test_client(), 1)
    phone = login(fresh_app.test_client(), 2)
    register.post('/pos/api/register/hello', json={'register_id': 'term_front', 'friendly_name': 'Front counter'})

    code = phone.post('/pos/api/pairing/request-code',
                      json={'peripheral_id': 'term_scan_phone', 'role': 'scanner'}).get_json()['pairing_code']
    assert register.post('/pos/api/register/claim-code', json={'code': code, 'register_id': 'term_front',
                                                               'friendly_name': 'Front counter'}).get_json()['success']
    status = phone.get('/pos/api/pairing/status?peripheral_id=term_scan_phone').get_json()
    assert status['paired'] and status['token']
    phone.post('/pos/api/pairing/ack', json={'peripheral_id': 'term_scan_phone', 'token': status['token']})
    assert phone.post('/pos/api/scanner/enter-slave', json={'token': status['token']}).get_json()['success']

    page = phone.get('/m/pos').get_data(as_text=True)
    assert 'm-reg-pill on' in page and 'Front counter' in page and 'Stop ringing into Front counter' in page
    assert act(phone, 'add', sku='RAM-16')['ok']
    desktop_cart = register.get('/pos/api/cart', headers={'X-Terminal-Id': 'term_front'}).get_json()
    assert desktop_cart['item_count'] == 1 and desktop_cart['subtotal'] == 54.99
    assert phone.get('/m/pos/cart').get_json()['linked'] == 'Front counter'

    phone.post('/pos/api/scanner/exit-slave', json={'token': status['token'], 'unpair': True})
    assert 'm-reg-pill on' not in phone.get('/m/pos').get_data(as_text=True)
