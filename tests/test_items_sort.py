import pytest
import os
import sys
from datetime import datetime, timedelta

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.environ.get('RSCP_DATA_DIR') or os.path.dirname(__file__), '..')))

from app.services.db import get_db_connection

@pytest.fixture
def app():
    """Create application for testing with an isolated database."""
    from app import create_app
    
    db_path = os.path.join(os.environ.get('RSCP_DATA_DIR') or os.path.dirname(__file__), 'test_items_sort.db')
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

        # Item whose search term ('arduino') lives ONLY in keywords, not in
        # name/sku/location/secondary_ids. Regression guard for keyword search.
        conn.execute('''
            INSERT INTO inventory_items (sku, name, sell_price, quantity, keywords, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', ('KW1', 'Blue Widget', 5.0, 3, 'arduino, raspberry pi', '2026-05-21 11:00:00'))

        # Three items sharing location_area='Retail' but differing sub-location.
        # Two are in the SAME full location (Retail/Slatwall) and must group
        # together once the sort honors the full area->aisle->shelf->bin
        # hierarchy. Insertion order is deliberately scrambled so an area-only
        # sort would interleave the Pegboard item between the two Slatwall ones.
        conn.execute('''
            INSERT INTO inventory_items (sku, name, quantity, location_area, location_aisle, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', ('LOC1', 'Zeta Part', 1, 'Retail', 'Slatwall', '2026-05-21 12:00:00'))
        conn.execute('''
            INSERT INTO inventory_items (sku, name, quantity, location_area, location_aisle, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', ('LOC2', 'Yotta Part', 1, 'Retail', 'Pegboard', '2026-05-21 12:01:00'))
        conn.execute('''
            INSERT INTO inventory_items (sku, name, quantity, location_area, location_aisle, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', ('LOC3', 'Xray Part', 1, 'Retail', 'Slatwall', '2026-05-21 12:02:00'))

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


def test_search_matches_keywords_only(app, authenticated_client):
    """Searching a term present only in an item's keywords should find it.

    'arduino' appears solely in the keywords column of 'Blue Widget'. It must
    match, and items without that term (e.g. 'Item Oldest') must be excluded.
    """
    response = authenticated_client.get('/inventory/items?q=arduino')
    assert response.status_code == 200

    html = response.data.decode('utf-8')
    assert "Blue Widget" in html          # matched via keywords
    assert "Item Oldest" not in html      # correctly filtered out


def test_sorting_by_location_groups_full_hierarchy(app, authenticated_client):
    """Sorting by location must order on area->aisle->shelf->bin, not area alone.

    'Xray Part' and 'Zeta Part' share the exact location (Retail/Slatwall) and
    must be adjacent, with the Retail/Pegboard 'Yotta Part' sorting before both.
    Under the old area-only sort the Pegboard item interleaved between them.
    """
    response = authenticated_client.get('/inventory/items?sort=location_area&order=asc')
    assert response.status_code == 200

    html = response.data.decode('utf-8')
    idx_yotta = html.find("Yotta Part")   # Retail / Pegboard
    idx_xray = html.find("Xray Part")     # Retail / Slatwall
    idx_zeta = html.find("Zeta Part")     # Retail / Slatwall

    assert idx_yotta != -1
    assert idx_xray != -1
    assert idx_zeta != -1
    # Pegboard before both Slatwall items; the two Slatwall items adjacent
    # (name tiebreaker puts Xray before Zeta).
    assert idx_yotta < idx_xray < idx_zeta
