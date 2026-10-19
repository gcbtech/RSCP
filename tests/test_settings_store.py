"""The typed settings store (app/services/settings.py), its compatibility shims,
migration 6 and the generic POST /admin/settings/save route."""
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from tests.conftest import login

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


@pytest.fixture
def st():
    from app.services import settings
    return settings


def _save(client, group, **fields):
    return client.post('/admin/settings/save', data=dict(group=group, **fields))


# --- typed API ---------------------------------------------------------------

def test_typed_round_trips(fresh_app, st):
    with fresh_app.app_context():
        assert st.get('AUTO_TRIM') is False                    # registry default
        assert st.get('TRUSTED_PROXIES') == ['127.0.0.1', '::1']
        st.set('AUTO_TRIM', 'on')
        st.set('SESSION_TIMEOUT_MINUTES', '45')
        st.set('TAX_RATE', '0.0825')
        st.set('CF_ACCESS_ALLOWED_DOMAINS', 'Example.com, @shop.org')
        st.set('LOCATION_OPTIONS', {'area': ['A', 'B']})
        st.set_many({'RECEIPT_FOOTER': 'Thanks!', 'POS_EMAIL_PORT': 465.0})
    with fresh_app.app_context():
        assert st.get('AUTO_TRIM') is True
        assert st.get('SESSION_TIMEOUT_MINUTES') == 45
        assert st.get('TAX_RATE') == pytest.approx(0.0825)
        assert st.get('CF_ACCESS_ALLOWED_DOMAINS') == ['@example.com', '@shop.org']
        assert st.get('LOCATION_OPTIONS') == {'area': ['A', 'B']}
        assert st.get('RECEIPT_FOOTER') == 'Thanks!'
        assert st.get('POS_EMAIL_PORT') == 465
        everything = st.all()
        assert set(st.SETTINGS) <= set(everything)
        conn = sqlite3.connect(fresh_app.config['DATABASE'])
        raw, who = conn.execute("SELECT value, updated_by FROM settings WHERE key='AUTO_TRIM'").fetchone()
        conn.close()
        assert json.loads(raw) is True


def test_validation_errors(fresh_app, st):
    with fresh_app.app_context():
        for key, value in (('TAX_RATE', 1.5), ('SESSION_TIMEOUT_MINUTES', 'soon'),
                           ('DATE_FORMAT', 'ISO'), ('TRUSTED_PROXIES', '10.0.0.1, not-an-ip'),
                           ('POS_AUTO_EMAIL_TIME', '25:00'), ('NOT_A_SETTING', 1),
                           ('AUTO_TRIM', 'maybe')):
            with pytest.raises(st.SettingsError):
                st.set(key, value)
        with pytest.raises(st.SettingsError, match='AUD tag'):
            st.set('CF_ACCESS_ENABLED', True)
        # nothing invalid was stored
        assert st.get('TAX_RATE') == 0.0
        assert st.get('CF_ACCESS_ENABLED') is False


def test_pos_setting_shims_keep_string_conventions(fresh_app, st):
    from app.routes.pos.core import get_pos_setting, set_pos_setting, get_tax_rate
    with fresh_app.app_context():
        assert get_pos_setting('ALLOW_HOLD_ORDERS') == 'true'    # baseline default, migrated
        assert get_pos_setting('CASH_DRAWER_PORT', 'fallback') == 'fallback'
        assert set_pos_setting('CASH_DISCOUNT_ENABLED', 'true')
        assert set_pos_setting('TAX_RATE', '0.07')
        assert set_pos_setting('POS_DEFAULT_REPORT_TIMEFRAME', '7')
    with fresh_app.app_context():
        assert st.get('CASH_DISCOUNT_ENABLED') is True
        assert get_pos_setting('CASH_DISCOUNT_ENABLED') == 'true'
        assert get_pos_setting('POS_DEFAULT_REPORT_TIMEFRAME') == '7'
        assert get_tax_rate() == pytest.approx(0.07)


