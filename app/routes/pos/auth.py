"""
POS Authentication Module
PIN and badge login for POS operators.
"""
import logging
import time
from collections import defaultdict
from flask import request, redirect, url_for, flash, render_template, session
from flask_login import login_user, current_user
from werkzeug.security import check_password_hash

from app.routes.pos import pos_bp
from app.services.db import get_db_connection, get_request_db
from app.services.auth import load_users, User
from app.utils.permissions import has_permission

logger = logging.getLogger(__name__)

# Rate limiting for POS login (in-memory, per worker)
_pos_login_attempts = defaultdict(list)
_POS_RATE_LIMIT = 5  # max attempts
_POS_RATE_WINDOW = 60  # seconds


def check_pos_rate_limit(identifier: str) -> bool:
    """
    Check if identifier (IP address) is rate limited.
    Returns True if rate limited (blocked), False if allowed.
    """
    current = time.time()
    # Clean old attempts
    _pos_login_attempts[identifier] = [
        t for t in _pos_login_attempts[identifier] 
        if current - t < _POS_RATE_WINDOW
    ]
    
    if len(_pos_login_attempts[identifier]) >= _POS_RATE_LIMIT:
        return True
    
    _pos_login_attempts[identifier].append(current)
    return False


def get_client_ip() -> str:
    """Get client IP, accounting for reverse proxy."""
    # X-Forwarded-For is handled by ProxyFix middleware
    return request.remote_addr or 'unknown'


@pos_bp.route('/login', methods=['GET', 'POST'])
def pos_login():
    """POS-specific login with PIN or badge support."""
    # If already authenticated, go to sales
    if current_user.is_authenticated:
        return redirect(url_for('pos.sales'))
    
    if request.method == 'POST':
        # Rate limiting check
        client_ip = get_client_ip()
        if check_pos_rate_limit(client_ip):
            logger.warning(f"POS rate limit exceeded for IP: {client_ip}")
            flash('Too many login attempts. Please wait a minute.')
            return render_template('pos/login.html')
        
        login_type = request.form.get('login_type', 'password')
        
        if login_type == 'badge':
            badge_id = request.form.get('badge_id', '').strip()
            if badge_id:
                user = authenticate_by_badge(badge_id)
                if user:
                    login_user(user)
                    session['login_time'] = time.time()
                    logger.info(f"POS badge login: {user.username}")
                    return redirect(url_for('pos.sales'))
                flash('Invalid badge.')
        
        elif login_type == 'pin':
            username = request.form.get('username', '').strip()
            pin = request.form.get('pin', '')
            
            if username and pin:
                user = authenticate_by_pin(username, pin)
                if user:
                    login_user(user)
                    session['login_time'] = time.time()
                    logger.info(f"POS PIN login: {user.username}")
                    return redirect(url_for('pos.sales'))
                flash('Invalid username or PIN.')
        
        else:  # Standard password login
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            
            if username and password:
                users = load_users()
                user_data = users.get(username)
                
                if user_data and check_password_hash(user_data['password_hash'], password):
                    user = User.get_by_username(username)
                    login_user(user)
                    session['login_time'] = time.time()
                    logger.info(f"POS password login: {username}")
                    return redirect(url_for('pos.sales'))
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

