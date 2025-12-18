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
            
    except Exception as e:
        logger.error(f"Migration Schema Check Error: {e}")
    finally:
        conn.close()
