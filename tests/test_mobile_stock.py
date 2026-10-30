"""Phone UI, Stock (/m/stock...): lookup, item page, lists, audits, quick add."""
import re

import pytest

from tests.conftest import login


@pytest.fixture
def seeded(fresh_app):
    """Items: one with a UPC and a price, one out of stock, one low against its
    own threshold, one low against the global threshold (5), plenty in stock."""
    from app.services import inventory
    from app.services.db import get_db_connection
    with fresh_app.app_context():
        conn = get_db_connection()
        ids = {}
        for key, fields in {
            'charger': {'sku': 'CHG-65', 'name': 'USB-C Charger 65W', 'quantity': 14, 'sell_price_cents': 4999,
                        'location_area': 'Back room', 'location_shelf': 'B3', 'upc': '012345678905'},
            'mouse': {'sku': 'MSE-1', 'name': 'Wireless Mouse', 'quantity': 0, 'location_area': 'Retail'},
            'ram': {'sku': 'RAM-16', 'name': 'Kingston 16GB SODIMM', 'quantity': 2, 'alert_threshold': 3,
                    'location_area': 'Retail'},
            'cable': {'sku': 'CBL-HDMI', 'name': 'HDMI Cable', 'quantity': 4, 'location_area': 'Retail'},
            'dock': {'sku': 'DCK-1', 'name': 'USB-C Dock', 'quantity': 30, 'location_area': 'Retail'},
        }.items():
            ids[key] = inventory.create_item(conn, fields, user='admin')
        conn.close()
    fresh_app.item_ids = ids
    return fresh_app


def db_one(app, sql, *args):
    from app.services.db import get_db_connection
    with app.app_context():
        conn = get_db_connection()
        try:
            return conn.execute(sql, args).fetchone()
        finally:
            conn.close()


def find(client, code):
    resp = client.post('/m/stock/find', json={'code': code})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


# ---------------------------------------------------------------- lookup

def test_lookup_screen_has_scanner_and_chips(seeded, fresh_client):
    login(fresh_client, 2)
    html = fresh_client.get('/m/stock').get_data(as_text=True)
    assert 'id="mScanCapture"' in html and 'data-state="listening"' in html
    assert 'Low stock <b>3</b>' in html               # out + own threshold + global threshold
    assert 'm/stockcard.js' in html and '"adjustUrl": "/inventory/api/quick_adjust/0"' in html
    assert '/m/stock/add' not in html                 # operators can't add


def test_find_by_sku_and_upc_shows_the_card(seeded, fresh_client):
    login(fresh_client, 2)
    for code in ('CHG-65', '012345678905'):
        data = find(fresh_client, code)
        assert data['kind'] == 'item' and data['feedback'] == 'ok'
        html = data['html']
        assert f'data-stock-item="{seeded.item_ids["charger"]}"' in html
        assert 'data-qty>14<' in html and '$49.99' in html and 'Back room / B3' in html
        assert '012345678905' in html
        assert 'Recent activity' in html and 'Initial' in html     # the opening movement
        assert 'Edit on the desktop page' not in html              # operator


def test_find_by_name_lists_or_opens(seeded, fresh_client):
    login(fresh_client, 1)
    many = find(fresh_client, 'USB-C')
    assert many['kind'] == 'list' and '2 items match' in many['html']
    assert f'/m/stock/item/{seeded.item_ids["dock"]}' in many['html']
    one = find(fresh_client, 'kingston')
    assert one['kind'] == 'item' and 'low at 3' in one['html'] and 'Edit on the desktop page' in one['html']
    none = find(fresh_client, 'zzz-nothing')
    assert none['kind'] == 'none' and none['feedback'] == 'bad' and 'Nothing matches' in none['html']
    assert fresh_client.post('/m/stock/find', json={'code': ''}).status_code == 400


