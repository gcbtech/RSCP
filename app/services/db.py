import sqlite3
import os
import logging
from app.utils.helpers import parse_date

logger = logging.getLogger(__name__)

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
DB_PATH = os.path.join(BASE_DIR, 'rscp.db')

def get_db_connection():
    """Returns a connection to the SQLite database with Row factory enabled."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes the database schema if it doesn't exist."""
    conn = get_db_connection()
    try:
        cur = conn.cursor()
        
        # 1. Users Table
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin BOOLEAN DEFAULT 0
            )
        ''')
        
        # 2. Packages Table
        # Merges fields from package_db.json and manifest.csv
        cur.execute('''
            CREATE TABLE IF NOT EXISTS packages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tracking_number TEXT UNIQUE NOT NULL,
                item_name TEXT,
                status TEXT DEFAULT 'pending', 
                source TEXT DEFAULT 'manifest',
                date_expected DATE,
                manual_date DATE,
                date_scanned TIMESTAMP,
                quantity INTEGER DEFAULT 1,
                priority BOOLEAN DEFAULT 0,
                image_url TEXT,
                refund_date DATE
            )
        ''')
        # status enum: expected, pending, past_due, received, return_pending, returned, refunded, on_time
        
        # 3. History Log
        # Replaces received_log.csv
        cur.execute('''
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                package_id INTEGER,
                user_id INTEGER,
                action TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                details TEXT,
                FOREIGN KEY (package_id) REFERENCES packages (id),
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')
        
        # Indexes for performance
        cur.execute('CREATE INDEX IF NOT EXISTS idx_tracking ON packages (tracking_number)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_status ON packages (status)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_date_expected ON packages (date_expected)')
        
        conn.commit()
        logger.info("Database initialized successfully.")
    except Exception as e:
        logger.error(f"Error initializing database: {e}")
        conn.rollback()
    finally:
        conn.close()

if __name__ == '__main__':
    # Initialize when run directly
    logging.basicConfig(level=logging.INFO)
    init_db()
