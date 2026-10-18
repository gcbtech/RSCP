"""The admin panel (templates/admin.html) renders through the shared layout
(templates/base.html): one document, one Bootstrap, every tab's forms present,
no secrets in the HTML."""
import json
import os
import re

import pytest

from tests.conftest import login

TABS = {
    # ?tab= value -> (tab-pane id, markers that must appear in the page)
    'packages': ('packages', [
        'id="packagesBody"', 'action="/admin/bulk_action"', 'action="/admin/add_manual_item"',
        'id="analyticsChart"', 'name="group" value="general"']),
    'inventory': ('inventory', ['name="group" value="inventory"']),
    'pos': ('pos-settings', ['name="group" value="pos"', 'name="group" value="pos_approval"',
                             'name="POS_EMAIL_PASSWORD"']),
    'settings': ('settings', [
        'action="/admin/add_user"', 'action="/admin/set_user_roles/admin"',
        'action="/admin/reset_password/cashier"', 'name="group" value="security"',
        'name="group" value="sso"',
        'name="group" value="notifications"', 'name="group" value="modules"',
        'data-action="checkForUpdates"', 'id="updateProgressModal"', 'id="rollbackSection"']),
}

SECRETS = {
    'sso': {'SSO_CLIENT_SECRET': 'sso-secret-ui-admin-111'},
    'pos': {'POS_EMAIL_PASSWORD': 'smtp-secret-ui-admin-333'},
    'notifications': {'WEBHOOK_URL': 'https://discord.com/api/webhooks/ui-admin-444'},
}


def _get(client, tab):
    r = client.get('/admin/?tab=' + tab)
    assert r.status_code == 200, tab
    return r.get_data(as_text=True)


@pytest.mark.parametrize('tab', sorted(TABS))
def test_admin_tab_renders_through_layout(fresh_client, tab):
    login(fresh_client, 1)
    body = _get(fresh_client, tab)

    assert body.count('<!DOCTYPE html>') == 1
    assert body.count('<html') == 1 and '<html lang="en">' in body
    assert body.count('</html>') == 1 and body.count('<body') == 1
    assert body.count('bootstrap.min.css') == 1
    assert body.count('bootstrap.bundle.min.js') == 1
    assert '<title>RSCP | Admin Panel</title>' in body
    assert 'id="adminTabs"' in body and 'id="adminTabContent"' in body
    # Chart.js comes from the pinned vendor macro, not an unpinned CDN URL
    assert 'chart.js@4.4.0' in body and 'npm/chart.js"' not in body
    # page scripts run after the Bootstrap bundle
    assert body.index('bootstrap.bundle.min.js') < body.index('js/pages/admin.js')
    # no Jinja left unrendered
    assert '{{' not in body and '{%' not in body

    # every tab's markup is always in the page; the requested one is active
    for _, markers in TABS.values():
        for marker in markers:
            assert marker in body, (tab, marker)
    pane_id = TABS[tab][0]
    pane = re.search(r'<div class="tab-pane fade([^"]*)"\s+id="%s"' % re.escape(pane_id), body)
    assert pane and 'show active' in pane.group(1), tab


def test_admin_page_data_is_valid_json(fresh_client):
    login(fresh_client, 1)
    body = _get(fresh_client, 'packages')
    m = re.search(r'<script type="application/json" id="page-data">(.*?)</script>', body, re.S)
    assert m
    data = json.loads(m.group(1))
    assert isinstance(data['graphData'], list)
    assert data['settingsSaveUrl'] == '/admin/settings/save'
    assert 'csrfToken' in data
    # the page script (static/js/pages/admin.js) reads it instead of embedding Jinja
    with open(os.path.join(os.path.dirname(__file__), '..', 'static', 'js', 'pages', 'admin.js'),
              encoding='utf-8') as f:
        admin_js = f.read()
    assert 'RSCP.pageData()' in admin_js
    assert 'ADMIN_DATA.csrfToken' in admin_js and 'ADMIN_DATA.settingsSaveUrl' in admin_js
    assert '{{' not in admin_js


def test_admin_partial_rows_stay_a_fragment(fresh_client):
    login(fresh_client, 1)
    fresh_client.post('/admin/add_manual_item', data={'tracking': '1Z999AA10123456784',
                                                      'name': 'Widget <b>x</b>'})
    r = fresh_client.get('/admin/?partial=1')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert '1Z999AA10123456784' in body
    assert '<html' not in body and 'DOCTYPE' not in body and 'bootstrap' not in body
    assert '<b>x</b>' not in body and '&lt;b&gt;x&lt;/b&gt;' in body


def test_admin_panel_never_renders_secrets(fresh_app, fresh_client):
    login(fresh_client, 1)
    with fresh_app.app_context():
        from app.services import settings
        settings.set_many({k: v for fields in SECRETS.values() for k, v in fields.items()})
        assert settings.get('SSO_CLIENT_SECRET') == SECRETS['sso']['SSO_CLIENT_SECRET']
        assert settings.get('WEBHOOK_URL') == SECRETS['notifications']['WEBHOOK_URL']
        stored_pw = settings.get('POS_EMAIL_PASSWORD')
        assert stored_pw

    for tab in TABS:
        body = _get(fresh_client, tab)
        for fields in SECRETS.values():
            for value in fields.values():
                assert value not in body, (tab, value)
        assert stored_pw not in body, tab
    body = _get(fresh_client, 'settings')
    assert '(saved - leave blank to keep)' in body       # SSO secret shown only as "set"


def test_settings_tab_has_no_email_ingest_card(fresh_client):
    """The Amazon/eBay email ingest was removed: no card, no route, no leftovers."""
    login(fresh_client, 1)
    body = _get(fresh_client, 'settings')
    for gone in ('name="group" value="automation"', 'IMAP_SERVER', 'name="EMAIL_USER"',
                 'name="EMAIL_PASS"', 'EMAIL_INGEST_ENABLED', 'ingestSwitch',
                 'run_email_ingest', 'Run Ingest Now', 'Auto-Ingest', 'icon-automation'):
        assert gone not in body, gone
    # the POS end-of-day SMTP settings are a different feature and stay
    assert 'name="POS_EMAIL_PASSWORD"' in body
    assert fresh_client.post('/admin/run_email_ingest').status_code in (400, 404)
