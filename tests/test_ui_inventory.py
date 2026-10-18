"""
Inventory pages render through the shared layout (templates/base.html).

Each page must come out as ONE well-formed document: a single doctype,
<html lang="en">, Bootstrap CSS/JS exactly once (unless the page opts out),
and only pinned third-party assets from cdn.jsdelivr.net (the CSP allows
nothing else).
"""
import re

import pytest

from tests.conftest import login

BOOTSTRAP_CSS = 'bootstrap@5.3.0/dist/css/bootstrap.min.css'
BOOTSTRAP_JS = 'bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js'
NAV_MARKER = 'id="quickSidebar"'  # only rendered by _nav.html


@pytest.fixture
def seeded(fresh_app):
    """One normal item, one low-stock item, an active and a completed audit."""
    from app.services.db import get_db_connection
    with fresh_app.app_context():
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('INSERT INTO inventory_items (sku, name, sell_price, quantity) VALUES (?, ?, ?, ?)',
                    ('UI-SKU-1', 'UI Widget', 9.99, 25))
        item_id = cur.lastrowid
        cur.execute('INSERT INTO inventory_items (sku, name, sell_price, quantity, alert_enabled, alert_threshold) '
                    'VALUES (?, ?, ?, ?, ?, ?)', ('UI-SKU-LOW', 'UI Low Widget', 5.0, 1, 1, 5))
        cur.execute("INSERT INTO audit_sessions (user_id, mode, status) VALUES ('admin', 'shelf', 'active')")
        active_id = cur.lastrowid
        cur.execute("INSERT INTO audit_sessions (user_id, mode, status, end_time) "
                    "VALUES ('admin', 'item', 'completed', CURRENT_TIMESTAMP)")
        done_id = cur.lastrowid
        cur.execute('INSERT INTO audit_records (session_id, item_id, sku, name, expected_qty, counted_qty) '
                    'VALUES (?, ?, ?, ?, ?, ?)', (done_id, item_id, 'UI-SKU-1', 'UI Widget', 25, 24))
        conn.commit()
        conn.close()
    return {'item_id': item_id, 'active_id': active_id, 'done_id': done_id}


@pytest.fixture
def admin(fresh_client):
    return login(fresh_client, 1)


def _assert_layout(html, *, nav=True, bootstrap_js=True):
    assert html.count('<!DOCTYPE html>') == 1
    assert html.count('<html') == 1
    assert '<html lang="en">' in html
    assert html.count('</html>') == 1
    assert html.count(BOOTSTRAP_CSS) == 1
    assert html.count('bootstrap-icons@1.10.0') == 1
    assert html.count(BOOTSTRAP_JS) == (1 if bootstrap_js else 0)
    assert ('css/rscp.css' in html) and ('js/rscp-common.js' in html)
    assert (NAV_MARKER in html) == nav
    # Every external asset is pinned and served from the CSP-allowed CDN.
    for url in re.findall(r'(?:src|href)="(https?://[^"]+\.(?:js|css))"', html):
        assert url.startswith('https://cdn.jsdelivr.net/npm/'), url
        assert '@' in url, 'unpinned asset: ' + url


def _static_js(rel):
    import os
    path = os.path.join(os.path.dirname(__file__), '..', 'static', 'js', *rel.split('/'))
    with open(path, encoding='utf-8') as f:
        return f.read()


def _get(client, url):
    resp = client.get(url)
    assert resp.status_code == 200, (url, resp.status_code)
    return resp.get_data(as_text=True)


@pytest.mark.parametrize('url, title, marker', [
    ('/inventory/', 'RSCP | Inventory Overview', 'id="salesChart"'),
    ('/inventory/alerts', 'RSCP | Stock Alerts', 'Stock Alert Settings'),
    ('/inventory/audit', 'RSCP | Inventory Audit', 'Resume Audit'),
    ('/inventory/audit/history', 'RSCP | Audit History', 'View Report'),
    ('/inventory/import', 'RSCP | Import Inventory', 'Import Inventory Items'),
    ('/inventory/lookup', 'RSCP | Inventory Lookup', 'id="lookupInput"'),
    ('/inventory/low-stock', 'RSCP | Low Stock Report', 'UI Low Widget'),
    ('/inventory/add', 'RSCP | Add Item', 'Add Item</h2>'),
])
def test_simple_pages_use_layout(seeded, admin, url, title, marker):
    html = _get(admin, url)
    _assert_layout(html)
    assert '<title>' + title + '</title>' in html
    assert marker in html


