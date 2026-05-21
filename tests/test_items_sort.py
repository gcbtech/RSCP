import pytest
import os
import sys
from datetime import datetime, timedelta

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.db import get_db_connection

@pytest.fixture
def app():
    """Create application for testing with an isolated database."""
    from app import create_app
    
    db_path = os.path.join(os.path.dirname(__file__), 'test_items_sort.db')
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
        
        # Insert items with distinct created_at times (Oldest first, Middle, Newest)
        conn.execute('''
            INSERT INTO inventory_items (sku, name, sell_price, quantity, created_at)
            VALUES (?, ?, ?, ?, ?)
        ''', ('ITEM1', 'Item Oldest', 10.0, 5, '2026-05-21 09:00:00'))
        
        conn.execute('''
            INSERT INTO inventory_items (sku, name, sell_price, quantity, created_at)
            VALUES (?, ?, ?, ?, ?)
        ''', ('ITEM2', 'Item Middle', 15.0, 10, '2026-05-21 09:30:00'))
        
        conn.execute('''
            INSERT INTO inventory_items (sku, name, sell_price, quantity, created_at)
            VALUES (?, ?, ?, ?, ?)
        ''', ('ITEM3', 'Item Newest', 20.0, 15, '2026-05-21 10:00:00'))
        
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

def test_default_sorting_by_name(app, authenticated_client):
    """By default, inventory listing should sort by name ascending."""
    response = authenticated_client.get('/inventory/items')
    assert response.status_code == 200
    
    html = response.data.decode('utf-8')
    # Since name sorting is ascending: "Item Middle" -> "Item Newest" -> "Item Oldest"
    idx_middle = html.find("Item Middle")
    idx_newest = html.find("Item Newest")
    idx_oldest = html.find("Item Oldest")
    
    assert idx_middle != -1
    assert idx_newest != -1
    assert idx_oldest != -1
    assert idx_middle < idx_newest < idx_oldest

def test_sorting_by_date_added_default_descending(app, authenticated_client):
    """Sorting by 'created_at' without order param should default to descending (newest first)."""
    response = authenticated_client.get('/inventory/items?sort=created_at')
    assert response.status_code == 200
    
    html = response.data.decode('utf-8')
    # Descending: "Item Newest" -> "Item Middle" -> "Item Oldest"
    idx_newest = html.find("Item Newest")
    idx_middle = html.find("Item Middle")
    idx_oldest = html.find("Item Oldest")
    
    assert idx_newest != -1
    assert idx_middle != -1
    assert idx_oldest != -1
    assert idx_newest < idx_middle < idx_oldest

def test_sorting_by_date_added_ascending(app, authenticated_client):
    """Sorting by 'created_at' with order=asc should sort ascending (oldest first)."""
    response = authenticated_client.get('/inventory/items?sort=created_at&order=asc')
    assert response.status_code == 200
    
    html = response.data.decode('utf-8')
    # Ascending: "Item Oldest" -> "Item Middle" -> "Item Newest"
    idx_oldest = html.find("Item Oldest")
    idx_middle = html.find("Item Middle")
    idx_newest = html.find("Item Newest")
    
    assert idx_oldest != -1
    assert idx_middle != -1
    assert idx_newest != -1
    assert idx_oldest < idx_middle < idx_newest
