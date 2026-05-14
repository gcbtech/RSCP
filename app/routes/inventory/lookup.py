"""
Inventory Lookup Module
Public-facing item lookup by SKU, UPC, or part number.
Designed for both staff and customer use — no edit controls.
"""
import json
import logging
from flask import request, render_template, jsonify
from flask_login import login_required

from app.routes.inventory import inventory_bp
from app.services.db import get_db_connection
from app.routes.inventory.core import get_inventory_item

logger = logging.getLogger(__name__)


@inventory_bp.route('/lookup')
@login_required
def lookup_page():
    """Render the Inventory Lookup page."""
    return render_template('inventory/lookup.html')


@inventory_bp.route('/lookup/search')
@login_required
def lookup_search():
    """API endpoint for inventory lookup. Searches by SKU, UPC, or part number."""
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({'error': 'No search query provided'}), 400

    # First try exact match via get_inventory_item (handles SKU, UPC, part number)
    item = get_inventory_item(query)
    if item:
        # Ensure item is a dict
        if not isinstance(item, dict):
            item = dict(item)

        # Parse secondary_ids for display
        secondary_ids = {}
        try:
            if item.get('secondary_ids'):
                secondary_ids = json.loads(item['secondary_ids']) if isinstance(item['secondary_ids'], str) else item['secondary_ids']
        except (json.JSONDecodeError, TypeError):
            pass

        # Build location (only non-empty fields)
        location_parts = [v for v in [
            item.get('location_area'), item.get('location_aisle'),
            item.get('location_shelf'), item.get('location_bin')
        ] if v and v != 'None']

        return jsonify({
            'found': True,
            'item': {
                'name': item.get('name', ''),
                'sku': item.get('sku', ''),
                'image_url': item.get('image_url', ''),
                'sell_price': item.get('current_price') or item.get('sell_price'),
                'is_on_sale': item.get('is_on_sale', False),
                'regular_price': item.get('regular_price') or item.get('sell_price'),
                'upc': secondary_ids.get('upc', ''),
                'part_number': secondary_ids.get('part_number', ''),
                'quantity': item.get('quantity', 0),
                'alert_threshold': item.get('alert_threshold', 0),
                'location': ' / '.join(location_parts) if location_parts else '',
            }
        })

    # If exact match fails, try a fuzzy name search
    conn = get_db_connection()
    try:
        # Tokenize query for multi-word search (each word must match independently)
        tokens = query.split()
        conditions = []
        params = []
        for token in tokens:
            pattern = f'%{token}%'
            conditions.append('''
                (name LIKE ?
                 OR sku LIKE ?
                 OR secondary_ids LIKE ?)
            ''')
            params.extend([pattern, pattern, pattern])

        where_clause = ' AND '.join(conditions)

        results = conn.execute(f'''
            SELECT id, sku, name, image_url, sell_price, secondary_ids,
                   sale_enabled, sale_price, sale_start, sale_end,
                   quantity, alert_threshold, location_area, location_aisle, location_shelf, location_bin
            FROM inventory_items
            WHERE COALESCE(is_legacy, 0) = 0
            AND {where_clause}
            ORDER BY name
            LIMIT 10
        ''', params).fetchall()

        if results:
            items = []
            from datetime import datetime
            now = datetime.now()
            for row in results:
                row_dict = dict(row)
                secondary_ids = {}
                try:
                    if row_dict.get('secondary_ids'):
                        secondary_ids = json.loads(row_dict['secondary_ids'])
                except (json.JSONDecodeError, TypeError):
                    pass

                # Sale logic
                current_price = row_dict.get('sell_price')
                is_on_sale = False
                if row_dict.get('sale_enabled') and row_dict.get('sale_price'):
                    try:
                        start_valid = True
                        end_valid = True
                        if row_dict.get('sale_start'):
                            start_dt = datetime.strptime(row_dict['sale_start'], '%Y-%m-%dT%H:%M')
                            if now < start_dt:
                                start_valid = False
                        if row_dict.get('sale_end'):
                            end_dt = datetime.strptime(row_dict['sale_end'], '%Y-%m-%dT%H:%M')
                            if now > end_dt:
                                end_valid = False
                        if start_valid and end_valid:
                            current_price = row_dict['sale_price']
                            is_on_sale = True
                    except (ValueError, TypeError):
                        pass

                # Build location (only non-empty fields)
                location_parts = [v for v in [
                    row_dict.get('location_area'), row_dict.get('location_aisle'),
                    row_dict.get('location_shelf'), row_dict.get('location_bin')
                ] if v and v != 'None']

                items.append({
                    'name': row_dict.get('name', ''),
                    'sku': row_dict.get('sku', ''),
                    'image_url': row_dict.get('image_url', ''),
                    'sell_price': current_price,
                    'is_on_sale': is_on_sale,
                    'regular_price': row_dict.get('sell_price'),
                    'upc': secondary_ids.get('upc', ''),
                    'part_number': secondary_ids.get('part_number', ''),
                    'quantity': row_dict.get('quantity', 0),
                    'alert_threshold': row_dict.get('alert_threshold', 0),
                    'location': ' / '.join(location_parts) if location_parts else '',
                })

            return jsonify({
                'found': True,
                'multiple': True,
                'items': items
            })

        return jsonify({'found': False})
    finally:
        conn.close()
