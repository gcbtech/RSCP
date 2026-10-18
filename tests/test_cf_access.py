"""
Cloudflare Access sign-in (app/services/cf_access.py).

Tokens are signed with locally generated RSA keys; the JWKS fetch is mocked,
so no test talks to Cloudflare.
"""
import base64
import json
import time
import warnings
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

with warnings.catch_warnings(record=True):
    warnings.simplefilter('ignore')
    from authlib.jose import JsonWebKey, JsonWebToken

from app.services import cf_access
from tests.conftest import login

TEAM = 'gcbtest.cloudflareaccess.com'
ISS = f'https://{TEAM}'
AUD = 'a' * 64
CASHIER_EMAIL = 'cashier@gcbcomputers.com'


def _rsa_key():
    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return JsonWebKey.import_key(pem, {'kty': 'RSA'})


KEY1 = _rsa_key()
KEY2 = _rsa_key()
EVIL = _rsa_key()


def _jwk(key, kid):
    d = key.as_dict(is_private=False)
    d.update(kid=kid, alg='RS256', use='sig')
    return d


def jwks(*pairs):
    return {'keys': [_jwk(k, kid) for k, kid in pairs]}


def make_token(key=KEY1, kid='k1', **overrides):
    now = int(time.time())
    claims = {'aud': [AUD], 'iss': ISS, 'email': CASHIER_EMAIL, 'iat': now, 'nbf': now,
              'exp': now + 600, 'sub': 'abc', 'type': 'app'}
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return JsonWebToken(['RS256']).encode({'alg': 'RS256', 'kid': kid, 'typ': 'JWT'}, claims, key).decode()


def _b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b'=').decode()


def hdr(token):
    return {cf_access.HEADER: token}


@pytest.fixture
def cf_app(fresh_app):
    from app.services import settings as store
    from app.services.db import get_db_connection
    cf_access.clear_cache()
    with fresh_app.app_context():
        store.set_many({'CF_ACCESS_TEAM_DOMAIN': TEAM, 'CF_ACCESS_AUD': AUD, 'CF_ACCESS_ENABLED': True})
        conn = get_db_connection()
        conn.execute('UPDATE users SET email = ? WHERE id = 2', (CASHIER_EMAIL,))
        conn.execute("UPDATE users SET email = 'admin@gcbcomputers.com' WHERE id = 1")
        conn.commit()
        conn.close()
    with patch.object(cf_access, '_fetch_jwks', return_value=jwks((KEY1, 'k1'))) as fetch:
        fresh_app.fetch = fetch
        yield fresh_app
    cf_access.clear_cache()


@pytest.fixture
def client(cf_app):
    return cf_app.test_client()


def _uid(client):
    with client.session_transaction() as sess:
        return sess.get('_user_id')


def _to_login(resp):
    return resp.status_code == 302 and '/login' in resp.headers['Location']


def _set(app, **values):
    from app.services import settings as store
    with app.app_context():
        store.set_many(values)


# --- happy path ----------------------------------------------------------------

def test_valid_token_signs_in_matching_user(cf_app, client):
    resp = client.get('/', headers=hdr(make_token()))
    assert resp.status_code == 200
    assert _uid(client) == '2'
    with client.session_transaction() as sess:
        assert sess.get('login_time')
    from app.services.db import get_db_connection
    with cf_app.app_context():
        conn = get_db_connection()
        row = conn.execute('SELECT username, success, device_info FROM login_attempts '
                           'ORDER BY id DESC LIMIT 1').fetchone()
        conn.close()
    assert row['username'] == 'cashier' and row['success'] == 1
    assert 'cf-access' in row['device_info']


def test_valid_token_on_protected_page(client):
    resp = client.get('/receiving', headers=hdr(make_token()))
    assert not _to_login(resp)
    assert _uid(client) == '2'


def test_email_match_is_case_insensitive(client):
    client.get('/', headers=hdr(make_token(email='Cashier@GCBComputers.com')))
    assert _uid(client) == '2'