def test_item_page_and_missing_item(seeded, fresh_client):
    login(fresh_client, 2)
    html = fresh_client.get(f'/m/stock/item/{seeded.item_ids["mouse"]}').get_data(as_text=True)
    assert 'Wireless Mouse' in html and 'data-qty>0<' in html and 'id="mCountSheet"' in html
    assert fresh_client.get('/m/stock/item/99999').status_code == 404


# ---------------------------------------------------------------- lists

def test_items_list_search_and_pages(seeded, fresh_client, monkeypatch):
    import app.routes.mobile.stock as stock
    login(fresh_client, 2)
    html = fresh_client.get('/m/stock/items?q=retail').get_data(as_text=True)
    assert '4 items' in html and 'HDMI Cable' in html and 'USB-C Charger' not in html
    monkeypatch.setattr(stock, 'ITEMS_PER_PAGE', 2)
    page2 = fresh_client.get('/m/stock/items?page=2').get_data(as_text=True)
    assert 'page 2 of 3' in page2 and 'Previous' in page2 and 'Next' in page2


def test_low_stock_matches_home(seeded, fresh_client):
    login(fresh_client, 2)
    low = fresh_client.get('/m/stock/low').get_data(as_text=True)
    main = low.split('<main', 1)[1].split('</main>', 1)[0]
    names = re.findall(r'<span class="m-t1">([^<]+)</span>', main)
    assert names == ['Wireless Mouse', 'Kingston 16GB SODIMM', 'HDMI Cable']
    home = fresh_client.get('/m/').get_data(as_text=True)
    assert '2 low · 1 out of stock' in home and 'href="/m/stock/low"' in home


# ---------------------------------------------------------------- audits

def test_shelf_audit_start_count_finish_report(seeded, fresh_client):
    login(fresh_client, 2)
    resp = fresh_client.post('/m/stock/audit/start', data={'mode': 'shelf'})
    sid = int(resp.headers['Location'].rsplit('/', 1)[1])
    live = fresh_client.get(f'/m/stock/audit/{sid}').get_data(as_text=True)
    assert 'Shelf audit' in live and '0 counted · 0 off' in live and 'm/audit.js' in live
    # one audit at a time: the same mode resumes, the other mode is refused
    assert fresh_client.post('/m/stock/audit/start', data={'mode': 'shelf'}).headers['Location'].endswith(f'/m/stock/audit/{sid}')
    assert fresh_client.post('/m/stock/audit/start', data={'mode': 'item'}).headers['Location'].endswith('/m/stock/audit')

    assert fresh_client.post(f'/m/stock/audit/{sid}/scan', json={'code': 'NOPE'}).status_code == 404
    item = fresh_client.post(f'/m/stock/audit/{sid}/scan', json={'code': '012345678905'}).get_json()
    assert item['item']['sku'] == 'CHG-65' and item['item']['quantity'] == 14 and item['prev_count'] is None

    ok = fresh_client.post(f'/m/stock/audit/{sid}/count', json={'item_id': item['item']['id'], 'count': 14}).get_json()
    assert ok['summary'] == {'counted': 1, 'off': 0} and 'Matched · 14' in ok['recent_html']
    off = fresh_client.post(f'/m/stock/audit/{sid}/count',
                            json={'item_id': seeded.item_ids['cable'], 'count': 1}).get_json()
    assert off['summary'] == {'counted': 2, 'off': 1} and 'Counted 1 · system had 4' in off['recent_html']
    assert db_one(seeded, 'SELECT quantity FROM inventory_items WHERE id = ?', seeded.item_ids['cable'])[0] == 1
    again = fresh_client.post(f'/m/stock/audit/{sid}/scan', json={'code': 'CBL-HDMI'}).get_json()
    assert again['prev_count'] == 1
    assert fresh_client.post(f'/m/stock/audit/{sid}/count', json={'item_id': 1, 'count': -1}).status_code == 400

    report = fresh_client.post(f'/m/stock/audit/{sid}/finish', follow_redirects=True).get_data(as_text=True)
    assert "Didn't match · 1" in report and 'HDMI Cable' in report and 'system 4, counted 1' in report
    assert db_one(seeded, 'SELECT status FROM audit_sessions WHERE id = ?', sid)[0] == 'completed'
    # a finished audit can't take more counts and its live page goes back to the list
    assert fresh_client.post(f'/m/stock/audit/{sid}/count', json={'item_id': 1, 'count': 1}).status_code == 409
    assert fresh_client.get(f'/m/stock/audit/{sid}').headers['Location'].endswith('/m/stock/audit')
    dashboard = fresh_client.get('/m/stock/audit').get_data(as_text=True)
    assert f'/m/stock/audit/{sid}/report' in dashboard and 'Start an audit' in dashboard


