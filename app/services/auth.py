import logging
import sqlite3
from typing import Dict, Any, Optional

from app.services.db import get_db_connection


import os
logger = logging.getLogger(__name__)

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def load_users() -> Dict[str, Any]:
    """Shim: Returns all users as a dictionary {username: {password_hash, is_admin}}."""
    conn = get_db_connection()
    try:
        # Phase 9: Include PIN status (Migrated check)
        # We need to handle case where column might not exist yet if migration failed? 
        # But assuming it ran.
        try:
             users = conn.execute("SELECT username, password_hash, is_admin, pin_hash FROM users").fetchall()
        except sqlite3.OperationalError:
             # Column missing fallback
             users = conn.execute("SELECT username, password_hash, is_admin FROM users").fetchall()
             for u in users: u = dict(u); u['pin_hash'] = None
             
        return {
            u['username']: {
                'password_hash': u['password_hash'],
                'is_admin': bool(u['is_admin']),
                'pin_set': bool(u['pin_hash']) if 'pin_hash' in u.keys() else False
            } 
            for u in users
        }
    except Exception as e:
        logger.error(f"Error loading users: {e}")
        return {}
    finally:
        conn.close()

def create_user(username, password_hash, is_admin=False, pin_hash=None) -> bool:
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, is_admin, pin_hash) VALUES (?, ?, ?, ?)",
            (username, password_hash or "", 1 if is_admin else 0, pin_hash)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False # duplicate
    except Exception as e:
        logger.error(f"Create user error: {e}")
        return False
    finally:
        conn.close()

def delete_user(username) -> bool:
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM users WHERE username = ?", (username,))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Delete user error: {e}")
        return False
    finally:
        conn.close()

def update_user_password(username, password_hash) -> bool:
    conn = get_db_connection()
    try:
        conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (password_hash, username))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Update password error: {e}")
        return False
    finally:
        conn.close()

# save_users is DEPRECATED and should not be used. 
# We remove it to force errors in admin.py so we catch them.
