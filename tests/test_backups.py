"""
Automatic backups (app/backup.py): retention, the backup run itself, status,
off-box copies, scheduling, the admin card/"Run backup now", the stale-backup
banner and restoring a nightly backup.
"""
import datetime
import io
import json
import os
import sqlite3
import tarfile

import pytest

from tests.conftest import login

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DT = datetime.datetime


@pytest.fixture
def bk(tmp_path, monkeypatch):
    """app.backup with its nightly folder in a temp dir."""
    from app import backup
    nightly = tmp_path / 'nightly'
    monkeypatch.setattr(backup, 'nightly_dir', lambda: str(nightly))
    monkeypatch.setattr(backup, 'OFFSITE_REQUIRE_SEPARATE_DEVICE', False)
    backup._WARNING_CACHE.update(at=0.0, key=None, value=None)
    backup.nightly = str(nightly)
    return backup


@pytest.fixture
def sources(tmp_path, fresh_app):
    """(db_path, uploads_dir, config_file) for a backup run."""
    uploads = tmp_path / 'uploads'
    (uploads / 'inventory').mkdir(parents=True)
    (uploads / 'inventory' / 'a.jpg').write_bytes(b'\xff\xd8 fake jpeg')
    config = tmp_path / 'config.json'
    config.write_text(json.dumps({'SECRET_KEY': 'k'}))
    return fresh_app.config['DATABASE'], str(uploads), str(config)


SETTINGS = {'BACKUP_ENABLED': True, 'BACKUP_TIME': '02:30', 'BACKUP_KEEP_DAILY': 14,
            'BACKUP_KEEP_WEEKLY': 8, 'BACKUP_OFFSITE_DIR': ''}


def _run(bk, sources, now, **extra):
    db, uploads, config = sources
    settings = dict(SETTINGS, **extra.pop('settings', {}))
    return bk.run_backup(db_path=db, uploads_dir=uploads, config_file=config,
                         root=bk.nightly, settings=settings, now=now, **extra)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def test_retention_keeps_dailies_and_weeklies_across_weeks():
    from app.backup import select_keep
    end = DT(2026, 10, 17, 2, 30)          # a Saturday
    stamps = [end - datetime.timedelta(days=i) for i in range(70)]
    extra_same_day = DT(2026, 10, 17, 1, 0)  # older run on the newest day
    manual = DT(2026, 10, 10, 23, 0)          # newer run on an older day
    keep = select_keep(stamps + [extra_same_day, manual], 14, 8)

    days = {t.date() for t in keep}
    dailies = {(end - datetime.timedelta(days=i)).date() for i in range(14)}
    # newest of each of the 8 most recent ISO weeks: this Saturday, then Sundays
    weeklies = {datetime.date(2026, 10, 17), datetime.date(2026, 10, 11), datetime.date(2026, 10, 4),
                datetime.date(2026, 9, 27), datetime.date(2026, 9, 20), datetime.date(2026, 9, 13),
                datetime.date(2026, 9, 6), datetime.date(2026, 8, 30)}
    assert days == dailies | weeklies
    assert len(keep) == len(dailies | weeklies)       # one backup per kept day
    assert extra_same_day not in keep
    assert manual in keep and DT(2026, 10, 10, 2, 30) not in keep


def test_retention_counts_days_with_backups_and_always_keeps_newest():
    from app.backup import select_keep
    # gaps: only days that have a backup count toward the daily quota
    stamps = [DT(2026, 1, 1), DT(2026, 1, 5), DT(2026, 1, 20), DT(2026, 3, 1)]
    assert select_keep(stamps, 2, 0) == {DT(2026, 3, 1), DT(2026, 1, 20)}
    assert select_keep(stamps, 0, 0) == {DT(2026, 3, 1)}
    assert select_keep([], 14, 8) == set()
    # weekly only: newest per ISO week
    week = [DT(2026, 10, 12, 2), DT(2026, 10, 14, 2), DT(2026, 10, 18, 2), DT(2026, 10, 19, 2)]
    assert select_keep(week, 0, 2) == {DT(2026, 10, 19, 2), DT(2026, 10, 18, 2)}


