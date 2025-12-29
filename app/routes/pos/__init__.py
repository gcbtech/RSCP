"""
POS Module - Point of Sale System
Routes for sales transactions, refunds, and management analytics.
"""
import logging
from flask import Blueprint

logger = logging.getLogger(__name__)

# Create the main POS blueprint BEFORE importing submodules
pos_bp = Blueprint('pos', __name__, url_prefix='/pos')

# Refund reason options
REFUND_REASONS = {
    'defective': 'Defective Product',
    'wrong_item': 'Wrong Item Received',
    'customer_changed_mind': 'Customer Changed Mind',
    'duplicate_charge': 'Duplicate Charge',
    'price_adjustment': 'Price Adjustment',
    'other': 'Other',
}

# Payment method options
PAYMENT_METHODS = {
    'cash': 'Cash',
    'card': 'Card',
    'split': 'Split Payment',
}

# Import submodules AFTER pos_bp is created to avoid circular imports
# These imports register routes with pos_bp
from app.routes.pos import core
from app.routes.pos import sales
from app.routes.pos import checkout
from app.routes.pos import refunds
from app.routes.pos import management
from app.routes.pos import api
from app.routes.pos import auth as pos_auth

# Re-export commonly used functions for convenience
from app.routes.pos.core import is_pos_enabled, get_tax_rate, get_pos_setting

