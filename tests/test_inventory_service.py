"""Unit tests for app.services.inventory (stock ledger, items, imports, audits)."""
import json
from datetime import datetime, timedelta

import pytest

from app.services import inventory as inv
from app.services.db import get_db_connection


@pytest.fixture
def conn(fresh_app):
    c = get_db_connection(fresh_app.config['DATABASE'])
    yield c
    c.close()


def _item(conn, sku='SKU-1', name='Widget', qty=5, **extra):
    fields = {'sku': sku, 'name': name, 'quantity': qty, 'location_area': 'A'}
    fields.update(extra)
    return inv.create_item(conn, fields, user='admin')


def _qty(conn, item_id):
    return conn.execute('SELECT quantity FROM inventory_items WHERE id = ?', (item_id,)).fetchone()[0]


def _ledger(conn, item_id):
    return [tuple(r) for r in conn.execute(
        'SELECT quantity_change, reason, user_id, source_tracking FROM inventory_transactions '
        'WHERE inventory_item_id = ? ORDER BY id', (item_id,))]


# --- stock ---------------------------------------------------------------

def test_adjust_stock_is_relative_and_logged(conn):
    item_id = _item(conn, qty=5)
    change = inv.adjust_stock(conn, item_id, 3, 'Received from Scan', 'cashier', '1ZTRACK')
    assert (change.old_quantity, change.new_quantity, change.delta) == (5, 8, 3)
    assert change.name == 'Widget' and change.sku == 'SKU-1'
    assert _qty(conn, item_id) == 8
    assert _ledger(conn, item_id) == [(5, 'Initial Stock', 'admin', None),
                                      (3, 'Received from Scan', 'cashier', '1ZTRACK')]
    assert not conn.in_transaction


def test_adjust_stock_refuses_to_go_negative(conn):
    item_id = _item(conn, qty=2)
    with pytest.raises(inv.InsufficientStock) as exc:
        inv.adjust_stock(conn, item_id, -3, 'Sold/Consumed', 'admin')
    assert str(exc.value) == 'Cannot reduce by 3 - only 2 in stock'
    assert (exc.value.requested, exc.value.available) == (-3, 2)
    assert _qty(conn, item_id) == 2
    assert len(_ledger(conn, item_id)) == 1  # only the initial stock
    assert not conn.in_transaction


def test_adjust_stock_allow_negative(conn):
    item_id = _item(conn, qty=1)
    assert inv.adjust_stock(conn, item_id, -3, 'Sold/Consumed', allow_negative=True).new_quantity == -2


def test_adjust_stock_zero_and_missing(conn):
    item_id = _item(conn, qty=4)
    assert inv.adjust_stock(conn, item_id, 0, 'x').new_quantity == 4
    assert len(_ledger(conn, item_id)) == 1
    with pytest.raises(inv.ItemNotFound):
        inv.adjust_stock(conn, 9999, 1, 'x')
    assert conn.execute('SELECT COUNT(*) FROM inventory_transactions WHERE inventory_item_id = 9999').fetchone()[0] == 0


def test_adjust_stock_joins_callers_transaction(conn):
    item_id = _item(conn, qty=5)
    conn.execute('BEGIN IMMEDIATE')
    inv.adjust_stock(conn, item_id, -1, 'Sold/Consumed')
    assert conn.in_transaction  # not committed behind the caller's back
    conn.rollback()
    assert _qty(conn, item_id) == 5
    assert len(_ledger(conn, item_id)) == 1


def test_changes_from_two_connections_both_land(fresh_app, conn):
    item_id = _item(conn, qty=10)
    other = get_db_connection(fresh_app.config['DATABASE'])
    try:
        inv.adjust_stock(conn, item_id, -2, 'Sold/Consumed')
        inv.adjust_stock(other, item_id, -3, 'Sold/Consumed')
    finally:
        other.close()
    assert _qty(conn, item_id) == 5


def test_set_stock_and_mark_out_of_stock(conn):
    item_id = _item(conn, qty=7)
    change = inv.set_stock(conn, item_id, 4, 'Live Audit Correction', 'admin')
    assert (change.delta, change.new_quantity) == (-3, 4)
    oos = inv.mark_out_of_stock(conn, item_id, '1')
    assert (oos.delta, oos.new_quantity, oos.name) == (-4, 0, 'Widget')
    assert _ledger(conn, item_id)[-1] == (-4, 'Marked OOS from Scan', '1', None)
    # Already zero: nothing to log
    inv.mark_out_of_stock(conn, item_id, '1')
    assert len(_ledger(conn, item_id)) == 3


def test_revert_transaction(conn):
    item_id = _item(conn, qty=5)
    inv.adjust_stock(conn, item_id, -2, 'Damaged')
    tx_id = conn.execute('SELECT MAX(id) FROM inventory_transactions').fetchone()[0]
    assert inv.revert_transaction(conn, tx_id) == item_id
    assert _qty(conn, item_id) == 5
    assert len(_ledger(conn, item_id)) == 1
    with pytest.raises(inv.TransactionNotFound):
        inv.revert_transaction(conn, tx_id)