def test_load_and_save_config_compat(fresh_app, st):
    from app.services.data_manager import load_config, save_config, CONFIG_FILE
    with open(CONFIG_FILE) as f:
        before = json.load(f)
    with fresh_app.app_context():
        assert save_config({'ORG_NAME': 'Corner Shop', 'LOW_STOCK_THRESHOLD': 3})
        conf = load_config()
        assert conf['ORG_NAME'] == 'Corner Shop'
        assert conf['LOW_STOCK_THRESHOLD'] == 3
        assert conf['INVENTORY_ENABLED'] is True              # imported from config.json
        assert conf['SECRET_KEY'] == 'test_key'               # bootstrap stays in config.json
        assert st.get('ORG_NAME') == 'Corner Shop'
    with open(CONFIG_FILE) as f:
        assert json.load(f) == before                         # config.json untouched


# --- cross-worker coherence --------------------------------------------------

def test_change_in_one_app_is_seen_by_another_on_same_db(fresh_app, st):
    from app import create_app
    other = create_app(test_config={'TESTING': True, 'DATABASE': fresh_app.config['DATABASE'],
                                    'WTF_CSRF_ENABLED': False, 'SECRET_KEY': 'test_key'})
    path = fresh_app.config['DATABASE']

    with other.test_request_context('/'):
        assert st.get('AUTO_TRIM') is False                  # "worker B" caches the old value
    stale = dict(st._CACHE[path])

    client = fresh_app.test_client()                         # "worker A" saves a change
    login(client, 1)
    _save(client, 'general', AUTO_TRIM='true')

    # Worker B has its own memory: put its stale cache back, as if A ran elsewhere.
    st._CACHE[path] = stale
    with other.test_request_context('/'):
        assert st.get('AUTO_TRIM') is True                   # next request sees it
        from app.services.data_manager import load_config
        assert load_config()['AUTO_TRIM'] is True


def test_change_from_another_process_is_seen(fresh_app, st, tmp_path):
    path = fresh_app.config['DATABASE']
    with fresh_app.test_request_context('/'):
        assert st.get('ORG_NAME') == 'Test Store'            # cached in this process
    script = (
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "from app.services import db\n"
        "db.DB_PATH = sys.argv[2]\n"
        "from app.services import settings\n"
        "settings.set('ORG_NAME', 'Changed Elsewhere', user='worker2')\n"
    )
    env = dict(os.environ, RSCP_DATA_DIR=str(tmp_path))
    subprocess.run([sys.executable, '-c', script, ROOT, path], check=True, env=env, timeout=60)
    with fresh_app.test_request_context('/'):
        assert st.get('ORG_NAME') == 'Changed Elsewhere'


# --- migration 6 -------------------------------------------------------------

def test_migration_6_imports_config_json_and_pos_settings(tmp_path, monkeypatch, st):
    from app.services import migration
    assert any(v == 6 for v, _, _ in migration.SCHEMA_MIGRATIONS)

    cfg = tmp_path / 'config.json'
    legacy = {'SECRET_KEY': 'keep-me-in-file', 'ORG_NAME': 'Legacy Shop', 'AUTO_TRIM': True,
              'SESSION_TIMEOUT_MINUTES': '45', 'TRUSTED_PROXIES': ['10.0.0.5'],
              'DATE_FORMAT': 'nonsense', 'SOME_OLD_KEY': {'x': 1}}
    cfg.write_text(json.dumps(legacy))
    monkeypatch.setattr('app.paths.CONFIG_FILE', str(cfg))

    db = str(tmp_path / 'legacy.db')
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute('CREATE TABLE pos_settings (key TEXT PRIMARY KEY, value TEXT)')
    conn.executemany('INSERT INTO pos_settings VALUES (?, ?)', [
        ('TAX_RATE', '0.0825'), ('CASH_DISCOUNT_ENABLED', 'true'), ('ALLOW_HOLD_ORDERS', 'false'),
        ('POS_DEFAULT_REPORT_TIMEFRAME', '90'), ('REQUIRE_MANAGER_VOID', 'maybe'),
        ('CASH_DISCOUNT_AMOUNT', '3')])
    for _ in range(2):  # idempotent
        conn.execute('BEGIN IMMEDIATE')
        migration._m6_settings_store(conn)
        conn.execute('COMMIT')
    rows = {k: json.loads(v) for k, v in conn.execute('SELECT key, value FROM settings')}
    pos_rows = conn.execute('SELECT COUNT(*) FROM pos_settings').fetchone()[0]
    conn.close()

    assert 'SECRET_KEY' not in rows
    assert rows['ORG_NAME'] == 'Legacy Shop'
    assert rows['AUTO_TRIM'] is True
    assert rows['SESSION_TIMEOUT_MINUTES'] == 45              # typed via the registry
    assert rows['TRUSTED_PROXIES'] == ['10.0.0.5']
    assert rows['SOME_OLD_KEY'] == {'x': 1}                   # unknown keys kept as-is
    assert 'DATE_FORMAT' not in rows                          # invalid: default applies
    assert rows['TAX_RATE'] == pytest.approx(0.0825)
    assert rows['CASH_DISCOUNT_ENABLED'] is True
    assert rows['ALLOW_HOLD_ORDERS'] is False
    assert rows['POS_DEFAULT_REPORT_TIMEFRAME'] == 90
    assert 'REQUIRE_MANAGER_VOID' not in rows                 # invalid: default applies
    # no longer a registered key (schema v15 converts it to CASH_DISCOUNT_PERCENT/_CENTS): kept as-is
    assert rows['CASH_DISCOUNT_AMOUNT'] == '3'
    assert pos_rows == 6                                      # legacy stores left alone
    assert json.loads(cfg.read_text()) == legacy


