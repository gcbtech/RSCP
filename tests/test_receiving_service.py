"""Unit tests for app.services.receiving (scan-in, returns, refunds, admin edits)."""
import datetime

import pytest

from app.services import inventory as inv
from app.services import receiving as rcv
from app.services.db import get_db_connection


@pytest.fixture
def conn(fresh_app):
    c = get_db_connection(fresh_app.config['DATABASE'])
    yield c
    c.close()


def _pkg(conn, tracking, name='Thing', status='expected', qty=1, priority=0, sku=None, asin=None):
    cur = conn.execute(
        "INSERT INTO packages (tracking_number, item_name, status, quantity, priority, sku, asin, date_expected) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, date('now'))", (tracking, name, status, qty, priority, sku, asin))
    conn.commit()
    return cur.lastrowid


def _status(conn, tracking):
    return [r[0] for r in conn.execute('SELECT status FROM packages WHERE tracking_number = ? ORDER BY id',
                                       (tracking,))]


def _history(conn, action=None):
    q = 'SELECT action, user_id, details FROM history'
    if action:
        return [tuple(r) for r in conn.execute(q + ' WHERE action = ?', (action,))]
    return [tuple(r) for r in conn.execute(q)]


# --- scan lookup ---------------------------------------------------------

def test_find_scan_packages_exact_is_case_insensitive(conn):
    _pkg(conn, '1ZABC123')
    tracking, rows = rcv.find_scan_packages(conn, ' "1zabc123" ')
    assert tracking == '1ZABC123' and len(rows) == 1


def test_find_scan_packages_long_barcode_suffix(conn):
    _pkg(conn, '7712345678', name='A')
    _pkg(conn, '7712345678', name='B')
    tracking, rows = rcv.find_scan_packages(conn, '9622001900007712345678')
    assert tracking == '7712345678' and {r['item_name'] for r in rows} == {'A', 'B'}
    assert rcv.find_scan_packages(conn, 'NOPE') == ('NOPE', [])


# --- receiving -----------------------------------------------------------

def test_receive_scan_receives_all_rows_then_duplicate(conn):
    _pkg(conn, 'TRK1', name='A', qty=2)
    _pkg(conn, 'TRK1', name='B')
    result = rcv.receive_scan(conn, 'TRK1', 'admin')
    assert result.kind == 'received' and result.tracking == 'TRK1'
    assert [i['name'] for i in result.items] == ['A', 'B'] and result.items[0]['qty'] == 2
    assert _status(conn, 'TRK1') == ['received', 'received']
    assert _history(conn, 'received') == [('received', 1, 'Qty: 2'), ('received', 1, 'Qty: 1')]
    assert not conn.in_transaction

    again = rcv.receive_scan(conn, 'TRK1', 'admin')
    assert again.kind == 'duplicate' and len(again.items) == 2
    assert len(_history(conn, 'received')) == 2


def test_receive_scan_priority_and_closed_status(conn):
    _pkg(conn, 'PRI1', priority=1)
    assert rcv.receive_scan(conn, 'PRI1', 'admin').kind == 'priority'
    _pkg(conn, 'RET1', status='return_pending')
    rcv.receive_scan(conn, 'RET1', 'admin')
    assert _status(conn, 'RET1') == ['return_pending']  # stamped, not reopened
    assert conn.execute("SELECT date_scanned FROM packages WHERE tracking_number = 'RET1'").fetchone()[0]


def test_receive_scan_unknown_and_inventory(conn):
    inv.create_item(conn, {'sku': 'INV-1', 'name': 'Shelf Item', 'quantity': 3,
                           'location_area': 'A', 'location_bin': '7', 'upc': '0001'})
    assert rcv.receive_scan(conn, 'GHOST', 'admin').kind == 'unknown'
    assert rcv.receive_scan(conn, '0001', 'admin').kind == 'unknown'  # inventory disabled
    hit = rcv.receive_scan(conn, '0001', 'admin', inventory_enabled=True)
    assert hit.kind == 'inventory'
    assert hit.inventory_item['sku'] == 'INV-1' and hit.inventory_item['location'] == 'A / 7'
    assert conn.execute('SELECT COUNT(*) FROM packages').fetchone()[0] == 0


def test_receive_scan_suggests_inventory_match(conn):
    inv.create_item(conn, {'sku': 'INV-9', 'name': 'Blue Widget', 'location_area': 'A'})
    _pkg(conn, 'M1', name='Unrelated name', sku='INV-9')
    assert rcv.receive_scan(conn, 'M1', 'admin', inventory_enabled=True).inventory_match['sku'] == 'INV-9'
    _pkg(conn, 'M2', name='blue widget, 2 pack')
    conn.execute("INSERT INTO product_mappings (package_name, inventory_sku) VALUES ('Mapped', 'INV-9')")
    conn.commit()
    _pkg(conn, 'M3', name='Mapped')
    assert rcv.receive_scan(conn, 'M3', 'admin', inventory_enabled=True).inventory_match['sku'] == 'INV-9'
    assert rcv.receive_scan(conn, 'M2', 'admin').inventory_match is None  # inventory disabled


