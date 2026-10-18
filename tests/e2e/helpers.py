"""
Reusable pieces for the browser tests: known users, login, seeding and the
page watcher that turns console errors / CSP violations / failed requests
into test failures.

Seeding goes through the same services the app uses (app.services.*) on a
plain connection to the e2e server's database, so seeded rows look exactly
like rows the pages would create.
"""
import contextlib
from urllib.parse import urlsplit

# Test-only accounts that exist solely in the throwaway e2e database.
USERS = {
    'admin': {'id': 1, 'password': 'E2e-Admin-Pass-1', 'roles': '["super_admin"]', 'is_admin': 1},
    'cashier': {'id': 2, 'password': 'E2e-Cashier-Pass-1', 'roles': '["operator"]', 'is_admin': 0},
}

# Settings written to the e2e config.json before the server starts (the
# first migration imports them into the settings table).
E2E_CONFIG = {
    'SECRET_KEY': 'e2e-secret-key-not-for-production',
    'ORG_NAME': 'E2E Store',
    'INVENTORY_ENABLED': True,
    'POS_ENABLED': True,
    'TIMECLOCK_ENABLED': True,
    'LOCATION_OPTIONS': {'area': ['Retail', 'Backroom']},
}


# ---------------------------------------------------------------------------
# Database / seeding
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def db(server):
    """sqlite3 connection (Row factory) to the e2e server's database."""
    from app.services.db import get_db_connection
    conn = get_db_connection(server.db_path)
    try:
        yield conn
    finally:
        conn.close()


def seed_users(server):
    from werkzeug.security import generate_password_hash
    with db(server) as conn:
        for name, u in USERS.items():
            conn.execute(
                'INSERT OR REPLACE INTO users (id, username, password_hash, is_admin, roles) VALUES (?, ?, ?, ?, ?)',
                (u['id'], name, generate_password_hash(u['password']), u['is_admin'], u['roles']))
        conn.commit()


def seed_package(server, tracking, name='E2E Package', quantity=1, status='expected', priority=False, sku=None):
    """An expected package (as an upload would create it). Returns its id."""
    with db(server) as conn:
        cur = conn.execute(
            "INSERT INTO packages (tracking_number, item_name, quantity, status, source, priority, sku, date_expected) "
            "VALUES (?, ?, ?, ?, 'manifest', ?, ?, date('now', 'localtime'))",
            (tracking, name, quantity, status, 1 if priority else 0, sku))
        conn.commit()
        return cur.lastrowid


def seed_item(server, name, quantity=0, sku=None, area='Retail', **fields):
    """An inventory item created through the inventory service. Returns its id."""
    from app.services import inventory
    with db(server) as conn:
        f = {'sku': sku or inventory.generate_sku(conn, 'ATO'), 'name': name,
             'quantity': quantity, 'location_area': area}
        f.update(fields)
        return inventory.create_item(conn, f, user='admin')


def package_status(server, tracking):
    with db(server) as conn:
        row = conn.execute('SELECT status, date_scanned FROM packages WHERE tracking_number = ?',
                           (tracking,)).fetchone()
        return (row['status'], row['date_scanned']) if row else (None, None)


def item_by_name(server, name):
    with db(server) as conn:
        return conn.execute('SELECT * FROM inventory_items WHERE name = ?', (name,)).fetchone()


def setting(server, key):
    """Current value of a setting as the app sees it (settings table)."""
    import json
    with db(server) as conn:
        row = conn.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row['value'])
    except (TypeError, ValueError):
        return row['value']


# ---------------------------------------------------------------------------
# Browser
# ---------------------------------------------------------------------------

def login(page, user='admin', password=None):
    """Sign in through the real login form. Leaves the page on the gateway."""
    page.goto('/login')
    page.fill('input[name="username"]', user)
    page.fill('input[name="password"]', password or USERS[user]['password'])
    page.click('button[type="submit"]')
    page.wait_for_url(lambda url: '/login' not in url)
    return page


def logout(page):
    page.goto('/logout')


# Recorded in the page before any script runs: CSP violations do not always
# reach the console in a form we can match, the DOM event always fires.
_CSP_HOOK = """
window.__rscpCsp = [];
document.addEventListener('securitypolicyviolation', function (e) {
  window.__rscpCsp.push(e.violatedDirective + ' blocked ' + (e.blockedURI || 'inline') +
                        (e.sourceFile ? ' at ' + e.sourceFile + ':' + e.lineNumber : ''));
});
"""


class PageWatcher:
    """Collects everything that should never happen on a healthy page:
    console errors (CSP violations included), uncaught exceptions, failed
    requests and HTTP errors from our own origin, CSP violation events."""

    def __init__(self, page, base_url):
        self.page = page
        self.origin = '{0.scheme}://{0.netloc}'.format(urlsplit(base_url))
        self.problems = []
        page.add_init_script(_CSP_HOOK)
        page.on('console', self._on_console)
        page.on('pageerror', lambda exc: self.problems.append(f'[{page.url}] uncaught: {exc}'))
        page.on('requestfailed', self._on_request_failed)
        page.on('response', self._on_response)

    def _on_console(self, msg):
        if msg.type == 'error':
            self.problems.append(f'[{self.page.url}] console error: {msg.text}')

    def _on_request_failed(self, request):
        failure = request.failure or ''
        # Requests cut off by navigating away (polling, prefetch) are not errors
        if 'ERR_ABORTED' in failure or 'NS_BINDING_ABORTED' in failure:
            return
        self.problems.append(f'[{self.page.url}] request failed: {request.method} {request.url} ({failure})')

    def _on_response(self, response):
        if response.url.startswith(self.origin) and response.status >= 400:
            self.problems.append(f'[{self.page.url}] HTTP {response.status}: '
                                 f'{response.request.method} {response.url}')

    def collect_csp(self):
        try:
            for v in self.page.evaluate('window.__rscpCsp || []'):
                self.problems.append(f'[{self.page.url}] CSP violation: {v}')
            self.page.evaluate('window.__rscpCsp = []')
        except Exception:
            pass  # page closed / navigating

    def take(self):
        """Problems seen so far (and reset)."""
        self.collect_csp()
        problems, self.problems = self.problems, []
        return problems

    def assert_clean(self):
        problems = self.take()
        assert not problems, 'Page problems:\n  ' + '\n  '.join(problems)


def settle(page, timeout=5000):
    """Wait until the page has loaded and its first requests are done."""
    page.wait_for_load_state('load')
    try:
        page.wait_for_load_state('networkidle', timeout=timeout)
    except Exception:
        pass  # pages that poll never go fully idle; 'load' is enough then
