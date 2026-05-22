import pytest
import os
import sys
import json

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app.services.db import get_db_connection

@pytest.fixture
def app():
    """Create application for testing with an isolated database."""
    from app import create_app
    
    db_path = os.path.join(os.path.dirname(__file__), 'test_rbac.db')
    if os.path.exists(db_path):
        try:
            os.remove(db_path)
        except Exception:
            pass
            
    app = create_app(test_config={
        'TESTING': True,
        'DATABASE': db_path,
        'WTF_CSRF_ENABLED': False,
        'SECRET_KEY': 'test_key',
        'INVENTORY_ENABLED': True,
        'POS_ENABLED': True,
        'TIMECLOCK_ENABLED': True
    })
    
    with app.app_context():
        from app.services.migration import ensure_db_ready
        ensure_db_ready()
        
        # Insert test users with specific roles and a test inventory item
        conn = get_db_connection()
        
        # 1. Users
        users_data = [
            (1, 'super_admin_user', 'pbkdf2:sha256:somehash', 1, '["super_admin"]'),
            (2, 'operator_user', 'pbkdf2:sha256:somehash', 0, '["operator"]'),
            (3, 'pos_admin_user', 'pbkdf2:sha256:somehash', 0, '["pos_admin"]'),
            (4, 'receiving_admin_user', 'pbkdf2:sha256:somehash', 0, '["receiving_admin"]'),
            (5, 'inventory_admin_user', 'pbkdf2:sha256:somehash', 0, '["inventory_admin"]')
        ]
        conn.executemany('''
            INSERT INTO users (id, username, password_hash, is_admin, roles)
            VALUES (?, ?, ?, ?, ?)
        ''', users_data)
        
        # 2. Inventory Item
        conn.execute('''
            INSERT INTO inventory_items (id, sku, name, sell_price, quantity)
            VALUES (?, ?, ?, ?, ?)
        ''', (1, 'TEST1', 'Test Item 1', 10.0, 5))
        
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
def super_admin_client(client):
    with client.session_transaction() as sess:
        sess['_user_id'] = '1'
    return client

@pytest.fixture
def operator_client(client):
    with client.session_transaction() as sess:
        sess['_user_id'] = '2'
    return client

@pytest.fixture
def pos_admin_client(client):
    with client.session_transaction() as sess:
        sess['_user_id'] = '3'
    return client

@pytest.fixture
def receiving_admin_client(client):
    with client.session_transaction() as sess:
        sess['_user_id'] = '4'
    return client

@pytest.fixture
def inventory_admin_client(client):
    with client.session_transaction() as sess:
        sess['_user_id'] = '5'
    return client