def test_receive_packages_unknown_tracking_creates_package(conn):
    conn.execute("INSERT INTO product_mappings (package_name, inventory_sku) VALUES ('Walk-in', 'SKU-W')")
    conn.commit()
    assert rcv.receive_packages(conn, 'NEW1', 'Walk-in', '3', 'admin') == 1
    row = conn.execute("SELECT status, source, quantity, sku, date_scanned FROM packages WHERE tracking_number = 'NEW1'").fetchone()
    assert (row['status'], row['source'], row['quantity'], row['sku']) == ('received', 'scan', 3, 'SKU-W')
    assert row['date_scanned']
    assert _history(conn, 'received') == [('received', 1, 'Qty: 3')]


def test_link_package_to_item_restocks_received_units(conn):
    item_id = inv.create_item(conn, {'sku': 'L-1', 'name': 'Linked', 'quantity': 1, 'location_area': 'A'})
    _pkg(conn, 'LNK', name='Box of things', qty=4)
    assert rcv.link_package_to_item(conn, 'LNK', 'L-1', 'admin', True, 'Box of things') == 0  # not received yet
    rcv.receive_scan(conn, 'LNK', 'admin')
    assert rcv.link_package_to_item(conn, 'LNK', 'L-1', 'admin') == 4
    assert inv.get_item(conn, item_id)['quantity'] == 5
    tx = conn.execute('SELECT delta, reason, user, note, ref_type, ref_id FROM inventory_movements '
                      'ORDER BY id DESC LIMIT 1').fetchone()
    pkg_id = conn.execute("SELECT MIN(id) FROM packages WHERE tracking_number = 'LNK'").fetchone()[0]
    assert tuple(tx) == (4, 'receive', 'admin', 'LNK', 'package', pkg_id)
    assert conn.execute("SELECT inventory_sku FROM product_mappings WHERE package_name = 'Box of things'").fetchone()[0] == 'L-1'
    assert conn.execute("SELECT sku FROM packages WHERE tracking_number = 'LNK'").fetchone()[0] == 'L-1'


# --- returns / refunds / status -----------------------------------------

def test_initiate_return_and_refund(conn):
    _pkg(conn, 'R1', status='received')
    rcv.initiate_return(conn, 'R1', 'RET-TRACK', 'Broken', 'admin')
    assert _status(conn, 'R1') == ['return_pending']
    assert _history(conn, 'return_initiated') == [('return_initiated', 1, 'Reason: Broken, New Track: RET-TRACK')]
    rcv.mark_refunded(conn, 'R1', 'admin', today='2026-10-17')
    row = conn.execute("SELECT status, refund_date FROM packages WHERE tracking_number = 'R1'").fetchone()
    assert tuple(row) == ('refunded', '2026-10-17')
    assert _history(conn, 'refunded') == [('refunded', 1, None)]


def test_set_status_variants(conn):
    _pkg(conn, 'S1')
    rcv.set_status(conn, 'S1', 'received')
    row = conn.execute("SELECT status, date_scanned FROM packages WHERE tracking_number = 'S1'").fetchone()
    assert row['status'] == 'received' and row['date_scanned']
    rcv.set_status(conn, 'S1', 'refunded', today='2026-01-02')
    assert conn.execute("SELECT refund_date FROM packages WHERE tracking_number = 'S1'").fetchone()[0] == '2026-01-02'
    rcv.set_status(conn, 'S1', 'past_due')
    assert _status(conn, 'S1') == ['past_due']
    assert _history(conn) == []  # admin overrides write no history


def test_bulk_unreceive_mark_late_delete(conn):
    _pkg(conn, 'B1')
    _pkg(conn, 'B2')
    rcv.bulk_receive(conn, ['B1', 'B2', 'B3'], 'admin')
    assert _status(conn, 'B1') == ['received'] and _status(conn, 'B3') == ['received']
    rcv.unreceive(conn, ['B1'])
    assert _status(conn, 'B1') == ['expected']
    assert conn.execute("SELECT date_scanned FROM packages WHERE tracking_number = 'B1'").fetchone()[0] is None
    assert len(_history(conn, 'received')) == 2
    rcv.mark_late(conn, ['B1', 'B2'])
    assert _status(conn, 'B1') == ['past_due'] and _status(conn, 'B2') == ['past_due']
    rcv.delete_by_tracking(conn, ['B1', 'B2'])
    assert _status(conn, 'B1') == [] and _status(conn, 'B2') == []


