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
    
    db_path = os.path.join(os.path.dirname(__file__), 'test_bulk_edit.db')
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
        from app.services.migration import ensure_db_ready
        ensure_db_ready()
        
        # Insert test items and user
        conn = get_db_connection()
        conn.execute('''
            INSERT INTO users (id, username, password_hash, is_admin)
            VALUES (?, ?, ?, ?)
        ''', (1, 'testuser', 'pbkdf2:sha256:somehash', 1))
        conn.execute('''
            INSERT INTO inventory_items (sku, name, sell_price, quantity, keywords)
            VALUES (?, ?, ?, ?, ?)
        ''', ('BULK1', 'Bulk Item 1', 10.0, 5, 'old1, old2'))
        conn.execute('''
            INSERT INTO inventory_items (sku, name, sell_price, quantity, keywords)
            VALUES (?, ?, ?, ?, ?)
        ''', ('BULK2', 'Bulk Item 2', 15.0, 10, 'old3'))
        conn.commit()
        conn.close()
        
    yield app
    
    # Cleanup DB
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except Exception:
            pass

@pytest.fixture
def client(app):
    """Create test client."""
    return app.test_client()

@pytest.fixture
def authenticated_client(app, client):
    """Create authenticated client for testing."""
    with client.session_transaction() as sess:
        sess['_user_id'] = '1'  # Flask-Login session key
        sess['user'] = 1
    return client

def test_bulk_edit_keywords(app, authenticated_client):
    """Test that keywords are successfully updated via bulk edit."""
    with app.app_context():
        conn = get_db_connection()
        item1 = conn.execute("SELECT id FROM inventory_items WHERE sku = 'BULK1'").fetchone()
        item2 = conn.execute("SELECT id FROM inventory_items WHERE sku = 'BULK2'").fetchone()
        conn.close()
        
    # Send bulk edit request
    response = authenticated_client.post('/inventory/bulk-edit', data={
        'item_ids': [str(item1['id']), str(item2['id'])],
        'confirm_bulk_edit': '1',
        'keywords': 'new_bulk_tag, promotional'
    }, follow_redirects=True)
    
    assert response.status_code == 200
    
    # Verify keywords in database
    with app.app_context():
        conn = get_db_connection()
        updated_item1 = conn.execute("SELECT keywords FROM inventory_items WHERE sku = 'BULK1'").fetchone()
        updated_item2 = conn.execute("SELECT keywords FROM inventory_items WHERE sku = 'BULK2'").fetchone()
        conn.close()
        
    assert updated_item1['keywords'] == 'new_bulk_tag, promotional'
    assert updated_item2['keywords'] == 'new_bulk_tag, promotional'
