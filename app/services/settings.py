"""
One typed settings store, kept in the database.

Every setting RSCP knows about is declared once in SETTINGS below (type,
default, group, label, permission, validation). Values live in the
`settings` table as JSON (schema migration 6 created it and imported the old
config.json keys and pos_settings rows). Only bootstrap values that are
needed before the database is usable (BOOTSTRAP_KEYS: SECRET_KEY) stay in
config.json.

API:
    get(key)                    typed value (stored, else the registry default)
    get_many(keys=None) / all() dict of typed values
    set(key, value, user=None)  validate, coerce and store one value
    set_many(values, user=None) same for several values, in one transaction

Cross-worker coherence: gunicorn workers each cache the table in memory. Every
write also stores a fresh random token in `settings_meta`; a worker compares
that one-row token on its first settings access of each request (or at most
once a second outside requests) and reloads when it changed. A change saved in
one worker is therefore visible to every worker on its next request.

Legacy callers keep working through data_manager.load_config()/save_config()
and pos.core.get_pos_setting()/set_pos_setting(), which are thin shims over
this module.
"""
import builtins
import copy
import ipaddress
import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import uuid

logger = logging.getLogger(__name__)

# Keys that stay in config.json: needed before the database can be opened.
BOOTSTRAP_KEYS = frozenset({'SECRET_KEY'})

# Environment variables override these keys wherever they are read.
ENV_OVERRIDES = {
    'WEBHOOK_URL': 'RSCP_WEBHOOK_URL',
    'SECRET_KEY': 'RSCP_SECRET_KEY',
    'SSO_CLIENT_ID': 'RSCP_SSO_CLIENT_ID',
    'SSO_CLIENT_SECRET': 'RSCP_SSO_CLIENT_SECRET',
    'SSO_DISCOVERY_URL': 'RSCP_SSO_DISCOVERY_URL',
}

# Unregistered keys whose name contains one of these are treated as secret.
SECRET_NAME_MARKERS = ('SECRET', 'PASS', 'TOKEN', 'API_KEY', 'ADMIN_HASH')

# Placeholder the admin page shows for a stored secret; posting it back keeps it.
SECRET_PLACEHOLDER = '********'


class SettingsError(ValueError):
    """A setting could not be saved (unknown key, invalid value, ...)."""


class Setting:
    def __init__(self, type, default, group, label, secret=False, validator=None,
                 permission='admin', choices=None, min=None, max=None, restart=False,
                 editable=True, form_scale=None, encode=None, from_form=None):
        self.type = type                # bool | int | float | str | list | 'json'
        self.default = default
        self.group = group
        self.label = label
        self.secret = secret            # never rendered; write-only (blank = keep)
        self.validator = validator      # fn(value) -> value (may normalise), raises ValueError
        self.permission = permission    # 'admin' or an RBAC permission like 'pos.manage'
        self.choices = choices
        self.min = min
        self.max = max
        self.restart = restart          # takes effect after a restart
        self.editable = editable        # accepted by the generic settings form
        self.form_scale = form_scale    # form value * scale = stored value (e.g. percent)
        self.encode = encode            # fn(plaintext) -> stored form, for submitted secrets
        self.from_form = from_form      # fn(form, key) -> value or _ABSENT, for compound fields
        self.key = None

    def coerce(self, value):
        """Return `value` converted to this setting's type, validated. Raises SettingsError."""
        try:
            value = _COERCERS[self.type](value)
            if self.choices is not None and value not in self.choices:
                raise ValueError(f"must be one of: {', '.join(str(c) for c in self.choices)}")
            if self.min is not None and value < self.min:
                raise ValueError(f"must be at least {self.min}")
            if self.max is not None and value > self.max:
                raise ValueError(f"must be at most {self.max}")
            if self.validator is not None:
                value = self.validator(value)
        except SettingsError:
            raise
        except (ValueError, TypeError) as e:
            raise SettingsError(f"{self.label}: {e}") from None
        return value

    def default_value(self):
        return copy.deepcopy(self.default)


