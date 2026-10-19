"""Tests for the v3.0 security and infrastructure hardening."""
import sqlite3

import pytest

from tests.conftest import login


# --- redirects --------------------------------------------------------------

@pytest.mark.parametrize('target, ok', [
    ('/inventory', True),
    ('/pos/sales?x=1', True),
    ('', False),
    (None, False),
    ('https://evil.com', False),
    ('//evil.com', False),
    ('/\\evil.com', False),
    ('javascript:alert(1)', False),
    ('inventory', False),
])
def test_is_safe_redirect(target, ok):
    from app.utils.helpers import is_safe_redirect
    assert is_safe_redirect(target) is ok


# --- proxy headers ----------------------------------------------------------

def _remote_addr_seen(peer, xff, trusted=None):
    from app.utils.proxy import TrustedProxyFix
    seen = {}

    def app(environ, start_response):
        seen['addr'] = environ.get('REMOTE_ADDR')
        start_response('200 OK', [])
        return [b'']

    wrapped = TrustedProxyFix(app, trusted)
    wrapped({'REMOTE_ADDR': peer, 'HTTP_X_FORWARDED_FOR': xff,
             'wsgi.url_scheme': 'http', 'HTTP_HOST': 'x'}, lambda *a: None)
    return seen['addr']


def test_forwarded_for_ignored_from_untrusted_peer():
    assert _remote_addr_seen('10.0.0.50', '1.2.3.4') == '10.0.0.50'


def test_network_setting_validates_and_saves(fresh_app, fresh_client):
    from app.services import settings
    login(fresh_client, 1)
    fresh_client.post('/admin/settings/save', data={'group': 'network', 'TRUSTED_PROXIES': 'not-an-ip'})
    with fresh_app.app_context():
        assert settings.get('TRUSTED_PROXIES') == ['127.0.0.1', '::1']
    fresh_client.post('/admin/settings/save', data={'group': 'network', 'TRUSTED_PROXIES': '127.0.0.1, 10.0.0.50'})
    with fresh_app.app_context():
        assert settings.get('TRUSTED_PROXIES') == ['127.0.0.1', '10.0.0.50']


def test_chained_proxies_resolve_real_client():
    # cloudflared (10.0.0.172) -> nginx (10.0.0.56) -> RSCP
    trusted = ['127.0.0.1', '10.0.0.172', '10.0.0.56']
    assert _remote_addr_seen('10.0.0.56', '203.0.113.7, 10.0.0.172', trusted) == '203.0.113.7'
    # a client-forged leading entry is ignored: the rightmost untrusted hop wins
    assert _remote_addr_seen('10.0.0.56', '6.6.6.6, 203.0.113.7, 10.0.0.172', trusted) == '203.0.113.7'


def test_forwarded_for_honoured_from_trusted_proxy():
    assert _remote_addr_seen('127.0.0.1', '1.2.3.4') == '1.2.3.4'
    assert _remote_addr_seen('10.0.0.9', '5.6.7.8', trusted=['10.0.0.9']) == '5.6.7.8'


# --- schema migrations ------------------------------------------------------

def test_migrations_bring_fresh_db_to_latest(tmp_path, fresh_app):
    from app.services.migration import run_migrations, LATEST_SCHEMA_VERSION
    db = str(tmp_path / 'm.db')
    with fresh_app.app_context():
        run_migrations(db)
        run_migrations(db)  # second run is a no-op
    conn = sqlite3.connect(db)
    try:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == LATEST_SCHEMA_VERSION
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {'rate_events', 'pto_requests', 'pos_orders', 'inventory_items'} <= tables
        cols = [r[1] for r in conn.execute('PRAGMA table_info(pos_orders)')]
        assert 'checkout_token' in cols
    finally:
        conn.close()


# --- login throttling -------------------------------------------------------

