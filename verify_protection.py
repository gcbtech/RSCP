
import sqlite3

def test_protection():
    print("Testing Manual Item Protection...")
    
    # In-Memory DB
    conn = sqlite3.connect(':memory:')
    cur = conn.cursor()
    
    # Schema
    cur.execute('''
        CREATE TABLE packages (
            id INTEGER PRIMARY KEY,
            tracking_number TEXT UNIQUE,
            source TEXT
        )
    ''')
    
    # 1. Setup: User manually adds a package that happens to match an Order ID
    manual_tracking = "ORDER-12345"
    print(f"Inserting Manual Item: {manual_tracking}")
    cur.execute("INSERT INTO packages (tracking_number, source) VALUES (?, ?)", (manual_tracking, 'manual'))
    conn.commit()
    
    # 2. Simulate Cleanup
    # The sync logic identifies "ORDER-12345" as a bad ID to clean from the manifest
    ids_to_clean = [manual_tracking]
    placeholders = ','.join('?' for _ in ids_to_clean)
    
    print(f"Attempting to delete {manual_tracking} with safe clause...")
    
    # THE FIX: AND source != 'manual'
    cur.execute(f"DELETE FROM packages WHERE tracking_number IN ({placeholders}) AND source != 'manual'", ids_to_clean)
    conn.commit()
    
    # 3. Verify
    row = cur.execute("SELECT * FROM packages WHERE tracking_number = ?", (manual_tracking,)).fetchone()
    
    if row:
        print("✅ SUCCESS: Manual item was NOT deleted.")
    else:
        print("❌ FAILURE: Manual item WAS deleted.")
        
    conn.close()

if __name__ == "__main__":
    test_protection()
