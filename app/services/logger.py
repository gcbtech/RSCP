import os
import sys
import traceback
import logging
from flask import g, has_app_context, has_request_context
from app.services.db import get_db_connection

# Configure standard logger fallback
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Process-wide logging: stderr only. Under systemd, journald captures stderr
# (journalctl -u rscp); there are no log files to rotate or clean up.
# ---------------------------------------------------------------------------

class RequestContextFilter(logging.Filter):
    """Adds request_id and user (set by the before_request hook in
    app/__init__.py) to every record; '-' outside a request."""

    def filter(self, record):
        request_id = user = '-'
        if has_request_context():
            request_id = getattr(g, 'request_id', None) or '-'
            user = getattr(g, 'user_id', None) or '-'
        record.request_id = request_id
        record.user = user
        return True


class _StderrHandler(logging.StreamHandler):
    """Writes to whatever sys.stderr is at emit time (so it keeps working when
    a test runner or gunicorn swaps the stream)."""

    def __init__(self):
        super().__init__()

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, _value):
        pass


def configure_logging(level=None):
    """Attach the RSCP stderr handler to the root logger. Idempotent: safe to
    call once per create_app() (tests build many apps per process).

    Level comes from RSCP_LOG_LEVEL (default INFO). journald stamps every
    line itself, so the timestamp is left out when stderr is the journal.
    """
    root = logging.getLogger()
    level = level or os.environ.get('RSCP_LOG_LEVEL', 'INFO').upper()
    root.setLevel(level)
    for handler in root.handlers:
        if getattr(handler, '_rscp', False):
            return handler

    fmt = '%(levelname)s [pid %(process)d] [req %(request_id)s] [user %(user)s] %(name)s: %(message)s'
    if not os.environ.get('JOURNAL_STREAM'):
        fmt = '%(asctime)s ' + fmt
    handler = _StderrHandler()
    handler._rscp = True
    handler.setFormatter(logging.Formatter(fmt))
    handler.addFilter(RequestContextFilter())
    root.addHandler(handler)
    return handler

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

        # 2. Write to DB (using local time instead of UTC)
        conn = get_db_connection()
        conn.execute('''
            INSERT INTO error_logs (timestamp, level, source, message, trace, user_id, status)
            VALUES (datetime('now', 'localtime'), ?, ?, ?, ?, ?, ?)
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
    req_id = getattr(g, 'request_id', 'NO_REQ_ID') if has_app_context() else 'NO_REQ_ID'
    
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
    user_id = getattr(g, 'user_id', None) if has_app_context() else None
    
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