def test_fresh_install_imports_conftest_config(fresh_app, st):
    with fresh_app.app_context():
        assert st.get('INVENTORY_ENABLED') is True
        assert st.get('POS_ENABLED') is True
        assert st.get('ORG_NAME') == 'Test Store'


# --- generic route: permissions, rules, secrets -------------------------------

def test_cashier_cannot_change_pos_tax_but_manager_can(fresh_app, fresh_client, st):
    login(fresh_client, 2)
    r = _save(fresh_client, 'pos', TAX_RATE='9')
    assert r.status_code == 403
    with fresh_app.app_context():
        assert st.get('TAX_RATE') == 0.0
    login(fresh_client, 3)
    r = _save(fresh_client, 'pos', TAX_RATE='8.25', next='/pos/management/settings')
    assert r.status_code == 302 and r.headers['Location'].endswith('/pos/management/settings')
    with fresh_app.app_context():
        assert st.get('TAX_RATE') == pytest.approx(0.0825)   # percent in, fraction stored


def test_non_admin_cannot_change_admin_settings(fresh_app, fresh_client, st):
    login(fresh_client, 3)  # POS manager, not an admin
    for group, fields in (('modules', {'POS_ENABLED': 'false'}),
                          ('security', {'SESSION_TIMEOUT_ENABLED': 'true'}),
                          ('pos_approval', {'POS_MAX_DISCOUNT_PERCENT': '90'})):
        assert _save(fresh_client, group, **fields).status_code == 403
    # an admin key smuggled into a POS form is ignored
    _save(fresh_client, 'pos', POS_ENABLED='false', RECEIPT_FOOTER='Bye')
    with fresh_app.app_context():
        assert st.get('POS_ENABLED') is True
        assert st.get('SESSION_TIMEOUT_ENABLED') is False
        assert st.get('POS_MAX_DISCOUNT_PERCENT') == 10.0
        assert st.get('RECEIPT_FOOTER') == 'Bye'
    login(fresh_client, 1)  # admin
    _save(fresh_client, 'security', SESSION_TIMEOUT_ENABLED='true', SESSION_TIMEOUT_MINUTES='60')
    with fresh_app.app_context():
        assert st.get('SESSION_TIMEOUT_ENABLED') is True
        assert st.get('SESSION_TIMEOUT_MINUTES') == 60


def test_checkbox_fallback_and_partial_forms(fresh_app, fresh_client, st):
    login(fresh_client, 1)
    fresh_client.post('/admin/settings/save', data={'group': 'notifications',
                                                    'NOTIFY_OOS': ['false', 'true'],
                                                    'NOTIFY_LOW_STOCK': ['false', 'true']})
    fresh_client.post('/admin/settings/save', data={'group': 'notifications', 'NOTIFY_OOS': ['false']})
    with fresh_app.app_context():
        assert st.get('NOTIFY_OOS') is False
        assert st.get('NOTIFY_LOW_STOCK') is True            # absent from 2nd form: untouched