def test_login_page_with_token_redirects_in(client):
    resp = client.get('/login?next=/receiving', headers=hdr(make_token()))
    assert resp.status_code == 302 and resp.headers['Location'].endswith('/receiving')
    assert _uid(client) == '2'
    resp = client.get('/login?next=https://evil.example/', headers=hdr(make_token()))
    assert 'evil' not in resp.headers['Location']


def test_aud_list_or_string(client):
    client.get('/', headers=hdr(make_token(aud=AUD)))
    assert _uid(client) == '2'


def test_token_is_never_logged(client, caplog):
    token = make_token(aud=['other'])
    with caplog.at_level('DEBUG'):
        client.get('/', headers=hdr(token))
        client.get('/', headers=hdr(make_token()))
    assert token not in caplog.text
    assert token.split('.')[2] not in caplog.text
    assert 'Sign-in rejected' in caplog.text


# --- rejections fall through to the normal login page --------------------------

def _hs256_token():
    # HS256 "signed" with the public key's PEM as the secret: the classic
    # algorithm-confusion forgery.
    import hashlib
    import hmac
    pub_pem = KEY1.as_pem(is_private=False)
    now = int(time.time())
    signing_input = _b64({'alg': 'HS256', 'kid': 'k1'}) + '.' + _b64(
        {'aud': [AUD], 'iss': ISS, 'email': CASHIER_EMAIL, 'exp': now + 600})
    sig = hmac.new(pub_pem, signing_input.encode(), hashlib.sha256).digest()
    return signing_input + '.' + base64.urlsafe_b64encode(sig).rstrip(b'=').decode()


def _none_token():
    now = int(time.time())
    return _b64({'alg': 'none', 'kid': 'k1'}) + '.' + _b64(
        {'aud': [AUD], 'iss': ISS, 'email': CASHIER_EMAIL, 'exp': now + 600}) + '.'


def _tampered_token():
    head, _payload, sig = make_token(email='cashier@gcbcomputers.com').split('.')
    now = int(time.time())
    payload = _b64({'aud': [AUD], 'iss': ISS, 'email': 'admin@gcbcomputers.com', 'exp': now + 600})
    return '.'.join((head, payload, sig))


BAD_TOKENS = {
    'wrong_aud': lambda: make_token(aud=['b' * 64]),
    'wrong_iss': lambda: make_token(iss='https://other.cloudflareaccess.com'),
    'missing_aud': lambda: make_token(aud=None),
    'expired': lambda: make_token(exp=int(time.time()) - 3600),
    'missing_exp': lambda: make_token(exp=None),
    'not_yet_valid': lambda: make_token(nbf=int(time.time()) + 3600),
    'bad_signature': lambda: make_token(key=EVIL, kid='k1'),
    'unknown_kid': lambda: make_token(key=EVIL, kid='evil'),
    'tampered': _tampered_token,
    'unknown_email': lambda: make_token(email='nobody@gcbcomputers.com'),
    'username_lookalike': lambda: make_token(email='admin@elsewhere.com'),
    'no_email': lambda: make_token(email=None, common_name='svc.access'),
    'hs256': _hs256_token,
    'alg_none': _none_token,
    'garbage': lambda: 'not-a-jwt',
    'garbage_3part': lambda: 'a.b.c',
}


@pytest.mark.parametrize('name', sorted(BAD_TOKENS))
def test_bad_token_falls_through_to_login(client, name):
    token = BAD_TOKENS[name]()
    resp = client.get('/', headers=hdr(token))
    assert _to_login(resp), name
    assert _uid(client) is None
    resp = client.get('/receiving', headers=hdr(token))
    assert _to_login(resp)
    # The login page itself renders (no loop, no 500)
    resp = client.get('/login', headers=hdr(token))
    assert resp.status_code == 200
    assert _uid(client) is None


def test_unknown_email_explained_on_login_page(client):
    resp = client.get('/login', headers=hdr(make_token(email='nobody@gcbcomputers.com')))
    assert resp.status_code == 200
    assert b'No RSCP account is linked to nobody@gcbcomputers.com' in resp.data


