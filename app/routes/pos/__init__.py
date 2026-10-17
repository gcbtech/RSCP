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
from app.routes.pos import coupons
from app.routes.pos import sales_mgmt
from app.routes.pos import pairing
from app.routes.pos import drawer

# Access policy for every /pos route: POS_ENABLED + pos.view. Routes that
# need more carry @require_permission('pos.manage'). The public endpoints
# authenticate themselves (pairing code / bearer token, or are the login and
# logout pages); their paths are also on the global login allowlist in
# app/__init__.py.
from app.utils.permissions import protect_blueprint
protect_blueprint(
    pos_bp, 'pos.view',
    module_flag='POS_ENABLED', module_name='POS',
    public={
        'pos.customer_display',
        'pos.pairing_request_code', 'pos.pairing_status', 'pos.pairing_ack',
        'pos.peripheral_poll', 'pos.peripheral_unpair',
        'pos.legacy_display_cart', 'pos.legacy_display_heartbeat',
        'pos.pos_login', 'pos.pos_logout',
    },
    json_endpoints={
        'pos.verify_manager', 'pos.open_drawer', 'pos.list_ports', 'pos.test_drawer',
        'pos.toggle_coupon', 'pos.delete_coupon',
    },
)

# Re-export commonly used functions for convenience
from app.routes.pos.core import is_pos_enabled, get_tax_rate, get_pos_setting