def test_prune_only_touches_backup_files(tmp_path):
    from app.backup import prune
    for day in range(1, 6):
        stamp = f'202610{day:02d}-023000'
        (tmp_path / f'rscp-{stamp}.db').write_text('x')
        (tmp_path / f'uploads-{stamp}.tar.gz').write_text('x')
    for other in ('status.json', 'pre-update-v3.0.0-20261001-000000.db', 'notes.txt', 'rscp-bad.db'):
        (tmp_path / other).write_text('x')
    deleted = prune(str(tmp_path), 2, 0)
    assert sorted(deleted) == sorted(f'{kind}-202610{d:02d}-023000.{ext}'
                                     for d in (1, 2, 3) for kind, ext in (('rscp', 'db'), ('uploads', 'tar.gz')))
    left = sorted(os.listdir(tmp_path))
    assert 'rscp-20261005-023000.db' in left and 'uploads-20261004-023000.tar.gz' in left
    for other in ('status.json', 'pre-update-v3.0.0-20261001-000000.db', 'notes.txt', 'rscp-bad.db'):
        assert other in left


# ---------------------------------------------------------------------------
# The backup run
# ---------------------------------------------------------------------------

def test_backup_writes_valid_db_copy_and_tarball(bk, sources):
    status = _run(bk, sources, DT(2026, 10, 17, 2, 30))
    assert status['ok'] and status['error'] is None
    assert status['db_file'] == 'rscp-20261017-023000.db'
    assert status['uploads_file'] == 'uploads-20261017-023000.tar.gz'

    db_copy = os.path.join(bk.nightly, status['db_file'])
    conn = sqlite3.connect(db_copy)
    try:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 3
        assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'delete'
    finally:
        conn.close()
    assert status['db_size'] == os.path.getsize(db_copy)

    with tarfile.open(os.path.join(bk.nightly, status['uploads_file'])) as tar:
        names = tar.getnames()
        assert 'uploads/inventory/a.jpg' in names
        assert json.load(tar.extractfile('config.json')) == {'SECRET_KEY': 'k'}

    # status file, no temp leftovers
    assert bk.read_status(bk.nightly)['last_success'] == '2026-10-17T02:30:00'
    assert not [n for n in os.listdir(bk.nightly) if n.endswith('.tmp')]


def test_failed_backup_is_recorded_and_keeps_last_success(bk, sources):
    _run(bk, sources, DT(2026, 10, 16, 2, 30))
    db, uploads, config = sources
    status = bk.run_backup(db_path=os.path.join(bk.nightly, 'missing.db'), uploads_dir=uploads,
                           config_file=config, root=bk.nightly, settings=SETTINGS,
                           now=DT(2026, 10, 17, 2, 30))
    assert status['ok'] is False
    assert 'database not found' in status['error']
    assert status['last_success'] == '2026-10-16T02:30:00'
    assert bk.read_status(bk.nightly) == status
    # the failed run left nothing behind; the earlier backup is untouched
    names = os.listdir(bk.nightly)
    assert not [n for n in names if '20261017' in n]
    assert 'rscp-20261016-023000.db' in names


def test_backup_prunes_old_backups(bk, sources):
    for day in range(1, 8):
        _run(bk, sources, DT(2026, 10, day, 2, 30), settings={'BACKUP_KEEP_DAILY': 3, 'BACKUP_KEEP_WEEKLY': 0})
    dbs = sorted(n for n in os.listdir(bk.nightly) if n.endswith('.db'))
    assert dbs == ['rscp-20261005-023000.db', 'rscp-20261006-023000.db', 'rscp-20261007-023000.db']


def test_only_one_backup_at_a_time(bk, sources):
    with bk._RunLock(bk.nightly):
        assert bk.is_running(bk.nightly)
        with pytest.raises(bk.BackupBusy):
            _run(bk, sources, DT(2026, 10, 17, 2, 30))
    assert not bk.is_running(bk.nightly)


# ---------------------------------------------------------------------------
# Off-box copy
# ---------------------------------------------------------------------------