def test_domain_allowlist(cf_app, client):
    _set(cf_app, CF_ACCESS_ALLOWED_DOMAINS=['@other.com'])
    assert _to_login(client.get('/', headers=hdr(make_token())))
    _set(cf_app, CF_ACCESS_ALLOWED_DOMAINS=['gcbcomputers.com'])
    client.get('/', headers=hdr(make_token()))
    assert _uid(client) == '2'


def test_domain_allowlist_defaults_to_sso_domains(cf_app, client):
    _set(cf_app, SSO_ALLOWED_DOMAINS=['@other.com'])
    assert _to_login(client.get('/', headers=hdr(make_token())))
    _set(cf_app, CF_ACCESS_ALLOWED_DOMAINS=['@gcbcomputers.com'])
    client.get('/', headers=hdr(make_token()))
    assert _uid(client) == '2'


def test_disabled_feature_ignores_header(cf_app, client):
    _set(cf_app, CF_ACCESS_ENABLED=False)
    assert _to_login(client.get('/', headers=hdr(make_token())))
    assert _uid(client) is None
    cf_app.fetch.assert_not_called()


def test_no_header_normal_flow(cf_app, client):
    assert _to_login(client.get('/'))
    assert _to_login(client.get('/receiving'))
    assert client.get('/login').status_code == 200
    cf_app.fetch.assert_not_called()


def test_post_requests_do_not_auto_sign_in(client):
    resp = client.post('/receiving', headers=hdr(make_token()))
    assert _uid(client) is None
    assert resp.status_code in (302, 401, 405)


def test_public_device_paths_skip_cf(cf_app, client):
    client.get('/static/js/rscp-common.js', headers=hdr(make_token()))
    client.get('/healthz', headers=hdr(make_token()))
    assert _uid(client) is None
    cf_app.fetch.assert_not_called()


def test_jwks_fetch_failure_is_not_a_500(cf_app, client):
    cf_app.fetch.side_effect = OSError('network down')
    resp = client.get('/', headers=hdr(make_token()))
    assert _to_login(resp)
    assert client.get('/login', headers=hdr(make_token())).status_code == 200


def test_already_logged_in_user_is_untouched(cf_app, client):
    login(client, 3)
    client.get('/', headers=hdr(make_token()))
    assert _uid(client) == '3'
    cf_app.fetch.assert_not_called()


# --- JWKS caching and key rotation ---------------------------------------------

def test_jwks_cached_across_requests(cf_app):
    for _ in range(3):
        c = cf_app.test_client()
        c.get('/', headers=hdr(make_token()))
        assert _uid(c) == '2'
    assert cf_app.fetch.call_count == 1


def test_kid_rotation_refetches(cf_app, monkeypatch):
    c = cf_app.test_client()
    c.get('/', headers=hdr(make_token()))
    assert cf_app.fetch.call_count == 1

    # Cloudflare rotates: a token signed by a new key arrives.
    cf_app.fetch.return_value = jwks((KEY1, 'k1'), (KEY2, 'k2'))
    monkeypatch.setattr(cf_access, 'JWKS_MIN_REFRESH', 0)
    c = cf_app.test_client()
    c.get('/', headers=hdr(make_token(key=KEY2, kid='k2')))
    assert _uid(c) == '2'
    assert cf_app.fetch.call_count == 2


def test_unknown_kid_refetch_is_rate_limited(cf_app):
    c = cf_app.test_client()
    c.get('/', headers=hdr(make_token()))
    for i in range(5):
        c.get('/', headers=hdr(make_token(key=EVIL, kid=f'junk{i}')))
    # The first fetch just happened: junk kids don't trigger more.
    assert cf_app.fetch.call_count == 1


