"""Stock +/- on the inventory overview ("Items Needing Attention") and the
Low Stock & Out of Stock report, via /inventory/api/quick_adjust."""
import os

import pytest

from app.services import inventory as inv
from app.services.db import get_db_connection
from tests.conftest import login

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture
def conn(fresh_app):
    c = get_db_connection(fresh_app.config['DATABASE'])
    c.isolation_level = None
    yield c
    c.close()


def _item(conn, sku, qty, threshold=0):
    return inv.create_item(conn, {'sku': sku, 'name': f'{sku} item', 'quantity': qty,
                                  'alert_threshold': threshold, 'location_area': 'A'}, user='admin')


def _last_movement(conn, item_id):
    return conn.execute('SELECT delta, reason, note FROM inventory_movements WHERE item_id = ? '
                        'ORDER BY id DESC LIMIT 1', (item_id,)).fetchone()


@pytest.mark.parametrize('source, note', [
    ('overview', 'Inventory overview'),
    ('low_stock', 'Low stock report'),
    (None, 'Scan screen'),
    ('anything-else', 'Scan screen'),
])
def test_quick_adjust_records_where_it_came_from(fresh_client, conn, source, note):
    item_id = _item(conn, 'ST-1', 2, threshold=5)
    client = login(fresh_client, 1)
    body = {'change': 1} if source is None else {'change': 1, 'source': source}
    resp = client.post(f'/inventory/api/quick_adjust/{item_id}', json=body)
    assert resp.get_json() == {'success': True, 'new_quantity': 3, 'name': 'ST-1 item'}
    assert tuple(_last_movement(conn, item_id)) == (1, inv.REASON_RECEIVE, note)

    resp = client.post(f'/inventory/api/quick_adjust/{item_id}', json={'change': -1, 'source': source})
    assert resp.get_json()['new_quantity'] == 2
    assert tuple(_last_movement(conn, item_id))[:2] == (-1, inv.REASON_CONSUMED)


@pytest.mark.parametrize('change', ['abc', None, [], 0, 1001, -1001])
def test_quick_adjust_rejects_bad_changes(fresh_client, conn, change):
    item_id = _item(conn, 'ST-2', 4)
    client = login(fresh_client, 1)
    resp = client.post(f'/inventory/api/quick_adjust/{item_id}', json={'change': change, 'source': 'overview'})
    assert resp.status_code == 400 and 'error' in resp.get_json()
    assert conn.execute('SELECT quantity FROM inventory_items WHERE id = ?', (item_id,)).fetchone()[0] == 4


def test_minus_at_zero_is_refused(fresh_client, conn):
    item_id = _item(conn, 'ST-3', 0)
    client = login(fresh_client, 1)
    resp = client.post(f'/inventory/api/quick_adjust/{item_id}', json={'change': -1, 'source': 'low_stock'})
    assert resp.status_code == 400


def test_overview_attention_list_has_steppers(fresh_client, conn):
    out_id = _item(conn, 'ST-OUT', 0)
    low_id = _item(conn, 'ST-LOW', 2, threshold=4)
    client = login(fresh_client, 1)
    html = client.get('/inventory/').get_data(as_text=True)
    assert f'data-item-id="{out_id}" data-source="overview"' in html
    assert f'data-item-id="{low_id}" data-source="overview" data-threshold="4"' in html
    assert f'class="stock-status" data-item-id="{low_id}"' in html
    assert 'data-action="stockStep" data-delta="-1"' in html and 'data-action="stockStep" data-delta="1"' in html


def test_low_stock_report_has_steppers(fresh_client, conn):
    out_id = _item(conn, 'ST-OUT2', 0, threshold=3)
    low_id = _item(conn, 'ST-LOW2', 1, threshold=3)
    client = login(fresh_client, 1)
    html = client.get('/inventory/low-stock').get_data(as_text=True)
    assert f'data-item-id="{out_id}" data-source="low_stock" data-threshold="3"' in html
    assert f'data-item-id="{low_id}" data-source="low_stock"' in html
    assert 'Out of Stock (0)' in html and 'Low (1)' in html


def test_stepper_script_is_in_shared_inventory_js():
    with open(os.path.join(ROOT, 'static', 'js', 'pages', 'inventory-common.js'), encoding='utf-8') as f:
        js = f.read()
    assert 'stockStep' in js and '/inventory/api/quick_adjust/' in js and 'X-CSRFToken' in js
