"""
No inline JavaScript, so the Content-Security-Policy can drop 'unsafe-inline'
from script-src. Rendered pages may only contain:
  * <script src="..."> (external files) and <script type="application/json"> (data)
  * no on*= event-handler attributes and no javascript: URLs

Page code lives in static/js/pages/*.js and uses the helpers in
static/js/rscp-common.js (data-action, data-confirm, RSCP.pageData()).

Each area has its own test function + page list below, so parallel work on
different templates doesn't collide in this file.
"""
import datetime
import io
import os
import re
from html.parser import HTMLParser

import pytest

from tests.conftest import login

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
URL_ATTRS = ('href', 'src', 'action', 'formaction', 'xlink:href')


class _InlineJsFinder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.problems = []
        self._in_inline_script = False
        self._script_text = ''

    def handle_starttag(self, tag, attrs):
        line = self.getpos()[0]
        for name, value in attrs:
            name = (name or '').lower()
            if name.startswith('on'):
                self.problems.append(f'line {line}: <{tag} {name}="{value}">')
            if name in URL_ATTRS and (value or '').strip().lower().startswith('javascript:'):
                self.problems.append(f'line {line}: <{tag} {name}="{value}">')
        if tag == 'script':
            a = dict(attrs)
            if not a.get('src') and (a.get('type') or '').lower() != 'application/json':
                self._in_inline_script = True
                self._script_text = ''
                self.problems.append(f'line {line}: inline <script{" type=" + a["type"] if a.get("type") else ""}>')

    handle_startendtag = handle_starttag

    def handle_data(self, data):
        if self._in_inline_script:
            self._script_text += data

    def handle_endtag(self, tag):
        if tag == 'script' and self._in_inline_script:
            self._in_inline_script = False
            self.problems[-1] += ': ' + self._script_text.strip()[:80]


def inline_js_problems(html):
    finder = _InlineJsFinder()
    finder.feed(html)
    finder.close()
    return finder.problems


def assert_no_inline_js(html, where):
    problems = inline_js_problems(html)
    assert not problems, f'{where} has inline JavaScript:\n  ' + '\n  '.join(problems)


def test_finder_catches_inline_js():
    """The checker itself: each kind of inline JS is reported; allowed forms are not."""
    assert inline_js_problems('<script>alert(1)</script>')
    assert inline_js_problems('<script type="module">x()</script>')
    assert inline_js_problems('<button onclick="x()">')
    assert inline_js_problems('<img src="a.png" onerror="x()">')
    assert inline_js_problems('<form onsubmit="return confirm(1)"></form>')
    assert inline_js_problems('<a href="javascript:history.back()">')
    assert not inline_js_problems('<script src="/static/js/a.js"></script>')
    assert not inline_js_problems('<script type="application/json" id="page-data">{"a": 1}</script>')
    assert not inline_js_problems('<button data-action="x" data-confirm="Sure?">')


def _template_problems(path):
    """Static check of a template's source, so branches a render doesn't hit are covered too."""
    with open(os.path.join(ROOT, 'templates', path), encoding='utf-8') as f:
        src = f.read()
    src = re.sub(r'\{#.*?#\}', '', src, flags=re.S)      # Jinja comments
    src = re.sub(r'<!--.*?-->', '', src, flags=re.S)     # HTML comments
    problems = []
    for m in re.finditer(r'<script\b[^>]*>', src):
        tag = m.group(0)
        if 'src=' not in tag and 'application/json' not in tag:
            problems.append(tag)
    problems += re.findall(r'\son[a-z]+\s*=', src)
    problems += re.findall(r'javascript:', src, flags=re.I)
    return problems


def _js_problems(path):
    """Markup built in JS (innerHTML templates) must not carry inline handlers either."""
    with open(os.path.join(ROOT, 'static', path), encoding='utf-8') as f:
        src = f.read()
    return re.findall(r'\son[a-z]+\s*=\s*\\?["\']', src) + re.findall(r'javascript:', src, flags=re.I)


