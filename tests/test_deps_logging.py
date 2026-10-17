"""Flask 3 runtime, stderr logging, admin journal view, pandas-free import."""
import glob
import io
import logging
import os
import shutil
import subprocess
import sys
from importlib.metadata import version

import pytest

from tests.conftest import login

APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


# --- framework versions -------------------------------------------------------

def test_runs_on_flask_and_werkzeug_3():
    assert int(version('flask').split('.')[0]) >= 3
    assert int(version('werkzeug').split('.')[0]) >= 3


# --- logging ----------------------------------------------------------------

def _rscp_handlers():
    return [h for h in logging.getLogger().handlers if getattr(h, '_rscp', False)]


def test_logging_goes_to_stderr_only_and_is_idempotent(fresh_app):
    from app.services.logger import configure_logging
    configure_logging()
    configure_logging()
    assert len(_rscp_handlers()) == 1
    for h in logging.getLogger().handlers:
        if type(h).__module__.startswith('_pytest'):
            continue  # pytest's own capture handlers
        assert not isinstance(h, logging.FileHandler), h


def test_create_app_writes_no_log_files(tmp_path):
    from app import create_app
    before = set(glob.glob(os.path.join(APP_ROOT, 'app*.log*')))
    create_app(test_config={'TESTING': True, 'DATABASE': str(tmp_path / 'x.db'),
                            'SECRET_KEY': 'k'})
    assert set(glob.glob(os.path.join(APP_ROOT, 'app*.log*'))) == before


def test_request_id_and_user_on_log_lines(fresh_app, fresh_client, capsys):
    def log_something():
        logging.getLogger('rscp.test').warning('hello from a request')
        return 'ok'

    fresh_app.add_url_rule('/_test/log', 'test_log', log_something)
    login(fresh_client, 1)
    capsys.readouterr()
    assert fresh_client.get('/_test/log').status_code == 200
    line = [l for l in capsys.readouterr().err.splitlines() if 'hello from a request' in l][0]
    assert 'WARNING' in line
    assert '[user admin]' in line
    assert '[req -]' not in line
    assert 'rscp.test' in line


def test_log_lines_outside_request_use_placeholders(fresh_app, capsys):
    logging.getLogger('rscp.test').warning('background message')
    line = [l for l in capsys.readouterr().err.splitlines() if 'background message' in l][0]
    assert '[req -] [user -]' in line


def test_no_session_or_config_dumps_in_logs():
    offenders = []
    for path in glob.glob(os.path.join(APP_ROOT, 'app', '**', '*.py'), recursive=True):
        with open(path, encoding='utf-8') as f:
            for n, line in enumerate(f, 1):
                if 'dict(session)' in line or ('log' in line and 'load_config()}' in line):
                    offenders.append(f'{path}:{n}')
    assert offenders == []


# --- admin logs page / journal ---------------------------------------------

def test_admin_logs_page_does_not_run_journalctl_by_default(fresh_app, fresh_client, monkeypatch):
    called = []
    monkeypatch.setattr('app.routes.admin.logs.read_journal', lambda n: called.append(n) or (None, None))
    login(fresh_client, 1)
    resp = fresh_client.get('/admin/logs')
    assert resp.status_code == 200
    assert b'Service Log' in resp.data
    assert called == []


def test_admin_logs_page_shows_journal_and_note(fresh_app, fresh_client, monkeypatch):
    monkeypatch.setattr('app.routes.admin.logs.read_journal',
                        lambda n: ('2026-10-17T10:00:00 host gunicorn[1]: <b>boot</b>', 'a note'))
    login(fresh_client, 1)
    resp = fresh_client.get('/admin/logs?journal=100')
    assert resp.status_code == 200
    assert b'&lt;b&gt;boot&lt;/b&gt;' in resp.data   # escaped, not rendered
    assert b'a note' in resp.data


def test_admin_logs_requires_admin(fresh_app, fresh_client):
    login(fresh_client, 2)
    assert fresh_client.get('/admin/logs?journal=100').status_code in (302, 403)


def test_read_journal_without_journalctl(monkeypatch):
    from app.routes.admin import logs
    monkeypatch.setattr(logs.shutil, 'which', lambda name: None)
    text, note = logs.read_journal(100)
    assert text is None and 'not available' in note


def _fake_run(stdout='', stderr='', returncode=0, exc=None, seen=None):
    def run(cmd, **kw):
        if seen is not None:
            seen.append((cmd, kw))
        if exc:
            raise exc
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
    return run


def test_read_journal_command_and_output(monkeypatch):
    from app.routes.admin import logs
    seen = []
    monkeypatch.setattr(logs.shutil, 'which', lambda name: '/usr/bin/journalctl')
    monkeypatch.setattr(logs.subprocess, 'run', _fake_run(stdout='line1\nline2\n', seen=seen))
    text, note = logs.read_journal(99999)
    assert text == 'line1\nline2\n' and note is None
    cmd, kw = seen[0]
    assert cmd == ['/usr/bin/journalctl', '-u', 'rscp', '-n', str(logs.JOURNAL_MAX_LINES),
                   '--no-pager', '-o', 'short-iso']
    assert kw['timeout'] == logs.JOURNAL_TIMEOUT


