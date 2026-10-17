"""Regression tests for issues found in the final 3.0 review."""
import datetime
import json

import pytest

from tests.conftest import login


def _db(app):
    from app.services.db import get_db_connection
    return get_db_connection(app.config['DATABASE'])


def test_legacy_encoded_webhook_url_survives_import(fresh_app, tmp_path):
    """2.x stored webhook URLs obscured; the import must keep them, not reject them."""
    from app.services import settings as settings_store
    from app.utils.helpers import obscure_string, reveal_string
    token = obscure_string('https://hooks.example.com/abc', 'legacy-key')
    cfg = tmp_path / 'config.json'
    cfg.write_text(json.dumps({'SECRET_KEY': 'legacy-key', 'WEBHOOK_URL': token, 'WEBHOOK_ENABLED': True}))
    with fresh_app.app_context():
        conn = _db(fresh_app)
        settings_store.import_legacy(conn, config_file=str(cfg))
        conn.commit()
        stored = json.loads(conn.execute("SELECT value FROM settings WHERE key='WEBHOOK_URL'").fetchone()[0])
        conn.close()
    assert stored == token
    assert reveal_string(stored, 'legacy-key') == 'https://hooks.example.com/abc'


def test_split_cash_discount_uses_cash_applied_not_handed_over(fresh_app, fresh_client):
    from app.routes.pos.core import set_pos_setting
    with fresh_app.app_context():
        conn = _db(fresh_app)
        conn.execute("INSERT OR REPLACE INTO pos_settings (key, value) VALUES ('TAX_RATE', '0')")
        cur = conn.execute("INSERT INTO inventory_items (sku, name, sell_price, quantity) VALUES ('S54', 'Item', 54, 5)")
        item_id = cur.lastrowid
        conn.commit()
        conn.close()
        set_pos_setting('TAX_RATE', '0')
        set_pos_setting('CASH_DISCOUNT_ENABLED', 'true')
        set_pos_setting('CASH_DISCOUNT_TYPE', 'percent')
        set_pos_setting('CASH_DISCOUNT_AMOUNT', '3')
    login(fresh_client, 2)
    with fresh_client.session_transaction() as sess:
        sess['pos_cart'] = {'items': [{'inventory_item_id': item_id, 'sku': 'S54', 'name': 'Item', 'quantity': 1,
                                       'unit_price': 54.0, 'discount_amount': 0, 'discount_type': None,
                                       'line_total': 54.0}],
                            'discount_amount': 0, 'discount_type': None, 'discount_reason': ''}
    fresh_client.post('/pos/checkout/process', data={'payment_method': 'split', 'cash_amount': '100',
                                                     'card_amount': ['34']})
    conn = _db(fresh_app)
    order = conn.execute('SELECT total, payment_details FROM pos_orders').fetchone()
    conn.close()
    assert order is not None
    # $20 of cash pays the sale: 3% of 20 = 0.60, not 3% of 100 = 3.00
    assert order['total'] == pytest.approx(53.40)
    assert json.loads(order['payment_details'])['cash_discount'] == pytest.approx(0.60)


def test_legacy_manifest_skips_known_shipments(fresh_app, tmp_path):
    from app.services import package_imports
    today = datetime.date.today().strftime('%Y-%m-%d')
    with fresh_app.app_context():
        conn = _db(fresh_app)
        conn.execute("INSERT INTO packages (tracking_number, item_name, status, date_expected, date_scanned) "
                     "VALUES ('1Z001', 'Widget A1', 'received', ?, CURRENT_TIMESTAMP)", (today,))
        conn.commit()
        csv_path = tmp_path / 'manifest.csv'
        csv_path.write_text('Tracking Number,Item Name,Date\n'
                            f'1Z001,Widget A2,{today}\n'
                            f'1Z999,Brand New,{today}\n')
        added = package_imports.import_legacy_manifest(conn, str(csv_path))
        conn.commit()
        rows = {r[0]: r[1] for r in conn.execute('SELECT item_name, tracking_number FROM packages')}
        conn.close()
    assert added == 1
    assert 'Brand New' in rows and 'Widget A2' not in rows


def test_migration_8_partial_refunds_get_no_tax(tmp_path):
    import sqlite3
    from app.services.migration import _m8_pos_ledger
    db = sqlite3.connect(str(tmp_path / 'm8.db'), isolation_level=None)
    db.executescript('''
        CREATE TABLE pos_orders (id INTEGER PRIMARY KEY, order_number TEXT, status TEXT, subtotal REAL,
            tax_rate REAL, tax_amount REAL, discount_amount REAL, total REAL, payment_method TEXT,
            payment_details TEXT, operator_id INTEGER, created_at TEXT);
        CREATE TABLE pos_refunds (id INTEGER PRIMARY KEY, order_id INTEGER, refund_type TEXT, amount REAL,
            reason TEXT, manager_id INTEGER, created_at TEXT);
        INSERT INTO pos_orders VALUES (1, 'A', 'completed', 100, 0.08, 8, 0, 108, 'card', '{}', 1, '2026-10-01 12:00:00');
        INSERT INTO pos_refunds VALUES (1, 1, 'partial', 25, 'other', 1, '2026-10-02 12:00:00');
        INSERT INTO pos_refunds VALUES (2, 1, 'full', 83, 'other', 1, '2026-10-03 12:00:00');
    ''')
    db.execute('BEGIN')
    _m8_pos_ledger(db)
    db.execute('COMMIT')
    taxes = dict(db.execute('SELECT id, tax_refunded FROM pos_refunds').fetchall())
    assert taxes[1] == 0.0
    assert taxes[2] == pytest.approx(83 * 8 / 108, abs=0.01)
