from flask import Blueprint, render_template, redirect, url_for, flash, request, session, current_app, abort, make_response
from flask_login import login_user, logout_user, login_required, current_user
from app.services.auth import User
from app.services.db import get_db_connection
from urllib.parse import urlparse
import logging

auth_bp = Blueprint('auth', __name__)
logger = logging.getLogger(__name__)


def log_login_attempt(username: str, success: bool):
    """Log a login attempt with device information."""
    try:
        ip_address = request.remote_addr or 'unknown'
        user_agent = request.headers.get('User-Agent', 'unknown')
        
        # Extract device info from user agent
        device_info = 'Unknown Device'
        ua_lower = user_agent.lower()
        if 'mobile' in ua_lower or 'android' in ua_lower or 'iphone' in ua_lower:
            device_info = 'Mobile'
        elif 'tablet' in ua_lower or 'ipad' in ua_lower:
            device_info = 'Tablet'
        elif 'windows' in ua_lower:
            device_info = 'Windows PC'
        elif 'macintosh' in ua_lower or 'mac os' in ua_lower:
            device_info = 'Mac'
        elif 'linux' in ua_lower:
            device_info = 'Linux'
        
        conn = get_db_connection()
        try:
            conn.execute('''
                INSERT INTO login_attempts (username, success, ip_address, user_agent, device_info)
                VALUES (?, ?, ?, ?, ?)
            ''', (username, 1 if success else 0, ip_address, user_agent[:500], device_info))
            conn.commit()
        finally:
            conn.close()
            
        log_msg = f"Login {'SUCCESS' if success else 'FAILED'} for '{username}' from {ip_address} ({device_info})"
        if success:
            logger.info(log_msg)
        else:
            logger.warning(log_msg)
    except Exception as e:
        logger.error(f"Failed to log login attempt: {e}")


import time
from collections import defaultdict

# Simple in-memory rate limiter for login attempts
_login_attempts = defaultdict(list)  # IP -> list of timestamps
_LOGIN_RATE_LIMIT = 5  # attempts
_LOGIN_RATE_WINDOW = 60  # seconds


def check_login_rate_limit():
    """Check if the current IP has exceeded login rate limit. Returns True if blocked."""
    ip = request.remote_addr or 'unknown'
    current_time = time.time()
    
    # Clean old attempts outside the window
    _login_attempts[ip] = [t for t in _login_attempts[ip] if current_time - t < _LOGIN_RATE_WINDOW]
    
    # Check if over limit
    if len(_login_attempts[ip]) >= _LOGIN_RATE_LIMIT:
        return True
    
    # Record this attempt
    _login_attempts[ip].append(current_time)
    return False


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    # Security: Rate limiting - 5 login attempts per minute per IP
    if request.method == 'POST' and check_login_rate_limit():
        response = make_response("Too many login attempts. Please wait a minute and try again.", 429)
        response.headers['Retry-After'] = '60'
        return response
    
    if current_user.is_authenticated and not request.args.get('force'):
        return redirect(url_for('main.index'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        remember = True if request.form.get('remember') else False

        user = User.authenticate(username, password)

        if not user:
            log_login_attempt(username, success=False)
            flash('Invalid username or password', 'error')
            return redirect(url_for('auth.login'))

        # Successful login
        log_login_attempt(username, success=True)
        login_user(user, remember=remember)
        
        # Store login timestamp for session timeout
        session['login_time'] = __import__('time').time()
        
        # Redirect to next page or default
        next_page = request.args.get('next')
        if not next_page or urlparse(next_page).netloc != '':
            next_page = url_for('main.index')
        
        return redirect(next_page)

    return render_template('login.html')

@auth_bp.route('/logout')
def logout():
    import logging
    from flask import make_response
    logger = logging.getLogger(__name__)
    logger.info(f"[LOGOUT] Route hit. current_user.is_authenticated: {current_user.is_authenticated}")
    
    # Logout via Flask-Login
    logout_user()
    
    # Clear all session data
    session.clear()
    
    logger.info("[LOGOUT] User logged out. Redirecting to login.")
    flash('You have been logged out.', 'info')
    
    # Create response and explicitly delete remember_token cookie
    response = make_response(redirect(url_for('auth.login', force=1)))
    response.delete_cookie('remember_token')
    response.delete_cookie('session')
    
    return response
