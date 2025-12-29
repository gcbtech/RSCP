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
from app.services.data_manager import load_config, MANIFEST_FILE, CONFIG_FILE
from app.services.file_handler import atomic_write

# Create the main admin blueprint
admin_bp = Blueprint('admin', __name__, url_prefix='/admin')
logger = logging.getLogger(__name__)

# --- SHARED HELPERS ---
def save_config_value(key, value):
    """Save a single config value."""
    conf = load_config() or {}
    conf[key] = value
    try:
        with atomic_write(CONFIG_FILE, 'w') as f:
            json.dump(conf, f, indent=4)
    except Exception as e:
        logger.error(f"Save config error: {e}")


def require_admin():
    """Check if current user is admin, return error response if not."""
    from flask_login import current_user
    if not current_user.is_authenticated or not current_user.is_admin:
        return "Unauthorized", 403
    return None


# Import all route modules to register their routes with admin_bp
from app.routes.admin import packages
from app.routes.admin import users
from app.routes.admin import uploads
from app.routes.admin import backup
from app.routes.admin import logs
from app.routes.admin import updates

# Re-export functions for backward compatibility (used by tests)
from app.routes.admin.users import validate_password
from app.routes.admin.updates import version_is_newer

