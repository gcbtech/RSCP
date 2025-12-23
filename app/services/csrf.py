import secrets
import logging
from flask import session, request, abort

logger = logging.getLogger(__name__)

def generate_csrf_token():
    if '_csrf_token' not in session:
        session['_csrf_token'] = secrets.token_hex(16)
    return session['_csrf_token']

def verify_csrf_token():
    # Only protect safe methods
    if request.method not in ('POST', 'PUT', 'DELETE', 'PATCH'):
        return

    # Helper to check if it's an API call or Form
    token = request.form.get('csrf_token') or request.headers.get('X-CSRFToken')
    session_token = session.get('_csrf_token')
    
    # Security: Don't log actual tokens, only validation status
    if not token or token != session_token:
        logger.warning(f"[CSRF] Token validation failed for {request.path}")
        abort(403, description="CSRF Token Mismatch")
    
    logger.debug(f"[CSRF] Token verified for {request.path}")
