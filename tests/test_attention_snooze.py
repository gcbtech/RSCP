import pytest
import os
import sys
from datetime import datetime, timedelta

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.db import get_db_connection

TEST_CONFIG = {'INVENTORY_ENABLED': True, 'LOW_STOCK_THRESHOLD': 5}


@pytest.fixture
def app(monkeypatch):
    """Create application for testing with an isolated database."""
    from app import create_app
    import app.routes.inventory.core as core

    monkeypatch.setattr(core, 'load_config', lambda: TEST_CONFIG)

    db_path = os.path.join(os.path.dirname(__file__), 'test_attention_snooze.db')
    if os.path.exists(db_path):
        os.remove(db_path)

    app = create_app(test_config={
        'TESTING': True,
        'DATABASE': db_path,
        'WTF_CSRF_ENABLED': False,
        'SECRET_KEY': 'test_key'
    })

    with app.app_context():
        from app.services.migration import run_migrations
        run_migrations()

        conn = get_db_connection()
        conn.execute('INSERT INTO users (id, username, password_hash, is_admin, roles) VALUES (?, ?, ?, ?, ?)',
                     (1, 'admin', 'pbkdf2:sha256:somehash', 1, '["super_admin"]'))
        conn.execute('INSERT INTO users (id, username, password_hash, is_admin, roles) VALUES (?, ?, ?, ?, ?)',
                     (2, 'operator', 'pbkdf2:sha256:somehash', 0, '["operator"]'))
        conn.execute("INSERT INTO inventory_items (sku, name, quantity) VALUES ('OOS1', 'Snooze Me Widget', 0)")
        conn.execute("INSERT INTO inventory_items (sku, name, quantity) VALUES ('LOW1', 'Still Low Gadget', 2)")
        conn.commit()
        conn.close()

    yield app

    if os.path.exists(db_path):
        os.remove(db_path)


def _login(client, user_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)
        sess['user'] = user_id
    return client


def _item(app, sku):
    with app.app_context():
        conn = get_db_connection()
        row = conn.execute('SELECT id, attention_snoozed_until FROM inventory_items WHERE sku = ?', (sku,)).fetchone()
        conn.close()
    return row


def test_snooze_hides_item_for_five_days(app):
    client = _login(app.test_client(), 1)

    page = client.get('/inventory/').get_data(as_text=True)
    assert 'Snooze Me Widget' in page
    assert 'Still Low Gadget' in page

    item = _item(app, 'OOS1')
    resp = client.post(f'/inventory/attention/{item["id"]}/snooze', follow_redirects=True)
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert 'Snooze Me Widget' not in page.split('Items Needing Attention')[1].split('Daily Sales')[0]
    assert 'Still Low Gadget' in page
    assert '1 snoozed' in page

    until = datetime.strptime(_item(app, 'OOS1')['attention_snoozed_until'], '%Y-%m-%d %H:%M:%S')
    assert timedelta(days=4, hours=23) < until - datetime.now() <= timedelta(days=5)


