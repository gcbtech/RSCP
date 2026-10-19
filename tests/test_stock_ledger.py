"""The stock ledger: inventory_movements is the truth for stock on hand and
inventory_items.quantity its cached sum (app/services/inventory.py,
schema migration 20).

  * every stock-changing flow writes exactly one movement with the right
    delta / reason / reference, and the cache follows;
  * the ledger is append-only (triggers + no code path updates/deletes it);
  * concurrent adjustments keep SUM(delta) == cache;
  * migration 20 backfills one opening balance per item;
  * drift is detected and the cache repaired from the ledger;
  * the item page shows the history.
"""
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.services import inventory as inv
from app.services import pos_checkout, pos_refunds, pos_sales
from app.services import receiving as rcv
from app.services.db import get_db_connection
from app.services.pos_pricing import PricingConfig
from tests.conftest import login

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
CASHIER = SimpleNamespace(id=2, username='cashier')
MANAGER = SimpleNamespace(id=3, username='manager')
ADMIN = SimpleNamespace(id=1, username='admin')


@pytest.fixture
def conn(fresh_app):
    c = get_db_connection(fresh_app.config['DATABASE'])
    c.isolation_level = None  # autocommit, as the POS routes use it
    yield c
    c.close()


def _item(conn, sku='W-1', qty=10, **extra):
    fields = {'sku': sku, 'name': f'{sku} item', 'quantity': qty, 'sell_price': 10.0,
              'location_area': 'A'}
    fields.update(extra)
    return inv.create_item(conn, fields, user='admin')


def _qty(conn, item_id):
    return conn.execute('SELECT quantity FROM inventory_items WHERE id = ?', (item_id,)).fetchone()[0]


def _last_id(conn):
    return conn.execute('SELECT COALESCE(MAX(id), 0) FROM inventory_movements').fetchone()[0]


def _sell(conn, item_id, qty=2, sku='W-1'):
    line = {'inventory_item_id': item_id, 'sku': sku, 'name': f'{sku} item', 'quantity': qty,
            'unit_price': 10.0, 'discount_amount': 0, 'discount_type': None, 'line_total': 10.0 * qty}
    cart = {'items': [line], 'discount_amount': 0, 'discount_type': None, 'discount_reason': ''}
    return pos_checkout.complete_sale(conn, pos_checkout.SessionCart(cart),
                                      pos_checkout.Payment('cash'), CASHIER,
                                      config=PricingConfig(tax_rate=0.0))


def _order_line(conn, order_id):
    return conn.execute('SELECT id FROM pos_order_items WHERE order_id = ?', (order_id,)).fetchone()[0]


def _pkg(conn, tracking, qty=1, status='expected'):
    return conn.execute(
        "INSERT INTO packages (tracking_number, item_name, status, quantity, date_expected) "
        "VALUES (?, 'Thing', ?, ?, date('now'))", (tracking, status, qty)).lastrowid


def _assert_ledger_matches_cache(conn):
    assert inv.ledger_drift(conn) == []


# --- every flow: exactly one movement ----------------------------------------
#
# Each flow sets up what it needs, calls ctx.mark() right before the
# stock-changing action, and returns the movement it expects:
# (item_id, delta, reason, ref_type, ref_id, note or ... for "don't care").

def flow_sale(ctx):
    item = _item(ctx.conn)
    ctx.mark()
    sale = _sell(ctx.conn, item, 2)
    return item, -2, 'sale', 'order', sale.order_id, sale.order_number


def flow_refund_restock(ctx):
    item = _item(ctx.conn)
    sale = _sell(ctx.conn, item, 2)
    ctx.mark()
    res = pos_refunds.process_refund(ctx.conn, sale.order_id, 'partial', 'customer_changed_mind', MANAGER,
                                     items=[(str(_order_line(ctx.conn, sale.order_id)), '1')])
    return item, 1, 'refund', 'refund', res.refund_id, sale.order_number


def flow_sale_edit(ctx):
    item = _item(ctx.conn)
    sale = _sell(ctx.conn, item, 2)
    ctx.mark()
    edits = [pos_sales.SaleLineEdit(str(_order_line(ctx.conn, sale.order_id)), '3', '10.00', 'W-1 item')]
    pos_sales.edit_sale(ctx.conn, sale.order_id, edits, [], MANAGER)
    return item, -1, 'sale_edit', 'order', sale.order_id, f'{sale.order_number}: qty increased'


