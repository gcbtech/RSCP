"""Phone UI, Receive (/m/receive...): scanning, acting on results, lists, returns."""
import pytest

from tests.conftest import login


@pytest.fixture
def seeded(fresh_app):
    """Packages and stock: one expected package that restocks an item, a
    priority one, a past-due one, and a stock item scannable by SKU."""
    from app.services.db import get_db_connection
    with fresh_app.app_context():
        conn = get_db_connection()
        conn.executemany(
            "INSERT INTO packages (tracking_number, item_name, quantity, date_expected, status, priority, sku) "
            "VALUES (?, ?, ?, date('now', 'localtime'), ?, ?, ?)",
            [('1ZEXPECTED01', 'USB-C Charger 65W', 2, 'expected', 0, 'CHG-65'),
             ('1ZPRIORITY01', 'Rush Order Laptop', 1, 'expected', 1, None),
             ('TBA123456789', 'Amazon Cable', 1, 'expected', 0, None)])
        conn.execute("INSERT INTO packages (tracking_number, item_name, quantity, date_expected, status) "
                     "VALUES ('1ZLATE000001', 'Late Mouse', 1, date('now', '-4 days'), 'past_due')")
        conn.execute("INSERT INTO inventory_items (name, sku, quantity, sell_price_cents) "
                     "VALUES ('USB-C Charger 65W', 'CHG-65', 14, 4999)")
        conn.commit()
        conn.close()
    return fresh_app


def db_row(app, sql, *args):
    from app.services.db import get_db_connection
    with app.app_context():
        conn = get_db_connection()
        try:
            return conn.execute(sql, args).fetchone()
        finally:
            conn.close()


def scan(client, code):
    resp = client.post('/m/receive/scan', json={'code': code})
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


def test_scan_screen_has_scanner_chips_and_counts(seeded, fresh_client):
    login(fresh_client, 2)
    html = fresh_client.get('/m/receive').get_data(as_text=True)
    assert 'id="mScanCapture"' in html and 'data-state="listening"' in html
    assert 'inputmode' not in html.split('id="mScanCapture"')[1].split('>')[0]   # set by scanner.js after focus
    assert 'Expected <b>0/3</b>' in html and 'Past due <b>1</b>' in html
    assert 'm/scanner.js' in html and 'm/receive.js' in html
    # link_item also answers GET /search: the sheet must post to its POST rule
    assert '"linkUrl": "/receiving/link_item"' in html


def test_receiving_a_package_offers_to_restock_its_item(seeded, fresh_client):
    login(fresh_client, 2)
    data = scan(fresh_client, '1ZEXPECTED01')
    assert data['kind'] == 'received' and data['feedback'] == 'ok'
    assert data['label'] == 'Received · 1 item'
    assert data['expected'] == '1/3'
    assert 'Matches <b>USB-C Charger 65W</b>' in data['html']
    assert 'data-action="mAddStock"' in data['html'] and 'data-qty="2"' in data['html']
    assert 'href="/m/stock/add' not in data['html']            # operators can't add items
    row = db_row(seeded, "SELECT status, date_scanned FROM packages WHERE tracking_number = '1ZEXPECTED01'")
    assert row['status'] == 'received' and row['date_scanned']

    again = scan(fresh_client, '1ZEXPECTED01')
    assert again['kind'] == 'duplicate' and again['feedback'] == 'warn'
    assert 'Nothing changed' in again['html']


def test_managers_get_a_new_item_link_back_to_the_phone(seeded, fresh_client):
    login(fresh_client, 1)
    html = scan(fresh_client, 'TBA123456789')['html']
    assert 'No stock item matches' in html
    assert 'href="/m/stock/add?tracking=TBA123456789' in html and 'next=/m/receive"' in html


def test_priority_unknown_and_stock_item_scans(seeded, fresh_client):
    login(fresh_client, 2)
    priority = scan(fresh_client, '1ZPRIORITY01')
    assert priority['kind'] == 'priority' and priority['feedback'] == 'priority'
    assert 'mAddStock' not in priority['html']

    unknown = scan(fresh_client, '1ZNOPE')
    assert unknown['kind'] == 'unknown' and unknown['feedback'] == 'bad'
    assert 'Not in manifest' in unknown['html']

    stock = scan(fresh_client, 'CHG-65')
    assert stock['kind'] == 'inventory' and stock['code'] == 'CHG-65'
    assert 'data-stock-item=' in stock['html'] and 'data-action="mAdjust"' in stock['html']
    assert '$49.99' in stock['html']


def test_blank_scan_is_rejected(seeded, fresh_client):
    login(fresh_client, 2)
    assert fresh_client.post('/m/receive/scan', json={'code': '  '}).status_code == 400


