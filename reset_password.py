import sys
import argparse
from werkzeug.security import generate_password_hash
from app.services.db import get_db_connection

def reset_password(username, new_password):
    print(f"Resetting password for user: {username}")
    conn = get_db_connection()
    try:
        # Check if user exists
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not user:
            print(f"Error: User '{username}' not found.")
            return

        # Update password
        p_hash = generate_password_hash(new_password)
        conn.execute("UPDATE users SET password_hash = ? WHERE username = ?", (p_hash, username))
        conn.commit()
        print(f"Success: Password for '{username}' has been updated.")
        
    except Exception as e:
        print(f"Error during reset: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python reset_password.py <username> <new_password>")
        sys.exit(1)
        
    username = sys.argv[1]
    password = sys.argv[2]
    
    reset_password(username, password)