def test_invalid_submission_saves_nothing(fresh_app, fresh_client, st):
    login(fresh_client, 1)
    r = _save(fresh_client, 'network', TRUSTED_PROXIES='127.0.0.1, bogus')
    assert r.status_code == 302
    _save(fresh_client, 'cf_access', CF_ACCESS_ENABLED='true', CF_ACCESS_AUD='',
          CF_ACCESS_TEAM_DOMAIN='gcbcomputers')
    with fresh_app.app_context():
        assert st.get('TRUSTED_PROXIES') == ['127.0.0.1', '::1']
        assert st.get('CF_ACCESS_ENABLED') is False
        assert st.get('CF_ACCESS_TEAM_DOMAIN') == ''
    _save(fresh_client, 'cf_access', CF_ACCESS_ENABLED='true', CF_ACCESS_AUD='a' * 64,
          CF_ACCESS_TEAM_DOMAIN='gcbcomputers')
    with fresh_app.app_context():
        assert st.get('CF_ACCESS_ENABLED') is True
        assert st.get('CF_ACCESS_TEAM_DOMAIN') == 'gcbcomputers.cloudflareaccess.com'
    assert _save(fresh_client, 'no_such_group').status_code == 302


def test_secrets_are_write_only_and_never_rendered(fresh_app, fresh_client, st):
    login(fresh_client, 1)
    _save(fresh_client, 'api', PUBLIC_API_KEY='api-secret-value-123')
    _save(fresh_client, 'pos', POS_EMAIL_PASSWORD='smtp-secret-value-789')
    # blank / placeholder keeps the stored secret
    _save(fresh_client, 'api', PUBLIC_API_KEY='')
    with fresh_app.app_context():
        assert st.get('PUBLIC_API_KEY') == 'api-secret-value-123'
        stored_pw = st.get('POS_EMAIL_PASSWORD')
        assert stored_pw and stored_pw != 'smtp-secret-value-789'   # encrypted at rest
        from app.services.security import decrypt
        assert decrypt(stored_pw) == 'smtp-secret-value-789'

    for url in ('/admin/?tab=settings', '/pos/management/settings'):
        r = fresh_client.get(url)
        assert r.status_code == 200, url
        body = r.get_data(as_text=True)
        for secret in ('api-secret-value-123', 'smtp-secret-value-789', stored_pw):
            assert secret not in body, url
    body = fresh_client.get('/admin/?tab=settings').get_data(as_text=True)
    assert '(saved - leave blank to keep)' in body                    # PUBLIC_API_KEY_SET

    with fresh_app.app_context():
        from app import _template_config
        from app.services.data_manager import load_config
        view = _template_config(load_config())
    assert view['PUBLIC_API_KEY_SET'] is True and 'PUBLIC_API_KEY' not in view


def test_json_save_for_auto_confirm(fresh_app, fresh_client, st):
    login(fresh_client, 1)
    r = fresh_client.post('/admin/settings/save',
                          json={'group': 'general', 'values': {'AUTO_CONFIRM_IMPORT': True}})
    assert r.status_code == 200 and r.get_json()['success'] is True
    with fresh_app.app_context():
        assert st.get('AUTO_CONFIRM_IMPORT') is True
    login(fresh_client, 2)
    r = fresh_client.post('/admin/settings/save',
                          json={'group': 'general', 'values': {'AUTO_CONFIRM_IMPORT': False}})
    assert r.status_code == 403
    r = fresh_client.post('/admin/settings/save', json={'group': 'pos', 'values': {'TAX_RATE': 'x'}})
    assert r.status_code == 403


def test_location_labels_and_options_from_form(fresh_app, fresh_client, st):
    login(fresh_client, 1)
    fresh_client.post('/admin/settings/save', data={
        'group': 'inventory', 'LOCATION_LABELS.area': 'Room', 'LOCATION_LABELS.aisle': '',
        'LOCATION_LABELS.shelf': 'Rack', 'LOCATION_LABELS.bin': 'Box',
        'LOCATION_OPTIONS.area': 'Front, Back', 'LOCATION_OPTIONS.aisle': '',
        'LOCATION_OPTIONS.shelf': '1,2', 'LOCATION_OPTIONS.bin': '',
        'LOCATION_ALLOW_CUSTOM': ['false']})
    with fresh_app.app_context():
        assert st.get('LOCATION_LABELS') == {'area': 'Room', 'aisle': 'Aisle', 'shelf': 'Rack', 'bin': 'Box'}
        assert st.get('LOCATION_OPTIONS') == {'area': ['Front', 'Back'], 'aisle': [], 'shelf': ['1', '2'], 'bin': []}
        assert st.get('LOCATION_ALLOW_CUSTOM') is False