# ---------------------------------------------------------------------------
# Core: receiving, scan, settings, auth, gateway, admin, timeclock, nav
# ---------------------------------------------------------------------------

CORE_TEMPLATES = [
    'base.html', '_nav.html', 'admin.html', '_admin_table_rows.html', 'dashboard.html',
    'history.html', 'scan.html', 'expected.html', 'past_due.html', 'open_returns.html',
    'refunded.html', 'return_mode.html', 'settings.html', 'error.html', 'login.html',
    'setup.html', 'gateway.html', 'admin_logs.html', 'admin/upload_preview.html',
    'timeclock/entry_edit.html', 'timeclock/export_custom.html', 'timeclock/index.html',
    'timeclock/manager.html', 'timeclock/manager_pto.html', 'timeclock/report_print.html',
    'timeclock/request_pto.html', 'timeclock/shifts.html', 'timeclock/timesheets.html',
]

CORE_JS = [
    'js/nav.js', 'js/pages/admin.js', 'js/pages/admin-logs.js', 'js/pages/admin-upload-preview.js',
    'js/pages/core-dashboard.js', 'js/pages/core-error.js', 'js/pages/receiving-history.js',
    'js/pages/receiving-lists.js', 'js/pages/receiving-scan.js', 'js/pages/receiving-settings.js',
    'js/pages/timeclock-export-custom.js', 'js/pages/timeclock-index.js',
    'js/pages/timeclock-report-print.js', 'js/pages/timeclock-request-pto.js',
    'js/pages/timeclock-shifts.js',
]

CORE_PAGES = [
    '/', '/receiving', '/history', '/expected', '/past_due', '/open_returns', '/refunded_log',
    '/return_mode', '/return_mode?track=1ZNOJSRET', '/settings', '/scan',
    '/admin/?tab=packages', '/admin/?tab=settings', '/admin/?partial=1', '/admin/logs',
    '/timeclock/', '/timeclock/manager', '/timeclock/manager/pto', '/timeclock/request_pto',
    '/timeclock/manager/shifts', '/timeclock/manager/timesheets',
    '/timeclock/manager/export/custom',
]


