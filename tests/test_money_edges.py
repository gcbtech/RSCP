"""Money at the edges after the cents migration: what people and other
programs see stays in dollars (public API, page JSON, CSV exports, forms),
dollar-era carts are upgraded, and deleting things that sales point at gives
a message instead of an IntegrityError."""
import csv
import io
import json
from datetime import date

from tests.conftest import login


def _db(app):
    from app.services.db import get_db_connection
    return get_db_connection(app.config['DATABASE'])


def _item(app, sku, cents, qty=5, **extra):
    conn = _db(app)
    cols = ['sku', 'name', 'sell_price_cents', 'quantity', 'location_aisle'] + list(extra)
    vals = [sku, f'{sku} item', cents, qty, 'Shop'] + list(extra.values())
    item_id = conn.execute(f'INSERT INTO inventory_items ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})',
                           vals).lastrowid
    conn.commit()
    conn.close()
    return item_id


def _sale(app, item_id, sku='SOLD', total=1070):
    conn = _db(app)
    order_id = conn.execute(
        "INSERT INTO pos_orders (order_number, status, subtotal_cents, tax_rate, tax_amount_cents, total_cents, "
        "payment_method, payment_details, operator_id) VALUES ('POS-EDGE-1', 'completed', 1000, 0.07, 70, ?, "
        "'card', '{}', 2)", (total,)).lastrowid
    conn.execute("INSERT INTO pos_order_items (order_id, inventory_item_id, sku, name, quantity, unit_price_cents, "
                 "discount_type, discount_percent, line_total_cents) VALUES (?, ?, ?, 'x', 1, 1000, 'percent', 0, 1000)",
                 (order_id, item_id, sku))
    conn.commit()
    conn.close()
    return order_id


def test_public_api_prices_stay_dollars(fresh_app, fresh_client, monkeypatch):
    import app.routes.public_api as public_api
    monkeypatch.setattr(public_api, 'ALLOWED_AISLES', ['Shop'])
    monkeypatch.setattr(public_api, 'load_config', lambda: {'PUBLIC_API_KEY': 'k'})
    _item(fresh_app, 'P1', 1999)
    _item(fresh_app, 'P2', 1000)
    _item(fresh_app, 'P3', None)
    _item(fresh_app, 'P4', 2500, sale_enabled=1, sale_price_cents=1750)
    resp = fresh_client.get('/api/public/inventory', headers={'X-API-Key': 'k'})
    assert resp.status_code == 200
    prices = {i['sku']: i['price'] for i in resp.get_json()}
    assert prices == {'P1': 19.99, 'P2': 10.0, 'P3': 0, 'P4': 17.5}
    raw = resp.get_data(as_text=True)
    assert '"price":19.99' in raw.replace(' ', '') and '"price":10.0' in raw.replace(' ', '')
    assert '"price":0,' in raw.replace(' ', '') or '"price":0}' in raw.replace(' ', '')
    assert 'cents' not in raw


def test_page_json_stays_dollars(fresh_app, fresh_client):
    _item(fresh_app, 'J1', 1999)
    login(fresh_client, 2)
    item = fresh_client.get('/pos/api/item/J1').get_json()
    assert item['sell_price'] == 19.99 and item['current_price'] == 19.99 and 'sell_price_cents' not in item
    search = fresh_client.get('/pos/api/search?q=J1').get_json()['items']
    assert search[0]['sell_price'] == 19.99
    fresh_client.post('/pos/cart/add', data={'sku': 'J1', 'quantity': '2'})
    cart = fresh_client.get('/pos/api/cart').get_json()
    assert (cart['subtotal'], cart['total'], cart['items'][0]['unit_price'], cart['items'][0]['line_total']) == \
        (39.98, 39.98, 19.99, 39.98)


def test_dollar_era_session_cart_is_upgraded_and_sells(fresh_app, fresh_client):
    item_id = _item(fresh_app, 'OLDCART', 1250)
    login(fresh_client, 2)
    with fresh_client.session_transaction() as sess:   # as a 2.x session cookie held it
        sess['pos_cart'] = {'items': [{'inventory_item_id': item_id, 'sku': 'OLDCART', 'name': 'Old cart line',
                                       'quantity': 2, 'unit_price': 12.5, 'discount_amount': 10,
                                       'discount_type': 'percent', 'line_total': 22.5}],
                            'discount_amount': 2.5, 'discount_type': 'fixed', 'discount_reason': 'old'}
    page = fresh_client.get('/pos/').get_data(as_text=True)
    assert '$22.50' in page and '$20.00' in page          # line, and total after the $2.50 order discount
    fresh_client.post('/pos/checkout/process', data={'payment_method': 'cash', 'tendered': '20'})
    conn = _db(fresh_app)
    order = conn.execute('SELECT subtotal_cents, discount_amount_cents, total_cents FROM pos_orders').fetchone()
    line = conn.execute('SELECT unit_price_cents, discount_type, discount_percent, line_total_cents '
                        'FROM pos_order_items').fetchone()
    conn.close()
    assert tuple(order) == (2250, 250, 2000)
    assert tuple(line) == (1250, 'percent', 10.0, 2250)