# --- type coercion -----------------------------------------------------------

_TRUE = {'true', 'on', '1', 'yes', 'y'}
_FALSE = {'false', 'off', '0', 'no', 'n', '', 'none'}


def _to_bool(v):
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ValueError(f"'{v}' is not a yes/no value")


def _to_int(v):
    if isinstance(v, bool):
        raise ValueError("expected a whole number")
    if isinstance(v, float):
        if not v.is_integer():
            raise ValueError("expected a whole number")
        return int(v)
    if isinstance(v, int):
        return v
    s = str(v).strip()
    try:
        return int(s)
    except ValueError:
        f = float(s)  # accept '30.0'
        if not f.is_integer():
            raise ValueError("expected a whole number")
        return int(f)


def _to_float(v):
    if isinstance(v, bool):
        raise ValueError("expected a number")
    f = float(str(v).strip()) if isinstance(v, str) else float(v)
    if not math.isfinite(f):
        raise ValueError("expected a finite number")
    return f


def _to_str(v):
    if v is None:
        return ''
    if isinstance(v, (dict, list)):
        raise ValueError("expected text")
    return str(v)


def _to_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        s = v.strip()
        if s.startswith('['):
            v = json.loads(s)
        else:
            return [p.strip() for p in s.replace('\n', ',').split(',') if p.strip()]
    if isinstance(v, (list, tuple)):
        return [str(p).strip() for p in v if str(p).strip()]
    raise ValueError("expected a list")


def _to_json(v):
    if isinstance(v, str):
        v = json.loads(v) if v.strip() else {}
    json.dumps(v)  # must be serialisable
    return v


_COERCERS = {bool: _to_bool, int: _to_int, float: _to_float, str: _to_str,
             list: _to_list, 'json': _to_json}


# --- validators / special rules ----------------------------------------------

def _normalize_domains(domains):
    out = []
    for d in domains:
        d = d.strip().lower()
        if d:
            d = d if d.startswith('@') else '@' + d
            if d not in out:
                out.append(d)
    return out


def _validate_proxies(entries):
    for entry in entries:
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            raise ValueError(f'"{entry}" is not a valid IP address or network') from None
    return entries or ['127.0.0.1', '::1']


def _validate_hhmm(v):
    v = v.strip()
    if v and not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', v):
        raise ValueError("use HH:MM (24-hour)")
    return v


def _validate_date(v):
    v = (v or '').strip()
    if v and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', v):
        raise ValueError("use YYYY-MM-DD")
    return v


def _validate_url(v):
    v = v.strip()
    if v and not re.match(r'https?://', v, re.I):
        raise ValueError("must start with http:// or https://")
    return v


def _encrypt(v):
    from app.services.security import encrypt
    return encrypt(v)


def _obscure(v):
    from flask import current_app
    from app.utils.helpers import obscure_string
    return obscure_string(v, current_app.secret_key)


_LOCATION_FIELDS = (('area', 'Area'), ('aisle', 'Aisle'), ('shelf', 'Shelf'), ('bin', 'Bin'))


def _location_labels_from_form(form, key):
    if not any(f'{key}.{sub}' in form for sub, _ in _LOCATION_FIELDS):
        return _ABSENT
    return {sub: (form.get(f'{key}.{sub}') or '').strip() or label for sub, label in _LOCATION_FIELDS}


def _location_options_from_form(form, key):
    if not any(f'{key}.{sub}' in form for sub, _ in _LOCATION_FIELDS):
        return _ABSENT
    return {sub: _to_list(form.get(f'{key}.{sub}') or '') for sub, _ in _LOCATION_FIELDS}


def _validate_location_options(v):
    if not isinstance(v, dict):
        raise ValueError("expected a mapping of field -> options")
    return {str(k): _to_list(opts) for k, opts in v.items()}


