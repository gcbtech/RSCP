"""
POS Authentication Module
PIN and badge login for POS operators.
"""
import logging
from flask import request, redirect, url_for, flash, render_template, session
from flask_login import login_user, current_user
from werkzeug.security import check_password_hash

from app.routes.pos import pos_bp
from app.services.db import get_db_connection, get_request_db
from app.services.auth import load_users, User

logger = logging.getLogger(__name__)


@pos_bp.route('/login', methods=['GET', 'POST'])
def pos_login():
    """POS-specific login with PIN or badge support."""
    # If already authenticated, go to sales
    if current_user.is_authenticated:
        return redirect(url_for('pos.sales'))
    
    if request.method == 'POST':
        login_type = request.form.get('login_type', 'password')
        
        if login_type == 'badge':
            badge_id = request.form.get('badge_id', '').strip()
            if badge_id:
                user = authenticate_by_badge(badge_id)
                if user:
                    login_user(user)
                    session['login_time'] = __import__('time').time()
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
                    session['login_time'] = __import__('time').time()
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
                    user = User(username, user_data)
                    login_user(user)
                    session['login_time'] = __import__('time').time()
                    logger.info(f"POS password login: {username}")
                    return redirect(url_for('pos.sales'))
                flash('Invalid username or password.')
    
    return render_template('pos/login.html')


def authenticate_by_badge(badge_id):
    """Authenticate user by badge ID."""
    try:
        conn = get_request_db()
        result = conn.execute('''
            SELECT username, password_hash, is_admin, pin_hash, badge_id
            FROM users WHERE badge_id = ?
        ''', (badge_id,)).fetchone()
        
        if result:
            user_data = {
                'password_hash': result['password_hash'],
                'is_admin': result['is_admin'],
                'pin_hash': result['pin_hash'],
                'badge_id': result['badge_id']
            }
            return User(result['username'], user_data)
    except Exception as e:
        logger.error(f"Badge auth error: {e}")
    
    return None


def authenticate_by_pin(username, pin):
    """Authenticate user by PIN."""
    users = load_users()
    user_data = users.get(username)
    
    if user_data and user_data.get('pin_hash'):
        if check_password_hash(user_data['pin_hash'], pin):
            return User(username, user_data)
    
    return None


@pos_bp.route('/logout')
def pos_logout():
    """Logout from POS and clear session."""
    from flask_login import logout_user
    logout_user()
    session.pop('pos_cart', None)
    session.pop('pos_manager_auth', None)
    session.pop('pos_refund_manager_auth', None)
    session.pop('pos_operator', None)
    session.pop('pos_locked', None)
    flash('Logged out.')
    return redirect(url_for('pos.pos_login'))


@pos_bp.route('/lock', methods=['POST'])
def pos_lock():
    """Lock POS screen - allows switching operators without full logout."""
    from flask_login import login_required
    
    if not current_user.is_authenticated:
        return redirect(url_for('pos.pos_login'))
    
    # Mark session as locked
    session['pos_locked'] = True
    session['pos_locked_by'] = current_user.username
    
    return redirect(url_for('pos.pos_unlock'))


@pos_bp.route('/unlock', methods=['GET', 'POST'])
def pos_unlock():
    """Unlock POS with PIN or Badge - can switch to different operator."""
    from flask import jsonify
    import time
    
    if not current_user.is_authenticated:
        return redirect(url_for('pos.pos_login'))
    
    # If not locked, go to sales
    if not session.get('pos_locked'):
        return redirect(url_for('pos.sales'))
    
    if request.method == 'POST':
        pin = request.form.get('pin', '')
        badge_id = request.form.get('badge_id', '')
        
        user = None
        
        # Try badge first
        if badge_id:
            user = authenticate_by_badge(badge_id)
        
        # Try PIN (need username for PIN)
        if not user and pin:
            # If PIN provided, try all users to find matching PIN
            users = load_users()
            for uname, user_data in users.items():
                if user_data.get('pin_hash'):
                    if check_password_hash(user_data['pin_hash'], pin):
                        # User class expects (id, username, is_admin, roles)
                        user = User(uname, uname, user_data.get('is_admin', False), user_data.get('roles', []))
                        break
        
        if user:
            # Unlock and set operator
            session['pos_locked'] = False
            session['pos_operator'] = user.username
            session['pos_operator_since'] = time.time()
            flash(f'POS unlocked. Operator: {user.username}')
            return redirect(url_for('pos.sales'))
        else:
            flash('Invalid PIN or Badge.')
    
    return render_template('pos/lock_screen.html', 
                           locked_by=session.get('pos_locked_by', 'Unknown'))


def get_pos_operator():
    """Get the current POS operator (may differ from logged-in user)."""
    if session.get('pos_operator'):
        return session['pos_operator']
    if current_user.is_authenticated:
        return current_user.username
    return None