def test_csv_exports_keep_their_dollar_numbers(fresh_app, fresh_client):
    item_id = _item(fresh_app, 'CSV1', 1000)
    _sale(fresh_app, item_id, 'CSV1')
    login(fresh_client, 1)
    today = date.today().isoformat()
    rows = list(csv.reader(io.StringIO(fresh_client.get(
        f'/pos/management/export?type=orders&date_from=2000-01-01&date_to=2999-12-31').get_data(as_text=True))))
    assert rows[1][3:7] == ['10.0', '0.7', '0.0', '10.7']
    rows = list(csv.reader(io.StringIO(fresh_client.get(
        f'/pos/management/export?type=items&date_from=2000-01-01&date_to=2999-12-31').get_data(as_text=True))))
    assert rows[1][5:] == ['10.0', '0.0', '10.0']
    rows = list(csv.reader(io.StringIO(fresh_client.get('/inventory/export_csv').get_data(as_text=True))))
    assert rows[1][5:7] == ['', '10.0']


def test_money_settings_are_entered_in_dollars_and_stored_in_cents(fresh_app, fresh_client):
    from app.services import settings
    login(fresh_client, 1)
    fresh_client.post('/admin/settings/save', data={'group': 'pos', 'CASH_DISCOUNT_TYPE': 'fixed',
                                                     'CASH_DISCOUNT_AMOUNT': '1.25'})
    fresh_client.post('/admin/settings/save', data={'group': 'pos_approval', 'POS_MAX_DISCOUNT_AMOUNT': '$25.50'})
    with fresh_app.app_context():
        assert settings.get('CASH_DISCOUNT_CENTS') == 125
        assert settings.get('POS_MAX_DISCOUNT_CENTS') == 2550
    page = fresh_client.get('/pos/management/settings').get_data(as_text=True)
    assert 'value="1.25"' in page
    fresh_client.post('/admin/settings/save', data={'group': 'pos', 'CASH_DISCOUNT_TYPE': 'percent',
                                                     'CASH_DISCOUNT_AMOUNT': '3.5'})
    with fresh_app.app_context():
        assert settings.get('CASH_DISCOUNT_PERCENT') == 3.5 and settings.get('CASH_DISCOUNT_CENTS') == 125
    fresh_client.post('/admin/settings/save', data={'group': 'pos_approval', 'POS_MAX_DISCOUNT_AMOUNT': 'lots'})
    with fresh_client.session_transaction() as sess:
        flashes = [m for _c, m in sess.get('_flashes', [])]
    assert any('expected a dollar amount' in m for m in flashes), flashes
    with fresh_app.app_context():
        assert settings.get('POS_MAX_DISCOUNT_CENTS') == 2550


def test_item_with_sales_is_kept_with_a_message(fresh_app, fresh_client):
    sold = _item(fresh_app, 'SOLD', 1000)
    _sale(fresh_app, sold)
    unsold = _item(fresh_app, 'UNSOLD', 500)
    login(fresh_client, 1)
    resp = fresh_client.post(f'/inventory/delete/{sold}', follow_redirects=True)
    assert 'has sales history' in resp.get_data(as_text=True)
    fresh_client.post(f'/inventory/delete/{unsold}')
    conn = _db(fresh_app)
    left = {r[0] for r in conn.execute('SELECT sku FROM inventory_items')}
    conn.close()
    assert left == {'SOLD'}