def _validate_location_labels(v):
    if not isinstance(v, dict):
        raise ValueError("expected a mapping of field -> label")
    return {str(k): str(label) for k, label in v.items()}


def _check_tax_rate(v):
    if not 0 <= v <= 1:
        raise ValueError("must be between 0% and 100%")
    return round(v, 6)


# --- the registry ------------------------------------------------------------
# group -> (title, admin panel tab to return to)
GROUPS = {
    'general': ('General', None),
    'modules': ('Modules', 'settings'),
    'inventory': ('Inventory', 'inventory'),
    'pos_approval': ('POS manager approval', 'pos'),
    'pos': ('POS', 'pos'),
    'security': ('Security', 'settings'),
    'notifications': ('Notifications', 'settings'),
    'sso': ('SSO / OIDC', 'settings'),
    'network': ('Reverse proxy', 'settings'),
    'timeclock': ('Timeclock', None),
    'api': ('Public API', 'settings'),
}

S = Setting
SETTINGS = {
    # General
    'ORG_NAME': S(str, '', 'general', 'Organization name'),
    'ORGANIZATION_NAME': S(str, '', 'general', 'Organization name (legacy)', editable=False),
    'AUTO_TRIM': S(bool, False, 'general', 'Auto-trim packages (60 days)'),
    'DATE_FORMAT': S(str, 'US', 'general', 'Date format', choices=('US', 'UK')),
    'AUTO_CONFIRM_IMPORT': S(bool, False, 'general', 'Auto-confirm imports'),
    'LOCATION_PREFIX': S(str, '', 'general', 'SKU location prefix (legacy)', editable=False),

    # Modules
    'INVENTORY_ENABLED': S(bool, False, 'modules', 'Inventory module'),
    'POS_ENABLED': S(bool, False, 'modules', 'POS module'),
    'TIMECLOCK_ENABLED': S(bool, False, 'modules', 'Timeclock module'),

    # Inventory
    'INVENTORY_AUTO_COPY_SKU': S(bool, False, 'inventory', 'Auto-copy SKU on create'),
    'INVENTORY_QUICK_ADD': S(bool, False, 'inventory', 'Quick Add'),
    'TRACK_FIRST_STOCK_DATE': S(bool, False, 'inventory', 'Track first stock date'),
    'LOCATION_LABELS': S('json', {}, 'inventory', 'Location labels',
                         validator=_validate_location_labels, from_form=_location_labels_from_form),
    'LOCATION_OPTIONS': S('json', {}, 'inventory', 'Location values',
                          validator=_validate_location_options, from_form=_location_options_from_form),
    'LOCATION_ALLOW_CUSTOM': S(bool, True, 'inventory', 'Allow custom location values'),
    'ITEM_ADDON_1_ENABLED': S(bool, False, 'inventory', 'Addon 1 enabled'),
    'ITEM_ADDON_1_LABEL': S(str, '', 'inventory', 'Addon 1 label'),
    'ITEM_ADDON_1_TEXT': S(str, '', 'inventory', 'Addon 1 receipt text'),
    'ITEM_ADDON_2_ENABLED': S(bool, False, 'inventory', 'Addon 2 enabled'),
    'ITEM_ADDON_2_LABEL': S(str, '', 'inventory', 'Addon 2 label'),
    'ITEM_ADDON_2_TEXT': S(str, '', 'inventory', 'Addon 2 receipt text'),
    'PREFERRED_MARGIN_ENABLED': S(bool, False, 'inventory', 'Show suggested sell price'),
    'PREFERRED_MARGIN_PERCENT': S(float, 20.0, 'inventory', 'Preferred margin %', min=0, max=1000),
    'PREFERRED_MARGIN_TYPE': S(str, 'margin', 'inventory', 'Profitability basis', choices=('margin', 'markup')),
    'LOW_STOCK_THRESHOLD': S(int, 5, 'inventory', 'Low stock threshold', min=0,
                             permission='inventory.manage'),

    # POS manager approval limits (admin, as before)
    'POS_MAX_DISCOUNT_PERCENT': S(float, 10.0, 'pos_approval', 'Max discount %', min=0, max=100),
    'POS_MAX_DISCOUNT_AMOUNT': S(float, 20.0, 'pos_approval', 'Max discount $', min=0),
    'POS_REQUIRE_MANAGER_REFUND': S(bool, False, 'pos_approval', 'Require manager for refunds'),

    # POS (formerly the pos_settings table): POS managers may change these
    'TAX_RATE': S(float, 0.0, 'pos', 'Sales tax rate', validator=_check_tax_rate,
                  permission='pos.manage', form_scale=0.01),
    'REQUIRE_MANAGER_VOID': S(bool, False, 'pos', 'Require manager for void', permission='pos.manage'),
    'ALLOW_HOLD_ORDERS': S(bool, True, 'pos', 'Allow hold/recall orders', permission='pos.manage'),
    'POS_DEFAULT_REPORT_TIMEFRAME': S(int, 30, 'pos', 'Default report timeframe (days)',
                                      choices=(1, 7, 30, 90, 365), permission='pos.manage'),
    'CASH_DISCOUNT_ENABLED': S(bool, False, 'pos', 'Cash discount', permission='pos.manage'),
    'CASH_DISCOUNT_TYPE': S(str, 'percent', 'pos', 'Cash discount type', choices=('percent', 'fixed'),
                            permission='pos.manage'),
    'CASH_DISCOUNT_AMOUNT': S(float, 0.0, 'pos', 'Cash discount amount', min=0, permission='pos.manage'),
    'RECEIPT_STORE_NAME': S(str, '', 'pos', 'Receipt store name', permission='pos.manage'),
    'RECEIPT_HEADER': S(str, '', 'pos', 'Receipt header', permission='pos.manage'),
    'RECEIPT_FOOTER': S(str, '', 'pos', 'Receipt footer', permission='pos.manage'),
    'RECEIPT_STORE_NAME_BOLD': S(bool, False, 'pos', 'Store name bold', permission='pos.manage'),
    'RECEIPT_STORE_NAME_ITALIC': S(bool, False, 'pos', 'Store name italic', permission='pos.manage'),
    'RECEIPT_HEADER_BOLD': S(bool, False, 'pos', 'Header bold', permission='pos.manage'),
    'RECEIPT_HEADER_ITALIC': S(bool, False, 'pos', 'Header italic', permission='pos.manage'),
    'RECEIPT_FOOTER_BOLD': S(bool, False, 'pos', 'Footer bold', permission='pos.manage'),
    'RECEIPT_FOOTER_ITALIC': S(bool, False, 'pos', 'Footer italic', permission='pos.manage'),
    'CASH_DRAWER_ENABLED': S(bool, False, 'pos', 'Cash drawer', permission='pos.manage'),
    'CASH_DRAWER_PORT': S(str, '', 'pos', 'Cash drawer port', permission='pos.manage'),
    'AUTO_PRINT_RECEIPT': S(bool, False, 'pos', 'Auto-print receipt', permission='pos.manage'),
    'POS_EMAIL_HOST': S(str, '', 'pos', 'SMTP host', permission='pos.manage'),
    'POS_EMAIL_PORT': S(int, 587, 'pos', 'SMTP port', min=1, max=65535, permission='pos.manage'),
    'POS_EMAIL_USER': S(str, '', 'pos', 'SMTP user', permission='pos.manage'),
    'POS_EMAIL_PASSWORD': S(str, '', 'pos', 'SMTP password', secret=True, encode=_encrypt,
                            permission='pos.manage'),
    'POS_EMAIL_RECIPIENTS': S(str, '', 'pos', 'Report recipients', permission='pos.manage'),
    'POS_AUTO_EMAIL_ENABLED': S(bool, False, 'pos', 'Automated end-of-day email', permission='pos.manage'),
    'POS_AUTO_EMAIL_TIME': S(str, '', 'pos', 'End-of-day email time', validator=_validate_hhmm,
                             permission='pos.manage'),
    'POS_LAST_AUTO_EMAIL_DATE': S(str, '', 'pos', 'Last end-of-day email', editable=False,
                                  permission='pos.manage'),

    # Security
    'SESSION_TIMEOUT_ENABLED': S(bool, False, 'security', 'Session timeout'),
    'SESSION_TIMEOUT_MINUTES': S(int, 30, 'security', 'Session timeout (minutes)', min=1, max=1440),
    'SECURE_COOKIES': S(bool, False, 'security', 'Secure-only cookies', restart=True),

    # Notifications
    'WEBHOOK_URL': S(str, '', 'notifications', 'Receiving webhook URL', secret=True,
                     validator=_validate_url, encode=_obscure),
    'WEBHOOK_ENABLED': S(bool, False, 'notifications', 'Receiving webhook'),
    'WEBHOOK_URL_INVENTORY': S(str, '', 'notifications', 'Inventory webhook URL', secret=True,
                               validator=_validate_url, encode=_obscure),
    'WEBHOOK_ENABLED_INVENTORY': S(bool, False, 'notifications', 'Inventory webhook'),
    'NOTIFY_LOW_STOCK': S(bool, False, 'notifications', 'Low stock alerts'),
    'NOTIFY_OOS': S(bool, False, 'notifications', 'Out of stock alerts'),
    'NOTIFY_PRIORITY_PACKAGES': S(bool, False, 'notifications', 'Priority package arrivals'),
    'NOTIFY_NORMAL_PACKAGES': S(bool, False, 'notifications', 'Normal package arrivals'),

    # SSO
    'SSO_ENABLED': S(bool, False, 'sso', 'SSO'),
    'SSO_CLIENT_ID': S(str, '', 'sso', 'SSO client ID', restart=True),
    'SSO_CLIENT_SECRET': S(str, '', 'sso', 'SSO client secret', secret=True, restart=True),
    'SSO_DISCOVERY_URL': S(str, 'https://accounts.google.com/.well-known/openid-configuration', 'sso',
                           'SSO discovery URL', validator=_validate_url, restart=True),
    'SSO_ALLOWED_DOMAINS': S(list, [], 'sso', 'SSO allowed domains', validator=_normalize_domains),
    'SSO_ALLOW_NEW_USERS': S(bool, False, 'sso', 'Auto-provision SSO users'),

    # Network
    'TRUSTED_PROXIES': S(list, ['127.0.0.1', '::1'], 'network', 'Trusted proxies',
                         validator=_validate_proxies, restart=True),

    # Timeclock
    'TIMECLOCK_PAY_PERIOD_START': S(str, '2024-01-01', 'timeclock', 'Pay period start',
                                    validator=_validate_date, permission='timeclock.manage'),
    'TIMECLOCK_PAY_PERIOD_TYPE': S(str, 'biweekly', 'timeclock', 'Pay period type',
                                   permission='timeclock.manage'),
    'TIMECLOCK_FULLTIME_HOURS': S(float, 40.0, 'timeclock', 'Full-time hours', min=0,
                                  permission='timeclock.manage'),
    'TIMECLOCK_MIN_SHIFT_DURATION': S(float, 4.0, 'timeclock', 'Minimum shift (hours)', min=0,
                                      permission='timeclock.manage'),
    'TIMECLOCK_PAID_BREAKS': S(bool, True, 'timeclock', 'Paid breaks', permission='timeclock.manage'),
    'TIMECLOCK_GRACE_PERIOD': S(int, 15, 'timeclock', 'Grace period (minutes)', min=0,
                                permission='timeclock.manage'),

    # Public API
    'PUBLIC_API_KEY': S(str, '', 'api', 'Public API key', secret=True),
}
del S
for _k, _s in SETTINGS.items():
    _s.key = _k


