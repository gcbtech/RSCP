import traceback
import logging
from app.services.db import get_db_connection

# Configure standard logger fallback
logger = logging.getLogger(__name__)

def log_error(message, level="ERROR", source="Backend", user_id=None, trace=None, status="Open"):
    """
    Logs an error to the database and standard logging.
    """
    try:
        # 1. Fallback to standard logging first (always works)
        log_msg = f"[{level}] {source}: {message}"
        if user_id: log_msg += f" (User: {user_id})"
        
        if level == "ERROR":
            logger.error(log_msg)
            if trace: logger.error(trace)
        else:
            logger.info(log_msg)

        # 2. Write to DB
        conn = get_db_connection()
        conn.execute('''
            INSERT INTO error_logs (timestamp, level, source, message, trace, user_id, status)
            VALUES (CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, ?)
        ''', (level, source, message, trace, user_id, status))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        # Fallback if DB fails
        logger.error(f"CRITICAL: Failed to write to error_logs DB: {e}")
        return False

def log_exception(e, source="Backend", user_id=None):
    """
    Helper to log a full exception with traceback.
    """
    msg = str(e)
    trace = traceback.format_exc()
    log_error(msg, level="ERROR", source=source, user_id=user_id, trace=trace)
