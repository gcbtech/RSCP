import logging
import sqlite3
from app.services.db import get_db_connection, init_db, DB_PATH

logger = logging.getLogger(__name__)

def ensure_db_ready():
    """Initializes the database and applies any pending schema updates."""
    init_db()
    
    conn = get_db_connection()
    try:
        # Schema Update Check (Phase 9 - PIN Support)
        try:
            conn.execute("ALTER TABLE users ADD COLUMN pin_hash TEXT")
            conn.commit()
            logger.info("Added pin_hash column to users.")
        except Exception: 
            pass # Column likely already exists
        
        # V1.16.1: Add asin and source_url to packages table
        package_migrations = [
            ('asin', 'ALTER TABLE packages ADD COLUMN asin TEXT'),
            ('source_url', 'ALTER TABLE packages ADD COLUMN source_url TEXT'),
        ]
        for col_name, sql in package_migrations:
            try:
                conn.execute(sql)
                conn.commit()
                logger.info(f"Added {col_name} column to packages.")
            except:
                pass  # Column already exists
        
        # Inventory Module Tables (V1.16)
        _create_inventory_tables(conn)
        
        # Logging Table (V1.17)
        conn.execute('''
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
            
    except Exception as e:
        logger.error(f"Migration Schema Check Error: {e}")
    finally:
        conn.close()


def _create_inventory_tables(conn):
    """Create inventory module tables if they don't exist."""
    try:
        # Inventory Items Table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS inventory_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                quantity INTEGER DEFAULT 0,
                
                -- Location (at least one required, enforced in app)
                location_area TEXT,
                location_aisle TEXT,
                location_shelf TEXT,
                location_bin TEXT,
                
                -- Optional fields
                asin TEXT,
                image_url TEXT,
                source_url TEXT,
                buy_price REAL,
                sell_price REAL,
                supplier TEXT,
                first_stock_date TEXT,
                resupply_interval INTEGER,
                
                -- Alert settings
                alert_enabled BOOLEAN DEFAULT 0,
                alert_threshold INTEGER DEFAULT 0,
                
                -- Audit
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Migrations: Add columns if they don't exist (for existing installs)
        migrations = [
            ('image_url', 'ALTER TABLE inventory_items ADD COLUMN image_url TEXT'),
            ('source_url', 'ALTER TABLE inventory_items ADD COLUMN source_url TEXT'),
            ('alert_enabled', 'ALTER TABLE inventory_items ADD COLUMN alert_enabled BOOLEAN DEFAULT 0'),
            ('alert_threshold', 'ALTER TABLE inventory_items ADD COLUMN alert_threshold INTEGER DEFAULT 0'),
            ('buy_price', 'ALTER TABLE inventory_items ADD COLUMN buy_price REAL DEFAULT 0.0'),
            ('sell_price', 'ALTER TABLE inventory_items ADD COLUMN sell_price REAL DEFAULT 0.0'),
        ]
        for col_name, sql in migrations:
            try:
                conn.execute(sql)
                conn.commit()
                logger.info(f"Added {col_name} column to inventory_items.")
            except:
                pass  # Column already exists
        
        # Indexes for inventory_items
        conn.execute('CREATE INDEX IF NOT EXISTS idx_inventory_sku ON inventory_items(sku)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_inventory_asin ON inventory_items(asin)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_inventory_name ON inventory_items(name)')
        
        # Inventory Transactions Table (audit trail)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS inventory_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inventory_item_id INTEGER NOT NULL,
                quantity_change INTEGER NOT NULL,
                reason TEXT DEFAULT 'Sold/Consumed',
                user_id TEXT,
                source_tracking TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (inventory_item_id) REFERENCES inventory_items(id)
            )
        ''')
        
        # Performance: Indexes for inventory_transactions (used in sales trend queries)
        conn.execute('CREATE INDEX IF NOT EXISTS idx_trans_item ON inventory_transactions(inventory_item_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_trans_date ON inventory_transactions(created_at)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_trans_reason ON inventory_transactions(reason)')
        
        # Audit Sessions Table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS audit_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                end_time TIMESTAMP,
                user_id TEXT,
                mode TEXT NOT NULL, 
                status TEXT DEFAULT 'active'
            )
        ''')

        # Audit Records Table
        conn.execute('''
            CREATE TABLE IF NOT EXISTS audit_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                item_id INTEGER,
                sku TEXT,
                name TEXT,
                expected_qty INTEGER,
                counted_qty INTEGER,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(session_id) REFERENCES audit_sessions(id),
                FOREIGN KEY(item_id) REFERENCES inventory_items(id)
            )
        ''')
        
        conn.commit()
        logger.info("Inventory tables initialized.")
        
    except Exception as e:
        logger.error(f"Error creating inventory tables: {e}")
