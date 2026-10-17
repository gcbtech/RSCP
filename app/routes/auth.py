from flask import Blueprint, render_template, redirect, url_for, flash, request, session, current_app, abort, make_response
from flask_login import login_user, logout_user, login_required, current_user
from app.services.auth import User
from app.services.db import get_db_connection
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

from app.services import throttle
from app.utils.helpers import is_safe_redirect

# Failed-login limits, shared across all gunicorn workers (see services/throttle).
# Per IP: slows down password spraying. Per username: slows down guessing one
# account from many IPs. Both reset on a successful login.
LOGIN_FAIL_LIMIT = 10
LOGIN_FAIL_WINDOW = 15 * 60  # seconds


def _login_throttled(username):
    ip = request.remote_addr or 'unknown'
    if throttle.is_limited('login_ip', ip, LOGIN_FAIL_LIMIT, LOGIN_FAIL_WINDOW):
        return True
    if username and throttle.is_limited('login_user', username.lower(), LOGIN_FAIL_LIMIT, LOGIN_FAIL_WINDOW):
        return True
    return False


@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST' and _login_throttled(request.form.get('username')):
        logger.warning(f"Login throttled for '{request.form.get('username')}' from {request.remote_addr}")
        response = make_response("Too many failed login attempts. Please wait 15 minutes and try again.", 429)
        response.headers['Retry-After'] = str(LOGIN_FAIL_WINDOW)
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
            throttle.hit('login_ip', request.remote_addr or 'unknown')
            if username:
                throttle.hit('login_user', username.lower())
            flash('Invalid username or password', 'error')
            return redirect(url_for('auth.login'))

        # Successful login
        log_login_attempt(username, success=True)
        throttle.clear('login_ip', request.remote_addr or 'unknown')
        throttle.clear('login_user', username.lower())
        login_user(user, remember=remember)
        
        # Store login timestamp for session timeout
        session['login_time'] = time.time()

        next_page = request.args.get('next')
        if not is_safe_redirect(next_page):
            next_page = url_for('main.index')
        return redirect(next_page)

    return render_template('login.html')


def _sso_available():
    """SSO is usable only when switched on, registered, and domain-restricted."""
    from app.services.data_manager import load_config
    conf = load_config()
    return bool(conf.get('SSO_ENABLED')
                and conf.get('SSO_ALLOWED_DOMAINS')
                and getattr(current_app, 'oauth', None) is not None
                and current_app.oauth.create_client('rscp_sso') is not None)


@auth_bp.route('/login/sso')
def login_sso():
    """Initiate OIDC Login Flow"""
    if not _sso_available():
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
    if not _sso_available():
        flash("SSO is not configured.", "error")
        return redirect(url_for('auth.login'))

    try:
        oauth = current_app.oauth.rscp_sso
        token = oauth.authorize_access_token()
        user_info = oauth.userinfo()

        if not user_info or not user_info.get('email'):
            flash("Failed to get user info from identity provider.", "error")
            return redirect(url_for('auth.login'))

        # An unverified address proves nothing about who is signing in.
        if user_info.get('email_verified') is not True:
            logger.warning(f"SSO Login Blocked: unverified email {user_info.get('email')}")
            flash("Your identity provider has not verified this email address.", "error")
            return redirect(url_for('auth.login'))

        email = user_info['email'].lower()

        # 1. Domain allowlist (required: _sso_available() refuses SSO without one)
        from app.services.data_manager import load_config
        conf = load_config()
        allowed_domains = [d.lower() if d.startswith('@') else '@' + d.lower()
                           for d in conf.get('SSO_ALLOWED_DOMAINS', [])]
        domain = '@' + email.rsplit('@', 1)[-1]
        if domain not in allowed_domains:
            logger.warning(f"SSO Login Blocked: Domain {domain} not in whitelist.")
            flash("Your email domain is not authorized to access this system.", "error")
            return redirect(url_for('auth.login'))

        # 2. Find the user by the email recorded on their account. There is
        # deliberately no fallback to matching the username against the
        # email's local part: that let anyone holding e.g. admin@<allowed
        # domain> take over the local "admin" account. To link an existing
        # account, an admin sets its email address first.
        user = User.get_by_email(email)

        # 3. Auto-provisioning (off unless explicitly enabled)
        if not user:
            if conf.get('SSO_ALLOW_NEW_USERS', False):
                preferred_username = email.split('@')[0]
                if User.get_by_username(preferred_username):
                    preferred_username = f"{preferred_username}_{int(time.time())}"
                user = User.create_sso_user(preferred_username, email, 'oidc')
                logger.info(f"SSO: Created new user '{preferred_username}' for '{email}'")
            else:
                flash("No RSCP account is linked to this email. Ask an administrator.", "error")
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