def test_offsite_copy_success_and_retention(bk, sources, tmp_path):
    nas = tmp_path / 'nas'
    nas.mkdir()
    (nas / 'someone-elses-file.txt').write_text('keep me')
    for day in (1, 2, 3):
        status = _run(bk, sources, DT(2026, 10, day, 2, 30),
                      settings={'BACKUP_OFFSITE_DIR': str(nas), 'BACKUP_KEEP_DAILY': 2, 'BACKUP_KEEP_WEEKLY': 0})
    assert status['offsite']['ok'] is True and status['offsite']['error'] is None
    assert status['offsite']['last_success'] == '2026-10-03T02:30:00'
    assert sorted(os.listdir(nas)) == ['rscp-20261002-023000.db', 'rscp-20261003-023000.db',
                                       'someone-elses-file.txt',
                                       'uploads-20261002-023000.tar.gz', 'uploads-20261003-023000.tar.gz']
    with open(nas / 'rscp-20261003-023000.db', 'rb') as a, \
            open(os.path.join(bk.nightly, 'rscp-20261003-023000.db'), 'rb') as b:
        assert a.read() == b.read()


def test_offsite_failure_does_not_fail_local_backup(bk, sources, tmp_path):
    status = _run(bk, sources, DT(2026, 10, 17, 2, 30),
                  settings={'BACKUP_OFFSITE_DIR': str(tmp_path / 'not-mounted')})
    assert status['ok'] is True
    assert status['offsite']['ok'] is False
    assert 'is the share mounted' in status['offsite']['error']
    assert os.path.exists(os.path.join(bk.nightly, status['db_file']))


def test_offsite_on_same_disk_is_refused(bk, sources, tmp_path, monkeypatch):
    monkeypatch.setattr(bk, 'OFFSITE_REQUIRE_SEPARATE_DEVICE', True)
    nas = tmp_path / 'nas'
    nas.mkdir()
    status = _run(bk, sources, DT(2026, 10, 17, 2, 30), settings={'BACKUP_OFFSITE_DIR': str(nas)})
    assert status['ok'] is True and status['offsite']['ok'] is False
    assert 'same disk' in status['offsite']['error']
    assert os.listdir(nas) == []


def test_deferred_offsite_copy_is_made_by_the_next_scheduled_check(bk, sources, tmp_path):
    nas = tmp_path / 'nas'
    nas.mkdir()
    conf = dict(SETTINGS, BACKUP_OFFSITE_DIR=str(nas))
    status = _run(bk, sources, DT(2026, 10, 17, 10, 0), settings=conf, defer_offsite=True)
    assert status['offsite']['pending'] is True and os.listdir(nas) == []

    synced = bk.sync_offsite(root=bk.nightly, settings=conf, now=DT(2026, 10, 17, 10, 15))
    assert synced['offsite']['ok'] is True and synced['offsite']['file'] == status['db_file']
    assert status['db_file'] in os.listdir(nas)
    assert bk.sync_offsite(root=bk.nightly, settings=conf, now=DT(2026, 10, 17, 10, 30)) is None


def test_failed_offsite_copy_is_retried_hourly(bk, sources, tmp_path):
    nas = tmp_path / 'nas'
    conf = dict(SETTINGS, BACKUP_OFFSITE_DIR=str(nas))
    _run(bk, sources, DT(2026, 10, 17, 2, 30), settings=conf)          # share missing
    assert bk.sync_offsite(root=bk.nightly, settings=conf, now=DT(2026, 10, 17, 2, 45)) is None
    nas.mkdir()                                                        # share back
    status = bk.sync_offsite(root=bk.nightly, settings=conf, now=DT(2026, 10, 17, 3, 45))
    assert status['offsite']['ok'] is True
    assert len(os.listdir(nas)) == 2


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

def test_is_due():
    from app.backup import is_due
    now = DT(2026, 10, 17, 3, 0)
    assert is_due(None, '02:30', now)
    assert not is_due({'last_success': '2026-10-17T02:31:00'}, '02:30', now)
    assert is_due({'last_success': '2026-10-16T02:31:00'}, '02:30', now)
    # before today's time: yesterday's slot counts
    assert not is_due({'last_success': '2026-10-16T02:31:00'}, '02:30', DT(2026, 10, 17, 1, 0))
    # a manual backup after the slot satisfies it; one before it doesn't
    assert not is_due({'last_success': '2026-10-17T02:45:00'}, '02:30', now)
    assert is_due({'last_success': '2026-10-17T01:00:00'}, '02:30', now)
    # a failed attempt is retried after an hour, not every 15 minutes
    failed = {'last_success': '2026-10-16T02:31:00', 'last_run': '2026-10-17T02:31:00'}
    assert not is_due(failed, '02:30', DT(2026, 10, 17, 3, 0))
    assert is_due(failed, '02:30', DT(2026, 10, 17, 3, 35))
    # time setting is honoured
    assert not is_due({'last_success': '2026-10-16T23:00:00'}, '23:00', DT(2026, 10, 17, 22, 0))
    assert is_due({'last_success': '2026-10-16T23:00:00'}, '23:00', DT(2026, 10, 17, 23, 1))


