"""
One permission model: roles -> permissions, guards declared per blueprint.

Covers the access matrix (anonymous / cashier / POS manager / admin, plus a
few other roles) over representative endpoints of every blueprint, module
toggles, the self-authenticating POS pairing endpoints, the legacy SSO
"user" role, and schema migration 9.
"""
import json
import sqlite3
from contextlib import contextmanager
from urllib.parse import urlparse

import pytest

from tests.conftest import login

ANON, CASHIER, MANAGER, ADMIN = None, 2, 3, 1
SSO_USER, TC_ADMIN, INV_ADMIN = 4, 5, 6

EXTRA_USERS = [
    (SSO_USER, 'sso_person', 0, '["user"]'),          # as written by old SSO provisioning
    (TC_ADMIN, 'tc_boss', 0, '["timeclock_admin"]'),
    (INV_ADMIN, 'stock_boss', 0, '["inventory_admin"]'),
]


@pytest.fixture
def app(fresh_app):
    from app.services.db import get_db_connection
    with fresh_app.app_context():
        conn = get_db_connection()
        conn.executemany(
            'INSERT INTO users (id, username, password_hash, is_admin, roles) VALUES (?, ?, ?, ?, ?)',
            [(uid, name, 'x', admin, roles) for uid, name, admin, roles in EXTRA_USERS])
        conn.commit()
        conn.close()
    return fresh_app


def client_as(app, user_id):
    c = app.test_client()
    if user_id is not None:
        login(c, user_id)
    return c


@contextmanager
def module(app, flag, enabled):
    # Settings live in the app's database (migration 6), so write them inside
    # this app's context.
    from app.services.data_manager import load_config, save_config
    with app.app_context():
        old = load_config(force_reload=True).get(flag, False)
        save_config({flag: enabled})
    try:
        yield
    finally:
        with app.app_context():
            save_config({flag: old})


def outcome(resp):
    """'login' (must authenticate), 'deny' (authenticated but refused) or 'allow'."""
    if resp.status_code == 401:
        return 'login'
    if resp.status_code == 403:
        return 'deny'
    if resp.status_code in (301, 302, 303):
        loc = urlparse(resp.headers['Location'])
        if loc.path == '/login':
            return 'deny' if 'force=' in loc.query else 'login'
        if loc.path == '/':
            return 'deny'
    if resp.status_code >= 400:
        return f'error {resp.status_code}'
    return 'allow'


L, A, D = 'login', 'allow', 'deny'

# path -> expected outcome for (anonymous, cashier, POS manager, admin)
MATRIX = [
    # receiving (main blueprint)
    ('/receiving',                 (L, A, A, A)),
    ('/history',                   (L, A, A, A)),
    ('/api/dashboard_stats',       (L, A, A, A)),
    ('/settings',                  (L, A, A, A)),
    # inventory
    ('/inventory/items',           (L, A, A, A)),
    ('/inventory/add',             (L, D, D, A)),
    ('/inventory/alerts',          (L, D, D, A)),
    ('/inventory/api/overview',    (L, A, A, A)),
    # pos
    ('/pos/',                      (L, A, A, A)),
    ('/pos/api/search?q=zz',       (L, A, A, A)),
    ('/pos/api/cart',              (L, A, A, A)),
    ('/pos/management',            (L, D, A, A)),
    ('/pos/management/sales',      (L, D, A, A)),
    ('/pos/coupons',               (L, D, A, A)),
    ('/pos/sales-manager',         (L, D, A, A)),
    # timeclock
    ('/timeclock/',                (L, A, A, A)),
    ('/timeclock/manager',         (L, D, D, A)),
    ('/timeclock/manager/pto',     (L, D, D, A)),
    # admin
    ('/admin/',                    (L, D, D, A)),
    ('/admin/logs',                (L, D, D, A)),
    ('/admin/update_status',       (L, D, D, A)),
    # notifications
    ('/api/notifications/count',   (L, A, A, A)),
]

_ROLES = (ANON, CASHIER, MANAGER, ADMIN)


@pytest.mark.parametrize('path,expected', MATRIX, ids=[m[0] for m in MATRIX])
def test_access_matrix(app, path, expected):
    got = tuple(outcome(client_as(app, uid).get(path)) for uid in _ROLES)
    assert got == expected, f'{path}: (anon, cashier, manager, admin) = {got}'


@pytest.mark.parametrize('user_id,path,expected', [
    (TC_ADMIN, '/timeclock/manager', A),
    (TC_ADMIN, '/pos/management', D),
    (TC_ADMIN, '/pos/', A),            # every *_admin role includes the operator baseline
    (TC_ADMIN, '/receiving', A),
    (INV_ADMIN, '/inventory/add', A),
    (INV_ADMIN, '/pos/', A),
    (INV_ADMIN, '/admin/', D),
    (MANAGER, '/receiving', A),
])
def test_other_roles(app, user_id, path, expected):
    assert outcome(client_as(app, user_id).get(path)) == expected


# --- response styles -------------------------------------------------------

def test_json_endpoints_get_json_errors(app):
    resp = client_as(app, ANON).get('/pos/api/cart')
    assert resp.status_code == 401 and resp.get_json()['error'] == 'authentication_required'

    resp = client_as(app, CASHIER).get('/admin/update_status')
    assert resp.status_code == 403 and resp.get_json()['error'] == 'forbidden'

    resp = client_as(app, CASHIER).post('/inventory/upload_photo')
    assert resp.status_code == 403
    assert resp.get_json()['permission'] == 'inventory.manage'

    resp = client_as(app, CASHIER).post('/inventory/fetch-image-from-url', json={'url': 'http://x'})
    assert resp.status_code == 403


def test_pages_redirect_with_flash(app):
    c = client_as(app, CASHIER)
    resp = c.get('/pos/management')
    assert resp.status_code == 302 and urlparse(resp.headers['Location']).path == '/'
    with c.session_transaction() as sess:
        assert sess.get('_flashes')

    # admin pages send a staffer to log in as an administrator
    resp = c.get('/admin/')
    assert resp.status_code == 302
    assert urlparse(resp.headers['Location']).path == '/login'
    assert 'force=' in resp.headers['Location']


def test_admin_form_posts_denied_without_side_effects(app):
    from app.services.data_manager import load_config
    with app.app_context():
        before = load_config(force_reload=True).get('AUTO_TRIM', False)
    # A POS manager may save the 'pos' group but not general (admin) settings
    resp = client_as(app, MANAGER).post('/admin/settings/save',
                                        data={'group': 'general', 'AUTO_TRIM': 'true' if not before else 'false'})
    assert resp.status_code in (302, 403)
    with app.app_context():
        assert load_config(force_reload=True).get('AUTO_TRIM', False) == before
    # and a non-admin cannot reach admin-blueprint form posts at all
    assert outcome(client_as(app, MANAGER).post('/admin/add_user', data={'username': 'x', 'password': 'Aa1aaaaa'})) == D


def test_void_order_is_admin_only(app):
    # Voiding replaced deleting and keeps its permission
    resp = client_as(app, MANAGER).post('/pos/order/NOPE-1/void')
    assert outcome(resp) == D
    # admin gets through the guard (and is told the order does not exist)
    resp = client_as(app, ADMIN).post('/pos/order/NOPE-1/void')
    assert resp.status_code == 302
    assert urlparse(resp.headers['Location']).path == '/pos/management/sales'
    # there is no delete any more
    assert client_as(app, ADMIN).post('/pos/order/NOPE-1/delete').status_code in (404, 405)


# --- module toggles --------------------------------------------------------

def test_pos_disabled(app):
    with module(app, 'POS_ENABLED', False):
        assert outcome(client_as(app, ADMIN).get('/pos/')) == D
        resp = client_as(app, CASHIER).get('/pos/api/cart')
        assert resp.status_code == 403 and resp.get_json()['error'] == 'module_disabled'
        # self-authenticating display endpoints are not tied to the session/module
        assert client_as(app, ANON).get('/pos/customer-display').status_code == 200


def test_inventory_disabled(app):
    with module(app, 'INVENTORY_ENABLED', False):
        assert outcome(client_as(app, CASHIER).get('/inventory/items')) == D
        assert outcome(client_as(app, ADMIN).get('/inventory/add')) == D
        resp = client_as(app, CASHIER).post('/inventory/api/add_quantity/1', json={'quantity': 1})
        assert resp.status_code == 403 and resp.get_json()['error'] == 'module_disabled'


def test_timeclock_disabled_blocks_every_route(app):
    from app.services.db import get_db_connection
    with module(app, 'TIMECLOCK_ENABLED', False):
        c = client_as(app, CASHIER)
        assert outcome(c.get('/timeclock/')) == D
        assert outcome(c.post('/timeclock/clock_in')) == D   # was only checked on the index
        assert outcome(client_as(app, ADMIN).get('/timeclock/manager')) == D
    with app.app_context():
        conn = get_db_connection()
        assert conn.execute('SELECT COUNT(*) FROM time_entries').fetchone()[0] == 0
        conn.close()


