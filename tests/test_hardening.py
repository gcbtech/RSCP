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
    from app.services.db import get_db_connection
    with app.app_context():
        conn = get_db_connection()
        row = conn.execute("SELECT value FROM pos_settings WHERE key='TAX_RATE'").fetchone()
        conn.close()
        return row['value'] if row else None


def test_cashier_cannot_change_tax_rate(fresh_app, fresh_client):
    login(fresh_client, 2)
    fresh_client.post('/pos/settings/save_tax_rate', data={'tax_rate': '0'})
    assert _tax_rate(fresh_app) in (None, '0.0')
    login(fresh_client, 3)  # POS manager
    fresh_client.post('/pos/settings/save_tax_rate', data={'tax_rate': '7'})
    assert float(_tax_rate(fresh_app)) == pytest.approx(0.07)


# --- manager PIN brute force ------------------------------------------------

def test_manager_pin_check_is_throttled(fresh_client):
    from app.utils.permissions import MANAGER_AUTH_FAIL_LIMIT
    login(fresh_client, 2)
    for _ in range(MANAGER_AUTH_FAIL_LIMIT):
        r = fresh_client.post('/pos/verify-manager', json={'pin': '0000'})
        assert r.status_code == 401
    r = fresh_client.post('/pos/verify-manager', json={'pin': '1234'})
    assert r.status_code == 429


# --- templates never receive secrets ----------------------------------------

def test_template_config_strips_secrets():
    from app import _template_config
    safe = _template_config({'ORG_NAME': 'GCB', 'SECRET_KEY': 'k', 'EMAIL_PASS': 'p',
                             'SSO_CLIENT_SECRET': '', 'POS_EMAIL_PASSWORD': 'x'})
    assert safe['ORG_NAME'] == 'GCB'
    for key in ('SECRET_KEY', 'EMAIL_PASS', 'SSO_CLIENT_SECRET', 'POS_EMAIL_PASSWORD'):
        assert key not in safe
    assert safe['SECRET_KEY_SET'] is True and safe['SSO_CLIENT_SECRET_SET'] is False


# --- federation is gone -----------------------------------------------------

def test_federation_endpoints_removed(fresh_client):
    assert fresh_client.get('/api/federation/ping').status_code in (302, 404)
    login(fresh_client, 1)
    assert fresh_client.get('/api/federation/ping').status_code == 404
