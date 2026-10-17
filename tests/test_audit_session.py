import pytest
import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.db import get_db_connection


@pytest.fixture
def app():
    """Create application for testing with an isolated database."""
    from app import create_app

    db_path = os.path.join(os.path.dirname(__file__), 'test_audit_session.db')
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except Exception:
            pass

    app = create_app(test_config={
        'TESTING': True,
        'DATABASE': db_path,
        'WTF_CSRF_ENABLED': False,
        'SECRET_KEY': 'test_key'
    })

    with app.app_context():
        from app.services.migration import run_migrations
        run_migrations()

        conn = get_db_connection()
        conn.execute('''
            INSERT INTO users (id, username, password_hash, is_admin)
            VALUES (?, ?, ?, ?)
        ''', (1, 'testuser', 'pbkdf2:sha256:somehash', 1))
        conn.commit()
        conn.close()

    yield app

    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except Exception:
            pass


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def authenticated_client(app, client):
    with client.session_transaction() as sess:
        sess['_user_id'] = '1'  # Flask-Login session key
        sess['user'] = 1
    return client


def _active_count(app):
    with app.app_context():
        conn = get_db_connection()
        n = conn.execute("SELECT COUNT(*) FROM audit_sessions WHERE status = 'active'").fetchone()[0]
        conn.close()
    return n


def test_start_audit_creates_active_session(app, authenticated_client):
    """Starting an audit with no active session creates exactly one."""
    resp = authenticated_client.post('/inventory/audit/start', data={'mode': 'shelf'})
    assert resp.status_code == 302
    assert '/inventory/audit/live/' in resp.headers['Location']
    assert _active_count(app) == 1


def test_start_same_mode_resumes_instead_of_forking(app, authenticated_client):
    """Tapping the same mode again must resume, not spawn a second session.

    This is the core regression: concurrent active sessions split one logical
    audit across multiple reports.
    """
    r1 = authenticated_client.post('/inventory/audit/start', data={'mode': 'shelf'})
    first_id = r1.headers['Location'].rstrip('/').split('/')[-1]

    r2 = authenticated_client.post('/inventory/audit/start', data={'mode': 'shelf'})
    second_id = r2.headers['Location'].rstrip('/').split('/')[-1]

    assert second_id == first_id            # resumed the same session
    assert _active_count(app) == 1          # no second session created


def test_start_different_mode_blocks_and_keeps_single_session(app, authenticated_client):
    """A different mode while active goes back to the dashboard, no new session."""
    authenticated_client.post('/inventory/audit/start', data={'mode': 'shelf'})

    resp = authenticated_client.post('/inventory/audit/start', data={'mode': 'item'})
    assert resp.status_code == 302
    assert resp.headers['Location'].endswith('/inventory/audit')   # dashboard, not live
    assert _active_count(app) == 1          # still just the original shelf audit


def test_dashboard_disables_start_cards_when_audit_active(app, authenticated_client):
    """With an active audit, the dashboard shows Resume and greys out the cards."""
    authenticated_client.post('/inventory/audit/start', data={'mode': 'shelf'})

    resp = authenticated_client.get('/inventory/audit')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')
    assert 'Resume Audit' in html                       # prominent resume action shown
    assert 'text-white audit-card-disabled' in html     # class applied to the card buttons
    assert '<button type="submit"' in html and 'disabled>' in html


def test_dashboard_enables_start_cards_when_idle(app, authenticated_client):
    """With no active audit, the start cards are selectable and no resume shows."""
    resp = authenticated_client.get('/inventory/audit')
    assert resp.status_code == 200
    html = resp.data.decode('utf-8')
    assert 'Resume Audit' not in html
    assert 'text-white audit-card-disabled' not in html   # cards render without the disabled class
