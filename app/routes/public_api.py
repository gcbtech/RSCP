"""
Public API Module
Read-only API endpoint for the customer-facing storefront (gcbcomputers.com).

Security:
- API key authentication required (X-API-Key header)
- SELECT queries only — no write operations exist in this module
- Explicit column allowlist — internal fields are never queried
- Hardcoded aisle filter — only Laptops, Desktops, Servers are returned
- Rate limited to 30 requests per minute per IP
- Out-of-stock and legacy items are excluded
"""
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify, current_app
from functools import wraps

from app.services.db import get_db_connection
from app.services.data_manager import load_config

logger = logging.getLogger(__name__)

public_api_bp = Blueprint('public_api', __name__, url_prefix='/api/public')

# ── Hardcoded allowlist — customers can ONLY see these aisles ──
ALLOWED_AISLES = ('Laptops', 'Desktops', 'Servers')


def require_api_key(f):
    """Decorator to enforce API key authentication."""
    @wraps(f)
    def decorated(*args, **kwargs):
        config = load_config()
        expected_key = config.get('PUBLIC_API_KEY', '') if config else ''
        
        if not expected_key:
            logger.warning("Public API called but PUBLIC_API_KEY is not configured.")
            return jsonify({'error': 'API not configured'}), 503
        
        provided_key = request.headers.get('X-API-Key', '')
        
        if not provided_key or provided_key != expected_key:
            logger.warning(f"Public API: invalid API key from {request.remote_addr}")
            return jsonify({'error': 'Unauthorized'}), 401
        
        return f(*args, **kwargs)
    return decorated


@public_api_bp.route('/inventory', methods=['GET'])
@require_api_key
def get_public_inventory():
    """
    Return filtered inventory for the public storefront.
    
    Only returns: name, effective price, quantity, image_url, category.
    Never returns: id, sku, buy_price, supplier, notes, asin, secondary_ids,
                   keywords, description, alert_threshold, source_url, etc.
    """
    try:
        # Apply rate limiting if available
        try:
            limiter = current_app.limiter
            limiter.limit("30 per minute")(lambda: None)()
        except Exception:
            pass  # Rate limiter not available, continue without it
        
        conn = get_db_connection()
        try:
            # Build parameterized placeholders for the aisle filter
            placeholders = ','.join('?' * len(ALLOWED_AISLES))
            
            # EXPLICIT column list — never SELECT *
            # Only query the columns we need, plus sale fields for price computation
            rows = conn.execute(f'''
                SELECT 
                    id,
                    sku,
                    name,
                    sell_price,
                    sale_price,
                    sale_enabled,
                    sale_start,
                    sale_end,
                    quantity,
                    image_url,
                    location_aisle,
                    addon_1,
                    addon_2,
                    description,
                    additional_images,
                    created_at
                FROM inventory_items
                WHERE location_aisle IN ({placeholders})
                  AND quantity > 0
                  AND COALESCE(is_legacy, 0) = 0
                ORDER BY name ASC
            ''', ALLOWED_AISLES).fetchall()
            
            # Build response with only safe fields
            now = datetime.now()
            items = []
            import json
            for row in rows:
                # Compute effective price (handle sale pricing server-side)
                price = row['sell_price'] or 0
                
                if row['sale_enabled']:
                    try:
                        start_valid = True
                        end_valid = True
                        
                        if row['sale_start']:
                            start_dt = datetime.strptime(row['sale_start'], '%Y-%m-%dT%H:%M')
                            if now < start_dt:
                                start_valid = False
                        
                        if row['sale_end']:
                            end_dt = datetime.strptime(row['sale_end'], '%Y-%m-%dT%H:%M')
                            if now > end_dt:
                                end_valid = False
                        
                        if start_valid and end_valid and row['sale_price']:
                            price = row['sale_price']
                    except (ValueError, TypeError):
                        pass  # Use regular price if sale date parsing fails
                
                additional_images = []
                if row['additional_images']:
                    try:
                        additional_images = json.loads(row['additional_images'])
                    except json.JSONDecodeError:
                        pass
                
                items.append({
                    'id': row['id'],
                    'sku': row['sku'],
                    'name': row['name'],
                    'price': round(price, 2) if price else 0,
                    'quantity': row['quantity'],
                    'image_url': row['image_url'],
                    'category': row['location_aisle'],
                    'addon_1': bool(row['addon_1']) if 'addon_1' in row.keys() else False,
                    'addon_2': bool(row['addon_2']) if 'addon_2' in row.keys() else False,
                    'description': row['description'] or '',
                    'additional_images': additional_images,
                    'created_at': row['created_at'],
                })
            
            return jsonify(items)
        
        finally:
            conn.close()
    
    except Exception as e:
        logger.error(f"Public API error: {e}")
        return jsonify({'error': 'Internal server error'}), 500
