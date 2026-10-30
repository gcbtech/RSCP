#!/usr/bin/env python3
"""
Prove that a change leaves the desktop UI exactly as it was.

    python3 scripts/desktop_parity.py            # compare this tree with `beta`
    python3 scripts/desktop_parity.py --base main

Two checks, both against a git ref (default `beta`):

1. Files: nothing under templates/ or static/ changed outside the phone UI's
   own folders (templates/m/, static/m/).
2. Pages: every parameterless GET page is rendered by BOTH trees, against
   copies of one seeded database, as admin, cashier, manager and signed out,
   with a desktop browser's User-Agent. The HTML must match byte for byte
   after masking what differs on every request anyway (CSRF and checkout
   tokens, ?v= static versions, clock seconds, epoch timestamps).

The phone UI (/m) is excluded; any other route added or removed is reported.
Exit status 0 = desktop unchanged, 1 = a difference (printed as a diff).

The base tree is checked out into a temporary git worktree and removed again.
Nothing touches the real data directory or database.
"""
import argparse
import difflib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

MOBILE_PREFIX = '/m'
MOBILE_DIRS = ('templates/m/', 'static/m/')
SKIP_WORDS = ('logout', 'sync', 'update', 'restart', 'rollback', 'export', 'download',
              'delete', 'run_', 'test', 'trigger', 'debug', 'callback',
              'print', 'backup', 'reset', 'clear', 'unpair', 'fetch')
DESKTOP_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36')
USERS = [  # id, username, is_admin, roles
    (1, 'admin', 1, '["super_admin"]'),
    (2, 'cashier', 0, '["operator"]'),
    (3, 'manager', 0, '["pos_admin"]'),
]
CONFIG = {'SECRET_KEY': 'parity', 'ORG_NAME': 'Parity Store', 'INVENTORY_ENABLED': True,
          'POS_ENABLED': True, 'TIMECLOCK_ENABLED': True}

MASKS = [
    (re.compile(r'(name="csrf_token"\s+value=")[^"]*'), r'\1CSRF'),
    (re.compile(r'(name="csrf-token"\s+content=")[^"]*'), r'\1CSRF'),
    (re.compile(r'(name="checkout_token"\s+value=")[^"]*'), r'\1TOKEN'),
    (re.compile(r'("csrfToken":\s*")[^"]*'), r'\1CSRF'),
    (re.compile(r'([?&](?:amp;)?v=)\d+'), r'\1V'),
    (re.compile(r'\b\d{1,2}:\d{2}:\d{2}\b'), 'HH:MM:SS'),
    (re.compile(r'\b1[6-9]\d{8}(?:\d{3})?(?:\.\d+)?\b'), 'EPOCH'),
    (re.compile(r'(Support Reference ID:<br>\s*<code>)[0-9a-f]{8}'), r'\1REQID'),
]


def git(*args, cwd=REPO):
    return subprocess.run(['git', '-c', 'safe.directory=*', *args], cwd=cwd,
                          check=True, capture_output=True, text=True).stdout


# ---------------------------------------------------------------- rendering
# Runs in a child process whose working tree is either the base or this one.

def _boot(root, data_dir, db_path):
    os.environ['RSCP_DATA_DIR'] = data_dir
    os.environ['TESTING'] = 'True'
    with open(os.path.join(data_dir, 'config.json'), 'w') as f:
        json.dump(CONFIG, f)
    sys.path.insert(0, root)
    os.chdir(root)
    from app import create_app
    app = create_app(test_config={'TESTING': True, 'DATABASE': db_path,
                                  'SECRET_KEY': 'parity', 'WTF_CSRF_ENABLED': False})
    return app


def make_seed(root, data_dir, db_path):
    """A migrated database with users and a little data for pages to show."""
    from werkzeug.security import generate_password_hash
    app = _boot(root, data_dir, db_path)
    with app.app_context():
        from app.services.migration import run_migrations
        from app.services.db import get_db_connection
        run_migrations()
        conn = get_db_connection()
        pw = generate_password_hash('Parity-Pass-1')
        conn.executemany('INSERT INTO users (id, username, password_hash, is_admin, roles) '
                         'VALUES (?, ?, ?, ?, ?)', [(u, n, pw, a, r) for u, n, a, r in USERS])
        seeds = [
            "INSERT INTO inventory_items (name, sku, quantity, sell_price_cents, buy_price_cents, location) "
            "VALUES ('Parity Widget', 'PAR-1', 3, 1999, 1000, 'Shelf A1')",
            "INSERT INTO inventory_items (name, sku, quantity, sell_price_cents, buy_price_cents) "
            "VALUES ('Parity Empty', 'PAR-2', 0, 500, 200)",
            "INSERT INTO packages (tracking_number, item_name, date_expected, status) "
            "VALUES ('1ZPARITY0001', 'Parity Package', date('now'), 'expected')",
            "INSERT INTO packages (tracking_number, item_name, date_expected, status) "
            "VALUES ('1ZPARITY0002', 'Late Package', date('now', '-3 days'), 'past_due')",
        ]
        for sql in seeds:
            try:
                conn.execute(sql)
            except sqlite3.Error as e:   # schema drift between refs: seed what fits
                print(f'  seed skipped ({e}): {sql[:60]}...', file=sys.stderr)
        conn.commit()
        conn.close()