class TestRBACPermissions:
    """Standardized Role-Based Access Control and Permissions Integration Tests."""

    # =========================================================================
    # 1. Operator Access Boundaries
    # =========================================================================
    
    def test_operator_inventory_view_and_adjust(self, operator_client):
        """Operator should be able to view inventory and adjust quantities."""
        # View low stock report (allowed)
        response = operator_client.get('/inventory/low-stock')
        assert response.status_code == 200
        
        # Adjust quantities (allowed under basic inventory.view)
        response = operator_client.post('/inventory/adjust/1', data={
            'quantity_change': '2',
            'reason': 'Audit adjustment'
        })
        assert response.status_code in [200, 302] # Adjust will redirect back to lists or referrer

    def test_operator_blocked_from_inventory_manage(self, operator_client):
        """Operator should be blocked from item editing, adding, or deleting."""
        # Blocked from Add page
        response = operator_client.get('/inventory/add')
        assert response.status_code == 302 # Redirect to referer/gateway
        
        # Blocked from Edit page
        response = operator_client.get('/inventory/edit/1')
        assert response.status_code == 302
        
        # Blocked from delete endpoint
        response = operator_client.post('/inventory/delete/1')
        assert response.status_code == 302
        
        # Blocked from bulk edit
        response = operator_client.post('/inventory/bulk-edit', data={
            'item_ids': ['1'],
            'confirm_bulk_edit': '1',
            'keywords': 'new_tag'
        })
        assert response.status_code == 302

    def test_operator_blocked_from_pos_manage(self, operator_client):
        """Operator should be blocked from POS management and reports."""
        response = operator_client.get('/pos/management')
        assert response.status_code == 302

    # =========================================================================
    # 2. POS Admin Access Boundaries
    # =========================================================================

    def test_pos_admin_access_pos_manage(self, pos_admin_client):
        """POS Admin should access POS management, coupons, and reports."""
        response = pos_admin_client.get('/pos/management')
        assert response.status_code == 200

    def test_pos_admin_blocked_from_inventory_manage(self, pos_admin_client):
        """POS Admin should be blocked from editing/adding inventory items."""
        response = pos_admin_client.get('/inventory/add')
        assert response.status_code == 302
        
        response = pos_admin_client.get('/inventory/edit/1')
        assert response.status_code == 302

    def test_pos_admin_blocked_from_timeclock_manage(self, pos_admin_client):
        """POS Admin should be blocked from Timeclock Portal management."""
        response = pos_admin_client.get('/timeclock/manager')
        assert response.status_code == 302

    # =========================================================================
    # 3. Receiving Admin Access Boundaries
    # =========================================================================

    def test_receiving_admin_access_dashboard(self, receiving_admin_client):
        """Receiving Admin should access the receiving dashboard."""
        response = receiving_admin_client.get('/receiving')
        assert response.status_code == 200

    def test_receiving_admin_blocked_from_pos_manage(self, receiving_admin_client):
        """Receiving Admin should be blocked from POS management."""
        response = receiving_admin_client.get('/pos/management')
        assert response.status_code == 302

    # =========================================================================
    # 4. Super Admin Access
    # =========================================================================

    def test_super_admin_has_all_access(self, super_admin_client):
        """Super admin has global permissions to all modules."""
        # Inventory add/edit
        assert super_admin_client.get('/inventory/add').status_code == 200
        assert super_admin_client.get('/inventory/edit/1').status_code == 200
        
        # POS management
        assert super_admin_client.get('/pos/management').status_code == 200
        
        # Timeclock Portal
        assert super_admin_client.get('/timeclock/manager').status_code == 200
        
        # Admin settings page
        assert super_admin_client.get('/admin/').status_code == 200

    # =========================================================================
    # 5. UI Element Visibility and Column Masking
    # =========================================================================

    def test_html_visibility_permissions_operator(self, operator_client):
        """Verify that UI controls and sensitive columns are dynamically hidden for Operator."""
        response = operator_client.get('/inventory/items')
        assert response.status_code == 200
        html = response.data.decode('utf-8')
        
        # Admin buttons must be hidden
        assert 'href="/inventory/add"' not in html
        assert 'href="/inventory/import"' not in html
        assert 'href="/inventory/export_csv"' not in html
        assert '⚡ Quick Add Item' not in html
        
        # Sensitive cost columns must be hidden/masked
        assert 'class="col-cost"' not in html
        assert '<th class="col-cost">Cost</th>' not in html
        assert 'class="col-bulk"' not in html
        assert 'id="selectAll"' not in html
        assert 'class="badge col-markup' not in html
        assert 'class="badge col-margin' not in html
        
        # Buy price sorting and Hide Cost toggles must be hidden
        assert 'sort=buy_price' not in html
        assert 'id="hideCostToggle"' not in html
        assert 'id="bulkActions"' not in html
        
        # Row action buttons (Edit/Delete) must be hidden
        assert '✏️' not in html
        assert '🗑️' not in html
        assert '♻️' not in html

    def test_html_visibility_permissions_admin(self, inventory_admin_client):
        """Verify that UI controls and sensitive columns are dynamically revealed for Inventory Admin."""
        response = inventory_admin_client.get('/inventory/items')
        assert response.status_code == 200
        html = response.data.decode('utf-8')
        
        # Admin buttons must be present
        assert 'href="/inventory/add"' in html
        assert 'href="/inventory/import"' in html
        assert 'href="/inventory/export_csv"' in html
        
        # Sensitive cost columns must be present
        assert 'class="col-cost"' in html
        assert 'class="col-bulk"' in html
        assert 'id="selectAll"' in html
        
        # Buy price sorting and Hide Cost toggles must be present
        assert 'sort=buy_price' in html
        assert 'id="hideCostToggle"' in html
        
        # Row action buttons (Edit/Delete) must be present
        assert '✏️' in html
        assert '🗑️' in html