# --- items ---------------------------------------------------------------

def test_create_item_rules(conn):
    item_id = _item(conn, sku='SKU-A', qty=0, asin='B000TEST01', alert_threshold=3,
                    upc='0123', addon_1=True)
    row = inv.get_item(conn, item_id)
    assert row['quantity'] == 0 and _ledger(conn, item_id) == []  # nothing to log
    assert row['alert_enabled'] == 1 and row['alert_threshold'] == 3
    assert row['image_url'] == inv.amazon_image_url('B000TEST01')
    assert row['source_url'] == 'https://www.amazon.com/dp/B000TEST01'
    assert json.loads(row['secondary_ids']) == {'upc': '0123'}
    assert row['addon_1'] == 1 and row['addon_2'] == 0


def test_create_item_gallery_supplies_cover(conn):
    item_id = _item(conn, sku='SKU-G', additional_images=['/static/uploads/inventory/a.jpg',
                                                          '/static/uploads/inventory/b.jpg'])
    row = inv.get_item(conn, item_id)
    assert row['image_url'] == '/static/uploads/inventory/a.jpg'
    assert json.loads(row['additional_images']) == ['/static/uploads/inventory/b.jpg']


def test_create_item_duplicate_sku(conn):
    _item(conn, sku='DUP')
    with pytest.raises(inv.DuplicateSku):
        _item(conn, sku='DUP', name='Other')
    assert conn.execute("SELECT COUNT(*) FROM inventory_items WHERE sku = 'DUP'").fetchone()[0] == 1
    assert not conn.in_transaction


def test_update_item_never_touches_stock(conn):
    a = _item(conn, sku='A', qty=9)
    _item(conn, sku='B')
    inv.update_item(conn, a, {'sku': 'A2', 'name': 'Renamed', 'location_area': 'X',
                              'asin': 'B000TEST02', 'source_url': None, 'is_legacy': True})
    row = inv.get_item(conn, a)
    assert (row['sku'], row['name'], row['quantity'], row['is_legacy']) == ('A2', 'Renamed', 9, 1)
    assert row['source_url'] is None  # not auto-filled on edit
    assert row['image_url'] == inv.amazon_image_url('B000TEST02')
    with pytest.raises(inv.DuplicateSku):
        inv.update_item(conn, a, {'sku': 'B', 'name': 'x'})


def test_generate_sku_sequence(conn):
    assert inv.generate_sku(conn, 'pri') == 'RSCP-PRI-0000-0001'
    _item(conn, sku='RSCP-PRI-0000-0041')
    assert inv.generate_sku(conn, 'PRI') == 'RSCP-PRI-0000-0042'
    assert inv.generate_sku(conn, 'x') == 'RSCP-XXX-0000-0001'


def test_find_item_by_code_and_sale_pricing(conn):
    _item(conn, sku='S1', upc='999', sell_price_cents=1000)
    conn.execute("UPDATE inventory_items SET sale_enabled = 1, sale_price_cents = 750, "
                 "sale_start = '2026-01-01T00:00', sale_end = '2026-01-31T23:59'")
    conn.commit()
    during = inv.find_item_by_code(conn, '999', now=datetime(2026, 1, 15))
    assert during['sku'] == 'S1' and during['is_on_sale'] and during['current_price_cents'] == 750
    after = inv.find_item_by_code(conn, 'S1', now=datetime(2026, 2, 1))
    assert not after['is_on_sale'] and after['current_price_cents'] == 1000
    assert after['regular_price_cents'] == 1000
    assert inv.find_item_by_code(conn, 'nope') is None


def test_toggle_legacy_and_delete(conn):
    item_id = _item(conn)
    assert inv.toggle_legacy(conn, item_id) == ('Widget', 1)
    assert inv.toggle_legacy(conn, item_id) == ('Widget', 0)
    inv.delete_item(conn, item_id)
    assert inv.get_item(conn, item_id) is None
    assert _ledger(conn, item_id) == []
    with pytest.raises(inv.ItemNotFound):
        inv.toggle_legacy(conn, item_id)


def test_bulk_update_items(conn):
    a, b = _item(conn, sku='A'), _item(conn, sku='B')
    assert inv.bulk_update_items(conn, [a, b], {'alert_threshold': 4, 'sell_price_cents': 250}) == 2
    rows = conn.execute('SELECT alert_enabled, alert_threshold, sell_price_cents FROM inventory_items').fetchall()
    assert [tuple(r) for r in rows] == [(1, 4, 250), (1, 4, 250)]
    with pytest.raises(ValueError):
        inv.bulk_update_items(conn, [a], {'quantity': 100})
    assert inv.bulk_update_items(conn, [a], {}) == 0


# --- audits --------------------------------------------------------------