def test_stale_keys_used_when_refresh_fails(cf_app, monkeypatch):
    c = cf_app.test_client()
    c.get('/', headers=hdr(make_token()))
    monkeypatch.setattr(cf_access, 'JWKS_TTL', 0)
    monkeypatch.setattr(cf_access, 'JWKS_MIN_REFRESH', 0)
    cf_app.fetch.side_effect = OSError('cloudflare unreachable')
    c = cf_app.test_client()
    c.get('/', headers=hdr(make_token()))
    assert _uid(c) == '2'
    assert cf_app.fetch.call_count == 2


def test_fetch_uses_https_certs_url_with_timeout():
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return jwks((KEY1, 'k1'))

    with patch.object(cf_access.requests, 'get', return_value=Resp()) as get:
        data = cf_access._fetch_jwks(TEAM)
    assert data['keys'][0]['kid'] == 'k1'
    args, kwargs = get.call_args
    assert args[0] == f'https://{TEAM}/cdn-cgi/access/certs'
    assert 0 < kwargs['timeout'] <= 10
    assert kwargs.get('allow_redirects') is False


def test_parse_jwks_skips_non_rsa_and_private_parts():
    data = jwks((KEY1, 'k1'))
    data['keys'].append({'kty': 'oct', 'kid': 'h', 'k': 'c2VjcmV0'})
    data['keys'].append({'kty': 'RSA', 'kid': 'enc', 'use': 'enc', 'n': 'x', 'e': 'AQAB'})
    keys = cf_access._parse_jwks(data)
    assert list(keys) == ['k1']


# --- logout ---------------------------------------------------------------------

def test_logout_suppresses_auto_sign_in(client):
    client.get('/', headers=hdr(make_token()))
    assert _uid(client) == '2'
    resp = client.get('/logout', headers=hdr(make_token()))
    assert any(c.startswith(cf_access.SUPPRESS_COOKIE + '=') for c in resp.headers.getlist('Set-Cookie'))
    assert _to_login(resp)
    # The header is still there, but the user just logged out: stay out.
    assert client.get('/login?force=1', headers=hdr(make_token())).status_code == 200
    assert _to_login(client.get('/', headers=hdr(make_token())))
    assert _uid(client) is None


def test_explicit_cf_button_signs_back_in_and_ends_suppression(client):
    client.get('/', headers=hdr(make_token()))
    client.get('/logout', headers=hdr(make_token()))
    resp = client.get('/login/cf-access?next=/receiving', headers=hdr(make_token()))
    assert resp.status_code == 302 and resp.headers['Location'].endswith('/receiving')
    assert _uid(client) == '2'
    assert client.get_cookie(cf_access.SUPPRESS_COOKIE) is None


def test_cf_button_without_token_explains(client):
    resp = client.get('/login/cf-access', follow_redirects=True)
    assert resp.status_code == 200
    assert b'did not come through Cloudflare Access' in resp.data
    assert _uid(client) is None


def test_password_login_ends_suppression(client):
    client.get('/', headers=hdr(make_token()))
    client.get('/logout')
    assert client.get_cookie(cf_access.SUPPRESS_COOKIE) is not None
    client.post('/login', data={'username': 'manager', 'password': 'Correct-Horse-1'})
    assert client.get_cookie(cf_access.SUPPRESS_COOKIE) is None


def test_logout_sets_no_cookie_when_disabled(cf_app, client):
    _set(cf_app, CF_ACCESS_ENABLED=False)
    login(client, 2)
    resp = client.get('/logout')
    assert not any(cf_access.SUPPRESS_COOKIE in c for c in resp.headers.getlist('Set-Cookie'))


# --- login page -----------------------------------------------------------------

def test_login_page_shows_cf_button_only_when_enabled(cf_app, client):
    assert b'Sign in with Cloudflare Access' in client.get('/login').data
    _set(cf_app, CF_ACCESS_ENABLED=False)
    assert b'Sign in with Cloudflare Access' not in client.get('/login').data


# --- admin test endpoint --------------------------------------------------------