def is_secret(key):
    s = SETTINGS.get(key)
    if s is not None:
        return s.secret
    return any(m in key.upper() for m in SECRET_NAME_MARKERS)


def settings_in_group(group):
    return [(k, s) for k, s in SETTINGS.items() if s.group == group]


# --- storage -----------------------------------------------------------------

_ABSENT = object()
_NO_TABLE = object()
_CACHE = {}          # db path -> {'token', 'values', 'checked'}
_CACHE_LOCK = threading.Lock()
_RECHECK_SECONDS = 1.0  # outside a request (background jobs, CLI)


def _db_path():
    from app.services import db as _db
    path = _db.DB_PATH
    try:
        from flask import current_app
        path = current_app.config.get('DATABASE', _db.DB_PATH)
    except RuntimeError:
        pass
    return path


def _read_conn(path):
    """(connection, should_close): the request's shared connection when there is one."""
    try:
        from flask import has_app_context
        if has_app_context():
            from app.services.db import get_request_db
            return get_request_db(), False
    except Exception:
        pass
    return sqlite3.connect(path, timeout=10), True


def _checked_this_request(path):
    try:
        from flask import g, has_request_context
        if has_request_context():
            return path in g.get('_settings_checked', ())
    except Exception:
        pass
    return None


def _mark_checked(path):
    try:
        from flask import g, has_request_context
        if has_request_context():
            seen = g.get('_settings_checked')
            if seen is None:
                seen = g._settings_checked = builtins.set()
            seen.add(path)
    except Exception:
        pass


