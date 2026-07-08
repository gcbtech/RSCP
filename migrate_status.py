from app.services.db import get_db_connection
import time

def run_migration():
    print("Starting migration: 'Pending' -> 'On Time'...")
    try:
        conn = get_db_connection()
        # count pending first
        count = conn.execute("SELECT count(*) as c FROM packages WHERE status='pending'").fetchone()['c']
        print(f"Found {count} packages with 'pending' status.")
        
        if count > 0:
            conn.execute("UPDATE packages SET status='on_time' WHERE status='pending'")
            conn.commit()
            print(f"Successfully updated {count} packages to 'on_time'.")
        else:
            print("No 'pending' packages found. Migration not needed or already done.")
            
        conn.close()
    except Exception as e:
        print(f"Migration failed: {e}")
        print("Tip: Make sure the web server is stopped before running this script to avoid database locks.")

if __name__ == "__main__":
    run_migration()