def test_login_throttles_after_repeated_failures(fresh_client):
    from app.routes.auth import LOGIN_FAIL_LIMIT
    for _ in range(LOGIN_FAIL_LIMIT):
        r = fresh_client.post('/login', data={'username': 'cashier', 'password': 'wrong'})
        assert r.status_code == 302
    r = fresh_client.post('/login', data={'username': 'cashier', 'password': 'Correct-Horse-1'})
    assert r.status_code == 429


def test_successful_login_clears_failures(fresh_client):
    for _ in range(3):
        fresh_client.post('/login', data={'username': 'cashier', 'password': 'wrong'})
    r = fresh_client.post('/login', data={'username': 'cashier', 'password': 'Correct-Horse-1'})
    assert r.status_code == 302 and '/login' not in r.headers['Location']


def test_login_rejects_offsite_next(fresh_client):
    r = fresh_client.post('/login?next=/\\evil.com',
                          data={'username': 'cashier', 'password': 'Correct-Horse-1'})
    assert 'evil.com' not in r.headers['Location']


# --- POS settings are manager-only ------------------------------------------

def _tax_rate(app):
    from app.services import settings
    with app.app_context():
        return settings.get('TAX_RATE')


def test_cashier_cannot_change_tax_rate(fresh_app, fresh_client):
    login(fresh_client, 2)
    fresh_client.post('/admin/settings/save', data={'group': 'pos', 'TAX_RATE': '5'})
    assert _tax_rate(fresh_app) == 0.0
    login(fresh_client, 3)  # POS manager
    fresh_client.post('/admin/settings/save', data={'group': 'pos', 'TAX_RATE': '7'})
    assert _tax_rate(fresh_app) == pytest.approx(0.07)


# --- manager approval brute force -------------------------------------------

def test_manager_approval_is_throttled(fresh_client):
    from app.utils.permissions import MANAGER_AUTH_FAIL_LIMIT
    login(fresh_client, 2)
    for i in range(MANAGER_AUTH_FAIL_LIMIT):
        r = fresh_client.post('/pos/verify-manager', json={'username': 'manager', 'password': f'guess-{i}'})
        assert r.status_code == 401
    r = fresh_client.post('/pos/verify-manager', json={'username': 'manager', 'password': 'Correct-Horse-1'})
    assert r.status_code == 429


# --- templates never receive secrets ----------------------------------------

def test_template_config_strips_secrets():
    from app import _template_config
    safe = _template_config({'ORG_NAME': 'GCB', 'SECRET_KEY': 'k', 'EMAIL_PASS': 'p',
                             'PUBLIC_API_KEY': '', 'POS_EMAIL_PASSWORD': 'x'})
    assert safe['ORG_NAME'] == 'GCB'
    for key in ('SECRET_KEY', 'EMAIL_PASS', 'PUBLIC_API_KEY', 'POS_EMAIL_PASSWORD'):
        assert key not in safe
    assert safe['SECRET_KEY_SET'] is True and safe['PUBLIC_API_KEY_SET'] is False


# --- federation is gone -----------------------------------------------------

def test_federation_endpoints_removed(fresh_client):
    assert fresh_client.get('/api/federation/ping').status_code in (302, 401, 404)
    login(fresh_client, 1)
    assert fresh_client.get('/api/federation/ping').status_code == 404


# --- values inside inline JS handlers ---------------------------------------

def test_js_filter_is_safe_in_attributes(fresh_app):
    import html
    import json
    nasty = "Bob's' + alert(1) + '\"</script><b>"
    with fresh_app.app_context():
        out = fresh_app.jinja_env.from_string('<a onclick="f({{ v|js }})">x</a>').render(v=nasty)
    attr = out.split('onclick="', 1)[1].split('">x', 1)[0]
    assert '"' not in attr and '<' not in attr          # cannot break out of the attribute
    assert json.loads(html.unescape(attr)[2:-1]) == nasty  # the JS sees exactly the original string