def _decode(raw):
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def stored_values(force=False):
    """Every row of the settings table as {key: decoded value}, or None when the
    table does not exist yet (database not migrated to v6)."""
    path = _db_path()
    entry = _CACHE.get(path)
    if entry is not None and not force:
        checked = _checked_this_request(path)
        if checked or (checked is None and time.monotonic() - entry['checked'] < _RECHECK_SECONDS):
            return entry['values']
    if path != ':memory:' and not os.path.exists(path):
        return None  # never create an empty database just by reading settings

    conn, close = _read_conn(path)
    try:
        try:
            row = conn.execute('SELECT token FROM settings_meta WHERE id = 1').fetchone()
            token = row[0] if row else None
        except sqlite3.OperationalError:
            token = _NO_TABLE
        if token is not _NO_TABLE and entry is not None and not force and entry['token'] == token:
            values = entry['values']
        elif token is _NO_TABLE:
            values = None
        else:
            # read after the token, so the values are at least as new as it
            values = {r[0]: _decode(r[1]) for r in conn.execute('SELECT key, value FROM settings')}
    finally:
        if close:
            conn.close()
    with _CACHE_LOCK:
        _CACHE[path] = {'token': token, 'values': values, 'checked': time.monotonic()}
    _mark_checked(path)
    return values


