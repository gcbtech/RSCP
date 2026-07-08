"""
SKU Migration Script
Updates existing SKUs with the new location prefix.
Run this AFTER setting the LOCATION_PREFIX in Admin > Federation.

Usage: python scripts/migrate_skus.py
"""
import os
import sys
import sqlite3
import json

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.data_manager import load_config, BASE_DIR

DB_PATH = os.path.join(BASE_DIR, 'rscp.db')


def migrate_skus():
    """Migrate all inventory SKUs from RSCP-* to XXXX-* format."""
    config = load_config()
    new_prefix = config.get('LOCATION_PREFIX', '').strip().upper()
    
    if not new_prefix or len(new_prefix) != 4:
        print("ERROR: LOCATION_PREFIX not set or invalid in config.")
        print("Please set a 4-character prefix in Admin > Federation first.")
        return False
    
    if new_prefix == 'RSCP':
        print("WARNING: Location prefix is still 'RSCP' (default).")
        confirm = input("Continue anyway? (y/N): ")
        if confirm.lower() != 'y':
            return False
    
    print(f"Migrating SKUs: RSCP-* -> {new_prefix}-*")
    print("=" * 50)
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    try:
        # Find all items with RSCP prefix
        items = conn.execute(
            "SELECT id, sku FROM inventory_items WHERE sku LIKE 'RSCP-%'"
        ).fetchall()
        
        if not items:
            print("No items found with RSCP-* prefix. Nothing to migrate.")
            return True
        
        print(f"Found {len(items)} items to migrate.")
        
        # Preview first 5
        print("\nPreview:")
        for item in items[:5]:
            old_sku = item['sku']
            new_sku = new_prefix + old_sku[4:]  # Replace first 4 chars
            print(f"  {old_sku} -> {new_sku}")
        
        if len(items) > 5:
            print(f"  ... and {len(items) - 5} more")
        
        confirm = input("\nProceed with migration? (y/N): ")
        if confirm.lower() != 'y':
            print("Migration cancelled.")
            return False
        
        # Perform migration
        migrated = 0
        for item in items:
            old_sku = item['sku']
            new_sku = new_prefix + old_sku[4:]
            
            conn.execute(
                "UPDATE inventory_items SET sku = ? WHERE id = ?",
                (new_sku, item['id'])
            )
            migrated += 1
        
        conn.commit()
        print(f"\n[OK] Successfully migrated {migrated} SKUs.")
        
        # Also update any references in transactions
        txn_count = conn.execute(
            "SELECT COUNT(*) FROM inventory_transactions"
        ).fetchone()[0]
        print(f"Note: {txn_count} transaction records remain unchanged (historical data).")
        
        return True
        
    except Exception as e:
        print(f"[ERROR] Migration failed: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


if __name__ == '__main__':
    print("RSCP SKU Migration Tool")
    print("=" * 50)
    print("This script will update all inventory SKUs from")
    print("RSCP-XXX-NNNN format to your new location prefix.")
    print()
    
    success = migrate_skus()
    sys.exit(0 if success else 1)
