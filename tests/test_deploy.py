"""
Deployment model: /healthz, `python -m app.migrate`, and the data-dir
updater (request file -> rscp-update.path -> status file).
"""
import json
import os
import shutil
import sqlite3
import subprocess
import time

import pytest

from tests.conftest import login

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------

def test_healthz_is_public_and_reports_version(fresh_client):
    resp = fresh_client.get('/healthz')
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['status'] == 'ok'
    with open(os.path.join(ROOT, 'VERSION')) as f:
        assert data['version'] == f.read().strip()


def test_healthz_returns_503_when_db_unreadable(fresh_app, monkeypatch):
    import app.services.db as db

    def boom(*a, **k):
        raise sqlite3.OperationalError('disk I/O error')
    monkeypatch.setattr(db, 'get_db_connection', boom)
    resp = fresh_app.test_client().get('/healthz')
    assert resp.status_code == 503
    assert resp.get_json()['status'] == 'error'


# ---------------------------------------------------------------------------
# python -m app.migrate
# ---------------------------------------------------------------------------

def test_migrate_creates_and_upgrades_schema(tmp_path, capsys):
    from app import migrate
    from app.services.migration import LATEST_SCHEMA_VERSION

    db = str(tmp_path / 'rscp.db')
    assert migrate.main(['--db', db, '--check']) == 1      # nothing there yet
    assert migrate.main(['--db', db]) == 0
    conn = sqlite3.connect(db)
    assert conn.execute('PRAGMA user_version').fetchone()[0] == LATEST_SCHEMA_VERSION
    conn.close()
    assert migrate.main(['--db', db, '--check']) == 0
    assert migrate.main(['--db', db]) == 0                 # idempotent
    assert 'up to date' in capsys.readouterr().out


def test_migrate_fails_cleanly_without_data_dir(tmp_path):
    from app import migrate
    assert migrate.main(['--db', str(tmp_path / 'missing' / 'rscp.db')]) == 2


def test_migrate_reports_failure(tmp_path, monkeypatch):
    from app import migrate
    import app.services.migration as migration

    def broken(db_path=None):
        raise RuntimeError('bad migration')
    monkeypatch.setattr(migration, 'run_migrations', broken)
    assert migrate.main(['--db', str(tmp_path / 'rscp.db')]) == 1


# ---------------------------------------------------------------------------
# Data-dir updater
# ---------------------------------------------------------------------------

@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    from app import paths
    d = tmp_path / 'data'
    d.mkdir()
    monkeypatch.setattr(paths, 'USES_DATA_DIR', True)
    monkeypatch.setattr(paths, 'DATA_DIR', str(d))
    return d


@pytest.fixture
def admin(fresh_client):
    return login(fresh_client, 1)


def _request(data_dir):
    with open(data_dir / 'update-request.json') as f:
        return json.load(f)


def test_update_writes_request_and_queued_status(admin, data_dir):
    resp = admin.post('/admin/update', data={'branch': 'beta'})
    assert resp.status_code == 200, resp.get_json()
    req = _request(data_dir)
    assert req['action'] == 'upgrade'
    assert req['channel'] == 'beta'
    assert req['requested_by'] == 'admin'
    status = admin.get('/admin/update_status').get_json()
    assert status['status'] == 'updating'
    assert status['request_id'] == req['id']
    # No temp files left behind
    assert sorted(os.listdir(data_dir)) == ['update-request.json', 'update-status.json']


def test_update_refused_while_running(admin, data_dir):
    assert admin.post('/admin/update', data={'branch': 'stable'}).status_code == 200
    resp = admin.post('/admin/update', data={'branch': 'stable'})
    assert resp.status_code == 400
    assert 'in progress' in resp.get_json()['error']


def test_update_rejects_unknown_channel(admin, data_dir):
    resp = admin.post('/admin/update', data={'branch': 'nightly; rm -rf /'})
    assert resp.status_code == 400
    assert not (data_dir / 'update-request.json').exists()


def test_update_requires_admin(fresh_client, data_dir):
    login(fresh_client, 2)  # cashier
    assert fresh_client.post('/admin/update', data={'branch': 'stable'}).status_code == 403
    assert not (data_dir / 'update-request.json').exists()


