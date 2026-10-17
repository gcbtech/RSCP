"""
Packages live only in the database: uploads, email ingest, placeholder
matching, the (tracking, item) identity rule and schema migration 10.
"""
import datetime
import io
import sqlite3

import pytest

from tests.conftest import login

CSV_HEADER = 'Tracking Number,Item Name,Expected Delivery Date,Quantity,Order ID\n'


def _upload(client, data, filename, mapping):
    resp = client.post('/admin/upload_file', data={'file': (io.BytesIO(data), filename)},
                       content_type='multipart/form-data')
    assert resp.status_code == 200, resp.data[:300]
    assert b'Import Preview' in resp.data
    resp = client.post('/admin/upload_confirm', data=mapping)
    assert resp.status_code == 302
    return resp


CSV_MAPPING = {'col_tracking': 'Tracking Number', 'col_name': 'Item Name',
               'col_date': 'Expected Delivery Date', 'col_quantity': 'Quantity'}


def _rows(app, sql='SELECT * FROM packages ORDER BY id', params=()):
    from app.services.db import get_db_connection
    with app.app_context():
        conn = get_db_connection()
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()


def _conn(app):
    from app.services.db import get_db_connection
    with app.app_context():
        return get_db_connection()


def test_csv_upload_writes_packages_and_logs_import(fresh_app, fresh_client):
    login(fresh_client, 1)
    csv_data = (CSV_HEADER +
                '1Z111,Blue Widget,2030-01-05,2,\n'
                ',Red Widget,2030-01-06,1,\n').encode()
    _upload(fresh_client, csv_data, 'orders.csv', CSV_MAPPING)

    rows = _rows(fresh_app)
    assert len(rows) == 1   # the row without tracking or order ID is skipped
    assert rows[0]['tracking_number'] == '1Z111'
    assert rows[0]['item_name'] == 'Blue Widget'
    assert rows[0]['quantity'] == 2
    assert rows[0]['date_expected'] == '2030-01-05'
    assert rows[0]['status'] == 'on_time'
    assert rows[0]['source'] == 'manifest'

    imports = _rows(fresh_app, 'SELECT * FROM package_imports')
    assert len(imports) == 1
    assert imports[0]['source'] == 'upload'
    assert imports[0]['filename'] == 'orders.csv'
    assert imports[0]['user'] == 'admin'
    assert (imports[0]['rows_added'], imports[0]['rows_updated']) == (1, 0)


def test_reupload_is_idempotent_and_updates_changes(fresh_app, fresh_client):
    login(fresh_client, 1)
    csv_data = (CSV_HEADER + '1Z222,Lamp,2030-02-01,1,\n9400111,Chair,2030-02-02,4,\n').encode()
    _upload(fresh_client, csv_data, 'a.csv', CSV_MAPPING)
    _upload(fresh_client, csv_data, 'a.csv', CSV_MAPPING)

    assert len(_rows(fresh_app)) == 2
    imports = _rows(fresh_app, 'SELECT rows_added, rows_updated FROM package_imports ORDER BY id')
    assert [(i['rows_added'], i['rows_updated']) for i in imports] == [(2, 0), (0, 0)]

    # A changed date updates the row in place
    changed = (CSV_HEADER + '1Z222,Lamp,2030-03-01,1,\n').encode()
    _upload(fresh_client, changed, 'b.csv', CSV_MAPPING)
    lamp = _rows(fresh_app, "SELECT * FROM packages WHERE tracking_number = '1Z222'")
    assert len(lamp) == 1 and lamp[0]['date_expected'] == '2030-03-01'


