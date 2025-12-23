from flask_login import UserMixin
from app.services.db import get_db_connection, BASE_DIR

# --- Shim Functions for Legacy Compatibility (main.py) ---
def load_users():
    """Legacy function used by main.py login route."""
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT * FROM users").fetchall()
        users = {}
        for r in rows:
            users[r['username']] = {
                "pin": r['password_hash'], # Legacy code might expect 'pin' key
                "is_admin": bool(r['is_admin'])
            }
        return users
    except:
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
    def __init__(self, id, username, is_admin=False):
        self.id = str(id)
        self.username = username
        self.is_admin = bool(is_admin)

    @staticmethod
    def get(user_id):
        conn = get_db_connection()
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        conn.close()
        
        if not user:
            return None
        return User(user['id'], user['username'], user['is_admin'])

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