def _seed_core(app):
    from app.services.db import get_db_connection
    today = datetime.date.today().strftime('%Y-%m-%d')
    now = datetime.datetime.utcnow()
    with app.app_context():
        conn = get_db_connection()
        try:
            conn.executemany(
                'INSERT INTO packages (tracking_number, item_name, quantity, status, date_expected, image_url) '
                'VALUES (?, ?, 1, ?, ?, ?)',
                [('1ZNOJSEXP', "NOJS Expected <Widget> & 'Co'", 'expected', today,
                  'https://example.com/img/nojs-widget.jpg'),
                 ('1ZNOJSPEND', 'NOJS Pending Widget', 'expected', 'Pending', None),
                 ('1ZNOJSLATE', 'NOJS Late Widget', 'past_due', '2020-01-01', None),
                 ('1ZNOJSRET', 'NOJS Return Widget', 'return_pending', '2020-01-01', None),
                 ('1ZNOJSREF', 'NOJS Refunded Widget', 'refunded', '2020-01-01', None)])
            conn.execute(
                "INSERT INTO inventory_items (sku, name, quantity, location_area, sell_price) "
                "VALUES ('NOJSSKU1', 'NOJS Inventory Item', 5, 'A', 9.99)")
            conn.execute(
                "INSERT INTO error_logs (level, source, message, trace) "
                "VALUES ('ERROR', 'test', 'NOJS log message', 'Traceback: NOJS')")
            cur = conn.execute(
                "INSERT INTO time_entries (user_id, type, clock_in, clock_out, notes) "
                "VALUES (2, 'shift', '2030-01-02 14:00:00', '2030-01-02 22:00:00', 'NOJS shift note')")
            entry_id = cur.lastrowid
            conn.execute(
                "INSERT INTO time_entries (user_id, type, clock_in) VALUES (3, 'shift', ?)",
                ((now - datetime.timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S'),))
            conn.execute(
                "INSERT INTO pto_requests (user_id, request_type, date, hours_requested, status) "
                "VALUES (2, 'full', '2030-02-01', 8, 'pending')")
            conn.execute(
                "INSERT INTO scheduled_shifts (user_id, start_time, end_time) VALUES (2, ?, ?)",
                (now.strftime('%Y-%m-%d 14:00:00'), now.strftime('%Y-%m-%d 22:00:00')))
            conn.commit()
        finally:
            conn.close()
    return entry_id


@pytest.mark.parametrize('path', CORE_TEMPLATES)
def test_core_template_sources_have_no_inline_js(path):
    assert not _template_problems(path), (path, _template_problems(path))


@pytest.mark.parametrize('path', CORE_JS)
def test_core_page_scripts_build_no_inline_handlers(path):
    assert not _js_problems(path), (path, _js_problems(path))


@pytest.mark.parametrize('url', CORE_PAGES)
def test_core_pages_have_no_inline_js(fresh_app, fresh_client, url):
    _seed_core(fresh_app)
    login(fresh_client, 1)
    resp = fresh_client.get(url)
    assert resp.status_code == 200, (url, resp.status_code, resp.headers.get('Location'))
    assert_no_inline_js(resp.get_data(as_text=True), url)


def test_core_pages_with_seeded_rows_have_no_inline_js(fresh_app, fresh_client):
    """Pages reached by POST or needing a specific record."""
    entry_id = _seed_core(fresh_app)
    login(fresh_client, 1)
    checks = {}

    # Scan results: a manifest package (item card + Link to Inventory button)...
    r = fresh_client.post('/scan', data={'tracking_input': '1ZNOJSEXP'})
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and 'data-action="openLinkModal"' in html
    checks['POST /scan (package)'] = html
    # ...and an inventory SKU (quick-adjust buttons)
    r = fresh_client.post('/scan', data={'tracking_input': 'NOJSSKU1'})
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and 'data-action="quickAdjust"' in html
    checks['POST /scan (inventory)'] = html

    r = fresh_client.get(f'/timeclock/manager/timesheet/edit/{entry_id}')
    assert r.status_code == 200
    checks['timesheet edit'] = r.get_data(as_text=True)

    r = fresh_client.post('/timeclock/manager/export/report',
                          data={'user_ids': ['2'], 'period': '2030-01-01|2030-01-31'})
    assert r.status_code == 200
    checks['timesheet report print'] = r.get_data(as_text=True)

    csv_data = b'Tracking Number,Item Name,Expected Delivery Date,Quantity\n1ZNOJSUP,NOJS Upload,2030-01-01,1\n'
    r = fresh_client.post('/admin/upload_file', data={'file': (io.BytesIO(csv_data), 'nojs.csv')},
                          content_type='multipart/form-data')
    assert r.status_code == 200
    checks['upload preview'] = r.get_data(as_text=True)

    r = fresh_client.get('/no-such-page-nojs')
    assert r.status_code == 404
    checks['error page'] = r.get_data(as_text=True)

    for where, html in checks.items():
        assert_no_inline_js(html, where)


def test_core_logged_out_pages_have_no_inline_js(fresh_app, fresh_client):
    r = fresh_client.get('/login')
    assert r.status_code == 200
    assert_no_inline_js(r.get_data(as_text=True), '/login')

    from app.services.db import get_db_connection
    with fresh_app.app_context():
        conn = get_db_connection()
        conn.execute('DELETE FROM users')
        conn.commit()
        conn.close()
    r = fresh_client.get('/setup')
    assert r.status_code == 200
    assert_no_inline_js(r.get_data(as_text=True), '/setup')


def test_scan_page_is_standalone_with_shared_scripts(fresh_client):
    """scan.html doesn't extend base.html, so it loads the shared pieces itself."""
    login(fresh_client, 1)
    html = fresh_client.get('/scan').get_data(as_text=True)
    assert '<meta name="csrf-token"' in html
    assert html.index('js/rscp-common.js') < html.index('js/nav.js') < html.index('js/pages/receiving-scan.js')
    assert 'id="page-data"' in html
