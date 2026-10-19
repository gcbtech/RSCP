"""
Manager approvals are username + password only (3.0).

Voids (/pos/api/validate-manager), discounts over the limit
(/pos/verify-manager) and refund access (/pos/refunds/auth) all take the
manager's account username and password. POS PINs and badge IDs approve
nothing: users.pin_hash and users.badge_id are kept (with their data) for a
future rebuild, but no code reads them.
"""
import os
import re

import pytest
from werkzeug.security import generate_password_hash

from tests.conftest import login

PASSWORD = 'Correct-Horse-1'
PIN = '4821'
BADGE = 'BADGE-0003'
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def app(fresh_app):
    """Every user has a PIN and the manager a badge, as a 2.x install might."""
    from app.services.db import get_db_connection
    conn = get_db_connection(fresh_app.config['DATABASE'])
    conn.execute('UPDATE users SET pin_hash = ?', (generate_password_hash(PIN),))
    conn.execute('UPDATE users SET badge_id = ? WHERE id = 3', (BADGE,))
    conn.commit()
    conn.close()
    return fresh_app


@pytest.fixture
def cashier(app):
    return login(app.test_client(), 2)


def _flashes(client):
    with client.session_transaction() as sess:
        return [m for _, m in sess.get('_flashes', [])]


# --- /pos/api/validate-manager (voids) ------------------------------------------

def test_validate_manager_with_password(cashier):
    resp = cashier.post('/pos/api/validate-manager', json={'username': 'Manager', 'password': PASSWORD})
    assert resp.status_code == 200 and resp.get_json() == {'valid': True}
    with cashier.session_transaction() as sess:
        assert sess['pos_manager_auth']['username'] == 'manager'


@pytest.mark.parametrize('body', [
    {'username': 'manager', 'pin': PIN},
    {'badge_id': BADGE},
    {'username': 'manager', 'badge_id': BADGE},
    {'username': 'manager'},
    {'password': PASSWORD},
    {'username': 'manager', 'password': ''},
    {'username': ['manager'], 'password': PASSWORD},
    {'username': 'manager', 'password': {'x': 1}},
])
def test_validate_manager_needs_username_and_password(cashier, body):
    resp = cashier.post('/pos/api/validate-manager', json=body)
    assert resp.status_code == 400 and resp.get_json()['valid'] is False
    with cashier.session_transaction() as sess:
        assert 'pos_manager_auth' not in sess


@pytest.mark.parametrize('username,password', [
    ('manager', PIN),            # the PIN is not a password
    ('manager', BADGE),
    ('manager', 'wrong'),
    ('cashier', PASSWORD),       # right password, but not a manager
    ('nobody', PASSWORD),
])
def test_validate_manager_refuses(cashier, username, password):
    resp = cashier.post('/pos/api/validate-manager', json={'username': username, 'password': password})
    assert resp.get_json() == {'valid': False, 'error': 'Invalid credentials'}
    with cashier.session_transaction() as sess:
        assert 'pos_manager_auth' not in sess


def test_void_needs_the_password_approval(app, cashier):
    from app.routes.pos.core import set_pos_setting
    with app.app_context():
        set_pos_setting('REQUIRE_MANAGER_VOID', 'true')
    headers = {'X-Response-Format': 'json'}
    resp = cashier.post('/pos/cart/remove/nope', headers=headers)
    assert resp.status_code == 403 and resp.get_json()['error'] == 'manager_required'
    cashier.post('/pos/api/validate-manager', json={'username': 'manager', 'pin': PIN})
    assert cashier.post('/pos/cart/remove/nope', headers=headers).status_code == 403
    cashier.post('/pos/api/validate-manager', json={'username': 'manager', 'password': PASSWORD})
    assert cashier.post('/pos/cart/remove/nope', headers=headers).status_code != 403


# --- /pos/verify-manager (discounts) --------------------------------------------

def test_verify_manager_with_password(cashier):
    resp = cashier.post('/pos/verify-manager', json={'username': 'manager', 'password': PASSWORD})
    assert resp.status_code == 200 and resp.get_json() == {'success': True, 'manager': 'manager'}
    with cashier.session_transaction() as sess:
        assert sess['pos_discount_manager_auth']['username'] == 'manager'


@pytest.mark.parametrize('body', [{'pin': PIN}, {'badge_id': BADGE}, {'pin': PIN, 'badge_id': BADGE}, {}])
def test_verify_manager_ignores_pin_and_badge(cashier, body):
    resp = cashier.post('/pos/verify-manager', json=body)
    assert resp.status_code == 400 and resp.get_json()['success'] is False
    assert 'username and password' in resp.get_json()['error']


@pytest.mark.parametrize('username,password', [('manager', PIN), ('cashier', PASSWORD), ('manager', 'x')])
def test_verify_manager_refuses(cashier, username, password):
    resp = cashier.post('/pos/verify-manager', json={'username': username, 'password': password})
    assert resp.status_code == 401 and resp.get_json()['success'] is False
    with cashier.session_transaction() as sess:
        assert 'pos_discount_manager_auth' not in sess


