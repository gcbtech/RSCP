"""Credential throttling is shared across gunicorn workers.

Every worker is its own process with its own memory, so a limit only holds
if its counts live in the shared database (services/throttle). These tests
model two workers as two separate app instances pointed at the same SQLite
file and spread attempts across them.
"""
import pytest

from tests.conftest import login

PASSWORD = 'Correct-Horse-1'   # every conftest user's password


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


# --- /login, across processes ------------------------------------------------

_OTHER_PROCESS = """
import sys
from app import create_app
app = create_app(test_config={'TESTING': True, 'DATABASE': sys.argv[1],
                              'WTF_CSRF_ENABLED': False, 'SECRET_KEY': 'test_key'})
client = app.test_client()
client.environ_base['REMOTE_ADDR'] = sys.argv[3]
for i in range(int(sys.argv[2])):
    resp = client.post('/login', data={'username': f'proc{i}', 'password': 'wrong'})
    assert resp.status_code == 302, resp.status_code
print('done')
"""


def _password_login(app, username, password, ip):
    return _client(app, ip).post('/login', data={'username': username, 'password': password})


def test_login_limit_holds_across_processes(fresh_app):
    """A real second process (as gunicorn runs workers) shares the count."""
    import os
    import subprocess
    import sys
    from app.routes.auth import LOGIN_FAIL_LIMIT
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    in_other = LOGIN_FAIL_LIMIT - 3
    out = subprocess.run([sys.executable, '-c', _OTHER_PROCESS, fresh_app.config['DATABASE'],
                          str(in_other), '10.10.10.10'],
                         cwd=repo, env=dict(os.environ), capture_output=True, text=True, timeout=180)
    assert out.returncode == 0 and 'done' in out.stdout, out.stderr[-2000:]
    for i in range(3):
        assert _password_login(fresh_app, f'here{i}', 'wrong', '10.10.10.10').status_code == 302
    # Even the right password is refused from that address now...
    assert _password_login(fresh_app, 'cashier', PASSWORD, '10.10.10.10').status_code == 429
    # ...but not from another one.
    resp = _password_login(fresh_app, 'cashier', PASSWORD, '10.10.10.11')
    assert resp.status_code == 302 and '/login' not in resp.headers['Location']


# --- manager override / refund sign-off ---------------------------------------

def _approve(client, username='manager', password=PASSWORD):
    return client.post('/pos/verify-manager', json={'username': username, 'password': password})


def test_manager_override_ip_limit_spans_cashier_accounts(fresh_app, second_worker):
    from app.utils.permissions import MANAGER_AUTH_FAIL_LIMIT, MANAGER_PIN
    assert MANAGER_PIN.ip_limit == 2 * MANAGER_AUTH_FAIL_LIMIT
    # Two accounts on one register, each within its own allowance, on two
    # workers, guessing different (non-)managers so only the IP limit applies.
    for app, user_id in ((fresh_app, 2), (second_worker, 1)):
        client = login(_client(app, '10.3.0.1'), user_id)
        for i in range(MANAGER_AUTH_FAIL_LIMIT):
            assert _approve(client, f'nobody{user_id}-{i}', 'wrong').status_code == 401
    # A third account on the same register is refused: the IP is spent.
    client = login(_client(fresh_app, '10.3.0.1'), 3)
    assert _approve(client).status_code == 429
    # The same account on another register is fine.
    client = login(_client(second_worker, '10.3.0.2'), 3)
    resp = _approve(client)
    assert resp.status_code == 200 and resp.get_json()['manager'] == 'manager'


def test_validate_manager_per_target_username(fresh_app, second_worker):
    from app.utils.permissions import MANAGER_PIN
    # Failures against "manager" from two registers/accounts/workers.
    a = login(_client(fresh_app, '10.4.0.1'), 2)
    b = login(_client(second_worker, '10.4.0.2'), 1)
    for i in range(MANAGER_PIN.user_limit):
        resp = (a, b)[i % 2].post('/pos/api/validate-manager',
                                  json={'username': 'Manager', 'password': f'guess-{i}'})
        assert resp.get_json()['valid'] is False and resp.status_code == 200
    resp = b.post('/pos/api/validate-manager', json={'username': 'manager', 'password': PASSWORD})
    assert resp.status_code == 429
    # A clean register can still get the manager's approval.
    c = login(_client(fresh_app, '10.4.0.3'), 3)
    resp = c.post('/pos/api/validate-manager', json={'username': 'manager', 'password': PASSWORD})
    assert resp.get_json()['valid'] is True


def test_refund_auth_is_throttled_and_success_resets_account(fresh_app, second_worker):
    from app.utils.permissions import MANAGER_AUTH_FAIL_LIMIT
    a = login(_client(fresh_app, '10.5.0.1'), 2)
    b = login(_client(second_worker, '10.5.0.1'), 2)   # same account, other worker
    good = {'username': 'manager', 'password': PASSWORD}
    for i in range(MANAGER_AUTH_FAIL_LIMIT - 1):
        resp = (a, b)[i % 2].post('/pos/refunds/auth', data={'username': f'nope-{i}', 'password': 'x'})
        assert resp.status_code == 200
    # A correct password clears the account's count...
    resp = a.post('/pos/refunds/auth', data=good)
    assert resp.status_code == 302
    for i in range(MANAGER_AUTH_FAIL_LIMIT):
        resp = b.post('/pos/refunds/auth', data={'username': f'again-{i}', 'password': 'x'})
        assert resp.status_code == 200
    # ...and the full allowance is shared between workers.
    assert a.post('/pos/refunds/auth', data=good).status_code == 429


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