def test_restoring_pre_v6_backup_applies_its_config_json(fresh_app, fresh_client, st, tmp_path, monkeypatch):
    import io
    import zipfile
    # A "2.x" backup: a database at schema v5 (no settings tables yet).
    from app.services import migration
    old_db = str(tmp_path / 'old.db')
    migration.run_migrations(old_db, target=5)
    conn = sqlite3.connect(old_db)
    conn.execute("INSERT INTO users (id, username, password_hash, is_admin, roles) "
                 "VALUES (1, 'admin', 'x', 1, '[\"super_admin\"]')")
    conn.commit()
    conn.close()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.write(old_db, 'rscp.db')
        zf.writestr('config.json', json.dumps({'SECRET_KEY': 'test_key', 'ORG_NAME': 'Restored Shop',
                                               'SESSION_TIMEOUT_MINUTES': 15}))
    buf.seek(0)
    monkeypatch.setattr('app.routes.admin.backup.DB_PATH', fresh_app.config['DATABASE'])
    monkeypatch.setattr('app.routes.admin.backup.CONFIG_FILE', str(tmp_path / 'config.json'))

    login(fresh_client, 1)
    fresh_client.post('/admin/import_backup', data={'backup_file': (buf, 'backup.zip')},
                      content_type='multipart/form-data')
    with fresh_app.app_context():
        assert st.get('ORG_NAME') == 'Restored Shop'
        assert st.get('SESSION_TIMEOUT_MINUTES') == 15


# --- migration 12: email ingest removed ----------------------------------------

EMAIL_INGEST_KEYS = ('EMAIL_INGEST_ENABLED', 'IMAP_SERVER', 'EMAIL_USER', 'EMAIL_PASS', 'EMAIL_PASSWORD')


def _db_copy_at(fresh_app, path, version):
    """A database whose schema really is at `version` (migrations run only that far)."""
    from app.services import migration
    migration.run_migrations(path, target=version)
    return sqlite3.connect(path)


def test_migration_12_deletes_email_ingest_settings_only(fresh_app, tmp_path):
    from app.services import migration
    db = str(tmp_path / 'v11.db')
    conn = _db_copy_at(fresh_app, db, 11)
    conn.executemany("INSERT OR REPLACE INTO settings (key, value, updated_by) VALUES (?, ?, 'test')",
                     [('EMAIL_INGEST_ENABLED', 'true'), ('IMAP_SERVER', '"imap.example.com"'),
                      ('EMAIL_USER', '"burner@example.com"'), ('EMAIL_PASS', '"imap-pw"'),
                      ('EMAIL_PASSWORD', '"old-imap-pw"'), ('ORG_NAME', '"Kept Shop"'),
                      ('POS_EMAIL_USER', '"eod@example.com"')])
    conn.execute("INSERT INTO packages (tracking_number, item_name, status, source) "
                 "VALUES ('ORDER-113-1234567-1234567-01', 'From email', 'on_time', 'email')")
    conn.commit()
    token_before = conn.execute('SELECT token FROM settings_meta').fetchone()[0]
    conn.close()

    migration.run_migrations(db)

    conn = sqlite3.connect(db)
    assert conn.execute('PRAGMA user_version').fetchone()[0] == migration.LATEST_SCHEMA_VERSION >= 12
    keys = {r[0] for r in conn.execute('SELECT key FROM settings')}
    assert not keys & set(EMAIL_INGEST_KEYS)
    assert {'ORG_NAME', 'POS_EMAIL_USER'} <= keys
    assert conn.execute("SELECT value FROM settings WHERE key = 'ORG_NAME'").fetchone()[0] == '"Kept Shop"'
    assert conn.execute('SELECT token FROM settings_meta').fetchone()[0] != token_before   # caches reload
    # packages the email ingest created are real data and stay
    assert conn.execute("SELECT count(*) FROM packages WHERE source = 'email'").fetchone()[0] == 1
    conn.close()


