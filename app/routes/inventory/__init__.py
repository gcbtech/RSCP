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

# Import all route modules to register their routes with inventory_bp
from app.routes.inventory import core, items, audit, alerts, api, imports

# Re-export commonly used functions for backward compatibility
from app.routes.inventory.core import is_inventory_enabled, generate_sku, validate_location, get_inventory_stats