def test_status_reads_updater_file(admin, data_dir):
    (data_dir / 'update-status.json').write_text(json.dumps(
        {'status': 'success', 'progress': 'Now running v3.0.1.', 'version': '3.0.1',
         'updated_at': int(time.time())}))
    data = admin.get('/admin/update_status').get_json()
    assert data['status'] == 'success'
    assert data['version'] == '3.0.1'
    # A finished run does not block the next update
    assert admin.post('/admin/update', data={'branch': 'stable'}).status_code == 200


def test_status_idle_without_file(admin, data_dir):
    assert admin.get('/admin/update_status').get_json()['status'] == 'idle'


def test_status_flags_request_never_picked_up(admin, data_dir):
    admin.post('/admin/update', data={'branch': 'stable'})
    old = time.time() - 600
    os.utime(data_dir / 'update-request.json', (old, old))
    data = admin.get('/admin/update_status').get_json()
    assert data['status'] == 'error'
    assert 'rscp-update.path' in data['error']
    # ...and the admin can retry
    assert admin.post('/admin/update', data={'branch': 'stable'}).status_code == 200


def test_status_flags_stale_run(admin, data_dir):
    (data_dir / 'update-status.json').write_text(json.dumps(
        {'status': 'updating', 'progress': 'Installing dependencies...',
         'updated_at': int(time.time()) - 3 * 3600}))
    data = admin.get('/admin/update_status').get_json()
    assert data['status'] == 'error'
    assert 'journalctl' in data['error']


def test_status_ignores_corrupt_file(admin, data_dir):
    (data_dir / 'update-status.json').write_text('{not json')
    assert admin.get('/admin/update_status').get_json()['status'] == 'idle'


@pytest.fixture
def releases(tmp_path, monkeypatch):
    """/opt/rscp-like tree with an active 3.0.1 and an older 3.0.0."""
    from app import paths
    root = tmp_path / 'opt' / 'releases'
    for name, version, age in (('3.0.0', '3.0.0', 200), ('2.9.9', '2.9.9', 400), ('3.0.1', '3.0.1', 0)):
        d = root / name
        d.mkdir(parents=True)
        (d / 'VERSION').write_text(version)
        t = time.time() - age
        os.utime(d, (t, t))
    (root / '.build-abc').mkdir()  # in-progress build: never listed
    monkeypatch.setattr(paths, 'APP_DIR', str(root / '3.0.1'))
    return root


def test_release_info_lists_rollback_target(admin, data_dir, releases):
    data = admin.get('/admin/release_info').get_json()
    assert data['layout'] == 'release'
    assert data['active'] == '3.0.1'
    names = [r['name'] for r in data['releases']]
    assert names == ['3.0.1', '3.0.0', '2.9.9']
    # The admin page offers the first non-active entry as the rollback target
    first_inactive = next(r for r in data['releases'] if not r['active'])
    assert first_inactive['name'] == '3.0.0' and first_inactive['previous']


def test_release_info_honours_previous_link(admin, data_dir, releases):
    try:
        os.symlink(str(releases / '2.9.9'), str(releases.parent / 'previous'))
    except (OSError, NotImplementedError):
        pytest.skip('symlinks not available')
    data = admin.get('/admin/release_info').get_json()
    first_inactive = next(r for r in data['releases'] if not r['active'])
    assert first_inactive['name'] == '2.9.9'


def test_rollback_writes_request(admin, data_dir, releases):
    resp = admin.post('/admin/rollback')
    assert resp.status_code == 200, resp.get_json()
    assert '3.0.0' in resp.get_json()['message']
    assert _request(data_dir)['action'] == 'rollback'


def test_rollback_without_previous_release(admin, data_dir, tmp_path, monkeypatch):
    from app import paths
    only = tmp_path / 'solo' / 'releases' / '3.0.1'
    only.mkdir(parents=True)
    monkeypatch.setattr(paths, 'APP_DIR', str(only))
    resp = admin.post('/admin/rollback')
    assert resp.status_code == 400
    assert not (data_dir / 'update-request.json').exists()


