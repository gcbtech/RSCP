"""
RBAC Permissions Module
Role-based access control for RSCP.
"""
import functools
from flask import flash, redirect, url_for, request
from flask_login import current_user
import json
import logging

logger = logging.getLogger(__name__)

# =============================================================================
# Role Definitions
# =============================================================================

ROLES = {
    'super_admin': 'Full system access, can manage all users and settings',
    'inventory_admin': 'Full inventory control, can manage inventory settings',
    'pos_admin': 'Full POS control, can manage POS settings',
    'receiving_admin': 'Full receiving/packages control',
    'operator': 'Basic access, can perform daily operations'
}

# =============================================================================
# Permission Map
# =============================================================================

PERMISSIONS = {
    # Inventory permissions
    'inventory.view': ['operator', 'inventory_admin', 'pos_admin', 'receiving_admin', 'super_admin'],
    'inventory.manage': ['inventory_admin', 'super_admin'],
    
    # POS permissions
    'pos.view': ['operator', 'pos_admin', 'super_admin'],
    'pos.manage': ['pos_admin', 'super_admin'],
    
    # Receiving/Packages permissions
    'receiving.view': ['operator', 'receiving_admin', 'super_admin'],
    'receiving.manage': ['receiving_admin', 'super_admin'],
    
    # Timeclock permissions
    'timeclock.view': ['operator', 'timeclock_admin', 'inventory_admin', 'pos_admin', 'receiving_admin', 'super_admin'],
    'timeclock.manage': ['timeclock_admin', 'super_admin'],
    
    # Admin permissions
    'admin.manage': ['super_admin'],
}

# =============================================================================
# Helper Functions
# =============================================================================

def get_user_roles(user):
    """Get roles for a user as a list."""
    if not user or not user.is_authenticated:
        return []
    
    # Get roles from user object
    roles_str = getattr(user, 'roles', None)
    if not roles_str:
        # Fallback: if is_admin, treat as super_admin
        if getattr(user, 'is_admin', False):
            return ['super_admin']
        return ['operator']
    
    try:
        if isinstance(roles_str, list):
            return roles_str
        return json.loads(roles_str)
    except (json.JSONDecodeError, TypeError):
        return ['operator']


def has_permission(user, permission):
    """Check if user has a specific permission."""
    if not user or not user.is_authenticated:
        return False
    
    user_roles = get_user_roles(user)
    
    # Super admin has all permissions
    if 'super_admin' in user_roles:
        return True
    
    # Check if any of the user's roles grant this permission
    allowed_roles = PERMISSIONS.get(permission, [])
    return any(role in allowed_roles for role in user_roles)


def has_role(user, role):
    """Check if user has a specific role."""
    return role in get_user_roles(user)


# =============================================================================
# Decorators
# =============================================================================

def require_permission(permission):
    """Decorator to require a specific permission for a route."""
    def decorator(f):
        @functools.wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                flash('Please log in to access this page.')
                return redirect(url_for('main.login'))
            
            if not has_permission(current_user, permission):
                flash('You do not have permission to access this page.')
                logger.warning(f"Permission denied: {current_user.username} tried to access {permission}")
                # Redirect based on context
                if request.referrer:
                    return redirect(request.referrer)
                return redirect(url_for('main.index'))
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def require_role(role):
    """Decorator to require a specific role for a route."""
    def decorator(f):
        @functools.wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                flash('Please log in to access this page.')
                return redirect(url_for('main.login'))
            
            if not has_role(current_user, role):
                flash(f'You need the {role} role to access this page.')
                if request.referrer:
                    return redirect(request.referrer)
                return redirect(url_for('main.index'))
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator


def require_any_role(*roles):
    """Decorator to require any of the specified roles."""
    def decorator(f):
        @functools.wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                flash('Please log in to access this page.')
                return redirect(url_for('main.login'))
            
            user_roles = get_user_roles(current_user)
            if not any(r in user_roles for r in roles):
                flash('You do not have the required role to access this page.')
                if request.referrer:
                    return redirect(request.referrer)
                return redirect(url_for('main.index'))
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator
