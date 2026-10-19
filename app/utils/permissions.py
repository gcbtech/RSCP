"""
RBAC Permissions Module
The single authorization model for RSCP: users hold roles, roles grant
permissions, and every guard asks has_permission(). There is no separate
"admin flag": an administrator is a user with the super_admin role, which
holds every permission (User.is_admin is derived from that).

Guards, in order of preference:
  * protect_blueprint(bp, permission, ...) -- declared once per blueprint:
    login + module-enabled flag + the blueprint's base permission, with
    explicit sets of public / login-only endpoint names.
  * @require_permission('x.manage') -- on a route that needs more than its
    blueprint's base permission.
  * has_permission(user, 'x.y') -- inline, only for business rules inside a
    route (e.g. "discounts above N% need a manager"), and in templates.
"""
import functools
from flask import flash, redirect, url_for, request, jsonify
from flask_login import current_user
import json
import logging

from app.services import throttle
from app.utils.proxy import get_client_ip

logger = logging.getLogger(__name__)

# =============================================================================
# Roles and permissions
# =============================================================================

ROLES = {
    'super_admin': 'Administrator: every permission, including users and system settings',
    'inventory_admin': 'Daily operations plus inventory management (add/edit/delete items, imports, alerts)',
    'pos_admin': 'Daily operations plus POS management (reports, coupons, sales, refunds, POS settings)',
    'receiving_admin': 'Daily operations plus receiving management',
    'timeclock_admin': 'Daily operations plus timeclock management (timesheets, shifts, PTO)',
    'operator': 'Daily operations: receive packages, view/adjust inventory, ring sales, clock in/out',
}

# What every staff role can do (the operator baseline).
BASE_PERMISSIONS = frozenset({
    'receiving.view',
    'inventory.view',
    'pos.view',
    'timeclock.view',
})

ALL_PERMISSIONS = BASE_PERMISSIONS | frozenset({
    'receiving.manage',
    'inventory.manage',
    'pos.manage',
    'timeclock.manage',
    'admin.manage',
})

# Every *_admin role is "operator + manage one area". (set_user_roles drops
# 'operator' when an admin role is chosen, so each admin role must include
# the baseline itself.)
ROLE_PERMISSIONS = {
    'super_admin': ALL_PERMISSIONS,
    'inventory_admin': BASE_PERMISSIONS | {'inventory.manage'},
    'pos_admin': BASE_PERMISSIONS | {'pos.manage'},
    'receiving_admin': BASE_PERMISSIONS | {'receiving.manage'},
    'timeclock_admin': BASE_PERMISSIONS | {'timeclock.manage'},
    'operator': BASE_PERMISSIONS,
}

# Least-privilege role for accounts whose roles are missing, empty or unknown.
DEFAULT_ROLE = 'operator'

# Role names written by older code. 'user' was given to SSO-provisioned
# accounts but never existed in the permission map.
LEGACY_ROLE_ALIASES = {
    'user': 'operator',
}

# permission -> roles that hold it (derived; kept for callers of the old map)
PERMISSIONS = {
    perm: [role for role, perms in ROLE_PERMISSIONS.items() if perm in perms]
    for perm in sorted(ALL_PERMISSIONS)
}


# =============================================================================
# Helper functions
# =============================================================================

