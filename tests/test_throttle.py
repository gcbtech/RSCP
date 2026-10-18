"""Credential throttling is shared across gunicorn workers.

Every worker is its own process with its own memory, so a limit only holds
if its counts live in the shared database (services/throttle). These tests
model two workers as two separate app instances pointed at the same SQLite
file and spread attempts across them.
"""
import pytest

from tests.conftest import login

PIN = '4821'


@pytest.fixture
def pos_users(fresh_app):
    """Give the cashier a PIN and the manager a PIN and a badge."""
    from werkzeug.security import generate_password_hash
    from app.services.db import get_db_connection
    conn = get_db_connection(fresh_app.config['DATABASE'])
    conn.execute('UPDATE users SET pin_hash = ? WHERE id IN (2, 3)', (generate_password_hash(PIN),))
    conn.execute("UPDATE users SET badge_id = 'BADGE-0003' WHERE id = 3")
    conn.commit()
    conn.close()
    return fresh_app


@pytest.fixture
def second_worker(fresh_app):
    """A second app instance on the same database, like another gunicorn worker."""
    from app import create_app
    return create_app(test_config={
        'TESTING': True,
        'DATABASE': fresh_app.config['DATABASE'],
        'WTF_CSRF_ENABLED': False,
        'SECRET_KEY': 'test_key',
    })


def _client(app, ip=None):
    client = app.test_client()
    if ip:
        client.environ_base['REMOTE_ADDR'] = ip
    return client


# The global session guard in create_app bounces anonymous requests for
# /pos/login to /login (it is not on the public allowlist), so the POS login
# view is driven directly, as the request it would serve.
def _pos_login(app, ip='127.0.0.1', **form):
    with app.test_request_context('/pos/login', method='POST', data=form,
                                  environ_base={'REMOTE_ADDR': ip}):
        return app.make_response(app.view_functions['pos.pos_login']())


def _pin_login(app, pin, username='cashier', ip='127.0.0.1'):
    return _pos_login(app, ip, login_type='pin', username=username, pin=pin)


def _badge_login(app, badge, ip='127.0.0.1'):
    return _pos_login(app, ip, login_type='badge', badge_id=badge)


def _signed_in(resp):
    return resp.status_code == 302 and resp.headers['Location'] == '/pos/'   # pos.sales


# --- the throttle itself ------------------------------------------------------

def test_counts_are_shared_between_app_instances(fresh_app, second_worker):
    from app.services import throttle
    for app in (fresh_app, second_worker) * 2:
        with app.app_context():
            throttle.hit('demo', 'k')
    with second_worker.app_context():
        assert throttle.is_limited('demo', 'k', 4, 60)
        assert not throttle.is_limited('demo', 'k', 5, 60)
        assert not throttle.is_limited('demo', 'other', 1, 60)
    with fresh_app.app_context():
        throttle.clear('demo', 'k')
    with second_worker.app_context():
        assert not throttle.is_limited('demo', 'k', 1, 60)


def test_failure_limit_user_bucket_spares_clean_ips(fresh_app):
    from app.services import throttle
    limit = throttle.FailureLimit('demo', ip_limit=3, ip_window=60, user_limit=2, user_window=60)
    with fresh_app.app_context():
        limit.failed('10.0.0.1', 'Bob')
        limit.failed('10.0.0.2', 'bob')
        assert limit.refused('10.0.0.1', 'BOB')        # dirty IP, user over limit
        assert not limit.refused('10.0.0.3', 'bob')    # clean IP may still try
        assert not limit.refused('10.0.0.1', 'alice')  # other users unaffected
        limit.succeeded('bob')
        assert not limit.refused('10.0.0.1', 'bob')
        limit.failed('10.0.0.1', 'x')
        limit.failed('10.0.0.1')
        assert limit.refused('10.0.0.1')               # 3 failures from this IP
        assert not limit.refused('10.0.0.2')


# --- POS PIN / badge / password login ----------------------------------------

def test_pos_pin_login_works(pos_users):
    assert _signed_in(_pin_login(pos_users, PIN))