def test_run_if_due_honours_enabled(bk, monkeypatch):
    calls = []
    monkeypatch.setattr(bk, 'run_backup', lambda **kw: calls.append(kw) or {'ok': True})
    monkeypatch.setattr(bk, 'load_settings', lambda: dict(SETTINGS, BACKUP_ENABLED=False))
    assert bk.run_if_due(now=DT(2026, 10, 17, 3, 0)) is None and calls == []
    monkeypatch.setattr(bk, 'load_settings', lambda: dict(SETTINGS))
    assert bk.run_if_due(now=DT(2026, 10, 17, 3, 0)) == {'ok': True}
    assert calls[0]['trigger'] == 'scheduled'


def test_cli_status_and_scheduled_skip(bk, monkeypatch, capsys):
    monkeypatch.setattr(bk, 'load_settings', lambda: dict(SETTINGS, BACKUP_ENABLED=False))
    assert bk.main(['--scheduled']) == 0
    assert bk.main(['--status']) == 0
    assert capsys.readouterr().out.strip().endswith('null')


def test_timer_installed_only_in_the_data_dir_layout(monkeypatch, tmp_path):
    from app import backup, paths
    monkeypatch.setattr(backup, 'SYSTEMD_TIMER', str(tmp_path / 'rscp-backup.timer'))
    (tmp_path / 'rscp-backup.timer').write_text('[Timer]')
    monkeypatch.setattr(paths, 'USES_DATA_DIR', False)
    assert not backup.timer_installed()               # 2.x layout: nothing schedules backups
    monkeypatch.setattr(paths, 'USES_DATA_DIR', True)
    assert backup.timer_installed()
    (tmp_path / 'rscp-backup.timer').unlink()
    assert not backup.timer_installed()               # timer missing


# ---------------------------------------------------------------------------
# Settings, admin card and "Run backup now"
# ---------------------------------------------------------------------------

def test_backup_settings_registry_and_validation(fresh_app):
    from app.services import settings as st
    from app.services.settings import SettingsError
    with fresh_app.app_context():
        assert st.get('BACKUP_ENABLED') is True
        assert st.get('BACKUP_TIME') == '02:30'
        assert st.get('BACKUP_KEEP_DAILY') == 14 and st.get('BACKUP_KEEP_WEEKLY') == 8
        assert st.get('BACKUP_OFFSITE_DIR') == ''
        for bad in ('relative/dir', '/mnt/my share', '/mnt/x;rm', '/mnt/$HOME'):
            with pytest.raises(SettingsError):
                st.set('BACKUP_OFFSITE_DIR', bad)
        for bad in ('', '25:00', '2:30pm'):
            with pytest.raises(SettingsError):
                st.set('BACKUP_TIME', bad)
        with pytest.raises(SettingsError):
            st.set('BACKUP_KEEP_DAILY', 0)
        assert st.SETTINGS['BACKUP_OFFSITE_DIR'].permission == 'admin'
        if os.sep == '/':
            for bad in ('/', '/mnt', '/etc/rscp', '/var/lib/x', '/mnt/../etc'):
                with pytest.raises(SettingsError):
                    st.set('BACKUP_OFFSITE_DIR', bad)
            assert st.set('BACKUP_OFFSITE_DIR', '/mnt/nas/rscp/') == '/mnt/nas/rscp'


def test_admin_saves_backup_settings_others_cannot(fresh_app, fresh_client):
    from app.services import settings as st
    form = {'group': 'backups', 'BACKUP_ENABLED': ['false', 'true'], 'BACKUP_TIME': '01:15',
            'BACKUP_KEEP_DAILY': '7', 'BACKUP_KEEP_WEEKLY': '4'}
    login(fresh_client, 3)  # pos manager
    assert fresh_client.post('/admin/settings/save', data=form).status_code == 403
    login(fresh_client, 1)
    assert fresh_client.post('/admin/settings/save', data=form).status_code == 302
    with fresh_app.app_context():
        assert st.get('BACKUP_TIME') == '01:15' and st.get('BACKUP_KEEP_DAILY') == 7