def test_check_update_uses_release_tags(admin, data_dir, monkeypatch):
    import app.routes.admin.updates as updates

    class Resp:
        def __init__(self, code, payload=None, text=''):
            self.status_code, self._payload, self.text = code, payload, text

        def json(self):
            return self._payload

    def fake_get(url, **kwargs):
        if url.endswith('/releases/latest'):
            return Resp(404, {})
        if url.endswith('/tags'):
            return Resp(200, [{'name': 'v3.0.2'}, {'name': 'v3.10.0'}, {'name': 'beta-x'}])
        if url.endswith('/beta/VERSION'):
            return Resp(200, text='3.11.0\n')
        return Resp(404)

    monkeypatch.setattr(updates.requests, 'get', fake_get)
    data = admin.get('/admin/check_update').get_json()
    assert data['branches']['stable']['version'] == '3.10.0'
    assert data['branches']['stable']['update_available'] is True
    assert data['branches']['beta']['version'] == '3.11.0'


def test_legacy_status_path_unchanged(admin, tmp_path, monkeypatch):
    """Without a data dir the old in-process updater's status file is used."""
    from app import paths
    import app.routes.admin.updates as updates
    legacy = tmp_path / 'update_status.json'
    legacy.write_text(json.dumps({'status': 'restarting', 'progress': 'x'}))
    monkeypatch.setattr(paths, 'USES_DATA_DIR', False)
    monkeypatch.setattr(updates, 'STATUS_FILE', str(legacy))
    assert admin.get('/admin/update_status').get_json()['status'] == 'restarting'


# ---------------------------------------------------------------------------
# Deployment files
# ---------------------------------------------------------------------------

def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


def test_unit_files_match_layout():
    unit = _read('deploy/rscp.service')
    for line in ('User=rscp', 'StateDirectory=rscp', 'Environment=RSCP_DATA_DIR=/var/lib/rscp',
                 'WorkingDirectory=/opt/rscp/current',
                 'ExecStartPre=/opt/rscp/current/venv/bin/python -m app.migrate',
                 'ExecStart=/opt/rscp/current/venv/bin/gunicorn -c /opt/rscp/current/gunicorn.conf.py wsgi:app',
                 'Restart=always', 'ProtectSystem=strict', 'ReadWritePaths=/var/lib/rscp',
                 'ProtectHome=true', 'PrivateTmp=true', 'NoNewPrivileges=true'):
        assert line in unit.splitlines(), line
    path_unit = _read('deploy/rscp-update.path')
    assert 'PathChanged=/var/lib/rscp/update-request.json' in path_unit
    updater = _read('deploy/rscp-update.service')
    assert 'ExecStart=/opt/rscp/bin/rscp-update --upgrade --noninteractive --from-request /var/lib/rscp/update-request.json' in updater
    assert 'Type=oneshot' in updater


def test_app_uses_same_request_and_status_names_as_installer():
    import app.routes.admin.updates as updates
    script = _read('install.sh')
    assert updates.UPDATE_STATUS_NAME in script
    assert updates.UPDATE_REQUEST_NAME in _read('deploy/rscp-update.path')


def test_install_script_is_wrapped_and_strict():
    script = _read('install.sh')
    lines = [l for l in script.splitlines() if l.strip()]
    assert lines[-1] == 'main "$@"'
    assert 'set -euo pipefail' in script
    assert 'apt-get upgrade' not in script
    assert '\r' not in script


def _working_bash():
    bash = shutil.which('bash')
    if not bash:
        return None
    try:  # on Windows `bash` may be a WSL stub with no distro behind it
        ok = subprocess.run([bash, '-c', 'true'], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    return bash if ok else None


def test_install_script_parses():
    bash = _working_bash()
    if not bash:
        pytest.skip('no working bash')
    result = subprocess.run([bash, '-n', os.path.join(ROOT, 'install.sh')],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_gunicorn_bind_from_env(monkeypatch):
    import runpy
    if not hasattr(os, 'sched_getaffinity'):
        monkeypatch.setattr(os, 'sched_getaffinity', lambda pid: {0, 1}, raising=False)
    monkeypatch.setenv('RSCP_BIND', '127.0.0.1:5055')
    conf = runpy.run_path(os.path.join(ROOT, 'gunicorn.conf.py'))
    assert conf['bind'] == '127.0.0.1:5055'
    assert conf['accesslog'] == '-' and conf['errorlog'] == '-'
    monkeypatch.delenv('RSCP_BIND')
    assert runpy.run_path(os.path.join(ROOT, 'gunicorn.conf.py'))['bind'] == '0.0.0.0:5000'
