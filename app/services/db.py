import sqlite3
import os
import logging
from app.utils.helpers import parse_date

logger = logging.getLogger(__name__)

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
DB_PATH = os.path.join(BASE_DIR, 'rscp.db')

def get_db_connection():
    """Returns a NEW connection to the SQLite database.
    
    Performance optimizations:
    - timeout=10: Prevents indefinite blocking on locked database
    - WAL mode: Allows concurrent reads during writes
    
    Note: For request-scoped connections, use get_request_db() instead
    to avoid creating multiple connections per request.
    """
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # Enable WAL mode for better concurrency (only needs to be set once per DB)
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def get_request_db():
    """Get a database connection scoped to the current Flask request.
    
    Uses Flask's g object to share a single connection per request,
    reducing connection overhead significantly.
    
    Usage:
        from app.services.db import get_request_db
        conn = get_request_db()
        result = conn.execute(...).fetchall()
        # No need to close - handled by teardown
    """
    try:
        from flask import g, has_app_context
        if has_app_context():
            if 'db' not in g:
                g.db = get_db_connection()
            return g.db
    except RuntimeError:
        # Not in app context (background thread, CLI, etc.)
        pass
    
    # Fallback: return new connection if not in Flask context
    return get_db_connection()


def close_request_db(error=None):
    """Close the request-scoped database connection.
    
    Called automatically by Flask teardown if init_app() was called.
    """
    try:
        from flask import g
        db = g.pop('db', None)
        if db is not None:
            db.close()
    except RuntimeError:
        pass


def init_app(app):
    """Register database teardown with Flask app.
    
    Call this in create_app() to enable automatic connection cleanup:
        from app.services.db import init_app as init_db_app
        init_db_app(app)
    """
    app.teardown_appcontext(close_request_db)

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
