"""
Browser end-to-end tests (Playwright + Chromium) against the real app.

Skipped unless RSCP_E2E=1, so the normal suite stays fast and needs no
browser:

    pip install -r requirements-dev.txt
    python -m playwright install chromium
    RSCP_E2E=1 python -m pytest tests/e2e

Fixtures:
  e2e_server   (session) the app in its own process on a free port, with a
               throwaway RSCP_DATA_DIR, migrated database and seeded users
  browser      (session) headless Chromium (RSCP_E2E_HEADED=1 to watch)
  page         (function) fresh browser context + page, base_url set, with
               a PageWatcher attached as page.watcher
  admin_page / cashier_page   page already signed in

RSCP_E2E_ARTIFACTS=<dir> keeps the server log and failure screenshots there.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from types import SimpleNamespace

import pytest

from tests.e2e import helpers

E2E_ENABLED = os.environ.get('RSCP_E2E') == '1'
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def pytest_collection_modifyitems(config, items):
    here = os.path.dirname(__file__)
    skip = pytest.mark.skip(reason='browser tests: set RSCP_E2E=1 (needs playwright + chromium)')
    for item in items:
        if str(item.fspath).startswith(here):
            item.add_marker(pytest.mark.e2e)
            if not E2E_ENABLED:
                item.add_marker(skip)


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def _artifacts_dir(tmp_root):
    d = os.environ.get('RSCP_E2E_ARTIFACTS') or os.path.join(tmp_root, 'artifacts')
    os.makedirs(d, exist_ok=True)
    return os.path.abspath(d)


def _wait_healthy(base_url, proc, log_path, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            with urllib.request.urlopen(base_url + '/healthz', timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.3)
    with open(log_path, encoding='utf-8', errors='replace') as f:
        tail = f.read()[-4000:]
    raise RuntimeError(f'e2e server did not come up (exit={proc.poll()}):\n{tail}')


@pytest.fixture(scope='session')
def e2e_server():
    tmp_root = tempfile.mkdtemp(prefix='rscp-e2e-')
    data_dir = os.path.join(tmp_root, 'data')
    os.makedirs(data_dir)
    with open(os.path.join(data_dir, 'config.json'), 'w') as f:
        json.dump(helpers.E2E_CONFIG, f)

    artifacts = _artifacts_dir(tmp_root)
    log_path = os.path.join(artifacts, 'server.log')
    port = _free_port()
    base_url = f'http://127.0.0.1:{port}'

    env = os.environ.copy()
    env['RSCP_DATA_DIR'] = data_dir
    env.pop('TESTING', None)          # the server must run as production does
    env.pop('FLASK_ENV', None)        # (secure cookies would break plain http)
    env['PYTHONPATH'] = REPO_ROOT + os.pathsep + env.get('PYTHONPATH', '')
    log = open(log_path, 'w', encoding='utf-8')
    proc = subprocess.Popen([sys.executable, os.path.join(REPO_ROOT, 'tests', 'e2e', 'serve.py'), str(port)],
                            cwd=REPO_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        _wait_healthy(base_url, proc, log_path)   # create_app() has migrated the DB by now
        server = SimpleNamespace(base_url=base_url, data_dir=data_dir,
                                 db_path=os.path.join(data_dir, 'rscp.db'),
                                 artifacts=artifacts, log_path=log_path, process=proc)
        helpers.seed_users(server)
        yield server
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


@pytest.fixture(scope='session')
def browser():
    sync_api = pytest.importorskip('playwright.sync_api', reason='pip install playwright')
    pw = sync_api.sync_playwright().start()
    try:
        b = pw.chromium.launch(headless=os.environ.get('RSCP_E2E_HEADED') != '1')
    except Exception as e:
        pw.stop()
        pytest.fail(f'Chromium not available ({e}); run: python -m playwright install chromium')
    yield b
    b.close()
    pw.stop()


@pytest.fixture
def page(request, browser, e2e_server):
    context = browser.new_context(base_url=e2e_server.base_url, viewport={'width': 1280, 'height': 900})
    context.set_default_timeout(15000)
    pg = context.new_page()
    pg.watcher = helpers.PageWatcher(pg, e2e_server.base_url)
    # A native confirm()/alert() would block the page: accept and remember it
    pg.dialogs = []
    pg.on('dialog', lambda d: (pg.dialogs.append(d.message), d.accept()))
    yield pg
    failed = getattr(request.node, 'rep_call', None)
    if failed is not None and failed.failed:
        try:
            pg.screenshot(path=os.path.join(e2e_server.artifacts, f'{request.node.name}.png'), full_page=True)
        except Exception:
            pass
    context.close()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f'rep_{rep.when}', rep)


@pytest.fixture
def admin_page(page):
    helpers.login(page, 'admin')
    return page


@pytest.fixture
def cashier_page(page):
    helpers.login(page, 'cashier')
    return page