def test_admin_test_endpoint_reports_mapping(client):
    login(client, 1)
    resp = client.get('/admin/cf-access/test', headers=hdr(make_token()))
    data = resp.get_json()
    assert resp.status_code == 200
    assert data['valid'] is True and data['user'] == 'cashier' and data['email'] == CASHIER_EMAIL
    assert data['would_sign_in'] is True and data['expires']
    assert _uid(client) == '1'  # testing signs nobody in
    assert make_token().split('.')[0] not in resp.get_data(as_text=True)


def test_admin_test_endpoint_without_header(client):
    login(client, 1)
    data = client.get('/admin/cf-access/test').get_json()
    assert data['header_present'] is False and data['valid'] is False and data['error']


def test_admin_test_endpoint_unknown_email(client):
    login(client, 1)
    data = client.get('/admin/cf-access/test',
                      headers=hdr(make_token(email='nobody@gcbcomputers.com'))).get_json()
    assert data['valid'] is False and 'nobody@gcbcomputers.com' in data['error']
    assert data['expires']


def test_admin_test_endpoint_works_before_enabling(cf_app, client):
    _set(cf_app, CF_ACCESS_ENABLED=False)
    login(client, 1)
    data = client.get('/admin/cf-access/test', headers=hdr(make_token())).get_json()
    assert data['valid'] is True and data['would_sign_in'] is False


def test_admin_test_endpoint_is_admin_only(cf_app, client):
    login(client, 2)
    resp = client.get('/admin/cf-access/test', headers=hdr(make_token()))
    assert resp.status_code == 403 and resp.is_json
    anon = cf_app.test_client()
    resp = anon.get('/admin/cf-access/test')
    assert resp.status_code in (401, 302)
    assert _uid(anon) is None


# --- settings -------------------------------------------------------------------

def test_settings_defaults_off(fresh_app):
    from app.services import settings as store
    with fresh_app.app_context():
        assert store.get('CF_ACCESS_ENABLED') is False
        assert store.get('CF_ACCESS_AUD') == ''
        assert cf_access.is_enabled() is False


def test_enable_requires_aud_and_team(fresh_app):
    from app.services import settings as store
    with fresh_app.app_context():
        with pytest.raises(store.SettingsError):
            store.set_many({'CF_ACCESS_ENABLED': True, 'CF_ACCESS_TEAM_DOMAIN': TEAM})
        with pytest.raises(store.SettingsError):
            store.set_many({'CF_ACCESS_ENABLED': True, 'CF_ACCESS_AUD': AUD})
        store.set_many({'CF_ACCESS_ENABLED': True, 'CF_ACCESS_AUD': AUD, 'CF_ACCESS_TEAM_DOMAIN': TEAM})
        with pytest.raises(store.SettingsError):
            store.set_many({'CF_ACCESS_AUD': ''})


@pytest.mark.parametrize('raw,expected', [
    ('gcbcomputers', 'gcbcomputers.cloudflareaccess.com'),
    ('GCBComputers.cloudflareaccess.com', 'gcbcomputers.cloudflareaccess.com'),
    ('https://gcbcomputers.cloudflareaccess.com/', 'gcbcomputers.cloudflareaccess.com'),
    ('', ''),
])
def test_team_domain_normalised(raw, expected):
    assert cf_access.normalize_team_domain(raw) == expected


@pytest.mark.parametrize('raw', ['evil.com', 'x.evil.com/cloudflareaccess.com', 'a.b.cloudflareaccess.com',
                                 'http://gcb.cloudflareaccess.com.evil.com', 'gcb..cloudflareaccess.com',
                                 'bad_name.cloudflareaccess.com'])
def test_team_domain_must_be_cloudflare(raw):
    with pytest.raises(ValueError):
        cf_access.normalize_team_domain(raw)