# --- POS pairing endpoints authenticate themselves -------------------------

def test_pos_token_endpoints_work_without_session(app):
    display = client_as(app, ANON)
    resp = display.post('/pos/api/pairing/request-code',
                        json={'peripheral_id': 'disp-x', 'role': 'display'})
    assert resp.status_code == 200
    code = resp.get_json()['pairing_code']

    staff = client_as(app, CASHIER)
    resp = staff.post('/pos/api/register/claim-code',
                      json={'code': code, 'register_id': 'REG-1', 'friendly_name': 'Front'})
    assert resp.status_code == 200, resp.get_json()

    status = display.get('/pos/api/pairing/status?peripheral_id=disp-x').get_json()
    token = status['token']
    assert display.post('/pos/api/pairing/ack',
                        json={'peripheral_id': 'disp-x', 'token': token}).status_code == 200
    resp = display.get('/pos/api/peripheral/poll?since=-1', headers={'X-Pairing-Token': token})
    assert resp.status_code == 200 and resp.get_json()['success']

    # an unknown token is an authoritative 403 {revoked}, not a login redirect
    resp = display.get('/pos/api/peripheral/poll', headers={'X-Pairing-Token': 'bogus'})
    assert resp.status_code == 403 and resp.get_json()['revoked'] is True
    assert display.get('/pos/api/customer-display/cart').status_code == 400
    assert display.get('/pos/customer-display').status_code == 200


def test_register_side_pairing_needs_pos_access(app):
    resp = client_as(app, ANON).post('/pos/api/register/claim-code', json={'code': '1', 'register_id': 'R'})
    assert resp.status_code == 401


# --- the role model --------------------------------------------------------

def test_sso_user_role_maps_to_operator(app):
    from app.services.auth import User
    with app.app_context():
        legacy = User.get(SSO_USER)
        assert legacy.roles == ['operator'] and not legacy.is_admin
        c = client_as(app, SSO_USER)
    assert outcome(c.get('/pos/')) == A
    assert outcome(c.get('/timeclock/')) == A
    assert outcome(c.get('/inventory/add')) == D
    assert outcome(c.get('/admin/')) == D


def test_is_admin_is_derived_and_read_only(app):
    from app.services.auth import User
    with app.app_context():
        assert User.get(ADMIN).is_admin
        assert not User.get(MANAGER).is_admin
        u = User(9, 'x', is_admin=True, roles=['operator'])
        assert u.is_admin is False   # the flag argument is ignored; roles decide
        with pytest.raises(AttributeError):
            u.is_admin = True


def test_normalize_roles():
    from app.utils.permissions import normalize_roles
    assert normalize_roles(None) == ['operator']
    assert normalize_roles('') == ['operator']
    assert normalize_roles('not json') == ['operator']
    assert normalize_roles('["user"]') == ['operator']
    assert normalize_roles('["POS_ADMIN", "pos_admin", "bogus"]') == ['pos_admin']
    assert normalize_roles(['operator', 'super_admin']) == ['super_admin', 'operator']


def test_permission_map_is_complete():
    from app.utils.permissions import ROLES, ROLE_PERMISSIONS, ALL_PERMISSIONS, BASE_PERMISSIONS
    assert set(ROLES) == set(ROLE_PERMISSIONS)
    assert ROLE_PERMISSIONS['super_admin'] == ALL_PERMISSIONS
    for role, perms in ROLE_PERMISSIONS.items():
        assert BASE_PERMISSIONS <= perms, role
    assert 'admin.manage' not in set().union(*(p for r, p in ROLE_PERMISSIONS.items() if r != 'super_admin'))


def test_promote_and_demote_keep_roles_in_sync(app):
    from app.services.db import get_db_connection
    admin = client_as(app, ADMIN)

    def row():
        with app.app_context():
            conn = get_db_connection()
            r = conn.execute("SELECT is_admin, roles FROM users WHERE username='cashier'").fetchone()
            conn.close()
            return r['is_admin'], json.loads(r['roles'])

    admin.post('/admin/set_user_admin/cashier/promote')
    assert row() == (1, ['super_admin', 'operator'])
    assert outcome(client_as(app, CASHIER).get('/admin/')) == A
    admin.post('/admin/set_user_admin/cashier/demote')
    assert row() == (0, ['operator'])
    assert outcome(client_as(app, CASHIER).get('/admin/')) == D


