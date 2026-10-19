"""
Inventory pages carry no inline JavaScript, so the Content-Security-Policy can
drop 'unsafe-inline' from script-src.

Every inventory page (and the ?partial=1 rows fragment the list page swaps in)
must have:
  - no <script> without src, except <script type="application/json"> data blocks
  - no on* event-handler attributes
  - no javascript: URLs
and every data-action / data-change / ... name it uses must be registered with
RSCP.actions() by one of the scripts the page loads.

_nav.html is stubbed out here: it belongs to the core-pages workstream and is
checked there.
"""
import json
import os
import re
from html.parser import HTMLParser

import pytest
from jinja2 import ChoiceLoader, DictLoader

from tests.conftest import login

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
STATIC = os.path.join(ROOT, 'static')
HANDLER_ATTRS = ('data-action', 'data-change', 'data-input', 'data-submit',
                 'data-keyup', 'data-keydown', 'data-focusout')
URL_ATTRS = ('href', 'src', 'action', 'formaction', 'data')

TRICKY_NAME = 'O\'Brien "Deluxe" <Widget> & Co'


class _Scanner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.problems = []
        self.script_srcs = []
        self.handler_names = set()
        self._in_inline_script = False
        self._inline_body = ''

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        for name, value in attrs:
            if name.lower().startswith('on'):
                self.problems.append('<%s %s="%s">' % (tag, name, (value or '')[:60]))
            if name.lower() in URL_ATTRS and (value or '').strip().lower().startswith('javascript:'):
                self.problems.append('<%s %s="%s">' % (tag, name, value))
            if name in HANDLER_ATTRS and value:
                self.handler_names.add(value)
        if tag == 'script':
            if a.get('src'):
                self.script_srcs.append(a['src'])
            elif (a.get('type') or '').lower() != 'application/json':
                self._in_inline_script = True
                self._inline_body = ''

    def handle_data(self, data):
        if self._in_inline_script:
            self._inline_body += data

    def handle_endtag(self, tag):
        if tag == 'script' and self._in_inline_script:
            self.problems.append('inline <script>: %s' % self._inline_body.strip()[:80])
            self._in_inline_script = False


def _registered_actions(js):
    names = set()
    for block in re.findall(r'RSCP\.actions\(\{(.*?)\n\s*\}\);', js, re.S):
        names.update(re.findall(r'(?m)^\s*(\w+)\s*:', block))
    return names


def _static_file(src):
    path = src.split('?', 1)[0]
    if not path.startswith('/static/'):
        return None
    return os.path.join(STATIC, *path[len('/static/'):].split('/'))


def _scan(html):
    scanner = _Scanner()
    scanner.feed(html)
    scanner.close()
    return scanner