def test_pos_login_works_after_a_few_typos(pos_users):
    for _ in range(3):
        resp = _pin_login(pos_users, '0000')
        assert resp.status_code == 200 and b'Invalid username or PIN' in resp.data
    assert _signed_in(_pin_login(pos_users, PIN))


def test_pos_badge_and_password_login_work(pos_users):
    assert _signed_in(_badge_login(pos_users, 'BADGE-0003'))
    resp = _pos_login(pos_users, login_type='password', username='cashier', password='Correct-Horse-1')
    assert _signed_in(resp)


def test_pos_login_ip_limit_is_shared_across_workers(pos_users, second_worker):
    from app.routes.pos.auth import POS_LOGIN
    workers = (pos_users, second_worker)
    # Alternate failures between the two workers, against different
    # usernames so only the per-IP limit is in play.
    for i in range(POS_LOGIN.ip_limit):
        assert _pin_login(workers[i % 2], '0000', username=f'nobody{i}').status_code == 200
    for app in workers:
        resp = _pin_login(app, PIN)   # even the right PIN is refused now
        assert resp.status_code == 429 and not _signed_in(resp)
    assert _badge_login(pos_users, 'BADGE-0003').status_code == 429


_OTHER_PROCESS = """
import sys
from app import create_app
app = create_app(test_config={'TESTING': True, 'DATABASE': sys.argv[1],
                              'WTF_CSRF_ENABLED': False, 'SECRET_KEY': 'test_key'})
for i in range(int(sys.argv[2])):
    form = {'login_type': 'pin', 'username': f'proc{i}', 'pin': '0000'}
    with app.test_request_context('/pos/login', method='POST', data=form,
                                  environ_base={'REMOTE_ADDR': sys.argv[3]}):
        assert app.make_response(app.view_functions['pos.pos_login']()).status_code == 200
print('done')
"""


def test_pos_login_limit_holds_across_processes(pos_users):
    """A real second process (as gunicorn runs workers) shares the count."""
    import os
    import subprocess
    import sys
    from app.routes.pos.auth import POS_LOGIN
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    in_other = POS_LOGIN.ip_limit - 3
    out = subprocess.run([sys.executable, '-c', _OTHER_PROCESS, pos_users.config['DATABASE'],
                          str(in_other), '10.10.10.10'],
                         cwd=repo, env=dict(os.environ), capture_output=True, text=True, timeout=180)
    assert out.returncode == 0 and 'done' in out.stdout, out.stderr[-2000:]
    for i in range(3):
        assert _pin_login(pos_users, '0000', username=f'here{i}', ip='10.10.10.10').status_code == 200
    assert _pin_login(pos_users, PIN, ip='10.10.10.10').status_code == 429
    assert _signed_in(_pin_login(pos_users, PIN, ip='10.10.10.11'))


def test_pos_badge_guessing_is_limited(pos_users, second_worker):
    from app.routes.pos.auth import POS_LOGIN
    for i in range(POS_LOGIN.ip_limit):
        resp = _badge_login((pos_users, second_worker)[i % 2], f'guess-{i}', ip='10.0.5.5')
        assert resp.status_code == 200 and b'Invalid badge' in resp.data
    assert _badge_login(pos_users, 'BADGE-0003', ip='10.0.5.5').status_code == 429


def test_pos_login_failures_elsewhere_do_not_block_another_register(pos_users, second_worker):
    from app.routes.pos.auth import POS_LOGIN
    for _ in range(POS_LOGIN.ip_limit):
        _pin_login(pos_users, '0000', ip='10.9.9.9')
    assert _pin_login(pos_users, PIN, ip='10.9.9.9').status_code == 429
    # A register on another IP (served by the other worker) signs in normally,
    # even as the same user.
    assert _signed_in(_pin_login(second_worker, PIN, ip='10.0.0.20'))