def test_expired_snooze_reappears(app):
    client = _login(app.test_client(), 1)
    past = (datetime.now() - timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S')
    with app.app_context():
        conn = get_db_connection()
        conn.execute("UPDATE inventory_items SET attention_snoozed_until = ? WHERE sku = 'OOS1'", (past,))
        conn.commit()
        conn.close()

    page = client.get('/inventory/').get_data(as_text=True)
    assert 'Snooze Me Widget' in page
    assert 'snoozed' not in page


def test_all_snoozed_does_not_claim_well_stocked(app):
    client = _login(app.test_client(), 1)
    for sku in ('OOS1', 'LOW1'):
        client.post(f'/inventory/attention/{_item(app, sku)["id"]}/snooze')

    page = client.get('/inventory/').get_data(as_text=True)
    assert 'All items are well stocked' not in page
    assert 'No items need attention (2 snoozed)' in page


def _snooze_then_set_qty(app, sku, start_qty, new_qty):
    """Snooze an item at start_qty, change quantity to new_qty, return whether it is still snoozed."""
    future = (datetime.now() + timedelta(days=5)).strftime('%Y-%m-%d %H:%M:%S')
    with app.app_context():
        conn = get_db_connection()
        conn.execute('UPDATE inventory_items SET quantity = ? WHERE sku = ?', (start_qty, sku))
        conn.execute('UPDATE inventory_items SET attention_snoozed_until = ? WHERE sku = ?', (future, sku))
        conn.commit()
        conn.execute('UPDATE inventory_items SET quantity = ? WHERE sku = ?', (new_qty, sku))
        conn.commit()
        conn.close()
    return _item(app, sku)['attention_snoozed_until'] is not None


@pytest.mark.parametrize('start_qty, new_qty, still_snoozed', [
    (3, 2, True),    # stock removed, still above 0 -> keep snooze
    (3, 3, True),    # unchanged
    (3, 0, False),   # hit zero -> reset
    (2, -1, False),  # dropped below zero from positive -> reset
    (0, -1, True),   # already out when snoozed, went further negative -> keep
    (0, 4, False),   # stock added -> reset
    (2, 3, False),   # stock added while low -> reset
])
def test_snooze_reset_rules(app, start_qty, new_qty, still_snoozed):
    assert _snooze_then_set_qty(app, 'LOW1', start_qty, new_qty) is still_snoozed


def test_snooze_reset_via_relative_update(app):
    """Covers the 'quantity = quantity + ?' style used by POS checkout, refunds, receiving."""
    future = (datetime.now() + timedelta(days=5)).strftime('%Y-%m-%d %H:%M:%S')
    with app.app_context():
        conn = get_db_connection()
        conn.execute("UPDATE inventory_items SET attention_snoozed_until = ? WHERE sku = 'LOW1'", (future,))
        conn.execute("UPDATE inventory_items SET quantity = quantity - 1 WHERE sku = 'LOW1'")  # 2 -> 1
        conn.commit()
        assert conn.execute("SELECT attention_snoozed_until FROM inventory_items WHERE sku = 'LOW1'").fetchone()[0]
        conn.execute("UPDATE inventory_items SET quantity = quantity + 5 WHERE sku = 'LOW1'")  # 1 -> 6
        conn.commit()
        assert conn.execute("SELECT attention_snoozed_until FROM inventory_items WHERE sku = 'LOW1'").fetchone()[0] is None
        conn.close()


def _attention_section(html):
    return html.split('⚠️ Items Needing Attention')[1].split('Daily Sales')[0]


def test_attention_rows_have_snooze_and_legacy_buttons(app):
    client = _login(app.test_client(), 1)
    section = _attention_section(client.get('/inventory/').get_data(as_text=True))
    item_id = _item(app, 'OOS1')['id']
    assert f'/inventory/attention/{item_id}/snooze' in section
    assert f'/inventory/toggle-legacy/{item_id}' in section
    assert section.count('💤 Snooze') == 2 and section.count('📦 Legacy') == 2

    # Marking legacy returns to the overview and drops the item from the list
    resp = client.post(f'/inventory/toggle-legacy/{item_id}', headers={'Referer': 'http://localhost/inventory/'},
                       follow_redirects=True)
    section = _attention_section(resp.get_data(as_text=True))
    assert 'Snooze Me Widget' not in section and 'Still Low Gadget' in section


def test_operator_sees_no_action_buttons(app):
    client = _login(app.test_client(), 2)   # operator: inventory.view but not inventory.manage
    resp = client.get('/inventory/')
    assert resp.status_code == 200
    section = _attention_section(resp.get_data(as_text=True))
    assert 'Snooze Me Widget' in section
    assert '💤 Snooze' not in section and '📦 Legacy' not in section


def test_operator_cannot_snooze(app):
    client = _login(app.test_client(), 2)
    item = _item(app, 'OOS1')
    client.post(f'/inventory/attention/{item["id"]}/snooze')
    assert _item(app, 'OOS1')['attention_snoozed_until'] is None
