from flask_login import UserMixin
from app.services.db import get_db_connection, BASE_DIR

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
            except:
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
    except:
        pass
    finally:
        conn.close()

def delete_user(username):
    """Legacy function used by admin.py."""
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM users WHERE username = ?", (username,))
        conn.commit()
    except:
        pass
    finally:
        conn.close()

def update_user_password(username, password_hash):
    """Legacy function used by admin.py."""
    conn = get_db_connection()
    try:
        conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (password_hash, username))
        conn.commit()
    except:
        pass
    finally:
        conn.close()

def update_user_admin_status(username, is_admin):
    """Legacy function used by admin.py."""
    conn = get_db_connection()
    try:
        conn.execute("UPDATE users SET is_admin = ? WHERE username = ?", (is_admin, username))
        conn.commit()
    except:
        pass
    finally:
        conn.close()
# -------------------------------------------------------

class User(UserMixin):
    def __init__(self, id, username, is_admin=False, roles=None):
        self.id = str(id)
        self.username = username
        self.is_admin = bool(is_admin)
        self._roles = roles or []

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
        except:
            roles = []
        
        return User(user['id'], user['username'], user['is_admin'], roles)

    @staticmethod
    def authenticate(username, password):
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()
        
        if not user:
            return None
            
        # For now, simplistic password check (assumes plain hash or similar)
        # In production, use werkzeug.security.check_password_hash
        # Current system seems to use hashed passwords in migration logs?
        # Reusing existing logic if available, otherwise implementation standard check
        # Assuming database has password_hash. 
        
        from werkzeug.security import check_password_hash
        if user['password_hash'] and check_password_hash(user['password_hash'], password):
             return User(user['id'], user['username'], user['is_admin'])
             
        return None

def load_user(user_id):
    return User.get(user_id)