def test_upgrade_from_2x_does_not_keep_email_ingest_settings(fresh_app, tmp_path, monkeypatch):
    """Migration 6 imports config.json wholesale; migration 12 then drops the ingest keys."""
    from app import paths
    from app.services import migration
    db = str(tmp_path / 'v5.db')
    conn = _db_copy_at(fresh_app, db, 5)   # no settings tables at v5
    conn.close()
    cfg = tmp_path / 'config.json'
    cfg.write_text(json.dumps({'SECRET_KEY': 'k', 'ORG_NAME': 'Old Shop', 'EMAIL_INGEST_ENABLED': True,
                               'IMAP_SERVER': 'imap.example.com', 'EMAIL_USER': 'u@example.com',
                               'EMAIL_PASS': 'imap-pw'}))
    monkeypatch.setattr(paths, 'CONFIG_FILE', str(cfg))

    migration.run_migrations(db)

    conn = sqlite3.connect(db)
    keys = {r[0] for r in conn.execute('SELECT key FROM settings')}
    conn.close()
    assert 'ORG_NAME' in keys
    assert not keys & set(EMAIL_INGEST_KEYS)


# --- migration 22: RSCP's own Google/OIDC sign-in removed -----------------------

SSO_ROWS = [('SSO_ENABLED', 'true'), ('SSO_CLIENT_ID', '"client-123.apps.googleusercontent.com"'),
            ('SSO_CLIENT_SECRET', '"oidc-client-secret"'),
            ('SSO_DISCOVERY_URL', '"https://accounts.google.com/.well-known/openid-configuration"'),
            ('SSO_ALLOWED_DOMAINS', '["@gcbcomputers.com", "Shop.org"]'), ('SSO_ALLOW_NEW_USERS', 'false')]


def _v21_with(fresh_app, tmp_path, rows):
    db = str(tmp_path / 'v21.db')
    conn = _db_copy_at(fresh_app, db, 21)   # the real schema just before migration 22
    conn.executemany("INSERT OR REPLACE INTO settings (key, value, updated_by) VALUES (?, ?, 'test')", rows)
    conn.executemany("INSERT INTO users (id, username, password_hash, is_admin, roles) VALUES (?, ?, 'x', ?, ?)",
                     [(1, 'admin', 1, '["super_admin"]'), (2, 'cashier', 0, '["operator"]'),
                      (3, 'manager', 0, '["pos_admin"]'), (7, 'boss2', 1, '["super_admin"]')])
    conn.commit()
    conn.close()
    return db