def test_big_discount_applies_after_password_approval(app, cashier):
    form = {'discount_amount': '50', 'discount_type': 'fixed', 'discount_reason': 'loyal'}
    cashier.post('/pos/cart/discount', data=form)
    assert any('Manager approval required' in m for m in _flashes(cashier))
    cashier.post('/pos/verify-manager', json={'username': 'manager', 'password': PASSWORD})
    cashier.post('/pos/cart/discount', data=form)
    assert any('approved by manager' in m for m in _flashes(cashier))


# --- /pos/refunds/auth ----------------------------------------------------------

def test_refund_auth_with_password(cashier):
    resp = cashier.post('/pos/refunds/auth', data={'username': 'manager', 'password': PASSWORD})
    assert resp.status_code == 302 and resp.headers['Location'].endswith('/pos/refunds')
    with cashier.session_transaction() as sess:
        assert sess['pos_refund_manager_auth']['username'] == 'manager'
    assert cashier.get('/pos/refunds').status_code == 200


@pytest.mark.parametrize('form', [
    {'badge_id': BADGE},
    {'username': 'manager', 'pin': PIN},
    {'username': 'manager', 'password': PIN},
    {'username': 'manager', 'password': PIN, 'pin': PIN, 'badge_id': BADGE},
    {'username': 'cashier', 'password': PASSWORD},
    {'username': '', 'password': PASSWORD},
])
def test_refund_auth_refuses(cashier, form):
    resp = cashier.post('/pos/refunds/auth', data=form)
    assert resp.status_code == 200 and b'Invalid manager credentials' in resp.data
    with cashier.session_transaction() as sess:
        assert 'pos_refund_manager_auth' not in sess
    resp = cashier.get('/pos/refunds')
    assert resp.status_code == 302 and '/pos/refunds/auth' in resp.headers['Location']


# --- accounts without a usable password -----------------------------------------

def test_account_from_removed_google_sign_in_cannot_approve(app, cashier):
    from app.services.db import get_db_connection
    conn = get_db_connection(app.config['DATABASE'])
    conn.execute("INSERT INTO users (id, username, password_hash, is_admin, roles, email, auth_provider) "
                 "VALUES (9, 'gmgr', 'sso_managed_0123456789abcdef', 0, '[\"pos_admin\"]', "
                 "'gmgr@example.com', 'oidc')")
    conn.commit()
    conn.close()
    for password in ('sso_managed_0123456789abcdef', '0123456789abcdef', ''):
        resp = cashier.post('/pos/verify-manager', json={'username': 'gmgr', 'password': password})
        assert resp.get_json()['success'] is False
        assert app.test_client().post('/login', data={'username': 'gmgr', 'password': password}) \
            .headers['Location'].endswith('/login')


def test_pin_and_badge_data_are_kept(app):
    from app.services.db import get_db_connection
    conn = get_db_connection(app.config['DATABASE'])
    row = conn.execute('SELECT pin_hash, badge_id FROM users WHERE id = 3').fetchone()
    conn.close()
    assert row['pin_hash'] and row['badge_id'] == BADGE


# --- nothing reads pin_hash / badge_id ------------------------------------------

def _source_files(*dirs, exts=('.py', '.html', '.js')):
    for d in dirs:
        for root, _dirs, files in os.walk(os.path.join(REPO, d)):
            for name in files:
                if name.endswith(exts):
                    yield os.path.join(root, name)


def test_no_code_reads_pin_or_badge():
    """Only the schema (migration.py / db.py) may name the columns; anything
    else is a comment explaining they are kept for the rebuild."""
    allowed = {os.path.join(REPO, 'app', 'services', 'migration.py'),
               os.path.join(REPO, 'app', 'services', 'db.py')}
    offenders = []
    for path in _source_files('app', 'templates', 'static'):
        if path in allowed:
            continue
        with open(path, encoding='utf-8') as f:
            for n, line in enumerate(f, 1):
                code = line.split('#', 1)[0] if path.endswith('.py') else line
                if re.search(r'pin_hash|badge_id|\bbadgeInput\b|\bpinInput\b', code):
                    offenders.append(f'{os.path.relpath(path, REPO)}:{n}: {line.strip()}')
    assert not offenders, '\n'.join(offenders)


def test_admin_user_editor_has_no_pin_or_badge(app):
    c = login(app.test_client(), 1)
    body = c.get('/admin/?tab=settings').get_data(as_text=True)
    for gone in ('set_user_pin', 'set_badge_id', 'name="pin"', 'name="badge_id"', 'Set PIN'):
        assert gone not in body, gone
    # The sign-in email is easy to find: shown per user and labelled in the editor.
    assert 'No sign-in email' in body and 'Sign-in email' in body and '/admin/set_user_email/cashier' in body
