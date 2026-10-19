from flask_login import UserMixin
import logging
from app.services.db import get_db_connection, BASE_DIR

logger = logging.getLogger(__name__)

# --- Shim Functions for Legacy Compatibility (main.py) ---
def load_users():
    """Legacy function used by main.py login route."""
    import json
    conn = get_db_connection()
    try:
        from app.utils.permissions import normalize_roles
        rows = conn.execute("SELECT * FROM users").fetchall()
        users = {}
        for r in rows:
            roles = normalize_roles(r['roles'] if 'roles' in r.keys() else None)
            # users.pin_hash / badge_id are deliberately not exposed: nothing
            # uses them until PIN/badge sign-in is rebuilt.
            users[r['username']] = {
                "password_hash": r['password_hash'],
                # Derived from roles; the users.is_admin column is only a
                # mirror kept for rollback to pre-3.0 code.
                "is_admin": 'super_admin' in roles,
                "roles": roles,
                "email": r['email'] if 'email' in r.keys() else None,
            }
        return users
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error loading users: {e}")
        return {}
    finally:
        conn.close()

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


def create_first_admin(username, password_hash):
    """Create the initial admin account, only if no user exists yet.

    The emptiness check and the insert happen under one write lock
    (BEGIN IMMEDIATE), so two concurrent setup submissions cannot both
    create an admin. Returns True if this call created the account.
    """
    conn = get_db_connection()
    try:
        conn.isolation_level = None
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT COUNT(*) AS c FROM users').fetchone()['c'] > 0:
            conn.execute('ROLLBACK')
            return False
        conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, roles) VALUES (?, ?, 1, '[\"super_admin\"]')",
            (username, password_hash))
        conn.execute('COMMIT')
        return True
    except Exception:
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        raise
    finally:
        conn.close()


def _roles_with_admin(roles, is_admin):
    """`roles` with super_admin added or removed; the is_admin column mirrors it."""
    import json
    from app.utils.permissions import normalize_roles
    roles = [r for r in normalize_roles(roles) if r != 'super_admin']
    if is_admin:
        roles.append('super_admin')
    return json.dumps(normalize_roles(roles)), 1 if is_admin else 0


def create_user(username, password_hash, is_admin=False):
    """Create a staff account (operator), or an administrator (super_admin)."""
    roles_json, admin_flag = _roles_with_admin(None, is_admin)
    conn = get_db_connection()
    try:
        conn.execute("INSERT INTO users (username, password_hash, is_admin, roles) VALUES (?, ?, ?, ?)",
                    (username, password_hash, admin_flag, roles_json))
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to create user '{username}': {e}")
    finally:
        conn.close()

def delete_user(username):
    """Legacy function used by admin.py."""
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM users WHERE username = ?", (username,))
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to delete user '{username}': {e}")
    finally:
        conn.close()

def update_user_password(username, password_hash):
    """Legacy function used by admin.py."""
    conn = get_db_connection()
    try:
        conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (password_hash, username))
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to update password for user '{username}': {e}")
    finally:
        conn.close()

def update_user_admin_status(username, is_admin):
    """Grant or revoke the super_admin role (and mirror it into is_admin)."""
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT roles FROM users WHERE username = ?", (username,)).fetchone()
        if not row:
            return
        roles_json, admin_flag = _roles_with_admin(row['roles'], is_admin)
        conn.execute("UPDATE users SET roles = ?, is_admin = ? WHERE username = ?",
                     (roles_json, admin_flag, username))
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to update admin status for user '{username}': {e}")
    finally:
        conn.close()
# -------------------------------------------------------

class User(UserMixin):
    def __init__(self, id, username, is_admin=None, roles=None, email=None):
        # `is_admin` is accepted for call compatibility but ignored: admin
        # status is derived from roles (super_admin), never stored apart.
        from app.utils.permissions import normalize_roles
        self.id = str(id)
        self.username = username
        self._roles = normalize_roles(roles)
        self.email = email

    @property
    def roles(self):
        return list(self._roles)

    @property
    def is_admin(self):
        """Read-only: True for the super_admin role."""
        return 'super_admin' in self._roles

    def has_role(self, role):
        """Check if user has a specific role. Admins have all roles."""
        if self.is_admin:
            return True
        return role in self._roles

    @staticmethod
    def get(user_id):
        import json
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        conn.close()
        
        if not user:
            return None
        
        roles = user['roles'] if 'roles' in user.keys() else None  # normalized by User()

        email = user['email'] if 'email' in user.keys() else None
        return User(user['id'], user['username'], roles=roles, email=email)

    @staticmethod
    def get_by_email(email):
        """Find user by email address."""
        if not email: return None
        conn = get_db_connection()
        try:
            # Check for direct email match
            user = conn.execute("SELECT * FROM users WHERE email = ?", (email.lower(),)).fetchone()
            if user:
                return User.get(user['id'])
            return None
        finally:
            conn.close()

    @staticmethod
    def get_by_username(username):
        """Find user by username (case insensitive)."""
        if not username: return None
        conn = get_db_connection()
        try:
            user = conn.execute("SELECT * FROM users WHERE LOWER(username) = ?", (username.lower(),)).fetchone()
            if user:
                return User.get(user['id'])
            return None
        finally:
            conn.close()

    @staticmethod
    def authenticate(username, password):
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()
        
        if not user:
            return None
            
        # For now, simplistic password check (assumes plain hash or similar)
        # In production, use werkzeug.security.check_password_hash
        
        from werkzeug.security import check_password_hash
        # Accounts created by the removed Google sign-in (2.x auto-provisioning)
        # have no usable password until an admin sets one.
        if user['password_hash'] and user['password_hash'].startswith('sso_managed_'):
            return None

        if user['password_hash'] and check_password_hash(user['password_hash'], password):
             return User.get(user['id']) # Use get() to return full object
             
        return None

def load_user(user_id):
    return User.get(user_id)
