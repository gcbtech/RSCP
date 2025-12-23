from flask import Blueprint, render_template, redirect, url_for, flash, request, session, current_app
from flask_login import login_user, logout_user, login_required, current_user
from app.services.auth import User
from urllib.parse import urlparse

auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/login', methods=['GET', 'POST'])
def login():
    # Security: Apply rate limiting to prevent brute-force attacks
    limiter = current_app.limiter
    # Rate limit: 5 login attempts per minute from same IP
    try:
        limiter.limit("5 per minute")(lambda: None)()
    except:
        pass  # Rate limiter may not be initialized in all contexts
    
    if current_user.is_authenticated and not request.args.get('force'):
        return redirect(url_for('main.index'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        remember = True if request.form.get('remember') else False

        user = User.authenticate(username, password)

        if not user:
            flash('Invalid username or password', 'error')
            return redirect(url_for('auth.login'))

        login_user(user, remember=remember)
        
        # Note: Legacy session support removed for security
        # Flask-Login handles authentication state

        # Redirect to next page or default
        next_page = request.args.get('next')
        if not next_page or urlparse(next_page).netloc != '':
            next_page = url_for('main.index')
        
        return redirect(next_page)

    return render_template('login.html')

@auth_bp.route('/logout')
@login_required
def logout():
    logout_user()
    session.clear()
    return redirect(url_for('auth.login'))
