"""
Admin Module - Refactored Package Structure
Routes for admin panel, user management, backups, logs, and updates.
"""
import os
import json
import logging
import datetime
from flask import Blueprint, session, flash, redirect, url_for

# Services
from app.services.db import get_db_connection, DB_PATH
from app.services.auth import load_users, BASE_DIR
from app.services.data_manager import load_config, save_config, MANIFEST_FILE, CONFIG_FILE
from app.services.file_handler import atomic_write

# Create the main admin blueprint
admin_bp = Blueprint('admin', __name__, url_prefix='/admin')
logger = logging.getLogger(__name__)

# --- SHARED HELPERS ---
def save_config_value(key, value):
    """Save a single config value. Uses save_config to properly update cache."""
    # Use save_config which properly handles caching
    save_config({key: value})


def _admin_required_page():
    from flask import request
    flash("Administrator access required. Please login with an Admin account.")
    return redirect(url_for('auth.login', force=True,
                            next=request.url if request.method == 'GET' else None))


# Every /admin route is for administrators (admin.manage = super_admin).
from app.utils.permissions import protect_blueprint, has_permission
protect_blueprint(
    admin_bp, 'admin.manage',
    module_name='Admin',
    json_endpoints={
        'admin.check_update', 'admin.perform_update', 'admin.release_info',
        'admin.rollback_update', 'admin.update_status', 'admin.save_auto_confirm',
        'admin.set_package_quantity', 'admin.set_package_sku', 'admin.set_tracking',
    },
    on_forbidden=_admin_required_page,
)


def require_admin():
    """Deprecated: the blueprint guard above already requires admin.manage.
    Kept so code written against the old pattern keeps working."""
    from flask_login import current_user
    if not has_permission(current_user, 'admin.manage'):
        return "Unauthorized", 403
    return None


# Import all route modules to register their routes with admin_bp
from app.routes.admin import packages
from app.routes.admin import users
from app.routes.admin import uploads
from app.routes.admin import backup
from app.routes.admin import logs
from app.routes.admin import updates
from app.routes.admin import automation
from app.routes.admin import settings

# Re-export functions for backward compatibility (used by tests)
from app.routes.admin.users import validate_password
from app.routes.admin.updates import version_is_newer