def flow_sale_delete(ctx):
    item = _item(ctx.conn)
    sale = _sell(ctx.conn, item, 2)
    ctx.mark()
    pos_sales.delete_order(ctx.conn, sale.order_number, True, ADMIN)
    return item, 2, 'sale_delete', 'order', sale.order_id, f'{sale.order_number}: sale deleted'


def flow_receive_linked_package(ctx):
    item = _item(ctx.conn, sku='L-1', qty=1)
    pkg = _pkg(ctx.conn, 'LNK-1', qty=4)
    rcv.receive_scan(ctx.conn, 'LNK-1', 'admin')
    ctx.mark()
    assert rcv.link_package_to_item(ctx.conn, 'LNK-1', 'L-1', 'admin') == 4
    return item, 4, 'receive', 'package', pkg, 'LNK-1'


def flow_receive_add_stock_page(ctx):
    item = _item(ctx.conn, qty=1)
    pkg = _pkg(ctx.conn, '1ZADD', qty=3, status='received')
    ctx.mark()
    ctx.client.post(f'/inventory/add_stock/{item}', data={'qty': '3', 'tracking': '1ZADD'})
    return item, 3, 'receive', 'package', pkg, '1ZADD'


def flow_receive_scan_screen(ctx):
    item = _item(ctx.conn, qty=1)
    ctx.mark()
    resp = ctx.client.post(f'/inventory/api/add_quantity/{item}', json={'quantity': 2, 'tracking': 'NOPKG'})
    assert resp.get_json() == {'success': True, 'new_quantity': 3}
    return item, 2, 'receive', None, None, 'NOPKG'


def flow_manual_adjust_preset(ctx):
    item = _item(ctx.conn)
    ctx.mark()
    ctx.client.post(f'/inventory/adjust/{item}', data={'quantity_change': '-2', 'reason': 'Damaged'})
    return item, -2, 'damaged', None, None, None


def flow_manual_adjust_custom(ctx):
    item = _item(ctx.conn)
    ctx.mark()
    ctx.client.post(f'/inventory/adjust/{item}', data={'quantity_change': '5', 'reason': 'Found in back room',
                                                        'source_tracking': '1ZBACK'})
    return item, 5, 'adjust', None, None, 'Found in back room / 1ZBACK'


def flow_scan_quick_adjust(ctx):
    item = _item(ctx.conn)
    ctx.mark()
    ctx.client.post(f'/inventory/api/quick_adjust/{item}', json={'change': -1})
    return item, -1, 'consumed', None, None, 'Scan screen'


def flow_absolute_count_audit(ctx):
    item = _item(ctx.conn, qty=10)
    session_id = ctx.conn.execute("INSERT INTO audit_sessions (user_id, mode) VALUES ('admin', 'item')").lastrowid
    ctx.mark()
    resp = ctx.client.post('/inventory/audit/submit_count',
                           json={'session_id': session_id, 'item_id': item, 'count': 7})
    assert resp.get_json() == {'success': True}
    return item, -3, 'count', 'audit', session_id, 'Audit count: 7 (was 10)'


def flow_absolute_count_mark_oos(ctx):
    item = _item(ctx.conn, qty=6)
    ctx.mark()
    ctx.client.post(f'/inventory/api/quick_adjust/{item}', json={'action': 'oos'})
    return item, -6, 'count', None, None, 'Marked out of stock from scan'


def flow_import_existing(ctx):
    item = _item(ctx.conn, sku='IMP-1', qty=2)
    ctx.mark()
    inv.import_items(ctx.conn, [{'sku': 'IMP-1', 'name': 'Imported', 'qty': '3'}], update_existing=True,
                     user='admin')
    return item, 3, 'import', None, None, 'Spreadsheet import'


def flow_import_new(ctx):
    ctx.mark()
    inv.import_items(ctx.conn, [{'sku': 'IMP-NEW', 'name': 'Brand new', 'qty': '4'}], update_existing=False,
                     user='admin')
    item = ctx.conn.execute("SELECT id FROM inventory_items WHERE sku = 'IMP-NEW'").fetchone()[0]
    return item, 4, 'initial', None, None, 'Spreadsheet import'