def test_access_policies_declared_and_consistent(app):
    guarded = {'main', 'admin', 'inventory', 'pos', 'timeclock', 'notifications'}
    for name in guarded:
        policy = getattr(app.blueprints[name], 'access_policy', None)
        assert policy is not None, name
        for endpoint in policy['public'] | policy['login_only'] | policy['json_endpoints']:
            assert endpoint in app.view_functions, endpoint
            assert endpoint.startswith(name + '.'), endpoint
    pos_public = app.blueprints['pos'].access_policy['public']
    # every POS public endpoint must also be on the global login allowlist
    for endpoint in pos_public:
        rule = next(r for r in app.url_map.iter_rules() if r.endpoint == endpoint)
        assert rule.rule == '/pos/logout' or rule.rule.startswith((
            '/pos/customer-display', '/pos/api/pairing/',
            '/pos/api/peripheral/', '/pos/api/customer-display/')), rule.rule


REMOVED_AUTH_ENDPOINTS = {
    'auth.login_sso': '/login/sso',            # RSCP's own Google/OIDC sign-in
    'auth.sso_callback': '/login/callback',
    'pos.pos_login': '/pos/login',             # POS PIN/badge sign-in page
    'admin.set_user_pin': '/admin/set_user_pin/cashier',
    'admin.set_badge_id': '/admin/set_badge_id/cashier',
}


def test_removed_sign_in_routes_are_gone(app):
    rules = {r.rule for r in app.url_map.iter_rules()}
    for endpoint, path in REMOVED_AUTH_ENDPOINTS.items():
        assert endpoint not in app.view_functions, endpoint
        assert path not in rules and not any(r.startswith(path + '/') for r in rules), path
    admin = client_as(app, ADMIN)
    assert admin.get('/login/sso').status_code == 404
    assert admin.get('/login/callback').status_code == 404
    assert admin.get('/pos/login').status_code == 404
    assert admin.post('/admin/set_user_pin/cashier', data={'pin': '1234'}).status_code in (404, 405)
    assert admin.post('/admin/set_badge_id/cashier', data={'badge_id': 'B1'}).status_code in (404, 405)


def test_anonymous_pos_login_goes_to_login(app):
    resp = client_as(app, ANON).get('/pos/login')
    assert resp.status_code == 302 and urlparse(resp.headers['Location']).path == '/login'


def test_pos_logout_uses_the_normal_logout(app):
    for who in (ANON, CASHIER, ADMIN):
        c = client_as(app, who)
        resp = c.get('/pos/logout')
        assert resp.status_code == 302 and urlparse(resp.headers['Location']).path == '/logout', who
        resp = c.get('/logout')
        assert resp.status_code == 302 and urlparse(resp.headers['Location']).path == '/login'
        with c.session_transaction() as sess:
            assert '_user_id' not in sess


# --- migration 9 -----------------------------------------------------------

def test_migration_9(app):
    from app.services.migration import SCHEMA_MIGRATIONS, _m9_unify_admin_roles
    assert any(v == 9 and fn is _m9_unify_admin_roles for v, _, fn in SCHEMA_MIGRATIONS)

    db_path = app.config['DATABASE']
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        rows = [
            (20, 'legacy_admin', 1, '[]'),
            (21, 'admin_null', 1, None),
            (22, 'admin_operator', 1, '["operator"]'),
            (23, 'sso', 0, '["user"]'),
            (24, 'junk', 0, 'not json'),
            (25, 'super_no_flag', 0, '["super_admin"]'),
            (26, 'mixed', 0, '["POS_ADMIN","pos_admin","bogus"]'),
            (27, 'plain', 0, '["operator"]'),
        ]
        conn.executemany('INSERT INTO users (id, username, password_hash, is_admin, roles) '
                         'VALUES (?, ?, ?, ?, ?)', [(i, n, 'x', a, r) for i, n, a, r in rows])
        conn.execute('BEGIN IMMEDIATE')
        _m9_unify_admin_roles(conn)
        _m9_unify_admin_roles(conn)   # idempotent
        conn.execute('COMMIT')
        got = {r[0]: (r[1], json.loads(r[2])) for r in
               conn.execute('SELECT id, is_admin, roles FROM users WHERE id >= 20')}
    finally:
        conn.close()

    assert got == {
        20: (1, ['super_admin']),
        21: (1, ['super_admin']),
        22: (1, ['super_admin', 'operator']),
        23: (0, ['operator']),
        24: (0, ['operator']),
        25: (1, ['super_admin']),
        26: (0, ['pos_admin']),
        27: (0, ['operator']),
    }
    # and the app agrees
    assert outcome(client_as(app, 20).get('/admin/')) == A
    assert outcome(client_as(app, 23).get('/admin/')) == D