def store_ready():
    return stored_values() is not None


def invalidate():
    """Drop this process's cache (all databases)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def _current_username():
    try:
        from flask_login import current_user
        if current_user and current_user.is_authenticated:
            return current_user.username
    except Exception:
        pass
    return None


def write_raw(values, user=None):
    """Store already-validated values in one BEGIN IMMEDIATE transaction."""
    if not values:
        return
    path = _db_path()
    if user is None:
        user = _current_username()
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    try:
        conn.execute('BEGIN IMMEDIATE')
        try:
            for key, value in values.items():
                conn.execute(
                    'INSERT OR REPLACE INTO settings (key, value, updated_at, updated_by) '
                    'VALUES (?, ?, CURRENT_TIMESTAMP, ?)',
                    (key, json.dumps(value, default=str), user))
            conn.execute('UPDATE settings_meta SET token = ? WHERE id = 1', (uuid.uuid4().hex,))
            conn.execute('COMMIT')
        except Exception:
            conn.execute('ROLLBACK')
            raise
    finally:
        conn.close()
    with _CACHE_LOCK:
        _CACHE.pop(path, None)


# --- typed API ---------------------------------------------------------------

def _env(key):
    var = ENV_OVERRIDES.get(key)
    return os.environ.get(var) if var else None


def _legacy_value(key):
    """Pre-migration fallback: config.json, then the pos_settings table."""
    from app.services.data_manager import load_config
    conf = load_config()
    if key in conf:
        return conf[key]
    conn, close = _read_conn(_db_path())
    try:
        row = conn.execute('SELECT value FROM pos_settings WHERE key = ?', (key,)).fetchone()
        return row[0] if row else _ABSENT
    except sqlite3.OperationalError:
        return _ABSENT
    finally:
        if close:
            conn.close()


def get(key, default=_ABSENT):
    """Typed value of `key`: env override, else stored, else the registry default.

    An unregistered key returns its raw stored value (or `default`/None)."""
    s = SETTINGS.get(key)
    env = _env(key)
    stored = stored_values()
    if env:
        value = env
    elif stored is not None:
        value = stored.get(key, _ABSENT)
    else:
        value = _legacy_value(key)
    if s is None:
        return (None if default is _ABSENT else default) if value is _ABSENT else value
    if value is _ABSENT:
        return s.default_value() if default is _ABSENT else default
    try:
        return s.coerce(value)
    except SettingsError:
        logger.warning(f"[Settings] Stored value for {key} is invalid ({value!r}); using the default")
        return s.default_value() if default is _ABSENT else default


def get_many(keys=None):
    """{key: typed value} for `keys` (default: every registered setting)."""
    return {k: get(k) for k in (keys if keys is not None else SETTINGS)}


def all():  # noqa: A001 - module-level API name
    return get_many()


def _cross_check(updates):
    """Rules that involve more than one setting."""
    if any(k.startswith('SSO_') for k in updates):
        enabled = updates.get('SSO_ENABLED', get('SSO_ENABLED'))
        domains = updates.get('SSO_ALLOWED_DOMAINS', get('SSO_ALLOWED_DOMAINS'))
        # SSO without a domain allowlist would admit any account the identity
        # provider vouches for (e.g. any Google account).
        if enabled and not domains:
            raise SettingsError('SSO needs at least one allowed domain before it can be enabled.')


def set_many(values, user=None):
    """Validate, coerce and store several settings atomically. Raises SettingsError."""
    clean = {}
    for key, value in (values or {}).items():
        s = SETTINGS.get(key)
        if s is None:
            raise SettingsError(f"Unknown setting: {key}")
        clean[key] = s.coerce(value)
    _cross_check(clean)
    if stored_values() is None:
        raise SettingsError('The settings table does not exist yet (database not migrated).')
    write_raw(clean, user)
    return clean


def set(key, value, user=None):  # noqa: A001 - module-level API name
    return set_many({key: value}, user)[key]


def set_lenient(values, user=None):
    """Compatibility writer for save_config()/set_pos_setting(): registered keys are
    coerced when possible; anything else is stored as given (old behaviour)."""
    out = {}
    for key, value in values.items():
        s = SETTINGS.get(key)
        if s is not None:
            try:
                value = s.coerce(value)
            except SettingsError as e:
                logger.warning(f"[Settings] Storing unvalidated value for {key}: {e}")
        out[key] = value
    write_raw(out, user)
    return out


def to_legacy_string(value):
    """The pos_settings string convention: 'true'/'false', str(number), text."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if value is None:
        return ''
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


