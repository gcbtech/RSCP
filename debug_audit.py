import sqlite3
import os

print("--- DB TABLES ---")
try:
    conn = sqlite3.connect('app/rscp.db')
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = cursor.fetchall()
    print([t[0] for t in tables])
    conn.close()
except Exception as e:
    print(f"DB Error: {e}")

print("\n--- APP LOG TAIL ---")
try:
    if os.path.exists('app.log'):
        with open('app.log', 'r') as f:
            lines = f.readlines()
            print("".join(lines[-50:]))
    else:
        print("app.log not found.")
except Exception as e:
    print(f"Log Error: {e}")
