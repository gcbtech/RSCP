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
        # Security: Stricter open redirect protection
        # Block: empty, external netloc, protocol-relative (//evil.com), and javascript:
        if (not next_page or 
            urlparse(next_page).netloc != '' or 
            next_page.startswith('//') or 
            next_page.lower().startswith('javascript:')):
            next_page = url_for('main.index')
        
        return redirect(next_page)

    return render_template('login.html')


@auth_bp.route('/login/sso')
def login_sso():
    """Initiate OIDC Login Flow"""
    if not hasattr(current_app, 'oauth') or not current_app.oauth:
        flash("SSO is not configured.", "error")
        return redirect(url_for('auth.login'))
        
    try:
        redirect_uri = url_for('auth.sso_callback', _external=True)
        return current_app.oauth.rscp_sso.authorize_redirect(redirect_uri)
    except Exception as e:
        logger.error(f"SSO Init Failed: {e}")
        flash(f"Failed to start SSO: {e}", "error")
        return redirect(url_for('auth.login'))


@auth_bp.route('/login/callback')
def sso_callback():
    """Handle OIDC Callback"""
    try:
        oauth = current_app.oauth.rscp_sso
        token = oauth.authorize_access_token()
        user_info = oauth.userinfo()
        
        if not user_info or not user_info.get('email'):
            flash("Failed to get user info from identity provider.", "error")
            return redirect(url_for('auth.login'))
            
        email = user_info['email'].lower()
        
        # 1. Check Domain Whitelist
        from app.services.data_manager import load_config
        conf = load_config()
        allowed_domains = conf.get('SSO_ALLOWED_DOMAINS', [])
        
        # If allowed_domains is set, check match
        if allowed_domains:
            domain = '@' + email.split('@')[-1]
            if domain not in allowed_domains:
                logger.warning(f"SSO Login Blocked: Domain {domain} not in whitelist.")
                flash("Your email domain is not authorized to access this system.", "error")
                return redirect(url_for('auth.login'))

        # 2. Find User
        user = User.get_by_email(email)
        
        # 3. Smart Linking (Fallback to username match)
        if not user:
            # Try matching username (e.g. 'bob' matches 'bob@company.com')
            local_user = email.split('@')[0]
            existing_user = User.get_by_username(local_user)
            
            if existing_user:
                # Link account
                User.link_sso_account(existing_user.id, email, 'oidc')
                user = User.get(existing_user.id) # Reload
                logger.info(f"SSO: Linked existing user '{local_user}' to '{email}'")
        
        # 4. Auto-Provisioning
        if not user:
            if conf.get('SSO_ALLOW_NEW_USERS', True):
                # Create new user
                preferred_username = email.split('@')[0]
                # Ensure username uniqueness (simple check)
                if User.get_by_username(preferred_username):
                    preferred_username = f"{preferred_username}_{int(time.time())}"
                
                user = User.create_sso_user(preferred_username, email, 'oidc')
                logger.info(f"SSO: Created new user '{preferred_username}' for '{email}'")
            else:
                flash("Account does not exist and auto-provisioning is disabled.", "error")
                return redirect(url_for('auth.login'))
        
        # 5. Login
        if user:
            login_user(user)
            # Store login timestamp for session timeout
            session['login_time'] = time.time()
            logger.info(f"SSO Login Success: {user.username} ({email})")
            return redirect(url_for('main.index'))
            
    except Exception as e:
        logger.error(f"SSO Callback Error: {e}")
        # flash(f"Authentication failed: {str(e)}", "error") # Don't expose raw error Details
        flash("Authentication failed. Please try again.", "error")
        
    return redirect(url_for('auth.login'))

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
