"""
Inventory API Module
JSON API endpoints for inventory operations.
Rate limited to 100 requests per minute per IP.
"""
import logging
from flask import request, session, jsonify, current_app

from app.routes.inventory import inventory_bp
from app.routes.inventory.core import is_inventory_enabled
from app.services.db import get_db_connection

logger = logging.getLogger(__name__)

# Rate limit decorator helper - applied to each API route
def get_api_limiter():
    """Get limiter with API rate limit (100 per minute per IP)."""
    try:
        return current_app.limiter.limit("100 per minute")
    except AttributeError:
        return lambda f: f  # No-op if limiter not available


@inventory_bp.route('/api/match/<asin>')
def match_asin(asin):
    """Check if an ASIN exists in inventory, return item if found."""
    if not is_inventory_enabled():
        return jsonify({"enabled": False}), 200
    
    conn = get_db_connection()
    try:
        item = conn.execute('''
            SELECT id, sku, name, quantity, location_area, location_aisle, 
                   location_shelf, location_bin 
            FROM inventory_items 
            WHERE asin = ?
        ''', (asin,)).fetchone()
        
        if item:
            return jsonify({
                "found": True,
                "item": {
                    "id": item['id'],
                    "sku": item['sku'],
                    "name": item['name'],
                    "quantity": item['quantity'],
                    "location": ' / '.join(filter(None, [
                        item['location_area'],
                        item['location_aisle'],
                        item['location_shelf'],
                        item['location_bin']
                    ]))
                }
            })
        else:
            return jsonify({"found": False})
    finally:
        conn.close()


@inventory_bp.route('/api/search')
def search_items():
    """Search inventory items by name, SKU, or UPC (secondary_ids) for autocomplete."""
    query = request.args.get('q', '').strip()
    if len(query) < 2:
        return jsonify([])
    
    conn = get_db_connection()
    try:
        # Search by name, SKU, or secondary_ids (contains UPC, part_number as JSON)
        items = conn.execute('''
            SELECT id, sku, name, quantity, secondary_ids
            FROM inventory_items 
            WHERE name LIKE ? 
               OR sku LIKE ?
               OR secondary_ids LIKE ?
            ORDER BY name 
            LIMIT 10
        ''', (f'%{query}%', f'%{query}%', f'%{query}%')).fetchall()
        
        return jsonify([{
            "id": item['id'],
            "sku": item['sku'],
            "name": item['name'],
            "quantity": item['quantity']
        } for item in items])
    finally:
        conn.close()


@inventory_bp.route('/api/add_quantity/<int:item_id>', methods=['POST'])
def api_add_quantity(item_id):
    """API endpoint to add quantity to existing item (used from scan screen)."""
    if not is_inventory_enabled():
        return jsonify({"error": "Inventory not enabled"}), 400
    
    data = request.get_json() or {}
    quantity = int(data.get('quantity', 1))
    tracking = data.get('tracking', '')
    
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE inventory_items 
            SET quantity = quantity + ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (quantity, item_id))
        
        conn.execute('''
            INSERT INTO inventory_transactions 
            (inventory_item_id, quantity_change, reason, user_id, source_tracking)
            VALUES (?, ?, ?, ?, ?)
        ''', (item_id, quantity, 'Received from Scan', session.get('user'), tracking))
        
        conn.commit()
        
        item = conn.execute('SELECT quantity FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
        
        return jsonify({
            "success": True,
            "new_quantity": item['quantity'] if item else 0
        })
    except Exception as e:
        logger.error(f"Error adding quantity via API: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()


@inventory_bp.route('/api/sku/<sku>')
def lookup_sku(sku):
    """Lookup inventory item by SKU for scan page."""
    if not is_inventory_enabled():
        return jsonify({"enabled": False}), 200
    
    conn = get_db_connection()
    try:
        item = conn.execute('''
            SELECT id, sku, name, quantity, image_url, sell_price,
                   location_area, location_aisle, location_shelf, location_bin
            FROM inventory_items 
            WHERE sku = ?
        ''', (sku,)).fetchone()
        
        if item:
            return jsonify({
                "found": True,
                "item": {
                    "id": item['id'],
                    "sku": item['sku'],
                    "name": item['name'],
                    "quantity": item['quantity'],
                    "image_url": item['image_url'],
                    "sell_price": item['sell_price'],
                    "location": ' / '.join(filter(None, [
                        item['location_area'],
                        item['location_aisle'],
                        item['location_shelf'],
                        item['location_bin']
                    ]))
                }
            })
        else:
            return jsonify({"found": False})
    finally:
        conn.close()


@inventory_bp.route('/api/quick_adjust/<int:item_id>', methods=['POST'])
def quick_adjust(item_id):
    """Quick quantity adjustment from scan page (supports -1, +1, custom, OOS)."""
    if not is_inventory_enabled():
        return jsonify({"error": "Inventory not enabled"}), 400
    
    data = request.get_json() or {}
    change = int(data.get('change', 0))
    action = data.get('action', '')  # 'oos' for mark out of stock
    
    conn = get_db_connection()
    try:
        current = conn.execute('SELECT quantity, name, sku FROM inventory_items WHERE id = ?', 
                               (item_id,)).fetchone()
        if not current:
            return jsonify({"error": "Item not found"}), 404
        
        if action == 'oos':
            new_qty = 0
            reason = "Marked OOS from Scan"
            change = -current['quantity']
        else:
            new_qty = current['quantity'] + change
            if new_qty < 0:
                return jsonify({
                    "error": f"Cannot reduce by {abs(change)} - only {current['quantity']} in stock"
                }), 400
            reason = "Sold/Consumed" if change < 0 else "Received from Scan"
        
        conn.execute('''
            UPDATE inventory_items 
            SET quantity = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (new_qty, item_id))
        
        if change != 0:
            conn.execute('''
                INSERT INTO inventory_transactions 
                (inventory_item_id, quantity_change, reason, user_id)
                VALUES (?, ?, ?, ?)
            ''', (item_id, change, reason, session.get('user')))
        
        conn.commit()
        
        return jsonify({
            "success": True,
            "new_quantity": new_qty,
            "name": current['name']
        })
    except Exception as e:
        logger.error(f"Quick adjust error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()