def test_reupload_does_not_clobber_received_or_manual(fresh_app, fresh_client):
    login(fresh_client, 1)
    conn = _conn(fresh_app)
    conn.execute("INSERT INTO packages (tracking_number, item_name, quantity, status, source, date_expected) "
                 "VALUES ('M1', 'Manual Thing', 3, 'on_time', 'manual', '2030-01-01')")
    conn.execute("INSERT INTO packages (tracking_number, item_name, quantity, status, source, date_expected, "
                 "date_scanned) VALUES ('R1', 'Got It', 1, 'received', 'manifest', '2030-01-01', CURRENT_TIMESTAMP)")
    conn.commit()
    conn.close()

    csv_data = (CSV_HEADER + 'M1,Manual Thing,2031-01-01,9,\nR1,Got It,2031-01-01,9,\n').encode()
    _upload(fresh_client, csv_data, 'c.csv', CSV_MAPPING)
    rows = {r['tracking_number']: r for r in _rows(fresh_app)}
    assert rows['M1']['quantity'] == 3 and rows['M1']['date_expected'] == '2030-01-01'
    assert rows['R1']['status'] == 'received' and rows['R1']['quantity'] == 1


def test_xlsx_upload(fresh_app, fresh_client):
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(['Tracking Number', 'Item Name', 'Expected Delivery Date', 'Quantity'])
    ws.append([940011189922385, 'Desk', datetime.datetime(2030, 4, 2), 2.0])
    ws.append(['1ZXLSX', 'Shelf', datetime.date(2030, 4, 3), 1])
    ws.append([None, None, None, None])
    buf = io.BytesIO()
    wb.save(buf)

    login(fresh_client, 1)
    _upload(fresh_client, buf.getvalue(), 'orders.xlsx', CSV_MAPPING)
    rows = {r['tracking_number']: r for r in _rows(fresh_app)}
    assert set(rows) == {'940011189922385', '1ZXLSX'}
    assert rows['940011189922385']['date_expected'] == '2030-04-02'
    assert rows['940011189922385']['quantity'] == 2
    assert rows['1ZXLSX']['date_expected'] == '2030-04-03'


def test_excel_file_named_csv_is_refused(fresh_app, fresh_client):
    login(fresh_client, 1)
    resp = fresh_client.post('/admin/upload_file',
                             data={'file': (io.BytesIO(b'PK\x03\x04junk'), 'orders.csv')},
                             content_type='multipart/form-data')
    assert resp.status_code == 302
    assert _rows(fresh_app) == []


def test_two_items_share_one_tracking_number(fresh_app, fresh_client):
    login(fresh_client, 1)
    csv_data = (CSV_HEADER + '1ZSAME,Item A,2030-01-01,1,\n1ZSAME,Item B,2030-01-01,2,\n').encode()
    _upload(fresh_client, csv_data, 'dup.csv', CSV_MAPPING)
    rows = _rows(fresh_app)
    assert sorted(r['item_name'] for r in rows) == ['Item A', 'Item B']

    # The same item twice under one tracking number is still refused
    conn = _conn(fresh_app)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO packages (tracking_number, item_name) VALUES ('1ZSAME', 'Item A')")
    conn.close()

    # One scan receives both items
    from app.services.data_manager import log_receipt
    with fresh_app.app_context():
        log_receipt('1ZSAME', 'Item A', '1', 'admin')
    assert {r['status'] for r in _rows(fresh_app)} == {'received'}


def test_ebay_export_uses_duplicate_tracking_column():
    from app.services import package_imports as pi
    data = ('TrackingNumber,ItemName,Quantity,TrackingNumber\n'
            '26-14075-32104,Camera,1,9400100000000000000001\n'
            '26-99999-11111,Lens,1,\n').encode()
    headers, rows = pi.read_csv_bytes(data)
    assert headers == ['TrackingNumber', 'ItemName', 'Quantity', 'TrackingNumber.1']
    items, skipped = pi.items_from_mapping(
        headers, rows, {'tracking': 'TrackingNumber', 'name': 'ItemName', 'quantity': 'Quantity'})
    assert skipped == 0
    by_name = {i['item_name']: i for i in items}
    assert by_name['Camera']['tracking'] == '9400100000000000000001'
    assert by_name['Camera']['order_id'] == '26-14075-32104'
    assert not by_name['Camera']['placeholder']
    assert by_name['Lens']['tracking'] == '26-99999-11111' and by_name['Lens']['placeholder']


ORDER = '113-1234567-1234567'