# --- templates ---------------------------------------------------------------

def template_view(conf):
    """`conf` minus secrets, for templates; secrets become <KEY>_SET booleans."""
    safe = {}
    for key, value in (conf or {}).items():
        if is_secret(key):
            safe[f'{key}_SET'] = bool(value)
        else:
            safe[key] = value
    return safe


# --- the generic settings form -----------------------------------------------

def user_can_edit(user, key):
    s = SETTINGS.get(key)
    if s is None or not getattr(user, 'is_authenticated', False):
        return False
    from app.utils.permissions import has_permission
    if s.permission == 'admin':
        return bool(getattr(user, 'is_admin', False)) or has_permission(user, 'admin.manage')
    return has_permission(user, s.permission)


def parse_submission(group, source, from_form=True):
    """Turn a submitted form (or JSON dict) for `group` into {key: value}.

    Only fields present are changed. Checkboxes should be preceded by a hidden
    input of the same name with value "false", so unchecked boxes are sent.
    Secrets left blank (or showing the placeholder) are kept.
    Raises SettingsError on unknown groups or invalid values.
    """
    if group not in GROUPS:
        raise SettingsError('Unknown settings group.')
    updates = {}
    for key, s in settings_in_group(group):
        if not s.editable:
            continue
        if from_form and s.from_form is not None:
            raw = s.from_form(source, key)
        elif key not in source:
            raw = _ABSENT
        elif from_form and s.type is bool:
            raw = source.getlist(key)[-1]
        else:
            raw = source.get(key)
            if isinstance(raw, str):
                raw = raw.strip()
        if raw is _ABSENT:
            continue
        if s.secret and (raw is None or str(raw).strip() in ('', SECRET_PLACEHOLDER)):
            continue
        if from_form and s.form_scale is not None and raw != '':
            try:
                raw = round(float(raw) * s.form_scale, 6)
            except ValueError:
                raise SettingsError(f"{s.label}: expected a number") from None
        value = s.coerce(raw)
        if s.secret and s.encode is not None:
            value = s.encode(value)
        updates[key] = value
    return updates