def normalize_roles(raw):
    """Return a clean list of known roles from a JSON string, list or None.

    Legacy aliases are mapped, unknown names dropped, duplicates removed and
    the result ordered as in ROLES. Never empty: an account with no usable
    role gets DEFAULT_ROLE.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode('utf-8', 'replace')
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except (ValueError, TypeError):
            raw = []
    if isinstance(raw, str):  # a bare JSON string such as '"operator"'
        raw = [raw]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        raw = []
    found = set()
    for role in raw:
        if not isinstance(role, str):
            continue
        role = role.strip().lower()
        role = LEGACY_ROLE_ALIASES.get(role, role)
        if role in ROLE_PERMISSIONS:
            found.add(role)
    if not found:
        found.add(DEFAULT_ROLE)
    return [role for role in ROLES if role in found]


def get_user_roles(user):
    """Get roles for a user as a normalized list ([] when not logged in)."""
    if not user or not getattr(user, 'is_authenticated', False):
        return []
    return normalize_roles(getattr(user, 'roles', None))


def permissions_for_roles(roles):
    perms = set()
    for role in normalize_roles(roles):
        perms |= ROLE_PERMISSIONS[role]
    return perms


def has_permission(user, permission):
    """Check if user has a specific permission."""
    if not user or not getattr(user, 'is_authenticated', False):
        return False
    return permission in permissions_for_roles(get_user_roles(user))


def has_role(user, role):
    """Check if user has a specific role."""
    return role in get_user_roles(user)


# =============================================================================
# Denial responses
# =============================================================================

# Endpoints that answer in JSON even though their path has no '/api/'
# segment. Filled by protect_blueprint(json_endpoints=...).
JSON_ENDPOINTS = set()


def wants_json():
    """Should an auth failure on this request be answered with JSON?"""
    if request.endpoint in JSON_ENDPOINTS:
        return True
    if '/api/' in request.path:
        return True
    if request.is_json:
        return True
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return True
    if request.headers.get('X-Response-Format') == 'json':  # POS scanner clients
        return True
    accept = request.accept_mimetypes
    return accept['application/json'] > accept['text/html']


def _safe_referrer():
    """The referring page as a same-site path, unless it is this page."""
    ref = request.referrer
    if not ref:
        return None
    from urllib.parse import urlparse
    parsed = urlparse(ref)
    if parsed.netloc and parsed.netloc != request.host:
        return None
    target = parsed.path or '/'
    if parsed.query:
        target += '?' + parsed.query
    from app.utils.helpers import is_safe_redirect
    if not is_safe_redirect(target) or target == request.full_path.rstrip('?'):
        return None
    return target


def unauthenticated_response(as_json=None):
    """401: not logged in."""
    if as_json if as_json is not None else wants_json():
        return jsonify({'success': False, 'error': 'authentication_required',
                        'message': 'Please log in.'}), 401
    flash('Please log in to access this page.')
    return redirect(url_for('auth.login', next=request.url if request.method == 'GET' else None))


def forbidden_response(message='You do not have permission to access this page.',
                       permission=None, as_json=None, fallback='main.index', error='forbidden'):
    """403: logged in but not allowed (JSON) / flash + redirect (pages)."""
    if current_user.is_authenticated:
        logger.warning(f"Permission denied: {current_user.username} -> {request.endpoint} "
                       f"(needs {permission or error})")
    if as_json if as_json is not None else wants_json():
        body = {'success': False, 'error': error, 'message': message}
        if permission:
            body['permission'] = permission
        return jsonify(body), 403
    flash(message)
    return redirect(_safe_referrer() or url_for(fallback))


# =============================================================================
# Guards
# =============================================================================

def protect_blueprint(bp, permission, *, module_flag=None, module_name=None,
                      public=(), login_only=(), json_endpoints=(), on_forbidden=None):
    """Declare a blueprint's access requirements once, as a before_request.

    For every endpoint of `bp` except the exempt ones:
      1. the user must be logged in                     (401)
      2. config[module_flag] must be true, if given     (403, module disabled)
      3. the user must hold `permission`, if given      (403)

    public:      full endpoint names that skip every check because they
                 authenticate themselves (pairing tokens, login pages).
    login_only:  endpoint names that need a login but no module/permission.
    json_endpoints: endpoints whose failures must be JSON although their
                 path lacks '/api/'.
    on_forbidden: optional callable returning the page (non-JSON) response
                 for a permission failure.
    """
    public = frozenset(public)
    login_only = frozenset(login_only)
    JSON_ENDPOINTS.update(json_endpoints)
    label = module_name or bp.name.capitalize()

    bp.access_policy = {
        'permission': permission,
        'module_flag': module_flag,
        'public': public,
        'login_only': login_only,
        'json_endpoints': frozenset(json_endpoints),
    }

    @bp.before_request
    def _enforce_access_policy():
        endpoint = request.endpoint
        if endpoint in public:
            return None
        if not current_user.is_authenticated:
            return unauthenticated_response()
        if endpoint in login_only:
            return None
        if module_flag:
            from app.services.data_manager import load_config
            if not (load_config() or {}).get(module_flag, False):
                return forbidden_response(f"{label} module is not enabled.",
                                          error='module_disabled')
        if permission and not has_permission(current_user, permission):
            if on_forbidden is not None and not wants_json():
                return on_forbidden()
            return forbidden_response(f"You don't have access to the {label} module.",
                                      permission=permission)
        return None

    return _enforce_access_policy


def require_permission(permission, json=None):
    """Decorator: the route needs `permission` (beyond its blueprint's base).

    json=True forces JSON failures; by default the style is inferred from
    the request (see wants_json).
    """
    def decorator(f):
        @functools.wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                return unauthenticated_response(as_json=json)
            if not has_permission(current_user, permission):
                return forbidden_response(permission=permission, as_json=json)
            return f(*args, **kwargs)
        decorated_function.required_permission = permission
        return decorated_function
    return decorator


# Manager authorization (void/discount approval, refund sign-off) checks a
# manager's username and password on top of the cashier's session
# (check_manager_password). PIN and badge approvals were removed in 3.0 and
# will be rebuilt from scratch. Unthrottled checks could be used to guess a
# manager's password from a register, so all counts are shared across
# gunicorn workers (services/throttle):
#   * per cashier account: 5 failures / 5 min (the account asking for the
#     override is the one that would be guessing);
#   * per register IP: 10 failures / 5 min, so switching accounts on one
#     machine doesn't buy more guesses, while two cashiers sharing a register
#     still get their own five each;
#   * per target manager username, where the form names one: 5 failures /
#     5 min from any IP (with FailureLimit's exception for IPs that have no
#     failures of their own, so a manager is never locked out by guessing
#     somewhere else).
# A successful authorization clears the account's and the manager's counts.
MANAGER_AUTH_FAIL_LIMIT = 5
MANAGER_AUTH_FAIL_WINDOW = 300  # seconds
MANAGER_PIN = throttle.FailureLimit('manager_pin', ip_limit=10, ip_window=300,
                                    user_limit=5, user_window=300)


def manager_auth_throttled(manager_username=None):
    if throttle.is_limited('manager_auth', f'user:{current_user.id}',
                           MANAGER_AUTH_FAIL_LIMIT, MANAGER_AUTH_FAIL_WINDOW):
        return True
    return MANAGER_PIN.refused(get_client_ip(), manager_username)


def record_manager_auth_failure(manager_username=None):
    throttle.hit('manager_auth', f'user:{current_user.id}')
    MANAGER_PIN.failed(get_client_ip(), manager_username)
    logger.warning(f"Failed manager authentication attempt by {current_user.username} "
                   f"from {get_client_ip()}")


def record_manager_auth_success(manager_username=None):
    throttle.clear('manager_auth', f'user:{current_user.id}')
    MANAGER_PIN.succeeded(manager_username)


def check_manager_password(username, password):
    """The User named `username` (case-insensitive) if they hold pos.manage and
    `password` is their account password, else None. The only way to approve
    a void, discount or refund; callers apply the MANAGER_PIN throttle."""
    from werkzeug.security import check_password_hash
    from app.services.auth import User
    from app.services.db import get_request_db

    if not isinstance(username, str) or not isinstance(password, str):
        return None
    username = username.strip()
    if not username or not password:
        return None
    user = User.get_by_username(username)
    if user is None or not has_permission(user, 'pos.manage'):
        return None
    row = get_request_db().execute('SELECT password_hash FROM users WHERE id = ?', (user.id,)).fetchone()
    pw_hash = row['password_hash'] if row else None
    # sso_managed_*: account made by the removed Google sign-in, no password yet.
    if not pw_hash or pw_hash.startswith('sso_managed_'):
        return None
    try:
        ok = check_password_hash(pw_hash, password)
    except (ValueError, TypeError):
        ok = False
    return user if ok else None
