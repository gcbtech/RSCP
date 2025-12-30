"""
Admin User Management Routes
Handles user CRUD operations, password resets, and admin promotion.
"""
import logging
from flask import request, redirect, url_for, session, flash
from flask_login import current_user
from werkzeug.security import generate_password_hash

from app.routes.admin import admin_bp, require_admin
from app.services.auth import create_user, delete_user, update_user_password, update_user_admin_status

logger = logging.getLogger(__name__)


def validate_password(password: str) -> tuple[bool, str]:
    """Validate password meets complexity requirements.
    Returns (is_valid, error_message).
    """
    if len(password) < 8:
        return False, "Password must be at least 8 characters long."
    if not any(c.isupper() for c in password):
        return False, "Password must contain at least one uppercase letter."
    if not any(c.isdigit() or not c.isalnum() for c in password):
        return False, "Password must contain at least one number or symbol."
    return True, ""


@admin_bp.route('/add_user', methods=['POST'])
def add_user_action():
    """Create a new user."""
    error = require_admin()
    if error:
        return error
    
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    
    if not username or not password:
        flash("Username and Password required.")
        return redirect(url_for('admin.admin_panel', tab='settings'))
    
    # Validate password complexity
    is_valid, error_msg = validate_password(password)
    if not is_valid:
        flash(error_msg)
        return redirect(url_for('admin.admin_panel', tab='settings'))
    
    try:
        pw_hash = generate_password_hash(password)
        create_user(username, pw_hash, is_admin=False)
        flash(f"User {username} created.")

    except Exception as e:
        logger.error(f"Add User Error: {e}")
        flash(f"System error adding user: {e}")
        
    return redirect(url_for('admin.admin_panel', tab='settings'))


@admin_bp.route('/reset_password/<username>', methods=['POST'])
def reset_password_action(username):
    """Reset a user's password."""
    error = require_admin()
    if error:
        return error
    
    # Cannot reset the built-in Admin account password (by anyone, including self)
    if username.lower() == 'admin':
        flash("Cannot reset the built-in Admin account password.")
        return redirect(url_for('admin.admin_panel', tab='settings'))
    
    # Check if target user is an admin (but allow resetting your OWN password)
    from app.services.auth import load_users
    users = load_users()
    target_user = users.get(username, {})
    is_self = (username == current_user.username)
    
    if target_user.get('is_admin', False) and not is_self:
        flash("Cannot reset another admin's password. They must reset it themselves.")
        return redirect(url_for('admin.admin_panel', tab='settings'))
    
    new_pass = request.form.get('new_password', '').strip()
    if not new_pass:
        flash("Password cannot be empty.")
        return redirect(url_for('admin.admin_panel', tab='settings'))
    
    # Validate password complexity
    is_valid, error_msg = validate_password(new_pass)
    if not is_valid:
        flash(error_msg)
        return redirect(url_for('admin.admin_panel', tab='settings'))
        
    try:
        pw_hash = generate_password_hash(new_pass)
        update_user_password(username, pw_hash)
        flash(f"Password for {username} updated.")
    except Exception as e:
        logger.error(f"Reset Password Error: {e}")
        flash(f"Error resetting password: {e}")
        
    return redirect(url_for('admin.admin_panel', tab='settings'))


@admin_bp.route('/delete_user/<username>', methods=['POST'])
def delete_user_action(username):
    """Delete a user."""
    error = require_admin()
    if error:
        return error
    
    if username == session.get('user'):
        flash("Cannot delete yourself.")
    elif username.lower() == 'admin':
        flash("Cannot delete the built-in Admin account.")
    else:
        delete_user(username)
        flash(f"User {username} deleted.")
    return redirect(url_for('admin.admin_panel', tab='settings'))


@admin_bp.route('/set_user_admin/<username>/<action>', methods=['POST'])
def set_user_admin(username, action):
    """Promote or demote a user's admin status."""
    error = require_admin()
    if error:
        return error
    
    if username == current_user.username:
        flash("Cannot change your own admin status.")
        return redirect(url_for('admin.admin_panel', tab='settings'))
    
    if username.lower() == 'admin' and action == 'demote':
        flash("Cannot demote the built-in Admin account.")
        return redirect(url_for('admin.admin_panel', tab='settings'))

    is_admin = True if action == 'promote' else False
    update_user_admin_status(username, is_admin)
    flash(f"User {username} {'promoted to Admin' if is_admin else 'demoted from Admin'}.")
    
    return redirect(url_for('admin.admin_panel', tab='settings'))


