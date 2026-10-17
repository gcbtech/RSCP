"""
Shared fixtures.

`fresh_app` builds an app on a brand-new temporary database (fully migrated)
with one user per common role, so tests never depend on the dev box's data.
Log a test client in with `login(client, user_id)`.
"""
import json
import os
import sys
import tempfile

import pytest

# Isolate every test run from the machine's real data and config.json: point
# the app's data directory at a throwaway folder BEFORE app modules import.
_TEST_DATA_DIR = tempfile.mkdtemp(prefix='rscp-test-data-')
os.environ['RSCP_DATA_DIR'] = _TEST_DATA_DIR
os.environ['TESTING'] = 'True'
with open(os.path.join(_TEST_DATA_DIR, 'config.json'), 'w') as _f:
    json.dump({'SECRET_KEY': 'test_key', 'ORG_NAME': 'Test Store',
               'INVENTORY_ENABLED': True, 'POS_ENABLED': True, 'TIMECLOCK_ENABLED': True}, _f)

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# id, username, is_admin, roles
FRESH_USERS = [
    (1, 'admin', 1, '["super_admin"]'),
    (2, 'cashier', 0, '["operator"]'),
    (3, 'manager', 0, '["pos_admin"]'),
]


@pytest.fixture
def fresh_app(tmp_path):
    from werkzeug.security import generate_password_hash
    from app import create_app
    from app.services.db import get_db_connection
    from app.services.migration import run_migrations

    db_path = str(tmp_path / 'rscp_test.db')
    app = create_app(test_config={
        'TESTING': True,
        'DATABASE': db_path,
        'WTF_CSRF_ENABLED': False,
        'SECRET_KEY': 'test_key',
    })
    with app.app_context():
        run_migrations()
        conn = get_db_connection()
        pw = generate_password_hash('Correct-Horse-1')
        conn.executemany(
            'INSERT INTO users (id, username, password_hash, is_admin, roles) VALUES (?, ?, ?, ?, ?)',
            [(uid, name, pw, admin, roles) for uid, name, admin, roles in FRESH_USERS])
        conn.commit()
        conn.close()
    yield app


@pytest.fixture
def fresh_client(fresh_app):
    return fresh_app.test_client()


def login(client, user_id):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user_id)
    return client