def test_admin_card_shows_status(bk, sources, fresh_client):
    _run(bk, sources, DT(2026, 10, 17, 2, 30))
    login(fresh_client, 1)
    html = fresh_client.get('/admin/?tab=settings').get_data(as_text=True)
    assert 'Automatic Backups' in html
    assert '2026-10-17 02:30:00' in html
    assert 'Run backup now' in html
    assert 'rscp-20261017-023000.db' in html          # listed for restore
    assert bk.nightly in html


def test_run_backup_now_requires_admin(bk, fresh_client):
    for user in (2, 3):
        login(fresh_client, user)
        resp = fresh_client.post('/admin/backups/run')
        assert resp.status_code in (302, 403)
        if resp.status_code == 302:
            assert '/login' in resp.headers['Location']
    assert not os.path.exists(bk.nightly) or not os.listdir(bk.nightly)


def test_run_backup_now_as_admin(bk, fresh_client):
    from app.routes.admin import backup as routes
    login(fresh_client, 1)
    resp = fresh_client.post('/admin/backups/run')
    assert resp.status_code == 302
    routes._RUN_THREAD.join(30)
    status = bk.read_status()
    assert status['ok'] is True and status['trigger'] == 'manual'
    assert os.path.exists(os.path.join(bk.nightly, status['db_file']))


def test_run_backup_now_refused_while_running(bk, fresh_client):
    login(fresh_client, 1)
    with bk._RunLock(bk.nightly):
        resp = fresh_client.post('/admin/backups/run', follow_redirects=True)
    assert 'already running' in resp.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Stale-backup banner
# ---------------------------------------------------------------------------

def test_warning_logic():
    from app.backup import warning_for
    now = DT(2026, 10, 17, 12, 0)
    assert warning_for(None, True, now) == 'No automatic backup has completed yet.'
    assert warning_for({'last_success': '2026-10-16T02:30:00'}, True, now) is None
    assert '2026-10-15 23:00' in warning_for({'last_success': '2026-10-15T23:00:00'}, True, now)
    assert warning_for({'last_success': '2026-10-01T02:30:00'}, False, now) is None
    assert warning_for(None, False, now) is None


def _write_status(bk, last_success):
    bk.write_status({'last_run': last_success, 'last_success': last_success, 'ok': True}, bk.nightly)
    bk._WARNING_CACHE.update(at=0.0, key=None, value=None)


def test_banner_shown_to_admins_when_stale(bk, fresh_app, fresh_client):
    old = (DT.now() - datetime.timedelta(hours=40)).isoformat(timespec='seconds')
    _write_status(bk, old)
    login(fresh_client, 1)
    assert 'id="backupWarning"' in fresh_client.get('/admin/').get_data(as_text=True)

    login(fresh_client, 2)  # not an admin
    assert 'id="backupWarning"' not in fresh_client.get('/history').get_data(as_text=True)

    _write_status(bk, DT.now().isoformat(timespec='seconds'))
    login(fresh_client, 1)
    assert 'id="backupWarning"' not in fresh_client.get('/admin/').get_data(as_text=True)

    _write_status(bk, old)
    from app.services import settings as st
    with fresh_app.app_context():
        st.set('BACKUP_ENABLED', False)
    assert 'id="backupWarning"' not in fresh_client.get('/admin/').get_data(as_text=True)


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------

def _org_name(db):
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = 'ORG_NAME'").fetchone()
        return json.loads(row[0]) if row else None
    finally:
        conn.close()


def _set_org_name(db, name):
    conn = sqlite3.connect(db)
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('ORG_NAME', ?)", (json.dumps(name),))
    conn.commit()
    conn.close()


def test_restore_from_nightly(bk, sources, fresh_app, fresh_client, monkeypatch, tmp_path):
    db = fresh_app.config['DATABASE']
    monkeypatch.setattr('app.routes.admin.backup.DB_PATH', db)
    _set_org_name(db, 'Before')
    status = _run(bk, sources, DT(2026, 10, 17, 2, 30))
    _set_org_name(db, 'After')

    login(fresh_client, 2)
    fresh_client.post('/admin/backups/restore', data={'name': status['db_file']})
    assert _org_name(db) == 'After'                    # non-admin: nothing happened

    login(fresh_client, 1)
    with fresh_client.session_transaction() as sess:
        sess.pop('_flashes', None)                     # the "admin required" notice
    for bad in ('../rscp_test.db', 'rscp-20261017-023000.db.bak', 'rscp-20990101-000000.db', ''):
        resp = fresh_client.post('/admin/backups/restore', data={'name': bad}, follow_redirects=True)
        assert 'does not exist' in resp.get_data(as_text=True)
    assert _org_name(db) == 'After'

    resp = fresh_client.post('/admin/backups/restore', data={'name': status['db_file']})
    assert resp.status_code == 302
    assert _org_name(db) == 'Before'
    safety = os.listdir(os.path.join(os.path.dirname(db), 'backups'))
    assert any(n.startswith('pre-restore-') for n in safety)
    with fresh_client.session_transaction() as sess:
        assert '_user_id' not in sess                  # logged out after restore


