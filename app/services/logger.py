import traceback
import logging
from flask import g
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

def log_exception(e, source="System"):
    """
    Log an exception with detailed context, including Request ID.
    Now supports RscpError specific codes.
    """
    error_code = getattr(e, 'code', 'RSCP-999') # Default to generic system error
    req_id = getattr(g, 'request_id', 'NO_REQ_ID')
    
    # Format: [REQ_ID] [CODE] [Source] Message
    # The message for standard logging should include the request ID and error code
    log_msg_for_std_log = f"[{req_id}] [{error_code}] [{source}] {str(e)}"
    
    # The message for DB might be cleaner without the req_id/code if they are separate columns
    # But for now, let's keep it consistent with the original log_error's message parameter
    # and let log_error handle its own formatting.
    
    # Get traceback
    full_trace = traceback.format_exc()

    # Log to standard logger (already configured as 'logger')
    logger.error(log_msg_for_std_log)
    logger.error(full_trace)

    # Log to DB via log_error function
    # We pass the original exception message, and the full trace.
    # user_id can be retrieved from flask.g if available, otherwise None.
    user_id = getattr(g, 'user_id', None) # Assuming user_id might be stored in g
    
    # The message for the DB should probably be just the exception string,
    # and the error_code can be appended or handled separately if the schema changes.
    # For now, let's pass the exception string as the message.
    db_message = f"[{error_code}] {str(e)}" # Include error code in DB message
    
    log_error(
        message=db_message, 
        level="ERROR", 
        source=source, 
        user_id=user_id, 
        trace=full_trace
    )
