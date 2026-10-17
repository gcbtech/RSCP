import os
import sqlite3
import sys

import pytest

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.db import get_db_connection

BASE_CONFIG = {'INVENTORY_ENABLED': True, 'INVENTORY_QUICK_ADD': True, 'LOW_STOCK_THRESHOLD': 5}


def _make_app(monkeypatch, config):
    from app import create_app
    import app as app_pkg
    import app.routes.inventory.core as core
    import app.routes.inventory.items as items

    for module in (app_pkg, core, items):
        monkeypatch.setattr(module, 'load_config', lambda: config)

    db_path = os.path.join(os.path.dirname(__file__), 'test_quick_add_price_hint.db')
    if os.path.exists(db_path):
        os.remove(db_path)
    app = create_app(test_config={'TESTING': True, 'DATABASE': db_path,
                                  'WTF_CSRF_ENABLED': False, 'SECRET_KEY': 'test_key'})
    with app.app_context():
        from app.services.migration import run_migrations
        run_migrations()
        conn = get_db_connection()
        conn.execute('INSERT INTO users (id, username, password_hash, is_admin, roles) VALUES (1, ?, ?, 1, ?)',
                     ('admin', 'pbkdf2:sha256:somehash', '["super_admin"]'))
        conn.commit()
        conn.close()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = '1'
        sess['user'] = 1
    return client, db_path


def _quick_add_html(monkeypatch, config):
    client, db_path = _make_app(monkeypatch, config)
    try:
        resp = client.get('/inventory/items')
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
    finally:
        os.remove(db_path)
    start = html.index('<!-- Quick Add Form -->')
    end = html.index('})();', start) + len('})();\n    </script>')
    return html[start:end]


def test_price_hint_uses_preferred_margin(monkeypatch):
    snippet = _quick_add_html(monkeypatch, {**BASE_CONFIG, 'PREFERRED_MARGIN_ENABLED': True, 'PREFERRED_MARGIN_PERCENT': 40.0})
    assert 'id="quickAddBuyPrice"' in snippet and 'id="quickAddSellPrice"' in snippet
    assert 'const marginEnabled = true;' in snippet
    assert 'const marginPercent = 40.0;' in snippet


def test_price_hint_disabled_when_margin_setting_off(monkeypatch):
    snippet = _quick_add_html(monkeypatch, {**BASE_CONFIG, 'PREFERRED_MARGIN_ENABLED': False})
    assert 'const marginEnabled = false;' in snippet


AREAS = {'LOCATION_OPTIONS': {'area': ['Retail', 'Build']}}


def test_retail_preselected_when_configured(monkeypatch):
    snippet = _quick_add_html(monkeypatch, {**BASE_CONFIG, **AREAS})
    assert '<option value="Retail" selected>Retail</option>' in snippet
    assert '<option value="Build" >Build</option>' in snippet


def test_quick_add_category_defaults_to_secondary(monkeypatch):
    snippet = _quick_add_html(monkeypatch, {**BASE_CONFIG, **AREAS})
    category = snippet.split('name="category"')[1].split('</select>')[0]
    assert '<option value="SEC" selected>Secondary</option>' in category
    assert category.count(' selected') == 1


def test_no_preselect_when_retail_not_an_area(monkeypatch):
    snippet = _quick_add_html(monkeypatch, {**BASE_CONFIG, 'LOCATION_OPTIONS': {'area': ['Front', 'Back']}})
    assert ' selected>' not in snippet.split('id="quickAddLocation"')[1].split('</select>')[0]


@pytest.mark.parametrize('areas, submitted, expected', [
    (['Retail', 'Build'], '', 'Retail'),       # blank -> Retail when configured
    (['Retail', 'Build'], 'Build', 'Build'),   # explicit choice is kept
    (['Front', 'Back'], '', 'General'),        # Retail not configured -> old fallback
])
def test_quick_add_saved_area(monkeypatch, areas, submitted, expected):
    client, db_path = _make_app(monkeypatch, {**BASE_CONFIG, 'LOCATION_OPTIONS': {'area': areas}})
    try:
        client.post('/inventory/add', data={'quick_add': '1', 'name': 'Quick Widget', 'category': 'PRI',
                                            'quantity': '1', 'buy_price': '100', 'location_area': submitted})
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT location_area, sku FROM inventory_items WHERE name = 'Quick Widget'").fetchone()
        conn.close()
    finally:
        os.remove(db_path)
    assert row is not None
    assert row[0] == expected and row[1].startswith('RSCP-PRI-')