def test_restore_refuses_a_bad_nightly_file(bk, fresh_app, fresh_client, monkeypatch):
    db = fresh_app.config['DATABASE']
    monkeypatch.setattr('app.routes.admin.backup.DB_PATH', db)
    os.makedirs(bk.nightly)
    with open(os.path.join(bk.nightly, 'rscp-20261017-023000.db'), 'wb') as f:
        f.write(b'not a database at all' * 100)
    login(fresh_client, 1)
    resp = fresh_client.post('/admin/backups/restore', data={'name': 'rscp-20261017-023000.db'},
                             follow_redirects=True)
    assert 'Restore refused' in resp.get_data(as_text=True)


def test_restore_uploaded_nightly_db(bk, sources, fresh_app, fresh_client, monkeypatch):
    db = fresh_app.config['DATABASE']
    monkeypatch.setattr('app.routes.admin.backup.DB_PATH', db)
    _set_org_name(db, 'From NAS')
    status = _run(bk, sources, DT(2026, 10, 17, 2, 30))
    _set_org_name(db, 'Current')
    with open(os.path.join(bk.nightly, status['db_file']), 'rb') as f:
        data = f.read()
    login(fresh_client, 1)
    fresh_client.post('/admin/import_backup', data={'backup_file': (io.BytesIO(data), status['db_file'])},
                      content_type='multipart/form-data')
    assert _org_name(db) == 'From NAS'


# ---------------------------------------------------------------------------
# systemd units / installer
# ---------------------------------------------------------------------------

def _read(rel):
    with open(os.path.join(ROOT, rel), encoding='utf-8') as f:
        return f.read()


def test_backup_units_match_layout():
    service = _read('deploy/rscp-backup.service').splitlines()
    for line in ('Type=oneshot', 'User=rscp', 'Environment=RSCP_DATA_DIR=/var/lib/rscp',
                 'WorkingDirectory=/opt/rscp/current',
                 'ExecStart=/opt/rscp/current/venv/bin/python -m app.backup --scheduled',
                 'ProtectSystem=strict', 'ReadWritePaths=/var/lib/rscp', 'ProtectHome=true',
                 'PrivateTmp=true', 'NoNewPrivileges=true'):
        assert line in service, line
    timer = _read('deploy/rscp-backup.timer').splitlines()
    for line in ('Persistent=true', 'Unit=rscp-backup.service', 'WantedBy=timers.target'):
        assert line in timer, line
    script = _read('install.sh')
    assert 'rscp-backup.service rscp-backup.timer' in script
    assert 'systemctl enable --now --quiet rscp-backup.timer' in script
    assert '--backup-paths)' in script
    # the off-box grant goes to the backup unit only, never the web service
    assert 'rscp-backup.service.d' in script and 'rscp.service.d' not in script


def test_uploads_archives_capped_separately(tmp_path):
    """Database copies follow daily/weekly retention; image archives keep only the newest N."""
    from app import backup as bk
    root = tmp_path / 'nightly'
    root.mkdir()
    for day in range(1, 8):
        stamp = f'202610{day:02d}-023000'
        (root / f'rscp-{stamp}.db').write_bytes(b'x')
        (root / f'uploads-{stamp}.tar.gz').write_bytes(b'y')
    bk.prune(str(root), keep_daily=14, keep_weekly=0, keep_uploads=2)
    names = sorted(p.name for p in root.iterdir())
    assert len([n for n in names if n.endswith('.db')]) == 7
    assert [n for n in names if n.endswith('.tar.gz')] == ['uploads-20261006-023000.tar.gz',
                                                           'uploads-20261007-023000.tar.gz']