def flow_create_item(ctx):
    ctx.mark()
    ctx.client.post('/inventory/add', data={'name': 'Form item', 'sku': 'FORM-1', 'quantity': '4',
                                            'location_area': 'A', 'category': 'ATO'})
    item = ctx.conn.execute("SELECT id FROM inventory_items WHERE sku = 'FORM-1'").fetchone()[0]
    return item, 4, 'initial', None, None, None


def flow_reversal(ctx):
    item = _item(ctx.conn)
    damaged = inv.adjust_stock(ctx.conn, item, -3, inv.REASON_DAMAGED, 'admin').movement_id
    ctx.mark()
    ctx.client.post(f'/inventory/movement/{damaged}/reverse')
    return item, 3, 'reversal', 'movement', damaged, ...


FLOWS = [flow_sale, flow_refund_restock, flow_sale_edit, flow_sale_delete,
         flow_receive_linked_package, flow_receive_add_stock_page, flow_receive_scan_screen,
         flow_manual_adjust_preset, flow_manual_adjust_custom, flow_scan_quick_adjust,
         flow_absolute_count_audit, flow_absolute_count_mark_oos,
         flow_import_existing, flow_import_new, flow_create_item, flow_reversal]


@pytest.mark.parametrize('flow', FLOWS, ids=[f.__name__[5:] for f in FLOWS])
def test_each_stock_flow_writes_exactly_one_movement(flow, conn, fresh_client):
    ctx = SimpleNamespace(conn=conn, client=login(fresh_client, 1), mark_id=None)

    def mark():
        ctx.mark_id = _last_id(conn)
    ctx.mark = mark

    item_id, delta, reason, ref_type, ref_id, note = flow(ctx)
    assert ctx.mark_id is not None
    rows = conn.execute('SELECT * FROM inventory_movements WHERE id > ? ORDER BY id',
                        (ctx.mark_id,)).fetchall()
    assert len(rows) == 1, [dict(r) for r in rows]
    m = rows[0]
    assert (m['item_id'], m['delta'], m['reason'], m['ref_type'], m['ref_id']) == \
        (item_id, delta, reason, ref_type, ref_id)
    if note is not ...:
        assert m['note'] == note
    assert m['user'] and m['created_at']
    assert reason in inv.REASON_LABELS
    _assert_ledger_matches_cache(conn)
    assert not conn.in_transaction


def test_bulk_receive_moves_packages_not_stock(conn):
    """Bulk "Mark Received" only changes package statuses; stock comes in when
    a package is linked to an item (flow_receive_linked_package)."""
    _item(conn)
    _pkg(conn, 'BULK-1')
    before = _last_id(conn)
    assert rcv.bulk_receive(conn, ['BULK-1', 'BULK-NEW'], 'admin') == []
    assert _last_id(conn) == before


def test_zero_changes_write_nothing(conn, fresh_client):
    item = _item(conn, qty=0)
    before = _last_id(conn)
    inv.adjust_stock(conn, item, 0, inv.REASON_ADJUST)
    inv.mark_out_of_stock(conn, item, '1')     # already zero
    assert _last_id(conn) == before


# --- append-only -------------------------------------------------------------

def test_triggers_refuse_update_and_delete(conn):
    item = _item(conn, qty=5)
    inv.adjust_stock(conn, item, -1, inv.REASON_SALE, '2')
    first = conn.execute('SELECT MIN(id) FROM inventory_movements').fetchone()[0]
    with pytest.raises(sqlite3.DatabaseError, match='append-only'):
        conn.execute('UPDATE inventory_movements SET delta = 50 WHERE id = ?', (first,))
    with pytest.raises(sqlite3.DatabaseError, match='append-only'):
        conn.execute("UPDATE inventory_movements SET note = 'edited'")
    # Not even the opening entry: the item has real history now
    with pytest.raises(sqlite3.DatabaseError, match='append-only'):
        conn.execute('DELETE FROM inventory_movements WHERE id = ?', (first,))
    with pytest.raises(sqlite3.DatabaseError, match='append-only'):
        conn.execute('DELETE FROM inventory_movements')
    assert [tuple(r) for r in conn.execute('SELECT delta, reason FROM inventory_movements ORDER BY id')] \
        == [(5, 'initial'), (-1, 'sale')]


def _python_sources():
    for top in ('app', 'scripts'):
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, top)):
            for name in files:
                if name.endswith('.py'):
                    path = os.path.join(dirpath, name)
                    with open(path, encoding='utf-8') as f:
                        yield os.path.relpath(path, ROOT).replace(os.sep, '/'), f.read()


