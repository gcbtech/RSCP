"""
POS Authentication Module
PIN and badge login for POS operators.
"""
import logging
import time
from flask import request, redirect, url_for, flash, render_template, session
from flask_login import login_user, current_user
from werkzeug.security import check_password_hash

from app.routes.pos import pos_bp
from app.services import throttle
from app.services.db import get_db_connection, get_request_db
from app.services.auth import load_users, User
from app.utils.permissions import has_permission
from app.utils.proxy import get_client_ip

logger = logging.getLogger(__name__)

# Failed POS sign-ins (badge, PIN or password), shared across all gunicorn
# workers via services/throttle. Per register IP: 10 failures a minute, so a
# busy register where cashiers fat-finger PINs is never held up for long,
# while one machine can't run through PINs quickly. Per username (PIN and
# password forms): 10 failures per 15 minutes, the same allowance as /login;
# with 4-digit PINs this is what actually stops guessing one account. A
# correct sign-in from a register without its own recent failures is never
# blocked by failures elsewhere (see throttle.FailureLimit).
POS_LOGIN = throttle.FailureLimit('pos_login', ip_limit=10, ip_window=60,
                                  user_limit=10, user_window=15 * 60)


def _sign_in(user, how):
    login_user(user)
    session['login_time'] = time.time()
    POS_LOGIN.succeeded(user.username)
    logger.info(f"POS {how} login: {user.username}")
    return redirect(url_for('pos.sales'))


@pos_bp.route('/login', methods=['GET', 'POST'])
def pos_login():
    """POS-specific login with PIN or badge support."""
    # If already authenticated, go to sales
    if current_user.is_authenticated:
        return redirect(url_for('pos.sales'))
    
    if request.method == 'POST':
        client_ip = get_client_ip()
        login_type = request.form.get('login_type', 'password')
        username = '' if login_type == 'badge' else request.form.get('username', '').strip()

        if POS_LOGIN.refused(client_ip, username):
            logger.warning(f"POS login throttled for '{username}' from {client_ip}")
            flash('Too many failed sign-in attempts. Please wait a minute and try again.')
            return render_template('pos/login.html'), 429
        
        if login_type == 'badge':
            badge_id = request.form.get('badge_id', '').strip()
            if badge_id:
                user = authenticate_by_badge(badge_id)
                if user:
                    return _sign_in(user, 'badge')
                POS_LOGIN.failed(client_ip)
                flash('Invalid badge.')
        
        elif login_type == 'pin':
            pin = request.form.get('pin', '')
            
            if username and pin:
                user = authenticate_by_pin(username, pin)
                if user:
                    return _sign_in(user, 'PIN')
                POS_LOGIN.failed(client_ip, username)
                flash('Invalid username or PIN.')
        
        else:  # Standard password login
            password = request.form.get('password', '')
            
            if username and password:
                users = load_users()
                user_data = users.get(username)
                
                if user_data and check_password_hash(user_data['password_hash'], password):
                    return _sign_in(User.get_by_username(username), 'password')
                POS_LOGIN.failed(client_ip, username)
                flash('Invalid username or password.')
    
    return render_template('pos/login.html')


def authenticate_by_badge(badge_id):

    """Authenticate user by badge ID."""
    try:
        conn = get_request_db()
        result = conn.execute('SELECT username FROM users WHERE badge_id = ?',
                              (badge_id,)).fetchone()
        if result:
            return User.get_by_username(result['username'])
    except Exception as e:
        logger.error(f"Badge auth error: {e}")
    
    return None


def authenticate_by_pin(username, pin):
    """Authenticate user by PIN."""
    users = load_users()
    user_data = users.get(username)
    
    if user_data and user_data.get('pin_hash'):
        if check_password_hash(user_data['pin_hash'], pin):
            return User.get_by_username(username)
    
    return None


@pos_bp.route('/logout')
def pos_logout():
    """Logout from POS and clear session."""
    from flask_login import logout_user
    logout_user()
    session.pop('pos_cart', None)
    session.pop('pos_manager_auth', None)
    session.pop('pos_refund_manager_auth', None)
    flash('Logged out.')
    return redirect(url_for('pos.pos_login'))

