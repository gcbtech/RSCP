"""
Inventory Module - Refactored Package Structure
Routes for inventory management, auditing, and stock tracking.
"""
import logging
from flask import Blueprint

# Create the main inventory blueprint
inventory_bp = Blueprint('inventory', __name__, url_prefix='/inventory')
logger = logging.getLogger(__name__)

# Predefined category codes (shared across modules)
CATEGORY_CODES = {
    'PRI': 'Primary',
    'SEC': 'Secondary',
    'ASC': 'Accessories',
    'ATO': 'Automatic',
}

# Access policy for every /inventory route: INVENTORY_ENABLED + inventory.view.
# Item editing, imports, alerts etc. add @require_permission('inventory.manage').
from app.utils.permissions import protect_blueprint
protect_blueprint(
    inventory_bp, 'inventory.view',
    module_flag='INVENTORY_ENABLED', module_name='Inventory',
    json_endpoints={
        'inventory.audit_scan', 'inventory.audit_submit_count', 'inventory.check_sku_exists',
        'inventory.fetch_image_from_url', 'inventory.generate_sku_api',
        'inventory.lookup_search', 'inventory.upload_photo',
    },
)

# Import all route modules to register their routes with inventory_bp
from app.routes.inventory import core, items, audit, alerts, api, imports, lookup

# Re-export commonly used functions for backward compatibility
from app.routes.inventory.core import is_inventory_enabled, generate_sku, validate_location, get_inventory_stats, get_inventory_item