def test_record_audit_count(conn):
    item_id = _item(conn, qty=10)
    session_id = conn.execute("INSERT INTO audit_sessions (user_id, mode) VALUES ('admin', 'item')").lastrowid
    conn.commit()
    inv.record_audit_count(conn, session_id, item_id, 8, 'admin')
    assert _qty(conn, item_id) == 8
    assert _ledger(conn, item_id)[-1] == (-2, 'Live Audit Correction', 'admin', None)
    rec = conn.execute('SELECT expected_qty, counted_qty FROM audit_records').fetchall()
    assert [tuple(r) for r in rec] == [(10, 8)]
    # Recount: the record is updated, not duplicated; stock follows
    inv.record_audit_count(conn, session_id, item_id, 9, 'admin')
    rec = conn.execute('SELECT expected_qty, counted_qty FROM audit_records').fetchall()
    assert [tuple(r) for r in rec] == [(10, 9)]
    assert _qty(conn, item_id) == 9
    with pytest.raises(inv.ItemNotFound):
        inv.record_audit_count(conn, session_id, 9999, 1)


# --- import --------------------------------------------------------------

def test_import_items(conn):
    existing = _item(conn, sku='OLD', qty=2)
    rows = [
        {'sku': 'NEW-1', 'name': 'New One', 'qty': '4', 'cost': '1.50', 'upc': '42'},
        {'sku': 'OLD', 'name': 'Old Renamed', 'qty': '3'},
        {'sku': '', 'name': 'No SKU'},
        {'sku': 'BAD', 'name': 'Bad Qty', 'qty': 'lots'},
    ]
    result = inv.import_items(conn, rows, update_existing=True, user='admin')
    assert (result.added, result.updated, result.skipped) == (1, 1, 1)
    assert len(result.errors) == 1 and result.errors[0].startswith('Row 5 Error')
    new = conn.execute("SELECT * FROM inventory_items WHERE sku = 'NEW-1'").fetchone()
    assert new['quantity'] == 4 and new['buy_price_cents'] == 150
    assert json.loads(new['secondary_ids']) == {'upc': '42'}
    assert _ledger(conn, new['id']) == [(4, 'CSV Import', 'admin', None)]
    old = inv.get_item(conn, existing)
    assert old['name'] == 'Old Renamed' and old['quantity'] == 5
    assert _ledger(conn, existing)[-1] == (3, 'CSV Import', 'admin', None)
    assert conn.execute("SELECT COUNT(*) FROM inventory_items WHERE sku = 'BAD'").fetchone()[0] == 0

    again = inv.import_items(conn, [{'sku': 'OLD', 'name': 'x', 'qty': '1'}], update_existing=False)
    assert (again.added, again.updated, again.skipped) == (0, 0, 1)
    assert _qty(conn, existing) == 5


# --- reporting -----------------------------------------------------------

def test_stats_attention_and_snooze(conn):
    out = _item(conn, sku='OUT', name='Out', qty=0)
    _item(conn, sku='LOW', name='Low', qty=2)                  # global threshold 5
    _item(conn, sku='OWN', name='Own', qty=4, alert_threshold=3)  # own threshold: fine
    _item(conn, sku='OK', name='Fine', qty=50)
    stats = inv.inventory_stats(conn, low_threshold=5)
    assert (stats['total_items'], stats['out_of_stock'], stats['low_stock']) == (4, 1, 1)
    assert stats['last_audit'] == 'Never'

    data = inv.overview_data(conn, low_threshold=5)
    assert [r['name'] for r in data['attention_items']] == ['Out', 'Low']

    now = datetime(2026, 10, 1, 12, 0)
    name, until = inv.snooze_attention(conn, out, now=now)
    assert name == 'Out' and until == now + timedelta(days=inv.ATTENTION_SNOOZE_DAYS)
    data = inv.overview_data(conn, low_threshold=5, now=now)
    assert [r['name'] for r in data['attention_items']] == ['Low'] and data['snoozed_count'] == 1
    assert len(data['sales_trend']) == 31


def test_list_items_search_sort_and_legacy(conn):
    _item(conn, sku='B-1', name='Beta bolt', keywords='hardware')
    _item(conn, sku='A-1', name='Alpha nut')
    legacy = _item(conn, sku='C-1', name='Gamma bolt')
    inv.toggle_legacy(conn, legacy)
    rows, total, sort_by, order = inv.list_items(conn, sort_by='bogus')
    assert (sort_by, order, total) == ('name', 'asc', 2)
    assert [r['name'] for r in rows] == ['Alpha nut', 'Beta bolt']
    rows, total, _, _ = inv.list_items(conn, search='bolt', show_legacy=True)
    assert total == 2 and {r['sku'] for r in rows} == {'B-1', 'C-1'}
    rows, _, _, order = inv.list_items(conn, sort_by='created_at')
    assert order == 'desc'


def test_notify_if_low_stock(fresh_app, conn):
    item_id = _item(conn, qty=1, alert_threshold=2)
    quiet = _item(conn, sku='Q', qty=0)  # alerts off
    with fresh_app.app_context():
        assert inv.notify_if_low_stock(conn, item_id, {'NOTIFY_LOW_STOCK': True}) is True
        assert inv.notify_if_low_stock(conn, quiet, {'NOTIFY_OOS': True}) is False
    titles = [r[0] for r in conn.execute('SELECT title FROM notifications')]
    assert len(titles) == 1 and 'LOW STOCK (1)' in titles[0]