def test_audit_start_rejects_unknown_mode(seeded, fresh_client):
    login(fresh_client, 2)
    assert fresh_client.post('/m/stock/audit/start', data={'mode': 'bogus'}).status_code == 400


def test_home_resumes_an_audit_on_the_phone(seeded, fresh_client):
    login(fresh_client, 2)
    sid = int(fresh_client.post('/m/stock/audit/start', data={'mode': 'item'}).headers['Location'].rsplit('/', 1)[1])
    home = fresh_client.get('/m/').get_data(as_text=True)
    assert f'href="/m/stock/audit/{sid}"' in home and 'Item audit in progress' in home


# ---------------------------------------------------------------- quick add

def test_add_item_is_for_managers(seeded, fresh_client):
    login(fresh_client, 2)
    assert fresh_client.get('/m/stock/add').status_code == 403
    assert fresh_client.post('/m/stock/add', data={'name': 'X', 'area': 'Retail'}).status_code == 403


def test_add_item_validates(seeded, fresh_client):
    login(fresh_client, 1)
    resp = fresh_client.post('/m/stock/add', data={'name': '', 'qty': '-2', 'price': 'abc'})
    assert resp.status_code == 400
    html = resp.get_data(as_text=True)
    for message in ('Give the item a name.', 'Use a whole number, 0 or more.', 'Use a price like 19.99.',
                    'Pick an area or type a shelf.'):
        assert message in html
    assert 'aria-invalid="true"' in html
    dup = fresh_client.post('/m/stock/add', data={'name': 'Dupe', 'area': 'Retail', 'sku': 'CHG-65'})
    assert dup.status_code == 400 and 'The SKU CHG-65 is already in use.' in dup.get_data(as_text=True)


def test_add_item_from_a_received_package(seeded, fresh_client):
    login(fresh_client, 1)
    form = fresh_client.get('/m/stock/add?tracking=1ZPKG&qty=2&name=Dell+Charger&next=/m/receive').get_data(as_text=True)
    assert 'value="Dell Charger"' in form and 'value="2"' in form and 'From package' in form
    resp = fresh_client.post('/m/stock/add', data={'name': 'Dell Charger', 'qty': '2', 'price': '39.50',
                                                   'area': 'Retail', 'shelf': 'A1', 'upc': '999',
                                                   'category': 'ASC', 'tracking': '1ZPKG', 'next': '/m/receive'})
    assert resp.status_code == 302 and resp.headers['Location'].endswith('/m/receive')
    row = db_one(seeded, "SELECT id, sku, quantity, sell_price_cents, location_shelf FROM inventory_items WHERE name = 'Dell Charger'")
    assert row['sku'].startswith('RSCP-ASC-') and row['quantity'] == 2 and row['sell_price_cents'] == 3950
    move = db_one(seeded, 'SELECT reason, note FROM inventory_movements WHERE item_id = ?', row['id'])
    assert move['reason'] == 'initial' and move['note'] == '1ZPKG'


def test_add_item_ignores_an_outside_next(seeded, fresh_client):
    login(fresh_client, 1)
    resp = fresh_client.post('/m/stock/add', data={'name': 'Safe', 'area': 'Retail', 'next': 'https://evil.example/'})
    assert '/m/stock/item/' in resp.headers['Location']