def render(root, data_dir, db_path, out_path):
    app = _boot(root, data_dir, db_path)
    paths = []
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        if 'GET' not in rule.methods or rule.arguments or rule.rule.startswith('/static'):
            continue
        if rule.rule == MOBILE_PREFIX or rule.rule.startswith(MOBILE_PREFIX + '/'):
            continue
        if any(w in rule.rule for w in SKIP_WORDS):
            continue
        paths.append(rule.rule)

    pages = {}
    for who in ('anon', 1, 2, 3):
        client = app.test_client()
        if who != 'anon':
            with client.session_transaction() as sess:
                sess['_user_id'] = str(who)
        for path in (['/', '/login'] if who == 'anon' else paths):
            resp = client.get(path, headers={'User-Agent': DESKTOP_UA})
            body = resp.get_data(as_text=True)
            # Each tree has its own throwaway data directory (shown on the admin page)
            body = body.replace(data_dir.replace('\\', '\\\\'), 'DATA_DIR').replace(data_dir, 'DATA_DIR')
            for rx, repl in MASKS:
                body = rx.sub(repl, body)
            pages[f'{who} {path}'] = {
                'status': resp.status_code,
                'location': resp.headers.get('Location', ''),
                'type': resp.headers.get('Content-Type', ''),
                'body': body,
            }
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(pages, f)


# ---------------------------------------------------------------- comparing

def changed_desktop_files(base):
    names = git('diff', '--name-only', base, '--', 'templates', 'static').split()
    untracked = git('ls-files', '--others', '--exclude-standard', '--', 'templates', 'static').split()
    return sorted(n for n in set(names) | set(untracked) if not n.startswith(MOBILE_DIRS))


def run_child(mode, root, data_dir, db_path, out_path=''):
    cmd = [sys.executable, os.path.abspath(__file__), '--child', mode, '--root', root,
           '--data', data_dir, '--db', db_path, '--out', out_path]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit(f'{mode} failed in {root}')


def compare(base):
    status = 0
    files = changed_desktop_files(base)
    if files:
        status = 1
        print(f'Desktop files changed since {base}:')
        for n in files:
            print(f'  {n}')
    else:
        print(f'Files: nothing under templates/ or static/ changed outside {", ".join(MOBILE_DIRS)}')

    work = tempfile.mkdtemp(prefix='rscp-parity-')
    base_tree = os.path.join(work, 'base')
    git('worktree', 'add', '--detach', base_tree, base)
    try:
        seed = os.path.join(work, 'seed.db')
        os.makedirs(os.path.join(work, 'seed-data'))
        run_child('seed', base_tree, os.path.join(work, 'seed-data'), seed)
        results = {}
        for label, root in (('base', base_tree), ('this', REPO)):
            data_dir = os.path.join(work, f'{label}-data')
            os.makedirs(data_dir)
            db = os.path.join(work, f'{label}.db')
            shutil.copyfile(seed, db)
            out = os.path.join(work, f'{label}.json')
            run_child('render', root, data_dir, db, out)
            with open(out, encoding='utf-8') as f:
                results[label] = json.load(f)
    finally:
        git('worktree', 'remove', '--force', base_tree)
        shutil.rmtree(work, ignore_errors=True)

    a, b = results['base'], results['this']
    for key in sorted(set(a) - set(b)):
        status = 1
        print(f'Page gone: {key}')
    for key in sorted(set(b) - set(a)):
        status = 1
        print(f'Page added outside {MOBILE_PREFIX}: {key}')
    differing = 0
    for key in sorted(set(a) & set(b)):
        if a[key] == b[key]:
            continue
        differing += 1
        status = 1
        print(f'\nPage differs: {key}')
        for field in ('status', 'location', 'type'):
            if a[key][field] != b[key][field]:
                print(f'  {field}: {a[key][field]!r} -> {b[key][field]!r}')
        if a[key]['body'] != b[key]['body']:
            diff = difflib.unified_diff(a[key]['body'].splitlines(), b[key]['body'].splitlines(),
                                        f'{base} {key}', f'this {key}', lineterm='', n=1)
            for line in list(diff)[:40]:
                print('  ' + line)
    same = len(set(a) & set(b)) - differing
    print(f'\nPages: {same} identical, {differing} different, '
          f'{len(set(a) - set(b))} gone, {len(set(b) - set(a))} added (desktop UA, 4 sign-in states)')
    print('DESKTOP UNCHANGED' if status == 0 else 'DESKTOP CHANGED')
    return status


def main():
    p = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    p.add_argument('--base', default='beta', help='git ref to compare against (default: beta)')
    p.add_argument('--child', choices=('seed', 'render'), help=argparse.SUPPRESS)
    p.add_argument('--root', help=argparse.SUPPRESS)
    p.add_argument('--data', help=argparse.SUPPRESS)
    p.add_argument('--db', help=argparse.SUPPRESS)
    p.add_argument('--out', help=argparse.SUPPRESS)
    args = p.parse_args()
    if args.child == 'seed':
        make_seed(args.root, args.data, args.db)
    elif args.child == 'render':
        render(args.root, args.data, args.db, args.out)
    else:
        sys.exit(compare(args.base))


if __name__ == '__main__':
    main()
