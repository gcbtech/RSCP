import sqlite3
import json
import os

DB_NAME = 'repro_test.db'

def setup_db():
    if os.path.exists(DB_NAME):
        os.remove(DB_NAME)
    
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    
    # Create tables mock
    conn.execute('''
        CREATE TABLE pos_orders (
            id INTEGER PRIMARY KEY,
            created_at TIMESTAMP,
            status TEXT,
            total REAL,
            tax_amount REAL,
            discount_amount REAL,
            payment_method TEXT,
            payment_details TEXT
        )
    ''')
    
    # Insert a split order: Total $100 -> $20 Cash, $80 Card
    conn.execute('''
        INSERT INTO pos_orders (created_at, status, total, tax_amount, discount_amount, payment_method, payment_details)
        VALUES (
            '2025-01-01 12:00:00',
            'completed',
            100.0,
            0.0,
            0.0,
            'split',
            ?
        )
    ''', (json.dumps({'cash': 20.0, 'cards': [80.0]}),))
    
    conn.commit()
    return conn

def run_report_logic(conn):
    # Mimic the flawed logic from management.py
    
    # 3. Payment Breakdown
    payments = conn.execute('''
        SELECT 
            payment_method, 
            COUNT(*) as count, 
            SUM(total) as total_amount,
            SUM(tax_amount) as tax_amount,
            payment_details -- NOTE: Original query didn't select this?
        FROM pos_orders
        GROUP BY payment_method
    ''').fetchall()
    
    # NOTE: The original query in management.py lines 608-617 does NOT select payment_details!
    # It blindly groups by payment_method.
    
    total_cash = 0
    total_card = 0
    
    print(f"DEBUG: Found {len(payments)} payment groups")
    
    for p in payments:
        method = p['payment_method']
        amount = p['total_amount'] or 0
        
        print(f"Processing method: {method}, Amount: {amount}")
        
    # FIXED Logic
    
    # 3b. Fetch splits
    split_orders = conn.execute('''
        SELECT payment_details, total, tax_amount
        FROM pos_orders
        WHERE  payment_method = 'split'
    ''').fetchall()

    split_cash_total = 0
    split_card_total = 0
    
    for sp in split_orders:
        try:
            details = json.loads(sp['payment_details'])
            split_cash_total += float(details.get('cash', 0))
            split_card_total += sum(float(x) for x in details.get('cards', []))
        except:
            pass
            
    print(f"DEBUG: Parsed Splits -> Cash: {split_cash_total}, Card: {split_card_total}")
    
    for p in payments:
        method = p['payment_method']
        amount = p['total_amount'] or 0
        
        print(f"Processing method: {method}, Amount: {amount}")
        
        if method == 'cash':
            total_cash += amount
        elif method == 'split':
            total_cash += split_cash_total
            total_card += split_card_total
        else:
            total_card += amount
            
    print("-" * 20)
    print(f"Calculated Total Cash: ${total_cash}")
    print(f"Calculated Total Card: ${total_card}")
    print("-" * 20)
    
    # Assertion
    # We expect $20 Cash and $80 Card
    if total_cash == 0 and total_card == 100:
        print("FAIL: Split payment was counted 100% as Card.")
    elif total_cash == 20 and total_card == 80:
        print("SUCCESS: Split payment was correctly separated.")
    else:
        print(f"UNKNOWN: Cash={total_cash}, Card={total_card}")

def main():
    conn = setup_db()
    run_report_logic(conn)
    conn.close()
    if os.path.exists(DB_NAME):
        os.remove(DB_NAME)

if __name__ == '__main__':
    main()
