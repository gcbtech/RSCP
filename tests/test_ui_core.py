"""
Core pages (receiving, settings, auth, gateway, admin logs/import preview,
timeclock) render through the shared layout templates/base.html.
"""
import datetime
import io
import os

import pytest

from tests.conftest import login

BOOTSTRAP_CSS = 'bootstrap@5.3.0/dist/css/bootstrap.min.css'
STATIC_DIR = os.path.join(os.path.dirname(__file__), '..', 'static')


def _static(path):
    with open(os.path.join(STATIC_DIR, path), encoding='utf-8') as f:
        return f.read()


def _seed(app):
    from app.services.db import get_db_connection
    today = datetime.date.today().strftime('%Y-%m-%d')
    with app.app_context():
        conn = get_db_connection()
        try:
            conn.executemany(
                'INSERT INTO packages (tracking_number, item_name, quantity, status, date_expected) '
                'VALUES (?, ?, 1, ?, ?)',
                [('1ZUIEXP', 'UICORE Expected Widget', 'expected', today),
                 ('1ZUILATE', 'UICORE Late Widget', 'past_due', '2020-01-01'),
                 ('1ZUIRET', 'UICORE Return Widget', 'return_pending', '2020-01-01'),
                 ('1ZUIREF', 'UICORE Refunded Widget', 'refunded', '2020-01-01')])
            cur = conn.execute(
                "INSERT INTO time_entries (user_id, type, clock_in, clock_out, notes) "
                "VALUES (2, 'shift', '2030-01-02 14:00:00', '2030-01-02 22:00:00', 'UICORE shift note')")
            entry_id = cur.lastrowid
            conn.commit()
        finally:
            conn.close()
    return entry_id


def assert_layout(resp, marker, nav=True):
    html = resp.get_data(as_text=True)
    assert html.count('<!DOCTYPE html>') == 1
    assert html.count('<html lang="en">') == 1
    assert html.count(BOOTSTRAP_CSS) == 1
    assert 'js/rscp-common.js' in html
    assert marker in html, marker
    assert ('id="quickSidebar"' in html) == nav
    return html


# (url, marker, has nav bar)
PAGES = [
    ('/', 'Select a workspace to begin', False),
    ('/receiving', 'SCAN MODE', True),
    ('/history', 'Apply Filters', True),
    ('/expected', 'UICORE Expected Widget', False),
    ('/past_due', 'UICORE Late Widget', False),
    ('/open_returns', 'UICORE Return Widget', False),
    ('/refunded_log', 'UICORE Refunded Widget', False),
    ('/return_mode', 'Step 1: Identify Item', False),
    ('/return_mode?track=1ZUIRET', 'Original Tracking: 1ZUIRET', False),
    ('/settings', 'Device Settings', True),
    ('/admin/logs', 'Export CSV', True),
    ('/timeclock/', 'Hours This Period', True),
    ('/timeclock/manager', 'Timeclock Administration', True),
    ('/timeclock/manager/pto', 'PTO Management', True),
    ('/timeclock/request_pto', 'Request Time Off', True),
    ('/timeclock/manager/shifts', 'Shift Schedule', True),
    ('/timeclock/manager/timesheets', 'Timesheet Editor', True),
    ('/timeclock/manager/export/custom', 'Select Staff Members', True),
]


@pytest.mark.parametrize('url,marker,nav', PAGES)
def test_page_uses_base_layout(fresh_app, fresh_client, url, marker, nav):
    _seed(fresh_app)
    login(fresh_client, 1)
    resp = fresh_client.get(url)
    assert resp.status_code == 200, (url, resp.status_code, resp.headers.get('Location'))
    assert_layout(resp, marker, nav)


def test_timesheet_entry_edit(fresh_app, fresh_client):
    entry_id = _seed(fresh_app)
    login(fresh_client, 1)
    resp = fresh_client.get(f'/timeclock/manager/timesheet/edit/{entry_id}')
    assert resp.status_code == 200
    assert_layout(resp, 'UICORE shift note')


def test_dashboard_loads_pinned_chartjs(fresh_app, fresh_client):
    login(fresh_client, 1)
    html = assert_layout(fresh_client.get('/receiving'), 'scannedSparkline')
    assert 'chart.js@4.4.0' in html
    assert 'npm/chart.js"' not in html
    # Chart.js must load before the page script that builds the sparkline
    assert html.index('chart.js@4.4.0') < html.index('js/pages/core-dashboard.js')


def test_export_custom_uses_pinned_tom_select(fresh_app, fresh_client):
    login(fresh_client, 1)
    html = assert_layout(fresh_client.get('/timeclock/manager/export/custom'), 'Export Timesheets')
    assert html.count('tom-select@2.2.2') == 2
    assert '<body class="bg-light">' in html
    assert html.index('tom-select.complete.min.js') < html.index('js/pages/timeclock-export-custom.js')


def test_login_page(fresh_client):
    resp = fresh_client.get('/login')
    assert resp.status_code == 200
    assert_layout(resp, 'Remember me', nav=False)


def test_setup_page(fresh_app, fresh_client):
    from app.services.db import get_db_connection
    with fresh_app.app_context():
        conn = get_db_connection()
        conn.execute('DELETE FROM users')
        conn.commit()
        conn.close()
    resp = fresh_client.get('/setup')
    assert resp.status_code == 200
    assert_layout(resp, 'Finish Setup', nav=False)


def test_error_page(fresh_client):
    login(fresh_client, 1)
    resp = fresh_client.get('/no-such-page-ui-core')
    assert resp.status_code == 404
    assert_layout(resp, 'Something went wrong', nav=False)


def test_upload_preview_page(fresh_client):
    login(fresh_client, 1)
    csv_data = b'Tracking Number,Item Name,Expected Delivery Date,Quantity\n1ZUIUP,UICORE Upload Widget,2030-01-01,1\n'
    resp = fresh_client.post('/admin/upload_file', data={'file': (io.BytesIO(csv_data), 'uicore.csv')},
                             content_type='multipart/form-data')
    assert resp.status_code == 200
    assert_layout(resp, 'File Summary', nav=False)


def test_nav_notifications_use_global_escape(fresh_client):
    """The nav renders notification fields through the shared escapeHtml
    (rscp-common.js) and never puts the link inside an inline JS string."""
    login(fresh_client, 1)
    html = fresh_client.get('/settings').get_data(as_text=True)
    assert 'function escapeHtml' not in html
    assert html.index('js/rscp-common.js') < html.index('id="quickSidebar"') < html.index('js/nav.js')
    nav_js = _static('js/nav.js')
    assert 'function escapeHtml' not in nav_js
    assert "openNotification(${n.id}, '${n.link" not in nav_js
    assert 'data-link="${escapeHtml(n.link' in nav_js