def _token(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute('SELECT token FROM settings_meta').fetchone()[0]
    finally:
        conn.close()


def _notices(conn):
    from app.services.migration import SSO_RETIRED_TITLE
    return sorted(r[0] for r in conn.execute('SELECT user_id FROM notifications WHERE title = ?',
                                             (SSO_RETIRED_TITLE,)))


def test_sso_settings_are_gone_from_the_registry(fresh_app, st):
    assert not [k for k in st.SETTINGS if k.startswith('SSO_')]
    assert 'sso' not in st.GROUPS
    assert not [k for k in st.ENV_OVERRIDES if k.startswith('SSO_')]
    with fresh_app.app_context():
        with pytest.raises(st.SettingsError):
            st.set('SSO_ENABLED', True)


def test_migration_22_retires_configured_sso(fresh_app, tmp_path):
    from app.services import migration
    db = _v21_with(fresh_app, tmp_path, SSO_ROWS + [('ORG_NAME', '"Kept Shop"')])
    token_before = _token(db)

    migration.run_migrations(db)

    conn = sqlite3.connect(db)
    assert conn.execute('PRAGMA user_version').fetchone()[0] == migration.LATEST_SCHEMA_VERSION >= 22
    keys = {r[0] for r in conn.execute('SELECT key FROM settings')}
    assert not {k for k in keys if k.startswith('SSO_')}          # client secret included
    assert 'ORG_NAME' in keys
    # Cloudflare Access fell back to the SSO domains: that restriction stays.
    cf = conn.execute("SELECT value FROM settings WHERE key = 'CF_ACCESS_ALLOWED_DOMAINS'").fetchone()[0]
    assert json.loads(cf) == ['@gcbcomputers.com', '@shop.org']
    assert conn.execute('SELECT token FROM settings_meta').fetchone()[0] != token_before
    # One notice per administrator (admin + boss2), nobody else.
    assert _notices(conn) == [1, 7]
    row = conn.execute('SELECT message, type, link, is_read FROM notifications WHERE user_id = 1').fetchone()
    assert 'Cloudflare Access' in row[0] and 'sign-in email' in row[0]
    assert row[1] == 'warning' and row[2].startswith('/admin/') and row[3] == 0
    conn.close()

    migration.run_migrations(db)                                   # runs once
    conn = sqlite3.connect(db)
    assert _notices(conn) == [1, 7]
    conn.close()


def test_migration_22_keeps_an_existing_cf_domain_list(fresh_app, tmp_path):
    from app.services import migration
    db = _v21_with(fresh_app, tmp_path, SSO_ROWS + [('CF_ACCESS_ALLOWED_DOMAINS', '["@cf.example"]')])
    migration.run_migrations(db)
    conn = sqlite3.connect(db)
    cf = conn.execute("SELECT value FROM settings WHERE key = 'CF_ACCESS_ALLOWED_DOMAINS'").fetchone()[0]
    assert json.loads(cf) == ['@cf.example']
    conn.close()


def test_migration_22_unconfigured_sso_leaves_no_notice(fresh_app, tmp_path):
    from app.services import migration
    # Saved once with defaults (switched off, no client): nothing to tell anyone.
    db = _v21_with(fresh_app, tmp_path, [('SSO_ENABLED', 'false'), ('SSO_CLIENT_ID', '""'),
                                         ('SSO_ALLOWED_DOMAINS', '[]')])
    migration.run_migrations(db)
    conn = sqlite3.connect(db)
    assert not conn.execute("SELECT 1 FROM settings WHERE key LIKE 'SSO%'").fetchone()
    assert not conn.execute("SELECT 1 FROM settings WHERE key = 'CF_ACCESS_ALLOWED_DOMAINS'").fetchone()
    assert _notices(conn) == []
    conn.close()


def test_migration_22_without_sso_settings_is_a_no_op(fresh_app, tmp_path):
    from app.services import migration
    db = _v21_with(fresh_app, tmp_path, [])
    token_before = _token(db)
    migration.run_migrations(db)
    conn = sqlite3.connect(db)
    assert conn.execute('SELECT token FROM settings_meta').fetchone()[0] == token_before
    assert _notices(conn) == []
    conn.close()


def test_upgrade_from_2x_config_json_drops_sso(fresh_app, tmp_path, monkeypatch):
    """Migration 6 imports config.json wholesale; migration 22 then drops the SSO keys."""
    from app import paths
    from app.services import migration
    db = str(tmp_path / 'v5.db')
    conn = _db_copy_at(fresh_app, db, 5)   # no settings tables at v5
    # 2.x accounts: an is_admin admin (migration 9 gives it super_admin) and staff
    conn.executemany("INSERT INTO users (id, username, password_hash, is_admin, roles) VALUES (?, ?, 'x', ?, ?)",
                     [(1, 'admin', 1, '[]'), (2, 'cashier', 0, '["user"]')])
    conn.commit()
    conn.close()
    cfg = tmp_path / 'config.json'
    cfg.write_text(json.dumps({'SECRET_KEY': 'k', 'ORG_NAME': 'Old Shop', 'SSO_ENABLED': True,
                               'SSO_CLIENT_ID': 'cid', 'SSO_CLIENT_SECRET': 'csecret',
                               'SSO_ALLOWED_DOMAINS': ['@old.example']}))
    monkeypatch.setattr(paths, 'CONFIG_FILE', str(cfg))

    migration.run_migrations(db)

    conn = sqlite3.connect(db)
    keys = {r[0] for r in conn.execute('SELECT key FROM settings')}
    cf = conn.execute("SELECT value FROM settings WHERE key = 'CF_ACCESS_ALLOWED_DOMAINS'").fetchone()[0]
    assert 'ORG_NAME' in keys and not {k for k in keys if k.startswith('SSO_')}
    assert json.loads(cf) == ['@old.example']
    assert _notices(conn) == [1]
    conn.close()