def test_add_stock_adds_units_and_validates(seeded, fresh_client):
    login(fresh_client, 2)
    item_id = db_row(seeded, "SELECT id FROM inventory_items WHERE sku = 'CHG-65'")['id']
    resp = fresh_client.post('/m/receive/add-stock', json={'item_id': item_id, 'qty': 2, 'tracking': '1ZEXPECTED01'})
    assert resp.get_json() == {'ok': True, 'new_quantity': 16, 'name': 'USB-C Charger 65W'}
    assert db_row(seeded, 'SELECT quantity FROM inventory_items WHERE id = ?', item_id)['quantity'] == 16

    assert fresh_client.post('/m/receive/add-stock', json={'item_id': item_id, 'qty': 0}).status_code == 400
    assert fresh_client.post('/m/receive/add-stock', json={'item_id': item_id, 'qty': 'x'}).status_code == 400
    assert fresh_client.post('/m/receive/add-stock', json={'item_id': 99999, 'qty': 1}).status_code == 404


def test_add_stock_needs_the_stock_module(seeded, fresh_client, monkeypatch):
    import app.routes.mobile as mobile
    monkeypatch.setattr(mobile, 'load_config', lambda *a, **k: {})   # inventory off
    login(fresh_client, 2)
    assert fresh_client.post('/m/receive/add-stock', json={'item_id': 1, 'qty': 1}).status_code == 403
    assert 'mAddStock' not in scan(fresh_client, '1ZEXPECTED01')['html']


def test_expected_and_past_due_lists(seeded, fresh_client):
    login(fresh_client, 2)
    scan(fresh_client, '1ZEXPECTED01')
    expected = fresh_client.get('/m/receive/expected').get_data(as_text=True)
    assert '1 of 3 scanned today' in expected
    assert 'UPS · 1 to go' in expected and 'Amazon · 1 to go' in expected
    assert '1ZEXPECTED01' not in expected
    past_due = fresh_client.get('/m/receive/past-due').get_data(as_text=True)
    assert 'Late Mouse' in past_due and '1ZLATE000001' in past_due


def test_start_a_return_then_mark_it_refunded(seeded, fresh_client):
    login(fresh_client, 2)
    found = fresh_client.get('/m/receive/return?q=Late').get_data(as_text=True)
    assert 'track=1ZLATE000001' in found
    form = fresh_client.get('/m/receive/return?track=1ZLATE000001').get_data(as_text=True)
    assert 'name="original_tracking" value="1ZLATE000001"' in form

    resp = fresh_client.post('/m/receive/return', data={'original_tracking': '1ZLATE000001',
                                                         'reason': 'Wrong Item', 'return_tracking': '1ZBACK01'})
    assert resp.status_code == 302 and resp.headers['Location'].endswith('/m/receive/returns')
    assert db_row(seeded, "SELECT status FROM packages WHERE tracking_number = '1ZLATE000001'")['status'] == 'return_pending'
    returns = fresh_client.get('/m/receive/returns').get_data(as_text=True)
    assert 'Return started: 1ZLATE000001' in returns          # flash
    assert 'Wrong Item' in returns and '1ZBACK01' in returns

    fresh_client.post('/m/receive/returns/refunded', data={'tracking': '1ZLATE000001'})
    row = db_row(seeded, "SELECT status, refund_date FROM packages WHERE tracking_number = '1ZLATE000001'")
    assert row['status'] == 'refunded' and row['refund_date']


def test_return_needs_a_known_reason(seeded, fresh_client):
    login(fresh_client, 2)
    resp = fresh_client.post('/m/receive/return', data={'original_tracking': '1ZLATE000001', 'reason': 'Bogus'})
    assert resp.status_code == 302
    assert db_row(seeded, "SELECT status FROM packages WHERE tracking_number = '1ZLATE000001'")['status'] == 'past_due'


def test_history_lists_actions_with_search_and_period(seeded, fresh_client):
    login(fresh_client, 2)
    scan(fresh_client, '1ZEXPECTED01')
    html = fresh_client.get('/m/receive/history?period=today').get_data(as_text=True)
    assert 'USB-C Charger 65W' in html and 'm-tone-ok">Received</span> ×2' in html
    assert 'cashier' in html
    assert 'Nothing found' in fresh_client.get('/m/receive/history?q=zzz').get_data(as_text=True)


def test_receive_is_forbidden_without_the_permission(seeded, fresh_client, monkeypatch):
    import app.routes.mobile as mobile
    monkeypatch.setattr(mobile, 'has_permission', lambda user, perm: perm != 'receiving.view')
    login(fresh_client, 2)
    for path in ('/m/receive', '/m/receive/expected', '/m/receive/returns', '/m/receive/history'):
        assert fresh_client.get(path).status_code == 403
    assert fresh_client.post('/m/receive/scan', json={'code': '1ZEXPECTED01'}).status_code == 403
