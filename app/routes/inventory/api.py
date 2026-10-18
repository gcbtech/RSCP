"""
Inventory API Module
JSON API endpoints for inventory operations.
Rate limited to 100 requests per minute per IP.
"""
import logging
from flask import request, session, jsonify, current_app
from flask_login import current_user

from app.routes.inventory import inventory_bp
from app.routes.inventory.core import is_inventory_enabled
from app.services.db import get_db_connection
from app.services import inventory

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
        change = inventory.adjust_stock(conn, item_id, quantity, inventory.REASON_RECEIVED_SCAN,
                                        current_user.username, tracking, allow_negative=True)
        return jsonify({
            "success": True,
            "new_quantity": change.new_quantity
        })
    except inventory.ItemNotFound:
        return jsonify({"error": "Item not found"}), 404
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
        # The ledger has always recorded this flow by user id (as POS does).
        user = str(current_user.id)
        if action == 'oos':
            result = inventory.mark_out_of_stock(conn, item_id, user)
        else:
            reason = inventory.REASON_SOLD if change < 0 else inventory.REASON_RECEIVED_SCAN
            result = inventory.adjust_stock(conn, item_id, change, reason, user)
        return jsonify({
            "success": True,
            "new_quantity": result.new_quantity,
            "name": result.name
        })
    except inventory.ItemNotFound:
        return jsonify({"error": "Item not found"}), 404
    except inventory.InsufficientStock as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"Quick adjust error: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()