def test_read_journal_without_permission(monkeypatch):
    from app.routes.admin import logs
    monkeypatch.setattr(logs.shutil, 'which', lambda name: '/usr/bin/journalctl')
    hint = ('Hint: You are currently not seeing messages from other users and the system.\n'
            '      Users in groups \'adm\', \'systemd-journal\' can see all messages.')
    monkeypatch.setattr(logs.subprocess, 'run', _fake_run(stdout='-- No entries --\n', stderr=hint))
    text, note = logs.read_journal(50)
    assert 'systemd-journal' in note


def test_read_journal_timeout(monkeypatch):
    from app.routes.admin import logs
    monkeypatch.setattr(logs.shutil, 'which', lambda name: '/usr/bin/journalctl')
    monkeypatch.setattr(logs.subprocess, 'run',
                        _fake_run(exc=subprocess.TimeoutExpired('journalctl', 5)))
    text, note = logs.read_journal(50)
    assert text is None and 'did not answer' in note


# --- inventory import without pandas ----------------------------------------

def _post_import(client, name, payload, update=False):
    data = {'file': (io.BytesIO(payload), name)}
    if update:
        data['update_existing'] = 'on'
    return client.post('/inventory/import', data=data, content_type='multipart/form-data')


def _item(app, sku):
    from app.services.db import get_db_connection
    with app.app_context():
        conn = get_db_connection()
        row = conn.execute('SELECT * FROM inventory_items WHERE sku = ?', (sku,)).fetchone()
        conn.close()
    return row


def test_import_module_has_no_pandas():
    with open(os.path.join(APP_ROOT, 'app', 'routes', 'inventory', 'imports.py'), encoding='utf-8') as f:
        assert 'pandas' not in f.read()


def test_xlsx_import_with_numeric_cells(fresh_app, fresh_client):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(['SKU', 'Item Name', 'Quantity', 'Cost', 'Price', 'UPC', 'Part Number'])
    ws.append([1001, 'Widget', 5, 2.5, 9.99, 12345678905, None])
    ws.append([None, None, None, None, None, None, None])   # blank row
    ws.append(['NO-NAME', None, 1, 1, 1, None, None])        # skipped: no name
    buf = io.BytesIO()
    wb.save(buf)

    login(fresh_client, 1)
    resp = _post_import(fresh_client, 'items.xlsx', buf.getvalue())
    assert resp.status_code == 302
    row = _item(fresh_app, '1001')
    assert row is not None
    assert row['name'] == 'Widget'
    assert row['quantity'] == 5
    assert row['buy_price'] == 2.5 and row['sell_price'] == 9.99
    assert '12345678905' in row['secondary_ids']
    assert _item(fresh_app, 'NO-NAME') is None


def test_csv_import_with_bom_and_update(fresh_app, fresh_client):
    login(fresh_client, 1)
    csv1 = '﻿SKU,Name,Qty,Price\r\nCSV-1,Thing,2,4.00\r\n'.encode('utf-8')
    assert _post_import(fresh_client, 'a.csv', csv1).status_code == 302
    assert _item(fresh_app, 'CSV-1')['quantity'] == 2

    csv2 = b'sku,name,quantity\nCSV-1,Thing v2,3\n'
    _post_import(fresh_client, 'b.csv', csv2, update=True)
    row = _item(fresh_app, 'CSV-1')
    assert row['quantity'] == 5 and row['name'] == 'Thing v2'


# --- production entry point ---------------------------------------------------

_IGNORE_NAMES = {'.git', 'tests', 'docs', '__pycache__', 'uploads', 'backups', 'temp',
                 '.pytest_cache', 'config.json'}


def _ignore(directory, names):
    return [n for n in names
            if n in _IGNORE_NAMES or n.startswith('.venv') or n == 'venv'
            or '.db' in n or n.endswith('.log') or '.log.' in n]


def test_wsgi_app_imports_in_legacy_mode(tmp_path):
    """`from wsgi import app` (what gunicorn does) works with no RSCP_DATA_DIR,
    on a copy of the tree so the real checkout gets no database or config."""
    tree = tmp_path / 'rscp'
    shutil.copytree(APP_ROOT, tree, ignore=_ignore)
    script = (
        "import app.services.background_tasks as bt\n"
        "import app.services.system_migrations as sm\n"
        "noop = lambda *a, **k: None\n"
        "for mod, name in ((bt, 'start_background_tasks'), (bt, 'start_scheduler'),\n"
        "                  (sm, 'run_system_migrations')):\n"
        "    if hasattr(mod, name): setattr(mod, name, noop)\n"
        "from app import paths\n"
        "assert not paths.USES_DATA_DIR, paths.DATA_DIR\n"
        "from wsgi import app\n"
        "resp = app.test_client().get('/login')\n"
        "print('STATUS', resp.status_code)\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in ('RSCP_DATA_DIR', 'TESTING')}
    proc = subprocess.run([sys.executable, '-c', script], cwd=str(tree), env=env,
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stderr[-3000:]
    status = int(proc.stdout.split('STATUS')[1].split()[0])
    assert status < 500
    assert glob.glob(str(tree / 'app*.log*')) == []