def test_email_placeholder_gets_real_tracking_from_shipping_email(fresh_app, monkeypatch):
    from app.services import package_imports as pi
    from app.services import data_manager

    monkeypatch.setattr(data_manager, 'load_config', lambda *a, **k: {
        'EMAIL_INGEST_ENABLED': True, 'IMAP_SERVER': 'imap.test', 'EMAIL_USER': 'u', 'EMAIL_PASS': 'p'})
    order_email = [{'tracking': f'ORDER-{ORDER}-01', 'order_id': ORDER, 'carrier_tracking': None,
                    'name': 'Garden Hose 50ft', 'image_url': 'http://img/hose.jpg', 'quantity': 2,
                    'date': '2030-01-01'}]
    with fresh_app.app_context():
        result = pi.run_email_ingest(fetch=lambda *a: order_email)
    assert result['status'] == 'success' and result['added'] == 1
    placeholder = _rows(fresh_app)[0]
    assert placeholder['tracking_number'] == f'ORDER-{ORDER}-01'
    assert placeholder['source'] == 'email' and placeholder['order_id'] == ORDER

    # Staff flag it as priority while it is on the way
    conn = _conn(fresh_app)
    conn.execute('UPDATE packages SET priority = 1 WHERE id = ?', (placeholder['id'],))
    conn.commit()
    conn.close()

    shipped = [dict(order_email[0], carrier_tracking='1ZSHIPPED123')]
    with fresh_app.app_context():
        result = pi.run_email_ingest(fetch=lambda *a: shipped)
    assert result['added'] == 0 and result['updated'] == 1
    rows = _rows(fresh_app)
    assert len(rows) == 1
    assert rows[0]['id'] == placeholder['id']
    assert rows[0]['tracking_number'] == '1ZSHIPPED123'
    assert rows[0]['priority'] == 1

    imports = _rows(fresh_app, 'SELECT source, rows_added, rows_updated FROM package_imports ORDER BY id')
    assert [(i['source'], i['rows_added'], i['rows_updated']) for i in imports] == \
        [('email', 1, 0), ('email', 0, 1)]

    # The same emails again change nothing and log nothing
    with fresh_app.app_context():
        pi.run_email_ingest(fetch=lambda *a: shipped)
    assert len(_rows(fresh_app)) == 1
    assert len(_rows(fresh_app, 'SELECT * FROM package_imports')) == 2


def test_upload_gives_placeholder_its_real_tracking(fresh_app, fresh_client):
    from app.services import package_imports as pi
    conn = _conn(fresh_app)
    pi.ingest_email_items(conn, [{'tracking': f'ORDER-{ORDER}-01', 'order_id': ORDER,
                                  'name': 'Garden Hose', 'image_url': 'http://img/hose.jpg',
                                  'quantity': 3, 'date': '2030-01-01'}])
    conn.commit()
    ph_id = conn.execute('SELECT id FROM packages').fetchone()['id']
    conn.close()

    login(fresh_client, 1)
    csv_data = (CSV_HEADER + f'1ZREAL999,Garden Hose 50 ft Green,2030-01-09,1,{ORDER}\n').encode()
    _upload(fresh_client, csv_data, 'amazon.csv', CSV_MAPPING)

    rows = _rows(fresh_app)
    assert len(rows) == 1
    row = rows[0]
    assert row['id'] == ph_id
    assert row['tracking_number'] == '1ZREAL999'
    assert row['item_name'] == 'Garden Hose 50 ft Green'
    assert row['image_url'] == 'http://img/hose.jpg'   # kept from the email
    assert row['quantity'] == 3                        # the email's quantity is trusted
    assert row['date_expected'] == '2030-01-09'


def test_email_after_upload_enriches_instead_of_duplicating(fresh_app, fresh_client):
    from app.services import package_imports as pi
    login(fresh_client, 1)
    csv_data = (CSV_HEADER + f'1ZUP1,Garden Hose,2030-01-09,1,{ORDER}\n').encode()
    _upload(fresh_client, csv_data, 'amazon.csv', CSV_MAPPING)

    conn = _conn(fresh_app)
    added, updated = pi.ingest_email_items(conn, [{
        'tracking': f'ORDER-{ORDER}-01', 'order_id': ORDER, 'name': 'Hose, garden, 50ft',
        'image_url': 'http://img/hose.jpg', 'quantity': 1, 'date': '2030-01-01'}])
    conn.commit()
    conn.close()
    assert (added, updated) == (0, 1)
    rows = _rows(fresh_app)
    assert len(rows) == 1 and rows[0]['image_url'] == 'http://img/hose.jpg'


