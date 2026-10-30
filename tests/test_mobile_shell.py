"""Phone UI shell under /m: access, tabs per module, Home data, desktop links."""
import re

from tests.conftest import login

ALL_ON = {'INVENTORY_ENABLED': True, 'POS_ENABLED': True, 'TIMECLOCK_ENABLED': True}


def tab_labels(html):
    nav = re.search(r'<nav class="m-tabs".*?</nav>', html, re.S).group(0)
    return re.findall(r'<span>([^<]+)</span></a>', nav)


def test_signed_out_goes_to_login_and_back(fresh_client):
    resp = fresh_client.get('/m/')
    assert resp.status_code == 302
    assert '/login' in resp.headers['Location'] and 'next=' in resp.headers['Location']


def test_home_renders_every_module_for_admin(fresh_client):
    login(fresh_client, 1)
    resp = fresh_client.get('/m/')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert tab_labels(html) == ['Home', 'Receive', 'Stock', 'POS', 'Time']
    for text in ('Expected today', 'Past due', 'Open returns', 'Low stock', 'Clocked out'):
        assert text in html
    # Phone shell only: no Bootstrap, no desktop sidebar or desktop stylesheet
    assert 'bootstrap' not in html
    assert 'quickSidebar' not in html
    assert 'css/rscp.css' not in html
    assert 'm/m.css' in html and 'm/theme.js' in html


def test_disabled_modules_drop_out_of_tabs_and_pages(fresh_client, monkeypatch):
    import app.routes.mobile as mobile
    monkeypatch.setattr(mobile, 'load_config', lambda *a, **k: {'INVENTORY_ENABLED': True})
    login(fresh_client, 2)
    html = fresh_client.get('/m/').get_data(as_text=True)
    assert tab_labels(html) == ['Home', 'Receive', 'Stock']
    assert 'Clocked' not in html
    assert fresh_client.get('/m/pos').status_code == 403
    assert fresh_client.get('/m/time').status_code == 403
    assert fresh_client.get('/m/stock').status_code == 200


def test_missing_permission_is_forbidden(fresh_client, monkeypatch):
    import app.routes.mobile as mobile
    monkeypatch.setattr(mobile, 'load_config', lambda *a, **k: ALL_ON)
    monkeypatch.setattr(mobile, 'has_permission', lambda user, perm: perm != 'inventory.view')
    login(fresh_client, 2)
    html = fresh_client.get('/m/').get_data(as_text=True)
    assert 'Stock' not in tab_labels(html)
    assert 'Low stock' not in html
    assert fresh_client.get('/m/stock').status_code == 403


def test_every_tab_has_its_own_phone_pages(fresh_client):
    login(fresh_client, 1)
    for path in ('/m/receive', '/m/stock', '/m/pos', '/m/time'):
        html = fresh_client.get(path).get_data(as_text=True)
        assert 'aria-current="page"' in html and 'screens for' not in html


def test_manager_only_links_hidden_from_operators(fresh_client):
    login(fresh_client, 2)  # operator: no *.manage
    assert 'href="/m/stock/add"' not in fresh_client.get('/m/stock').get_data(as_text=True)
    assert 'href="/m/time/team"' not in fresh_client.get('/m/time').get_data(as_text=True)


def test_home_shows_shift_audit_and_counts(fresh_app, fresh_client):
    from app.services.db import get_db_connection
    with fresh_app.app_context():
        conn = get_db_connection()
        conn.execute("INSERT INTO time_entries (user_id, clock_in, type) VALUES (1, datetime('now', '-75 minutes'), 'shift')")
        conn.execute("INSERT INTO audit_sessions (user_id, mode, status) VALUES ('admin', 'item', 'active')")
        conn.execute("INSERT INTO inventory_items (name, sku, quantity) VALUES ('Gone', 'G-1', 0)")
        conn.execute("INSERT INTO packages (tracking_number, item_name, date_expected, status) "
                     "VALUES ('1ZLATE', 'Late', date('now', '-2 days'), 'past_due')")
        conn.commit()
        conn.close()
    login(fresh_client, 1)
    html = fresh_client.get('/m/').get_data(as_text=True)
    assert 'Clocked in' in html and '1h 15m' in html
    assert 'Item audit in progress' in html
    assert '0 low · 1 out of stock' in html
    assert re.search(r'Past due.*?m-pill m-tone-wn">1<', html, re.S)


def test_sheets_carry_theme_switch_and_sign_out(fresh_client):
    login(fresh_client, 1)
    html = fresh_client.get('/m/').get_data(as_text=True)
    for value in ('light', 'dark', 'auto'):
        assert f'name="mTheme" value="{value}"' in html
    assert 'href="/logout"' in html
    assert 'href="/admin/"' in html          # admins only
    login(fresh_client, 2)
    assert 'href="/admin/"' not in fresh_client.get('/m/').get_data(as_text=True)


def test_phone_pages_keep_pinch_zoom(fresh_client):
    login(fresh_client, 1)
    for path in ('/m/', '/m/receive', '/m/stock', '/m/pos', '/m/time'):
        html = fresh_client.get(path).get_data(as_text=True)
        assert 'user-scalable=no' not in html and 'maximum-scale' not in html



def test_home_team_row_is_for_managers(fresh_app, fresh_client):
    from app.services.db import get_db_connection
    with fresh_app.app_context():
        conn = get_db_connection()
        conn.execute("INSERT INTO time_entries (user_id, clock_in, type) VALUES (2, datetime('now', '-5 minutes'), 'shift')")
        conn.execute("INSERT INTO pto_requests (user_id, request_type, date, hours_requested, status) "
                     "VALUES (2, 'full', date('now', '+3 days'), 8, 'pending')")
        conn.commit()
        conn.close()
    login(fresh_client, 2)                      # operator: no timeclock.manage
    assert 'href="/m/time/team"' not in fresh_client.get('/m/').get_data(as_text=True)
    login(fresh_client, 1)
    html = fresh_client.get('/m/').get_data(as_text=True)
    row = re.search(r'<a class="m-row" href="/m/time/team">.*?</a>', html, re.S).group(0)
    assert '1 on the clock · 1 time-off request waiting' in row
    assert 'm-pill m-tone-wn">1<' in row


def test_home_refreshes_itself_and_shows_the_date(fresh_client):
    login(fresh_client, 1)
    html = fresh_client.get('/m/').get_data(as_text=True)
    assert '<div class="m-stack" id="mHome">' in html and 'm/home.js' in html
    assert re.search(r'<h2 class="m-lbl">Today · \w{3}, \w{3} \d{1,2}</h2>', html)


def test_shell_has_offline_bar_and_notification_icons(fresh_client):
    login(fresh_client, 1)
    html = fresh_client.get('/m/stock').get_data(as_text=True)
    assert re.search(r'<div class="m-offline" id="mOffline" role="alert" hidden>', html)
    assert 'id="mReadAll" data-action="mMarkAllRead" disabled' in html
    for name in ('info', 'warning', 'error', 'success', 'desktop'):
        assert f'<span data-icon="{name}">' in html