# --- admin package edits -------------------------------------------------

def test_status_for_expected_date():
    today = datetime.date(2026, 10, 17)
    assert rcv.status_for_expected_date('2026-10-17', today) == 'expected'
    assert rcv.status_for_expected_date('2026-10-01', today) == 'past_due'
    assert rcv.status_for_expected_date('2026-11-01', today) == 'on_time'
    assert rcv.status_for_expected_date('', today) == 'on_time'
    assert rcv.status_for_expected_date('junk', today) == 'on_time'


def test_add_manual_package_records_import(conn):
    status = rcv.add_manual_package(conn, 'MAN1', 'Hand added', 'SKU-M', '2000-01-01', True, 'admin')
    assert status == 'past_due'
    row = conn.execute("SELECT source, priority, manual_date, sku FROM packages WHERE tracking_number = 'MAN1'").fetchone()
    assert tuple(row) == ('manual', 1, '2000-01-01', 'SKU-M')
    assert conn.execute("SELECT COUNT(*) FROM package_imports WHERE source = 'manual'").fetchone()[0] == 1


def test_package_field_edits(conn):
    a = _pkg(conn, 'E1', name='Item A')
    _pkg(conn, 'E2', name='Item A')
    rcv.set_package_sku(conn, a, 'SKU-E')
    rcv.set_package_quantity(conn, a, 6)
    rcv.toggle_priority(conn, 'E1')
    row = conn.execute('SELECT sku, quantity, priority FROM packages WHERE id = ?', (a,)).fetchone()
    assert tuple(row) == ('SKU-E', 6, 1)
    with pytest.raises(ValueError):
        rcv.set_package_quantity(conn, a, -1)

    with pytest.raises(rcv.TrackingConflict):
        rcv.set_package_tracking(conn, a, 'E2')  # same item already under E2
    assert rcv.set_package_tracking(conn, a, 'E3') is True
    assert rcv.set_package_tracking(conn, 9999, 'E4') is False

    rcv.receive_scan(conn, 'E3', 'admin')
    deleted = rcv.delete_package(conn, a)
    assert deleted['tracking_number'] == 'E3'
    assert _history(conn) == []
    assert rcv.delete_package(conn, a) is None


def test_set_expected_date_refreshes_status(conn):
    _pkg(conn, 'D1', status='on_time')
    rcv.set_expected_date(conn, 'D1', '2000-01-01')
    row = conn.execute("SELECT manual_date, date_expected, status FROM packages WHERE tracking_number = 'D1'").fetchone()
    assert (row['manual_date'], row['date_expected']) == ('2000-01-01', '2000-01-01')
    assert row['status'] == 'past_due'


# --- thin routes still answer as before ----------------------------------

def test_scan_route_uses_service(fresh_app, fresh_client, conn):
    from tests.conftest import login
    _pkg(conn, 'ROUTE1', name='Routed', priority=0)
    client = login(fresh_client, 1)
    html = client.post('/scan', data={'tracking_input': 'ROUTE1'}).get_data(as_text=True)
    assert 'background-color: #77dd77;' in html and 'Routed' in html  # green
    html = client.post('/scan', data={'tracking_input': 'ROUTE1'}).get_data(as_text=True)
    assert 'background-color: #ffd700;' in html  # gold
    html = client.post('/scan', data={'tracking_input': 'NOPE-NOPE'}).get_data(as_text=True)
    assert 'background-color: #ff6961;' in html  # red


def test_stock_routes_keep_responses(fresh_app, fresh_client, conn):
    from tests.conftest import login
    item_id = inv.create_item(conn, {'sku': 'RT-1', 'name': 'Route Item', 'quantity': 2, 'location_area': 'A'})
    client = login(fresh_client, 1)
    resp = client.post(f'/inventory/api/quick_adjust/{item_id}', json={'change': -5})
    assert resp.status_code == 400 and resp.get_json() == {'error': 'Cannot reduce by 5 - only 2 in stock'}
    resp = client.post(f'/inventory/api/quick_adjust/{item_id}', json={'change': 1})
    assert resp.get_json() == {'success': True, 'new_quantity': 3, 'name': 'Route Item'}
    resp = client.post(f'/inventory/api/quick_adjust/{item_id}', json={'action': 'oos'})
    assert resp.get_json()['new_quantity'] == 0
    assert client.post('/inventory/api/quick_adjust/9999', json={'change': 1}).status_code == 404

    client.post(f'/inventory/adjust/{item_id}', data={'quantity_change': '-1'})
    with client.session_transaction() as sess:
        assert ('message', 'Cannot reduce by 1 - only 0 in stock.') in sess['_flashes']
