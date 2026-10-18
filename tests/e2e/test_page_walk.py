"""Open every main page as admin and fail on any JS console error, CSP
violation, uncaught exception or failed request.

This is the guard for moving inline JavaScript into static files: once the
CSP drops 'unsafe-inline', any inline script left behind shows up here as a
CSP violation."""
from urllib.parse import urlsplit

import pytest

from tests.e2e import helpers

# Pages that are not in the nav (or only behind a tab / button) but are
# part of daily use. The nav's own links are discovered at run time.
EXTRA_PAGES = [
    '/admin/',
    '/admin/?tab=packages',
    '/admin/?tab=inventory',
    '/admin/?tab=pos',
    '/admin/?tab=settings',
    '/scan/receive',
    '/expected',
    '/past_due',
    '/return_mode',
    '/open_returns',
    '/refunded_log',
    '/inventory/add',
    '/inventory/low-stock',
    '/inventory/audit',
    '/inventory/import',
]

# Module landing pages; each one's sidebar lists that module's pages.
MODULE_HOMES = ['/receiving', '/inventory', '/pos', '/timeclock']

# Never visited by the walk.
SKIP_PREFIXES = ('/logout', '/static/', '/login')


def _links(page, base_url, selector):
    """Same-origin paths of the links matching `selector` on the current page."""
    origin = urlsplit(base_url)
    hrefs = page.eval_on_selector_all(selector, 'els => els.map(e => e.href)')
    out = []
    for href in hrefs:
        u = urlsplit(href)
        if u.netloc != origin.netloc or not u.path:
            continue
        path = u.path + (('?' + u.query) if u.query else '')
        if path.startswith(SKIP_PREFIXES):
            continue
        out.append(path)
    return out


@pytest.fixture
def seeded(e2e_server):
    helpers.seed_package(e2e_server, '1ZWALK0000000001', name='Walk Package')
    helpers.seed_item(e2e_server, 'Walk Item', quantity=4)


def test_every_main_page_is_clean(admin_page, e2e_server, seeded):
    page = admin_page
    page.goto('/')                      # gateway: module cards, no sidebar
    gateway = _links(page, e2e_server.base_url, 'a[href]')
    nav = []                            # the sidebar shows the current module's pages
    for module in MODULE_HOMES:
        page.goto(module)
        nav += _links(page, e2e_server.base_url, 'nav#quickSidebar a[href]')
    assert len(set(nav)) >= 15, nav     # the nav really was found
    pages = []
    for path in ['/'] + gateway + nav + EXTRA_PAGES:
        if path not in pages:
            pages.append(path)

    print(f"page walk: {len(pages)} pages: {pages}")
    page.watcher.take()  # start clean
    failures = {}
    for path in pages:
        resp = page.goto(path)
        helpers.settle(page)
        problems = page.watcher.take()
        if resp is None or resp.status >= 400:
            problems.insert(0, f'HTTP {resp.status if resp else "no response"}')
        if '/login' in page.url:
            problems.insert(0, 'bounced to the login page')
        if problems:
            failures[path] = problems

    assert not failures, 'Pages with problems:\n' + '\n'.join(
        f'{path}:\n    ' + '\n    '.join(p) for path, p in failures.items())