def test_edit_item(seeded, admin):
    html = _get(admin, '/inventory/edit/%d' % seeded['item_id'])
    _assert_layout(html)
    assert '<title>RSCP | Edit Item</title>' in html
    assert 'UI-SKU-1' in html
    assert html.count('tom-select@2.2.2/dist/js/tom-select.complete.min.js') == 1
    assert html.count('tom-select@2.2.2/dist/css/tom-select.bootstrap5.min.css') == 1


def test_list_page_keeps_ajax_targets(seeded, admin):
    html = _get(admin, '/inventory/items')
    _assert_layout(html)
    assert '<title>RSCP | Inventory Items</title>' in html
    # list.html's DOMParser loader swaps these parts of the fetched full page.
    assert html.count('id="inventoryBody"') == 1
    assert 'sort-controls' in html
    assert 'UI Widget' in html
    assert 'id="imageModal"' in html
    # Page code lives in static JS (no inline scripts), still escaping inserted values.
    list_js = _static_js('pages/inventory-list.js')
    assert 'escapeHtml(newSku)' in list_js
    assert 'function loadInventoryPage' in list_js and "new TomSelect('#quickAddLocation'" in list_js
    # Page scripts run after the Bootstrap bundle and Tom Select.
    assert html.index(BOOTSTRAP_JS) < html.index('js/pages/inventory-list.js')
    assert html.index('tom-select.complete.min.js') < html.index('js/pages/inventory-list.js')
    assert html.index('id="inventoryBody"') < html.index(BOOTSTRAP_JS)


def test_list_partial_stays_a_fragment(seeded, admin):
    html = _get(admin, '/inventory/items?partial=1')
    assert '<!DOCTYPE' not in html and '<html' not in html
    assert 'UI Widget' in html


def test_overview_uses_pinned_chartjs(seeded, admin):
    html = _get(admin, '/inventory/')
    assert 'chart.js@4.4.0/dist/chart.umd.min.js' in html
    assert 'npm/chart.js"' not in html
    assert 'new Chart(' in _static_js('pages/inventory-overview.js')
    assert html.index('chart.umd.min.js') < html.index('js/pages/inventory-overview.js')


def test_lookup_uses_shared_escape(seeded, admin):
    import os
    html = _get(admin, '/inventory/lookup')
    assert 'js/pages/inventory-lookup.js' in html
    lookup_js = _static_js('pages/inventory-lookup.js')
    assert 'escapeHtml(item.image_url)' in lookup_js
    # The page relies on window.escapeHtml from rscp-common.js (the copy in
    # _nav.html is private to an IIFE), so it must not define its own.
    src = os.path.join(os.path.dirname(__file__), '..', 'templates', 'inventory', 'lookup.html')
    with open(src, encoding='utf-8') as f:
        assert 'function escapeHtml' not in f.read()
    assert 'function escapeHtml' not in lookup_js


def test_import_keeps_body_class(seeded, admin):
    html = _get(admin, '/inventory/import')
    assert '<body class="bg-light">' in html


def test_audit_report(seeded, admin):
    html = _get(admin, '/inventory/audit/report/%d' % seeded['done_id'])
    _assert_layout(html)
    assert '<title>RSCP | Audit Report</title>' in html
    assert 'Audit Report #%d' % seeded['done_id'] in html


def test_audit_live_is_full_screen(seeded, admin):
    html = _get(admin, '/inventory/audit/live/%d' % seeded['active_id'])
    _assert_layout(html, nav=False)
    assert '<title>RSCP | Audit Live</title>' in html
    assert 'Ready to Scan' in html
    # new bootstrap.Modal(...) runs at parse time: the bundle must load first.
    assert 'new bootstrap.Modal' in _static_js('pages/inventory-audit-live.js')
    assert html.index(BOOTSTRAP_JS) < html.index('js/pages/inventory-audit-live.js')


def test_add_stock_has_no_nav_or_bootstrap_js(seeded, admin):
    html = _get(admin, '/inventory/add_stock/%d?qty=3' % seeded['item_id'])
    _assert_layout(html, nav=False, bootstrap_js=False)
    assert '<title>RSCP | Add Stock</title>' in html
    assert 'Add Stock to Inventory' in html
    assert html.index('js/dymo-print.js') < html.index('js/pages/inventory-add-stock.js')
    assert 'function printDymoLabel' in _static_js('dymo-print.js')


def test_bulk_edit(seeded, admin):
    resp = admin.post('/inventory/bulk-edit', data={'item_ids': [str(seeded['item_id'])]})
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    _assert_layout(html)
    assert '<title>RSCP | Bulk Edit Items</title>' in html
    assert 'Bulk Edit Inventory' in html
    assert 'new TomSelect' in _static_js('pages/inventory-bulk-edit.js')
    assert html.index('tom-select.complete.min.js') < html.index('js/pages/inventory-bulk-edit.js')
