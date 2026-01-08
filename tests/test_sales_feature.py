import unittest
from datetime import datetime, timedelta
import sys
import os

# Add app to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import MagicMock
sys.modules['authlib'] = MagicMock()
sys.modules['authlib.integrations'] = MagicMock()
sys.modules['authlib.integrations.flask_client'] = MagicMock()

from app import create_app
from app.services.db import get_db_connection

class TestSalesFeature(unittest.TestCase):
    def setUp(self):
        self.app = create_app(test_config={'TESTING': True, 'DATABASE': ':memory:'})
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        
        # Init DB
        from app.services.migration import run_migrations
        run_migrations()
        
        # Create test item
        conn = get_db_connection()
        conn.execute('''
            INSERT INTO inventory_items (sku, name, sell_price, quantity, sale_enabled, sale_price, sale_start, sale_end, sale_end_on_stock)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', ('TEST-SKU', 'Test Item', 10.0, 5, 0, 5.0, None, None, 0))
        conn.commit()
        conn.close()

    def tearDown(self):
        self.ctx.pop()

    def test_get_inventory_item_regular(self):
        """Test item price when sale is disabled."""
        from app.routes.inventory.core import get_inventory_item
        item = get_inventory_item('TEST-SKU')
        self.assertEqual(item['current_price'], 10.0)
        self.assertFalse(item['is_on_sale'])

    def test_get_inventory_item_sale(self):
        """Test item price when sale is active (no dates)."""
        conn = get_db_connection()
        conn.execute('UPDATE inventory_items SET sale_enabled = 1 WHERE sku = ?', ('TEST-SKU',))
        conn.commit()
        conn.close()
        
        from app.routes.inventory.core import get_inventory_item
        item = get_inventory_item('TEST-SKU')
        self.assertEqual(item['current_price'], 5.0)
        self.assertTrue(item['is_on_sale'])

    def test_sale_dates_future(self):
        """Test sale waiting to start."""
        future = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        conn = get_db_connection()
        conn.execute('UPDATE inventory_items SET sale_enabled = 1, sale_start = ? WHERE sku = ?', 
                    (future, 'TEST-SKU'))
        conn.commit()
        conn.close()
        
        from app.routes.inventory.core import get_inventory_item
        item = get_inventory_item('TEST-SKU')
        self.assertEqual(item['current_price'], 10.0) # Should be regular price
        self.assertFalse(item['is_on_sale'])

    def test_sale_dates_active(self):
        """Test currently active date range."""
        past = (datetime.now() - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        future = (datetime.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        conn = get_db_connection()
        conn.execute('UPDATE inventory_items SET sale_enabled = 1, sale_start = ?, sale_end = ? WHERE sku = ?', 
                    (past, future, 'TEST-SKU'))
        conn.commit()
        conn.close()
        
        from app.routes.inventory.core import get_inventory_item
        item = get_inventory_item('TEST-SKU')
        self.assertEqual(item['current_price'], 5.0)
        self.assertTrue(item['is_on_sale'])

if __name__ == '__main__':
    unittest.main()