def test_user_with_sales_is_kept_and_timeclock_rows_go_with_a_deleted_user(fresh_app, fresh_client):
    _sale(fresh_app, _item(fresh_app, 'U1', 100))                 # rung up by user 2 (cashier)
    conn = _db(fresh_app)
    conn.execute("INSERT INTO users (id, username, password_hash, roles) VALUES (7, 'temp', 'x', '[\"operator\"]')")
    conn.execute("INSERT INTO time_entries (user_id, clock_in, clock_out) VALUES (7, '2026-01-01 15:00:00', "
                 "'2026-01-01 20:00:00')")
    conn.commit()
    conn.close()
    login(fresh_client, 1)
    resp = fresh_client.post('/admin/delete_user/cashier', follow_redirects=True)
    assert 'has sales history' in resp.get_data(as_text=True)
    resp = fresh_client.post('/admin/delete_user/temp', follow_redirects=True)
    assert 'User temp deleted, with their 1 timeclock record(s).' in resp.get_data(as_text=True)
    conn = _db(fresh_app)
    users = {r[0] for r in conn.execute('SELECT username FROM users')}
    entries = conn.execute('SELECT COUNT(*) FROM time_entries').fetchone()[0]
    conn.close()
    assert 'cashier' in users and 'temp' not in users and entries == 0


def test_redeemed_coupon_is_kept_with_a_message(fresh_app, fresh_client):
    order_id = _sale(fresh_app, _item(fresh_app, 'C1', 100))
    conn = _db(fresh_app)
    used = conn.execute("INSERT INTO pos_coupons (name, code, coupon_type, discount_type, discount_cents) "
                        "VALUES ('Used', 'USED', 'generic', 'order_dollar', 100)").lastrowid
    unused = conn.execute("INSERT INTO pos_coupons (name, code, coupon_type, discount_type, discount_percent) "
                          "VALUES ('Unused', 'UNUSED', 'generic', 'order_percent', 10)").lastrowid
    conn.execute('INSERT INTO pos_coupon_redemptions (coupon_id, order_id, discount_applied_cents, redeemed_by) '
                 'VALUES (?, ?, 100, 2)', (used, order_id))
    conn.commit()
    conn.close()
    login(fresh_client, 3)
    resp = fresh_client.post(f'/pos/coupons/{used}/delete', follow_redirects=True)
    assert 'Deactivate it instead' in resp.get_data(as_text=True)
    fresh_client.post(f'/pos/coupons/{unused}/delete')
    conn = _db(fresh_app)
    codes = {r[0] for r in conn.execute('SELECT code FROM pos_coupons')}
    conn.close()
    assert codes == {'USED'}


def test_coupon_form_round_trips_dollars_and_percents(fresh_app, fresh_client):
    login(fresh_client, 3)
    fresh_client.post('/pos/coupons/create', data={'name': 'Five off', 'coupon_type': 'generic', 'code': 'FIVE',
                                                    'discount_type': 'order_dollar', 'discount_value': '5.25',
                                                    'min_purchase': '$20'})
    fresh_client.post('/pos/coupons/create', data={'name': 'Pct', 'coupon_type': 'generic', 'code': 'PCT',
                                                    'discount_type': 'order_percent', 'discount_value': '12.5'})
    conn = _db(fresh_app)
    rows = {r['code']: (r['discount_cents'], r['discount_percent'], r['min_purchase_cents'])
            for r in conn.execute('SELECT * FROM pos_coupons')}
    five = conn.execute("SELECT id FROM pos_coupons WHERE code = 'FIVE'").fetchone()[0]
    conn.close()
    assert rows == {'FIVE': (525, None, 2000), 'PCT': (None, 12.5, None)}
    page = fresh_client.get(f'/pos/coupons/{five}/edit').get_data(as_text=True)
    assert 'value="5.25"' in page and 'value="20.0"' in page
    listing = fresh_client.get('/pos/coupons').get_data(as_text=True)
    assert '$5.25 off' in listing and '12% off' in listing


def test_audit_log_keeps_dollars(fresh_app):
    """pos_audit_log details are a human-readable record: dollar totals, as before."""
    from types import SimpleNamespace
    from app.services import pos_sales
    item_id = _item(fresh_app, 'AUD', 1000)
    order_id = _sale(fresh_app, item_id, 'AUD')
    conn = _db(fresh_app)
    line = conn.execute('SELECT id FROM pos_order_items').fetchone()[0]
    conn.isolation_level = None
    pos_sales.edit_sale(conn, order_id, [pos_sales.SaleLineEdit(str(line), '2', '10.00', 'x')], [],
                        SimpleNamespace(id=1, username='admin'))
    details = json.loads(conn.execute("SELECT details FROM pos_audit_log").fetchone()[0])
    conn.close()
    assert details['old_total'] == 10.7 and details['new_total'] == 21.4