def test_no_code_path_updates_or_deletes_movements():
    writes = re.compile(r'\b(UPDATE(\s+OR\s+\w+)?|DELETE\s+FROM|REPLACE\s+INTO|INSERT\s+OR\s+REPLACE\s+INTO)'
                        r'\s+inventory_movements\b', re.I)
    found = [(path, m.group(0)) for path, src in _python_sources() for m in writes.finditer(src)]
    # The single exception: delete_item removes an item's opening entries
    # together with the item (it refuses items with any other movement).
    assert found == [('app/services/inventory.py', 'DELETE FROM inventory_movements')]
    inserts = sorted({path for path, src in _python_sources()
                      if re.search(r'INSERT\s+INTO\s+inventory_movements\b', src, re.I)})
    assert inserts == ['app/services/inventory.py', 'app/services/migration.py']


def test_only_the_inventory_service_writes_the_quantity_cache():
    stmt = re.compile(r'UPDATE\s+inventory_items\s+SET(.*?)(?:WHERE|\'\'\'|"""|\'\)|"\))', re.I | re.S)
    writers = sorted({path for path, src in _python_sources()
                      for m in stmt.finditer(src) if re.search(r'(?<!\w)quantity\s*=', m.group(1))})
    assert writers == ['app/services/inventory.py', 'app/services/migration.py']


# --- concurrency -------------------------------------------------------------