def test_pos_login_per_username_limit_across_ips(pos_users, second_worker):
    from app.routes.pos.auth import POS_LOGIN
    apps = (pos_users, second_worker)
    # One wrong PIN each from many addresses: no IP comes near its own limit.
    for i in range(POS_LOGIN.user_limit):
        assert _pin_login(apps[i % 2], '0000', ip=f'10.1.0.{i + 1}').status_code == 200
    # Any of those addresses is now refused for this username, right PIN or not...
    assert _pin_login(second_worker, PIN, ip='10.1.0.1').status_code == 429
    # ...but not for other usernames,
    assert _signed_in(_pin_login(pos_users, PIN, username='manager', ip='10.1.0.1'))
    # and a register with no failures of its own still signs the user in.
    assert _signed_in(_pin_login(pos_users, PIN, ip='10.2.0.1'))


def test_pos_login_has_no_in_memory_limiter():
    import app.routes.pos.auth as pos_auth
    assert not hasattr(pos_auth, '_pos_login_attempts')
    assert not hasattr(pos_auth, 'check_pos_rate_limit')


# --- manager override / refund sign-off ---------------------------------------

def test_manager_override_ip_limit_spans_cashier_accounts(pos_users, second_worker):
    from app.utils.permissions import MANAGER_AUTH_FAIL_LIMIT, MANAGER_PIN
    assert MANAGER_PIN.ip_limit == 2 * MANAGER_AUTH_FAIL_LIMIT
    # Two accounts on one register, each within its own allowance, on two workers.
    for app, user_id in ((pos_users, 2), (second_worker, 1)):
        client = login(_client(app, '10.3.0.1'), user_id)
        for _ in range(MANAGER_AUTH_FAIL_LIMIT):
            assert client.post('/pos/verify-manager', json={'pin': '0000'}).status_code == 401
    # A third account on the same register is refused: the IP is spent.
    client = login(_client(pos_users, '10.3.0.1'), 3)
    assert client.post('/pos/verify-manager', json={'pin': PIN}).status_code == 429
    # The same account on another register is fine.
    client = login(_client(second_worker, '10.3.0.2'), 3)
    resp = client.post('/pos/verify-manager', json={'pin': PIN})
    assert resp.status_code == 200 and resp.get_json()['manager'] == 'manager'


def test_validate_manager_per_target_username(pos_users, second_worker):
    from app.utils.permissions import MANAGER_PIN
    # Failures against "manager" from two registers/accounts/workers.
    a = login(_client(pos_users, '10.4.0.1'), 2)
    b = login(_client(second_worker, '10.4.0.2'), 1)
    for i in range(MANAGER_PIN.user_limit):
        resp = (a, b)[i % 2].post('/pos/api/validate-manager',
                                  json={'username': 'Manager', 'pin': '0000'})
        assert resp.get_json()['valid'] is False and resp.status_code == 200
    resp = b.post('/pos/api/validate-manager', json={'username': 'manager', 'pin': PIN})
    assert resp.status_code == 429
    # A clean register can still get the manager's approval.
    c = login(_client(pos_users, '10.4.0.3'), 3)
    resp = c.post('/pos/api/validate-manager', json={'username': 'manager', 'pin': PIN})
    assert resp.get_json()['valid'] is True


def test_refund_auth_is_throttled_and_success_resets_account(pos_users, second_worker):
    from app.utils.permissions import MANAGER_AUTH_FAIL_LIMIT
    a = login(_client(pos_users, '10.5.0.1'), 2)
    b = login(_client(second_worker, '10.5.0.1'), 2)   # same account, other worker
    for i in range(MANAGER_AUTH_FAIL_LIMIT - 1):
        resp = (a, b)[i % 2].post('/pos/refunds/auth', data={'badge_id': f'nope-{i}'})
        assert resp.status_code == 200
    # A correct badge clears the account's count...
    resp = a.post('/pos/refunds/auth', data={'badge_id': 'BADGE-0003'})
    assert resp.status_code == 302
    for i in range(MANAGER_AUTH_FAIL_LIMIT):
        assert b.post('/pos/refunds/auth', data={'badge_id': f'again-{i}'}).status_code == 200
    # ...and the full allowance is shared between workers.
    assert a.post('/pos/refunds/auth', data={'badge_id': 'BADGE-0003'}).status_code == 429


# --- pairing codes --------------------------------------------------------------