@admin_bp.route('/reset_users')
def reset_users_route():
    """Recovery/debug route."""
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/set_user_pin/<username>', methods=['POST'])
def set_user_pin(username):
    """Set or update a user's POS PIN."""
    error = require_admin()
    if error:
        return error
    
    pin = request.form.get('pin', '').strip()
    
    if not pin:
        flash("PIN cannot be empty.")
        return redirect(url_for('admin.admin_panel', tab='settings'))
    
    if not pin.isdigit() or len(pin) < 4 or len(pin) > 6:
        flash("PIN must be 4-6 digits.")
        return redirect(url_for('admin.admin_panel', tab='settings'))
    
    try:
        from app.services.db import get_db_connection
        pin_hash = generate_password_hash(pin)
        conn = get_db_connection()
        conn.execute('UPDATE users SET pin_hash = ? WHERE username = ?', (pin_hash, username))
        conn.commit()
        conn.close()
        flash(f"PIN set for {username}.")
    except Exception as e:
        logger.error(f"Set PIN Error: {e}")
        flash(f"Error setting PIN: {e}")
    
    return redirect(url_for('admin.admin_panel', tab='settings'))


@admin_bp.route('/set_badge_id/<username>', methods=['POST'])
def set_badge_id(username):
    """Set or update a user's badge ID for POS login."""
    error = require_admin()
    if error:
        return error
    
    badge_id = request.form.get('badge_id', '').strip()
    
    try:
        from app.services.db import get_db_connection
        conn = get_db_connection()
        
        # Check for duplicate badge ID (if provided)
        if badge_id:
            existing = conn.execute(
                'SELECT username FROM users WHERE badge_id = ? AND username != ?', 
                (badge_id, username)
            ).fetchone()
            if existing:
                flash(f"Badge ID already assigned to {existing['username']}.")
                conn.close()
                return redirect(url_for('admin.admin_panel', tab='settings'))
        
        conn.execute('UPDATE users SET badge_id = ? WHERE username = ?', 
                     (badge_id if badge_id else None, username))
        conn.commit()
        conn.close()
        
        if badge_id:
            flash(f"Badge ID set for {username}.")
        else:
            flash(f"Badge ID cleared for {username}.")
    except Exception as e:
        logger.error(f"Set Badge ID Error: {e}")
        flash(f"Error setting Badge ID: {e}")
    
    return redirect(url_for('admin.admin_panel', tab='settings'))


@admin_bp.route('/set_user_roles/<username>', methods=['POST'])
def set_user_roles(username):
    """Set a user's RBAC roles."""
    error = require_admin()
    if error:
        return error
    
    # Prevent changing your own super_admin status
    if username == current_user.username:
        flash("Cannot change your own roles. Ask another super_admin.")
        return redirect(url_for('admin.admin_panel', tab='settings'))
    
    # Cannot change built-in admin account
    if username.lower() == 'admin':
        flash("Cannot change roles for the built-in Admin account.")
        return redirect(url_for('admin.admin_panel', tab='settings'))
    
    # Valid RBAC roles
    valid_roles = ['super_admin', 'inventory_admin', 'pos_admin', 'receiving_admin', 'operator']
    
    # Get selected roles from form (checkboxes)
    roles = []
    for role in valid_roles:
        if request.form.get(f'role_{role}'):
            roles.append(role)
    
    # Ensure at least operator role
    if not roles:
        roles = ['operator']
    
    # If has any admin role, don't need operator
    admin_roles = [r for r in roles if r.endswith('_admin')]
    if admin_roles and 'operator' in roles:
        roles.remove('operator')
    
    try:
        import json
        from app.services.db import get_db_connection
        
        conn = get_db_connection()
        
        # Also update is_admin flag for backwards compatibility
        is_admin = 'super_admin' in roles
        conn.execute('UPDATE users SET roles = ?, is_admin = ? WHERE username = ?', 
                     (json.dumps(roles), 1 if is_admin else 0, username))
        conn.commit()
        conn.close()
        
        flash(f"Roles for {username} updated: {', '.join(roles)}")
    except Exception as e:
        logger.error(f"Set Roles Error: {e}")
        flash(f"Error setting roles: {e}")
    
    return redirect(url_for('admin.admin_panel', tab='settings'))


def get_user_roles(username):
    """Get a user's roles as a list."""
    import json
    from app.services.db import get_db_connection
    
    try:
        conn = get_db_connection()
        result = conn.execute('SELECT roles FROM users WHERE username = ?', (username,)).fetchone()
        conn.close()
        
        if result and result['roles']:
            return json.loads(result['roles'])
        return []
    except Exception as e:
        logger.error(f"Get Roles Error: {e}")
        return []


def user_has_role(username, role):
    """Check if a user has a specific role."""
    roles = get_user_roles(username)
    return role in roles