def test_concurrent_adjustments_keep_ledger_equal_to_cache(fresh_app, conn):
    item = _item(conn, qty=100)
    db = fresh_app.config['DATABASE']
    errors = []
    start = threading.Barrier(3)

    def worker(sign):
        c = get_db_connection(db)
        try:
            start.wait()
            for i in range(25):
                inv.adjust_stock(c, item, sign * (1 + i % 3), inv.REASON_ADJUST, 'worker', allow_negative=True)
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)
        finally:
            c.close()

    threads = [threading.Thread(target=worker, args=(s,)) for s in (1, -1, -1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    expected = 100 + sum(1 + i % 3 for i in range(25)) * (1 - 2)
    assert _qty(conn, item) == expected == inv.stock_on_hand(conn, item)
    assert inv.movement_count(conn, item) == 1 + 75
    _assert_ledger_matches_cache(conn)


def test_caller_transaction_is_joined_and_rolled_back_together(conn):
    item = _item(conn, qty=5)
    conn.execute('BEGIN IMMEDIATE')
    inv.adjust_stock(conn, item, -2, inv.REASON_SALE, '2')
    inv.set_stock(conn, item, 1, inv.REASON_COUNT, 'admin')
    assert conn.in_transaction
    conn.rollback()
    assert _qty(conn, item) == 5 and inv.movement_count(conn, item) == 1
    _assert_ledger_matches_cache(conn)


def test_works_with_foreign_keys_enforced(fresh_app):
    c = get_db_connection(fresh_app.config['DATABASE'])
    try:
        c.execute('PRAGMA foreign_keys = ON')
        item = _item(c, qty=3)
        inv.adjust_stock(c, item, -1, inv.REASON_SALE, '2')
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO inventory_movements (item_id, delta, reason) VALUES (424242, 1, 'adjust')")
        c.rollback()
        # Opening-only item: deleted together with its entries
        fresh = _item(c, sku='FRESH', qty=2)
        inv.delete_item(c, fresh)
        assert inv.get_item(c, fresh) is None and inv.movement_count(c, fresh) == 0
        # No movements, but an audit record points at it: refused politely
        counted = _item(c, sku='COUNTED', qty=0)
        session = c.execute("INSERT INTO audit_sessions (user_id, mode) VALUES ('admin', 'item')").lastrowid
        c.execute('INSERT INTO audit_records (session_id, item_id, expected_qty, counted_qty) VALUES (?, ?, 0, 0)',
                  (session, counted))
        c.commit()
        with pytest.raises(inv.ItemHasHistory):
            inv.delete_item(c, counted)
        assert inv.get_item(c, counted) is not None and not c.in_transaction
        assert c.execute('PRAGMA foreign_key_check').fetchall() == []
    finally:
        c.close()


# --- migration 20 ------------------------------------------------------------

def test_migration_backfills_one_opening_balance_per_item(tmp_path, monkeypatch):
    from app.services import migration
    db = str(tmp_path / 'old.db')
    # A database as the previous release left it (schema v14, no ledger)
    monkeypatch.setattr(migration, 'SCHEMA_MIGRATIONS', [m for m in migration.SCHEMA_MIGRATIONS if m[0] < 20])
    monkeypatch.setattr(migration, 'LATEST_SCHEMA_VERSION', 14)
    migration.run_migrations(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    assert not c.execute("SELECT 1 FROM sqlite_master WHERE name = 'inventory_movements'").fetchone()
    for sku, qty in (('A', 5), ('B', 0), ('C', None), ('D', -2), ('E', 12)):
        c.execute('INSERT INTO inventory_items (sku, name, quantity) VALUES (?, ?, ?)', (sku, sku, qty))
    # 2.x history that does not add up (deleted rows, unlogged changes)
    c.execute("INSERT INTO inventory_transactions (inventory_item_id, quantity_change, reason, user_id) "
              "VALUES (1, 3, 'Initial Stock', 'admin'), (1, -1, 'Sold/Consumed', '2')")
    c.commit()
    c.close()

    monkeypatch.undo()
    migration.run_migrations(db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        assert c.execute('PRAGMA user_version').fetchone()[0] == migration.LATEST_SCHEMA_VERSION >= 20
        rows = c.execute('''SELECT i.sku, i.quantity, m.delta, m.reason, m.user, m.created_at
                            FROM inventory_items i JOIN inventory_movements m ON m.item_id = i.id
                            ORDER BY i.id''').fetchall()
        assert [(r['sku'], r['quantity'], r['delta'], r['reason']) for r in rows] == [
            ('A', 5, 5, 'opening_balance'), ('B', 0, 0, 'opening_balance'), ('C', 0, 0, 'opening_balance'),
            ('D', -2, -2, 'opening_balance'), ('E', 12, 12, 'opening_balance')]
        assert all(r['user'] == 'system' and r['created_at'] for r in rows)
        assert inv.ledger_drift(c) == []
        # The old trail is left where it was
        assert c.execute('SELECT COUNT(*) FROM inventory_transactions').fetchone()[0] == 2
    finally:
        c.close()
    # Re-running is a no-op (already at the latest version)
    migration.run_migrations(db)
    c = sqlite3.connect(db)
    assert c.execute('SELECT COUNT(*) FROM inventory_movements').fetchone()[0] == 5
    c.close()


# --- drift -------------------------------------------------------------------

def test_drift_is_detected_and_repaired_from_the_ledger(conn, caplog):
    a = _item(conn, sku='A', qty=5)
    b = _item(conn, sku='B', qty=2)
    inv.adjust_stock(conn, a, -1, inv.REASON_SALE, '2')
    assert inv.check_stock_ledger(conn) == inv.LedgerCheck([], 0, False)
    # A bug (or a hand edit) overwrites the cache
    conn.execute('UPDATE inventory_items SET quantity = 99 WHERE id = ?', (a,))
    conn.execute('UPDATE inventory_items SET quantity = NULL WHERE id = ?', (b,))

    found = inv.check_stock_ledger(conn, repair=False)
    assert found.drift == [inv.Drift(a, 'A', 'A item', 99, 4), inv.Drift(b, 'B', 'B item', None, 2)]
    assert not found.repaired and _qty(conn, a) == 99

    with caplog.at_level(logging.WARNING, logger='app.services.inventory'):
        fixed = inv.check_stock_ledger(conn)
    assert fixed.repaired and len(fixed.drift) == 2
    assert _qty(conn, a) == 4 and _qty(conn, b) == 2
    assert 'cache repaired from the ledger' in caplog.text and '(A)' in caplog.text
    assert inv.ledger_drift(conn) == [] and not conn.in_transaction
    # The ledger itself was not touched
    assert inv.movement_count(conn, a) == 2 and inv.movement_count(conn, b) == 1


def test_package_maintenance_job_repairs_drift(fresh_app, conn):
    from app import jobs
    item = _item(conn, qty=3)
    conn.execute('UPDATE inventory_items SET quantity = 7 WHERE id = ?', (item,))
    conn.execute("INSERT INTO inventory_movements (item_id, delta, reason) VALUES (424242, 1, 'adjust')")
    with fresh_app.app_context():
        detail = jobs.package_maintenance('test')
        assert 'stock cache repaired for 1 item(s)' in detail
        assert 'stock ledger has 1 deleted item(s)' in detail
        assert _qty(conn, item) == 3
        assert inv.run_stock_ledger_check() == 'stock ledger has 1 deleted item(s)'


# --- history page, reverse, delete -------------------------------------------

def test_item_page_shows_stock_history_with_links(conn, fresh_client):
    item = _item(conn, qty=10)
    sale = _sell(conn, item, 2)
    refund = pos_refunds.process_refund(conn, sale.order_id, 'partial', 'customer_changed_mind', MANAGER,
                                        items=[(str(_order_line(conn, sale.order_id)), '1')])
    pkg = _pkg(conn, '1ZHIST', qty=3, status='received')
    inv.adjust_stock(conn, item, 3, inv.REASON_RECEIVE, 'admin', '1ZHIST',
                     ref_type=inv.REF_PACKAGE, ref_id=pkg)
    damaged = inv.adjust_stock(conn, item, -1, inv.REASON_DAMAGED, 'admin', 'dropped').movement_id
    conn.execute("INSERT INTO inventory_transactions (inventory_item_id, quantity_change, reason, user_id) "
                 "VALUES (?, 4, 'Received from Scan', 'olduser')", (item,))

    client = login(fresh_client, 1)
    html = client.get(f'/inventory/edit/{item}').get_data(as_text=True)
    assert 'Stock history' in html and '5 entries' in html
    assert f'href="/pos/receipt/{sale.order_number}">Order {sale.order_number}</a>' in html
    assert f'Refund #{refund.refund_id} on {sale.order_number}' in html
    assert 'Package 1ZHIST' in html and 'search_term=1ZHIST' in html
    assert 'cashier' in html          # POS rows store the user id; shown by name
    assert 'dropped' in html and 'Damaged' in html and 'Initial stock' in html
    assert f'/inventory/movement/{damaged}/reverse' in html
    assert 'Earlier history' in html and 'olduser' in html   # the 2.x trail, read-only
    assert '/inventory/transaction/delete/' not in html

    # Reverse from the page: appended, the original stays and is marked
    client.post(f'/inventory/movement/{damaged}/reverse')
    assert _qty(conn, item) == 10 - 2 + 1 + 3 - 1 + 1
    html = client.get(f'/inventory/edit/{item}').get_data(as_text=True)
    assert 'reversed by' in html and f'/inventory/movement/{damaged}/reverse' not in html
    _assert_ledger_matches_cache(conn)


def test_item_page_with_deleted_references(conn, fresh_client):
    item = _item(conn, qty=4)
    sale = _sell(conn, item, 1)
    pos_sales.delete_order(conn, sale.order_number, False, ADMIN)
    html = login(fresh_client, 1).get(f'/inventory/edit/{item}').get_data(as_text=True)
    assert 'Order (deleted)' in html and sale.order_number in html   # the note keeps the number


def test_delete_route_refuses_items_with_history(conn, fresh_client):
    used = _item(conn, sku='USED', qty=4)
    _sell(conn, used, 1, sku='USED')
    unused = _item(conn, sku='UNUSED', qty=4)
    client = login(fresh_client, 1)
    client.post(f'/inventory/delete/{used}')
    with client.session_transaction() as sess:
        flashes = [m for _cat, m in sess['_flashes']]
    assert any("can't be deleted" in m and 'Legacy' in m for m in flashes)
    assert inv.get_item(conn, used) is not None
    client.post(f'/inventory/delete/{unused}')
    assert inv.get_item(conn, unused) is None


def test_overview_counts_ledger_and_legacy_sales(conn):
    item = _item(conn, qty=20)
    _sell(conn, item, 2)
    inv.adjust_stock(conn, item, -1, inv.REASON_DAMAGED, 'admin')
    inv.adjust_stock(conn, item, -4, inv.REASON_COUNT, 'admin')     # not a sale
    conn.execute("INSERT INTO inventory_transactions (inventory_item_id, quantity_change, reason) "
                 "VALUES (?, -3, 'Sold/Consumed')", (item,))
    data = inv.overview_data(conn, low_threshold=0, now=datetime.utcnow())
    assert [(r['id'], r['sold']) for r in data['top_movers']] == [(item, 2 + 1 + 3)]
    assert data['sales_trend'][-1]['count'] == 2 + 3