@pytest.fixture
def app_with_all_features(fresh_app, monkeypatch):
    """Turn on the optional inventory UI (Quick Add, margins, addons) and seed data."""
    import app as app_pkg
    import app.routes.inventory.core as core
    import app.routes.inventory.items as items
    import app.services.data_manager as dm
    from app.services.db import get_db_connection

    config = dict(dm.load_config() or {})
    config.update({
        'INVENTORY_ENABLED': True, 'INVENTORY_QUICK_ADD': True,
        'PREFERRED_MARGIN_ENABLED': True, 'PREFERRED_MARGIN_PERCENT': 35,
        'LOCATION_ALLOW_CUSTOM': True,
        'ITEM_ADDON_1_ENABLED': True, 'ITEM_ADDON_1_LABEL': 'Warranty', 'ITEM_ADDON_1_TEXT': '30 days',
    })
    for module in (app_pkg, core, items, dm):
        monkeypatch.setattr(module, 'load_config', lambda: config)

    # _nav.html is converted in another workstream; keep it out of this check.
    fresh_app.jinja_env.loader = ChoiceLoader([DictLoader({'_nav.html': ''}), fresh_app.jinja_env.loader])

    with fresh_app.app_context():
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute('INSERT INTO inventory_items (sku, name, sell_price, buy_price, quantity, image_url, '
                    'additional_images, is_legacy) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    ('NJ-SKU-1', TRICKY_NAME, 9.99, 4.0, 25, '/static/img/a.png',
                     json.dumps(['/static/img/b.png']), 1))
        item_id = cur.lastrowid
        cur.execute('INSERT INTO inventory_items (sku, name, sell_price, quantity, alert_enabled, alert_threshold, '
                    'image_url) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    ('NJ-SKU-LOW', 'Low Widget', 5.0, 1, 1, 5, '/static/img/c.png'))
        cur.execute('INSERT INTO inventory_transactions (inventory_item_id, quantity_change, reason, user_id) '
                    'VALUES (?, ?, ?, ?)', (item_id, 5, 'Received', 'admin'))
        # Stock history: an opening balance and a reversible receipt
        cur.execute("INSERT INTO inventory_movements (item_id, delta, reason, user) VALUES (?, 20, 'opening_balance', 'system')",
                    (item_id,))
        cur.execute("INSERT INTO inventory_movements (item_id, delta, reason, note, user) VALUES (?, 5, 'receive', ?, 'admin')",
                    (item_id, TRICKY_NAME))
        cur.execute("INSERT INTO audit_sessions (user_id, mode, status) VALUES ('admin', 'shelf', 'active')")
        active_id = cur.lastrowid
        cur.execute("INSERT INTO audit_sessions (user_id, mode, status, end_time) "
                    "VALUES ('admin', 'item', 'completed', CURRENT_TIMESTAMP)")
        done_id = cur.lastrowid
        cur.execute('INSERT INTO audit_records (session_id, item_id, sku, name, expected_qty, counted_qty) '
                    'VALUES (?, ?, ?, ?, ?, ?)', (done_id, item_id, 'NJ-SKU-1', TRICKY_NAME, 25, 24))
        conn.commit()
        conn.close()
    return {'item_id': item_id, 'active_id': active_id, 'done_id': done_id}


def _pages(ids):
    return [
        ('GET', '/inventory/'),
        ('GET', '/inventory/alerts'),
        ('GET', '/inventory/audit'),
        ('GET', '/inventory/audit/history'),
        ('GET', '/inventory/audit/live/%d' % ids['active_id']),
        ('GET', '/inventory/audit/report/%d' % ids['done_id']),
        ('GET', '/inventory/import'),
        ('GET', '/inventory/lookup'),
        ('GET', '/inventory/low-stock'),
        ('GET', '/inventory/add'),
        ('GET', '/inventory/edit/%d' % ids['item_id']),
        ('GET', '/inventory/add_stock/%d?qty=3' % ids['item_id']),
        ('GET', '/inventory/items?show_legacy=1&per_page=1'),
        ('GET', '/inventory/items?show_legacy=1&partial=1'),
        ('POST', '/inventory/bulk-edit'),
    ]


def test_inventory_pages_have_no_inline_js(app_with_all_features, fresh_client):
    ids = app_with_all_features
    client = login(fresh_client, 1)
    failures = {}
    list_page_actions = set()
    for method, url in _pages(ids):
        if method == 'POST':
            resp = client.post(url, data={'item_ids': [str(ids['item_id'])]})
        else:
            resp = client.get(url)
        assert resp.status_code == 200, (url, resp.status_code)
        html = resp.get_data(as_text=True)
        scan = _scan(html)

        registered = set()
        for src in scan.script_srcs:
            path = _static_file(src)
            if path:
                assert os.path.isfile(path), (url, src)
                with open(path, encoding='utf-8') as f:
                    registered |= _registered_actions(f.read())
        if url.startswith('/inventory/items'):
            if 'partial=1' in url:
                registered = list_page_actions  # the fragment runs inside the list page
            else:
                list_page_actions = registered
        missing = scan.handler_names - registered
        if missing:
            scan.problems.append('unregistered handlers: %s' % sorted(missing))
        if scan.problems:
            failures[url] = scan.problems
    assert not failures, failures


def test_list_rows_fragment_uses_data_attributes(app_with_all_features, fresh_client):
    """The ?partial=1 rows are swapped in with innerHTML: values travel in escaped data-* attributes."""
    client = login(fresh_client, 1)
    html = client.get('/inventory/items?show_legacy=1&partial=1').get_data(as_text=True)
    assert '<!DOCTYPE' not in html
    assert 'data-action="copyToClipboard" data-text="NJ-SKU-1"' in html
    assert 'data-action="promptPrintLabel" data-sku="NJ-SKU-1"' in html
    assert 'data-action="printAndSubmitAdjust" data-sku="NJ-SKU-1"' in html
    assert 'data-confirm="Delete O&#39;Brien &#34;Deluxe&#34; &lt;Widget&gt; &amp; Co?"' in html
    assert TRICKY_NAME not in html  # never raw in markup


def test_page_data_is_json(app_with_all_features, fresh_client):
    client = login(fresh_client, 1)
    html = client.get('/inventory/items').get_data(as_text=True)
    m = re.search(r'<script type="application/json" id="page-data">(.*?)</script>', html, re.S)
    data = json.loads(m.group(1))
    assert data['quickAddMarginEnabled'] is True
    assert data['quickAddMarginPercent'] == 35
    assert data['totalPages'] >= 1


INVENTORY_JS = ['js/dymo-print.js'] + [
    'js/pages/' + name for name in sorted(os.listdir(os.path.join(STATIC, 'js', 'pages')))
    if name.startswith('inventory-') and name.endswith('.js')]


@pytest.mark.parametrize('rel', INVENTORY_JS)
def test_inventory_js_files_are_static_and_handler_free(rel):
    with open(os.path.join(STATIC, *rel.split('/')), encoding='utf-8') as f:
        js = f.read()
    assert '{{' not in js and '{%' not in js, 'Jinja inside a static JS file'
    # HTML built in JS (innerHTML templates) must not carry inline handlers either.
    assert not re.search(r'\son[a-z]+\s*=\s*["\'`$]', js), 'inline handler in generated HTML'
    assert 'javascript:' not in js
    # Every data-action name in generated HTML is registered in the same file.
    used = set(re.findall(r'data-(?:action|change|input|submit|keyup|keydown|focusout)="(\w+)"', js))
    assert used <= _registered_actions(js), used - _registered_actions(js)
