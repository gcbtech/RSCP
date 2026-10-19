#!/usr/bin/env python3
"""
Render every parameterless GET page as an admin against a COPY of a database
and report anything that is not a 2xx/3xx. Read-only with respect to the
source database.

    python3 scripts/smoke_routes.py /path/to/rscp.db

Routes that trigger actions on GET (sync, update, restart, export...) are
skipped.
"""
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

SKIP_WORDS = ('logout', 'sync', 'update', 'restart', 'rollback', 'export', 'download',
              'delete', 'run_', 'test', 'trigger', 'debug', 'callback',
              'print', 'backup', 'reset', 'clear', 'unpair', 'fetch')


def main(src_db):
    work = tempfile.mkdtemp(prefix='rscp-smoke-')
    db = os.path.join(work, 'rscp.db')
    src = sqlite3.connect(src_db)
    dst = sqlite3.connect(db)
    src.backup(dst)
    dst.close()
    src.close()

    os.environ['TESTING'] = 'True'
    from app import create_app
    app = create_app(test_config={'TESTING': True, 'DATABASE': db, 'SECRET_KEY': 'smoke',
                                  'WTF_CSRF_ENABLED': False})
    with app.app_context():
        from app.services.migration import run_migrations
        run_migrations(db)

    conn = sqlite3.connect(db)
    admin_id = conn.execute("SELECT id FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1").fetchone()[0]
    conn.close()

    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(admin_id)

    bad, ok = [], 0
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        if 'GET' not in rule.methods or rule.arguments or rule.rule.startswith('/static'):
            continue
        if any(w in rule.rule for w in SKIP_WORDS):
            continue
        resp = client.get(rule.rule)
        if resp.status_code >= 400:
            bad.append((resp.status_code, rule.rule))
        else:
            ok += 1
    shutil.rmtree(work, ignore_errors=True)

    print(f"{ok} pages OK")
    for code, url in bad:
        print(f"  {code}  {url}")
    return 1 if any(code >= 500 for code, _ in bad) else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1]))
