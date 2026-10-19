"""
Admin User Management Routes
Handles user CRUD operations, password resets, and admin promotion.
"""
import logging
from flask import request, redirect, url_for, session, flash
from flask_login import current_user
from werkzeug.security import generate_password_hash

from app.routes.admin import admin_bp, require_admin
from app.services.auth import (create_user, delete_user, update_user_password,
                               update_user_admin_status, validate_password)

logger = logging.getLogger(__name__)


@admin_bp.route('/add_user', methods=['POST'])
def add_user_action():
    """Create a new user."""
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
    if username == current_user.username:
        flash("Cannot delete yourself.")
    elif username.lower() == 'admin':
        flash("Cannot delete the built-in Admin account.")
    else:
        flash(_delete_user_checked(username))
    return redirect(url_for('admin.admin_panel', tab='settings'))


# Records that must stay attributed to their user (ON DELETE RESTRICT)
_SALES_HISTORY_REFS = (('pos_orders', 'operator_id'), ('pos_refunds', 'manager_id'),
                       ('pos_coupon_redemptions', 'redeemed_by'),
                       ('drawer_sessions', 'opened_by'), ('drawer_sessions', 'closed_by'))
# Timeclock rows belong to the user and are deleted with the account (ON DELETE CASCADE)
_TIMECLOCK_TABLES = ('time_entries', 'scheduled_shifts', 'recurring_shift_rules',
                     'pto_requests', 'user_pto_balances')


def _delete_user_checked(username):
    """Delete a user unless sales records point at them; returns the message to show."""
    from app.services.db import get_db_connection
    conn = get_db_connection()
    try:
        row = conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
        if not row:
            return f"User {username} not found."
        uid = row['id']
        for table, col in _SALES_HISTORY_REFS:
            if conn.execute(f'SELECT 1 FROM {table} WHERE {col} = ? LIMIT 1', (uid,)).fetchone():
                return (f"User {username} has sales history (sales, refunds or cash drawer sessions), "
                        f"so the account is kept to keep those records attributed. Reset their password "
                        f"and remove their roles instead.")
        timeclock = sum(conn.execute(f'SELECT COUNT(*) FROM {t} WHERE user_id = ?', (uid,)).fetchone()[0]
                        for t in _TIMECLOCK_TABLES)
    finally:
        conn.close()
    delete_user(username)
    check = get_db_connection()
    try:
        gone = check.execute('SELECT 1 FROM users WHERE username = ?', (username,)).fetchone() is None
    finally:
        check.close()
    if not gone:
        return f"User {username} could not be deleted."
    if timeclock:
        return f"User {username} deleted, with their {timeclock} timeclock record(s)."
    return f"User {username} deleted."


@admin_bp.route('/set_user_admin/<username>/<action>', methods=['POST'])
def set_user_admin(username, action):
    """Promote or demote a user's admin status."""
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


# POS PINs and badge IDs: users.pin_hash and users.badge_id (and their data)
# are kept, but nothing reads them in 3.0 and there is no admin UI to set them.
# PIN/badge sign-in and approvals will be rebuilt from scratch.


@admin_bp.route('/set_user_email/<username>', methods=['POST'])
def set_user_email(username):
    """Set or clear the email that Cloudflare Access sign-in matches on.

    Whoever controls this address can sign in as the account, so the same
    rule as password resets applies: not on another admin's account."""
    import re
    from app.services.auth import load_users
    from app.services.db import get_db_connection
    back = redirect(url_for('admin.admin_panel', tab='settings'))

    users = load_users()
    if username not in users:
        flash("User not found.")
        return back
    if users[username].get('is_admin') and username != current_user.username:
        flash("Cannot change another admin's sign-in email. They must set it themselves.")
        return back

    email = request.form.get('email', '').strip().lower()
    if email and (len(email) > 254 or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email)):
        flash("Enter a valid email address.")
        return back

    conn = get_db_connection()
    try:
        if email:
            other = conn.execute('SELECT username FROM users WHERE LOWER(email) = ? AND username != ?',
                                 (email, username)).fetchone()
            if other:
                flash(f"That email is already used by {other['username']}.")
                return back
        conn.execute('UPDATE users SET email = ? WHERE username = ?', (email or None, username))
        conn.commit()
    finally:
        conn.close()
    logger.info(f"Admin: {current_user.username} set the sign-in email of {username} "
                f"to {email or '(none)'}")
    flash(f"Sign-in email {'set' if email else 'cleared'} for {username}.")
    return back


@admin_bp.route('/set_user_roles/<username>', methods=['POST'])
def set_user_roles(username):
    """Set a user's RBAC roles."""
    # Prevent changing your own super_admin status
    if username == current_user.username:
        flash("Cannot change your own roles. Ask another super_admin.")
        return redirect(url_for('admin.admin_panel', tab='settings'))
    
    # Cannot change built-in admin account
    if username.lower() == 'admin':
        flash("Cannot change roles for the built-in Admin account.")
        return redirect(url_for('admin.admin_panel', tab='settings'))
    
    # Valid RBAC roles: the single source is app.utils.permissions.ROLES
    from app.utils.permissions import ROLES
    valid_roles = list(ROLES)
    
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
        
        # Mirror super_admin into the legacy is_admin column (rollback safety)
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