def test_extract_tracking_number_needs_a_label():
    from email_ingest import extract_tracking_number
    assert extract_tracking_number('Your Tracking number: 1Z999AA10123456784 ships') == '1Z999AA10123456784'
    assert extract_tracking_number('Tracking ID TBA301234567890') == 'TBA301234567890'
    assert extract_tracking_number('Order # 113-1234567-1234567 total $12.00') is None
    assert extract_tracking_number('Tracking number available soon') is None


def test_status_refresh_follows_the_calendar(fresh_app):
    from app.services import package_imports as pi
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    today = datetime.date.today().isoformat()
    conn = _conn(fresh_app)
    conn.executemany("INSERT INTO packages (tracking_number, item_name, status, source, date_expected) "
                     "VALUES (?, ?, 'on_time', ?, ?)",
                     [('A', 'a', 'manifest', yesterday), ('B', 'b', 'manifest', today),
                      ('C', 'c', 'email', yesterday)])
    pi.refresh_package_statuses(conn)
    conn.commit()
    status = {r['tracking_number']: r['status'] for r in conn.execute('SELECT * FROM packages')}
    conn.close()
    assert status == {'A': 'past_due', 'B': 'expected', 'C': 'on_time'}


def test_dashboard_shows_last_import(fresh_app, fresh_client):
    login(fresh_client, 1)
    resp = fresh_client.get('/receiving')
    assert resp.status_code == 200
    assert b'None yet' in resp.data
    assert b'Manifest' not in resp.data

    _upload(fresh_client, (CSV_HEADER + '1ZDASH,Thing,2030-01-01,1,\n').encode(), 'd.csv', CSV_MAPPING)
    resp = fresh_client.get('/receiving')
    assert resp.status_code == 200
    assert b'Last import (upload): 0.0h' in resp.data


def test_manual_add_allows_second_item_and_logs_import(fresh_app, fresh_client):
    login(fresh_client, 1)
    for name in ('Item A', 'Item B'):
        resp = fresh_client.post('/admin/add_manual_item', data={'tracking': '1ZMAN', 'name': name})
        assert resp.status_code == 302
    assert len(_rows(fresh_app, "SELECT * FROM packages WHERE tracking_number = '1ZMAN'")) == 2
    imports = _rows(fresh_app, 'SELECT source, user FROM package_imports')
    assert imports == [{'source': 'manual', 'user': 'admin'}] * 2


# ---------------------------------------------------------------------------
# Migration 10
# ---------------------------------------------------------------------------