def test_settings_form_saves_group(fresh_app):
    from app.services import settings as store
    c = fresh_app.test_client()
    login(c, 1)
    resp = c.post('/admin/settings/save', data={
        'group': 'cf_access', 'CF_ACCESS_ENABLED': ['false', 'true'],
        'CF_ACCESS_TEAM_DOMAIN': 'gcbcomputers', 'CF_ACCESS_AUD': AUD,
        'CF_ACCESS_ALLOWED_DOMAINS': 'gcbcomputers.com'})
    assert resp.status_code == 302
    with fresh_app.app_context():
        assert store.get('CF_ACCESS_ENABLED') is True
        assert store.get('CF_ACCESS_TEAM_DOMAIN') == 'gcbcomputers.cloudflareaccess.com'
        assert store.get('CF_ACCESS_ALLOWED_DOMAINS') == ['@gcbcomputers.com']
    # non-admins may not change it
    c2 = fresh_app.test_client()
    login(c2, 3)
    resp = c2.post('/admin/settings/save', data={'group': 'cf_access', 'CF_ACCESS_ENABLED': 'false'})
    assert resp.status_code == 403


def test_admin_page_renders_card(fresh_app):
    c = fresh_app.test_client()
    login(c, 1)
    resp = c.get('/admin/?tab=settings')
    if resp.status_code == 308:
        resp = c.get(resp.headers['Location'])
    assert resp.status_code == 200
    assert b'cf-access-card' in resp.data and b'CF_ACCESS_AUD' in resp.data


# --- account email --------------------------------------------------------------

def _email_of(app, user_id):
    from app.services.db import get_db_connection
    with app.app_context():
        conn = get_db_connection()
        row = conn.execute('SELECT email FROM users WHERE id = ?', (user_id,)).fetchone()
        conn.close()
    return row['email']


def _set_email(app, user_id, email):
    from app.services.db import get_db_connection
    with app.app_context():
        conn = get_db_connection()
        conn.execute('UPDATE users SET email = ? WHERE id = ?', (email, user_id))
        conn.commit()
        conn.close()


def test_mixed_case_stored_email_matches(cf_app, client):
    _set_email(cf_app, 2, 'Cashier@GCBComputers.com')
    client.get('/', headers=hdr(make_token()))
    assert _uid(client) == '2'


def test_duplicate_email_is_refused(cf_app, client):
    _set_email(cf_app, 3, CASHIER_EMAIL.upper())
    assert _to_login(client.get('/', headers=hdr(make_token())))
    assert _uid(client) is None


def test_admin_sets_user_email(cf_app):
    c = cf_app.test_client()
    login(c, 1)
    c.post('/admin/set_user_email/manager', data={'email': ' Manager@GCBComputers.com '})
    assert _email_of(cf_app, 3) == 'manager@gcbcomputers.com'
    # duplicates and junk are refused
    c.post('/admin/set_user_email/manager', data={'email': CASHIER_EMAIL})
    assert _email_of(cf_app, 3) == 'manager@gcbcomputers.com'
    c.post('/admin/set_user_email/manager', data={'email': 'not an email'})
    assert _email_of(cf_app, 3) == 'manager@gcbcomputers.com'
    c.post('/admin/set_user_email/manager', data={'email': ''})
    assert _email_of(cf_app, 3) is None


def test_set_email_rules(cf_app):
    from app.services.db import get_db_connection
    from werkzeug.security import generate_password_hash
    with cf_app.app_context():
        conn = get_db_connection()
        conn.execute('INSERT INTO users (id, username, password_hash, is_admin, roles) '
                     "VALUES (4, 'boss', ?, 1, '[\"super_admin\"]')", (generate_password_hash('x'),))
        conn.commit()
        conn.close()
    c = cf_app.test_client()
    login(c, 1)
    # not on another admin's account (that would hand over their sign-in)
    c.post('/admin/set_user_email/boss', data={'email': 'me@gcbcomputers.com'})
    assert _email_of(cf_app, 4) is None
    # non-admins may not use it at all
    c2 = cf_app.test_client()
    login(c2, 3)
    c2.post('/admin/set_user_email/cashier', data={'email': 'x@gcbcomputers.com'})
    assert _email_of(cf_app, 2) == CASHIER_EMAIL