def test_pairing_claim_guessing_is_limited_across_workers(fresh_app, second_worker):
    from app.routes.pos.pairing import PAIRING_CLAIM
    display = _client(fresh_app, '10.6.0.9')
    code = display.post('/pos/api/pairing/request-code',
                        json={'peripheral_id': 'disp-t', 'role': 'display'}).get_json()['pairing_code']
    wrong = '000000' if code != '000000' else '111111'
    staff = [login(_client(fresh_app, '10.6.0.1'), 2), login(_client(second_worker, '10.6.0.1'), 2)]
    for i in range(PAIRING_CLAIM.ip_limit):
        resp = staff[i % 2].post('/pos/api/register/claim-code',
                                 json={'code': wrong, 'register_id': 'R1'})
        assert resp.status_code == 404
    resp = staff[0].post('/pos/api/register/claim-code', json={'code': code, 'register_id': 'R1'})
    assert resp.status_code == 429
    # Another register still pairs with the real code.
    other = login(_client(second_worker, '10.6.0.2'), 2)
    resp = other.post('/pos/api/register/claim-code', json={'code': code, 'register_id': 'R2'})
    assert resp.status_code == 200 and resp.get_json()['success']


def test_pairing_code_requests_are_limited_across_workers(fresh_app, second_worker):
    from app.routes.pos.pairing import PAIRING_REQUEST_LIMIT
    clients = [_client(fresh_app, '10.7.0.1'), _client(second_worker, '10.7.0.1')]
    for i in range(PAIRING_REQUEST_LIMIT):
        resp = clients[i % 2].post('/pos/api/pairing/request-code',
                                   json={'peripheral_id': f'p{i}', 'role': 'display'})
        assert resp.status_code == 200
    resp = clients[0].post('/pos/api/pairing/request-code',
                           json={'peripheral_id': 'one-more', 'role': 'display'})
    assert resp.status_code == 429
    resp = _client(fresh_app, '10.7.0.2').post('/pos/api/pairing/request-code',
                                               json={'peripheral_id': 'elsewhere', 'role': 'display'})
    assert resp.status_code == 200


def test_client_ip_comes_from_trusted_proxy_handling(fresh_app):
    """Through the full WSGI stack: X-Forwarded-For from an untrusted peer is
    ignored (no fresh IP per request), while a trusted proxy's is honoured."""
    from app.routes.pos.pairing import PAIRING_CLAIM

    def claim(client, xff):
        return client.post('/pos/api/register/claim-code', json={'code': '999999', 'register_id': 'R'},
                           headers={'X-Forwarded-For': xff})

    direct = login(_client(fresh_app, '203.0.113.7'), 2)      # not a trusted proxy
    for i in range(PAIRING_CLAIM.ip_limit):
        assert claim(direct, f'198.51.100.{i}').status_code == 404
    assert claim(direct, '198.51.100.250').status_code == 429

    proxied = login(_client(fresh_app, '127.0.0.1'), 2)       # local cloudflared/nginx
    for _ in range(PAIRING_CLAIM.ip_limit):
        assert claim(proxied, '192.0.2.1').status_code == 404
    assert claim(proxied, '192.0.2.1').status_code == 429
    assert claim(proxied, '192.0.2.2').status_code == 404     # a different real client


# --- public storefront API key ----------------------------------------------------

def test_public_api_key_guessing_is_limited(fresh_app, second_worker, monkeypatch):
    import app.routes.public_api as public_api
    monkeypatch.setattr(public_api, 'load_config', lambda: {'PUBLIC_API_KEY': 'right-key'})
    clients = [_client(fresh_app, '10.8.0.1'), _client(second_worker, '10.8.0.1')]
    for i in range(public_api.API_KEY_FAILURES.ip_limit):
        resp = clients[i % 2].get('/api/public/inventory', headers={'X-API-Key': f'guess-{i}'})
        assert resp.status_code == 401
    resp = clients[0].get('/api/public/inventory', headers={'X-API-Key': 'right-key'})
    assert resp.status_code == 429
    resp = _client(fresh_app, '10.8.0.2').get('/api/public/inventory', headers={'X-API-Key': 'right-key'})
    assert resp.status_code == 200
