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
        rows = conn.execute("SELECT * FROM users").fetchall()
        users = {}
        for r in rows:
            # Parse roles from JSON (handle missing column gracefully)
            roles = []
            try:
                roles_str = r['roles'] if 'roles' in r.keys() else None
                if roles_str:
                    roles = json.loads(roles_str)
            except Exception as e:
                logger.warning(f"Failed to parse roles for user {r['username']}: {e}")
                roles = []
            
            users[r['username']] = {
                "pin": r['password_hash'], # Legacy code might expect 'pin' key
                "password_hash": r['password_hash'],
                "pin_hash": r['pin_hash'] if 'pin_hash' in r.keys() else None,
                "is_admin": bool(r['is_admin']),
                "roles": roles,
                "badge_id": r['badge_id'] if 'badge_id' in r.keys() else ''
            }
        return users
    except Exception as e:
        import logging
        logging.getLogger(__name__).error(f"Error loading users: {e}")
        return {}
    finally:
        conn.close()

def create_user(username, password_hash, is_admin=False):
    """Legacy function used by main.py setup wizard."""
    conn = get_db_connection()
    try:
        conn.execute("INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)", 
                    (username, password_hash, is_admin))
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
    """Legacy function used by admin.py."""
    conn = get_db_connection()
    try:
        conn.execute("UPDATE users SET is_admin = ? WHERE username = ?", (is_admin, username))
        conn.commit()
    except Exception as e:
        logger.error(f"Failed to update admin status for user '{username}': {e}")
    finally:
        conn.close()
# -------------------------------------------------------

class User(UserMixin):
    def __init__(self, id, username, is_admin=False, roles=None, email=None, auth_provider=None):
        self.id = str(id)
        self.username = username
        self.is_admin = bool(is_admin)
        self._roles = roles or []
        self.email = email
        self.auth_provider = auth_provider

    @property
    def roles(self):
        return self._roles
    
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
        
        # Parse roles
        roles = []
        try:
            if user['roles']:
                roles = json.loads(user['roles'])
        except Exception as e:
            logger.warning(f"Failed to parse roles for user ID {user_id}: {e}")
            roles = []
        
        # Safely get new columns (might handle migration lag)
        email = user['email'] if 'email' in user.keys() else None
        auth_provider = user['auth_provider'] if 'auth_provider' in user.keys() else None
        
        return User(user['id'], user['username'], user['is_admin'], roles, email, auth_provider)

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
    def create_sso_user(username, email, provider='oidc'):
        """Create a new user from SSO data."""
        conn = get_db_connection()
        try:
            # Generate random password (they should only login via SSO)
            import secrets
            pwd_hash = "sso_managed_" + secrets.token_hex(8) 
            
            conn.execute("""
                INSERT INTO users (username, password_hash, is_admin, roles, email, auth_provider) 
                VALUES (?, ?, 0, '["user"]', ?, ?)
            """, (username, pwd_hash, email, provider))
            conn.commit()
            
            # Fetch and return the new user
            return User.get_by_username(username)
        except Exception as e:
            logger.error(f"Failed to create SSO user {username}: {e}")
            return None
        finally:
            conn.close()

    @staticmethod
    def link_sso_account(user_id, email, provider):
        """Link an existing user account to an SSO identity."""
        conn = get_db_connection()
        try:
            conn.execute("UPDATE users SET email = ?, auth_provider = ? WHERE id = ?", (email, provider, user_id))
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to link SSO account for user {user_id}: {e}")
            return False
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
        # Block SSO-only users from password login if password starts with sso_managed
        if user['password_hash'] and user['password_hash'].startswith('sso_managed_'):
            return None

        if user['password_hash'] and check_password_hash(user['password_hash'], password):
             return User.get(user['id']) # Use get() to return full object
             
        return None

def load_user(user_id):
    return User.get(user_id)