def _old_schema_db(path):
    """A database at schema v1 with the pre-3.0 UNIQUE(tracking_number) table."""
    from app.services.migration import ensure_db_ready
    ensure_db_ready(path)
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA user_version = 1')
    conn.executemany('''
        INSERT INTO packages (id, tracking_number, item_name, status, source, date_expected,
                              quantity, priority, date_scanned, image_url, sku)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', [
        (5, '1ZOLD1', 'Old Lamp', 'received', 'manifest', '2025-01-01', 1, 1, '2025-01-02 10:00:00', 'http://i/1', 'SKU1'),
        (9, f'ORDER-{ORDER}-01', 'Email Thing', 'incoming', 'auto_email', '2025-02-01', 2, 0, None, '', None),
        (12, 'ORDER-26-14075-32104-01', 'Ebay Thing', 'expected', 'auto-email', '2025-02-02', 1, 0, None, '', None),
    ])
    conn.execute("INSERT INTO history (package_id, user_id, action) VALUES (5, 1, 'received')")
    conn.execute("UPDATE sqlite_sequence SET seq = 40 WHERE name = 'packages'")
    conn.commit()
    unique_cols = []
    for idx in conn.execute('PRAGMA index_list(packages)').fetchall():
        if idx[2]:
            unique_cols.append([r[2] for r in conn.execute(f'PRAGMA index_info("{idx[1]}")')])
    conn.close()
    assert ['tracking_number'] in unique_cols   # really the old table


def test_migration_10_rebuilds_table_and_imports_legacy_manifest(tmp_path, monkeypatch):
    from app.services import migration
    db = str(tmp_path / 'old.db')
    _old_schema_db(db)

    manifest = tmp_path / 'manifest.csv'
    manifest.write_text('TrackingNumber,ItemName,Date,Quantity\n'
                        '1ZOLD1,Old Lamp,2025-01-01,1\n'          # already in packages
                        '1ZNEW1,New Rug,2030-05-01,2\n'
                        '1ZNEW1,Rug Pad,2030-05-01,1\n', encoding='utf-8')
    monkeypatch.setattr(migration, '_m10_legacy_manifest_paths', lambda: [str(manifest)])

    migration.run_migrations(db)

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    assert conn.execute('PRAGMA user_version').fetchone()[0] == migration.LATEST_SCHEMA_VERSION
    rows = {r['id']: dict(r) for r in conn.execute('SELECT * FROM packages')}

    old = rows[5]
    assert (old['tracking_number'], old['item_name'], old['status'], old['priority'],
            old['date_scanned'], old['image_url'], old['sku']) == \
        ('1ZOLD1', 'Old Lamp', 'received', 1, '2025-01-02 10:00:00', 'http://i/1', 'SKU1')
    assert rows[9]['source'] == 'email' and rows[9]['status'] == 'on_time'
    assert rows[9]['order_id'] == ORDER
    assert rows[12]['source'] == 'email' and rows[12]['order_id'] == '26-14075-32104'

    new = sorted((r['tracking_number'], r['item_name'], r['quantity']) for i, r in rows.items() if i > 12)
    assert new == [('1ZNEW1', 'New Rug', 2), ('1ZNEW1', 'Rug Pad', 1)]
    assert min(i for i in rows if i > 12) > 40   # AUTOINCREMENT high-water mark kept
    assert len(rows) == 5

    indexes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='packages'")}
    for name in ('idx_packages_tracking_nocase', 'idx_tracking', 'idx_status', 'idx_date_expected',
                 'idx_packages_tracking_item', 'idx_packages_order_id'):
        assert name in indexes
    plan = ' '.join(r[3] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM packages WHERE tracking_number = 'x' COLLATE NOCASE"))
    assert 'idx_packages_tracking_nocase' in plan

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO packages (tracking_number, item_name) VALUES ('1ZNEW1', 'New Rug')")
    conn.execute("INSERT INTO packages (tracking_number, item_name) VALUES ('1ZOLD1', 'Second Item')")

    assert conn.execute('SELECT package_id FROM history').fetchone()[0] == 5
    imports = [dict(r) for r in conn.execute('SELECT source, filename, rows_added, user FROM package_imports')]
    assert imports == [{'source': 'upload', 'filename': 'manifest.csv (legacy import)',
                        'rows_added': 2, 'user': 'system'}]
    assert manifest.exists()   # left in place

    # Importing the same file again adds nothing
    from app.services import package_imports as pi
    conn.rollback()
    assert pi.import_legacy_manifest(conn, str(manifest)) == 0
    conn.close()


def test_migration_10_survives_unreadable_manifest(tmp_path, monkeypatch):
    from app.services import migration
    db = str(tmp_path / 'old.db')
    _old_schema_db(db)
    bad = tmp_path / 'manifest.csv'
    bad.write_bytes(b'PK\x03\x04 this is an xlsx')
    monkeypatch.setattr(migration, '_m10_legacy_manifest_paths', lambda: [str(bad)])

    migration.run_migrations(db)
    conn = sqlite3.connect(db)
    assert conn.execute('PRAGMA user_version').fetchone()[0] == migration.LATEST_SCHEMA_VERSION
    assert conn.execute('SELECT count(*) FROM packages').fetchone()[0] == 3
    conn.close()
