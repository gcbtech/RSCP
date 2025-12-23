
import sys
import os
import sqlite3
import traceback

# Setup paths
BASE_DIR = os.getcwd()
DB_PATH = os.path.join(BASE_DIR, 'rscp.db')

print(f"Diagnostics: Checking DB at {DB_PATH}")

def check_db():
    conn = sqlite3.connect(DB_PATH)
    try:
        # Check Table
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='error_logs'")
        if not cur.fetchone():
            print("FAIL: Table 'error_logs' DOES NOT exist.")
            
            # Attempt Force Create
            print("Attempting to create table...")
            cur.execute('''
                CREATE TABLE IF NOT EXISTS error_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    level TEXT,
                    source TEXT,
                    message TEXT,
                    trace TEXT,
                    user_id TEXT,
                    status TEXT
                )
            ''')
            conn.commit()
            print("SUCCESS: Table 'error_logs' created.")
        else:
            print("SUCCESS: Table 'error_logs' exists.")
            
        # Try Write
        try:
            print("Attempting to write test log...")
            cur.execute("""
                INSERT INTO error_logs (level, source, message, trace, user_id)
                VALUES (?, ?, ?, ?, ?)
            """, ('INFO', 'DiagnosticScript', 'Test Log Entry', 'No Trace', 'System'))
            conn.commit()
            print("SUCCESS: Test log written to DB.")
        except Exception as e:
            print(f"FAIL: Could not write to DB: {e}")
            
    except Exception as e:
        print(f"CRITICAL SQL ERROR: {e}")
        traceback.print_exc()
    finally:
        conn.close()

if __name__ == "__main__":
    check_db()