# --- schema migration 6 ------------------------------------------------------

def create_tables(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_by TEXT
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS settings_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            token TEXT NOT NULL
        )
    ''')
    conn.execute('INSERT OR IGNORE INTO settings_meta (id, token) VALUES (1, ?)', (uuid.uuid4().hex,))


def import_legacy(conn, config_file=None):
    """Copy config.json (minus bootstrap keys) and pos_settings into `settings`.

    Typed through the registry; values that fail validation are skipped (the
    registry default applies) and logged. Existing rows are never overwritten,
    so this is idempotent. config.json and pos_settings are left untouched.
    """
    from app import paths
    config_file = config_file or paths.CONFIG_FILE
    legacy = {}
    if os.path.exists(config_file):
        # An unreadable config.json fails the migration loudly rather than
        # silently starting with no settings.
        with open(config_file, 'r') as f:
            legacy = json.load(f) or {}
    rows = []
    for key, value in legacy.items():
        if key not in BOOTSTRAP_KEYS:
            rows.append((key, value))
    try:
        rows += list(conn.execute('SELECT key, value FROM pos_settings').fetchall())
    except sqlite3.OperationalError:
        pass

    imported = 0
    for key, value in rows:
        s = SETTINGS.get(key)
        if s is not None and s.encode is not None:
            # 2.x already stored these encoded (webhook URLs obscured,
            # SMTP password encrypted) in the same form this store keeps;
            # validating the encoded text as plaintext would reject it.
            value = '' if value is None else str(value)
        elif s is not None:
            try:
                value = s.coerce(value)
            except SettingsError as e:
                logger.warning(f"[Settings] Not importing {key}={value!r}: {e}")
                continue
        cur = conn.execute('INSERT OR IGNORE INTO settings (key, value, updated_by) VALUES (?, ?, ?)',
                           (key, json.dumps(value, default=str), 'migration'))
        imported += cur.rowcount
    logger.info(f"[Settings] Imported {imported} legacy settings into the database")
    return imported
