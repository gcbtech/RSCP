from flask import Flask, session, g, request
import os
import logging
from datetime import timedelta
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Import Routes
from app.routes.main import main_bp
from app.routes.admin import admin_bp
from app.routes.inventory import inventory_bp
from app.routes.pos import pos_bp
from app.routes.federation import federation_bp
from app.routes.timeclock import timeclock_bp
from app.routes.public_api import public_api_bp
from app.utils.helpers import format_date_filter
from app.services.data_manager import load_config, BASE_DIR
from app.services.migration import ensure_db_ready
from app.services.logger import log_exception

def create_app(test_config=None):
    # Clean up any stale update status file on startup
    try:
        status_file = os.path.join(BASE_DIR, 'update_status.json')
        if os.path.exists(status_file):
            os.remove(status_file)
    except Exception:
        pass

    # Logging Setup
    from logging.handlers import RotatingFileHandler
    
    file_handler = None
    try:
        file_handler = RotatingFileHandler(
            os.path.join(BASE_DIR, 'app.log'), 
            maxBytes=10*1024*1024, # 10MB
            backupCount=5
        )
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] [REQ_%(thread)d] %(message)s' 
        ))
        file_handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(file_handler)
    except Exception as e:
        # Fallback to console if file write fails (e.g. production permissions or tests)
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        logging.getLogger().addHandler(console)
        print(f"Warning: Could not set up file logging: {e}")

    logging.getLogger().setLevel(logging.INFO)
    
    # DB Migration (Async) - Skip during testing
    import sys
    is_testing = (test_config and test_config.get('TESTING')) or 'pytest' in sys.modules or os.environ.get('TESTING') == 'True'
    if not is_testing:
        import threading
        def run_db_init():
            try:
                ensure_db_ready()
            except Exception as e:
                logging.error(f"Startup DB Init Error: {e}")
                
        threading.Thread(target=run_db_init, daemon=True).start()
    else:
        try:
            ensure_db_ready()
        except Exception as e:
            logging.error(f"Test DB Init Error: {e}")

    app = Flask(__name__, template_folder='../templates', static_folder='../static')
    
    # Request Correlation ID
    import uuid
    @app.before_request
    def add_request_id():
        g.request_id = str(uuid.uuid4())[:8] # Short UUID
        
        # Try to get logged in user
        try:
            from flask_login import current_user
            if current_user and current_user.is_authenticated:
                g.user_id = current_user.username
            else:
                g.user_id = 'Guest'
        except Exception:
            g.user_id = 'Guest'

    # Config
    if test_config:
        # Load test config
        app.config.update(test_config)
        secret_key = app.config.get('SECRET_KEY', 'test_key')
        
    else:
        # Load production config
        conf = load_config()
        secret_key = conf.get('SECRET_KEY')
        
        # SECRET_KEY validation: Only allow fallback during first-run setup
        if not secret_key:
            # Check if this is a first-run scenario (no users yet)
            try:
                from app.services.db import get_db_connection
                conn = get_db_connection()
                user_count = conn.execute("SELECT count(*) as c FROM users").fetchone()['c']
                conn.close()
                if user_count > 0:
                    logging.warning("SECRET_KEY missing from config.json but users exist. Using temp key - sessions will be invalid!")
            except Exception:
                pass  # DB not ready yet, first run
            secret_key = 'temp_setup_key'
            
    app.secret_key = secret_key
    app.permanent_session_lifetime = timedelta(days=7)
    
    # File Upload Size Limit (32MB)
    app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024
    
    # Rate Limiter (attached to app, used in routes)
    # Per-IP rate limiting to prevent abuse
    limiter = Limiter(
        key_func=get_remote_address,
        app=app,
        default_limits=[],
        storage_uri="memory://"
    )
    app.limiter = limiter  # Make accessible to blueprints
    
    # Static file caching (1 week for images, CSS, JS)
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 604800  # 7 days in seconds
    
    # Apply rate limits to specific routes
    # Login: 5 attempts per minute per IP (brute-force protection)
    limiter.limit("5 per minute")(app.view_functions.get('auth.login', lambda: None))
    
    # Database connection cleanup (for request-scoped connections)
    from app.services.db import init_app as init_db_app
    init_db_app(app)

    # Initialize Authlib (SSO)
    from authlib.integrations.flask_client import OAuth
    oauth = OAuth(app)
    
    # Register OIDC Provider if configured
    # We use a try/except block to avoid crashing if config is missing (will just disable SSO)
    try:
        # load_config is already imported globally
        conf = load_config()
        if conf.get('SSO_CLIENT_ID') and conf.get('SSO_CLIENT_SECRET'):
            oauth.register(
                name='rscp_sso',
                client_id=conf.get('SSO_CLIENT_ID'),
                client_secret=conf.get('SSO_CLIENT_SECRET'),
                server_metadata_url=conf.get('SSO_DISCOVERY_URL', 'https://accounts.google.com/.well-known/openid-configuration'),
                client_kwargs={'scope': 'openid email profile'}
            )
    except Exception as e:
        app.logger.warning(f"SSO Registration Failed: {e}")

    # Make oauth available to blueprints
    app.oauth = oauth
    
    # Blueprints
    app.register_blueprint(main_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(pos_bp)
    app.register_blueprint(federation_bp)
    app.register_blueprint(timeclock_bp)
    app.register_blueprint(public_api_bp)
    
    # Notifications API
    from app.routes.notifications import notifications_bp
    app.register_blueprint(notifications_bp)
    
    # Initialize SocketIO for POS terminal pairing
    try:
        from app.services.websocket import init_socketio
        socketio = init_socketio(app)
        app.socketio = socketio
        logging.info("SocketIO initialized for terminal pairing")
    except ImportError as e:
        logging.warning(f"SocketIO not available (flask-socketio not installed): {e}")
        app.socketio = None
    except Exception as e:
        logging.warning(f"SocketIO initialization failed: {e}")
        app.socketio = None
    
    # Global Error Handler
    from app.utils.errors import RscpError
    from flask import render_template
    
    @app.errorhandler(RscpError)
    def handle_rscp_error(error):
        log_exception(error, source="AppError")
        return render_template('error.html', 
                             error_code=error.code, 
                             message=error.message,
                             request_id=getattr(g, 'request_id', 'Initial')), error.status_code

    @app.errorhandler(Exception)
    def unhandled_exception(e):
        # Handle standard HTTP errors (e.g. 404, 403, 405)
        # We must catch this before 'UnhandledCrasher' to prevent rapid scanning
        # or invalid URLs from triggering the RSCP-999 crash screen
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            if request.path.startswith('/api/'):
                from flask import jsonify
                return jsonify({
                    'error': e.name if hasattr(e, 'name') else 'HTTP Error',
                    'message': e.description if hasattr(e, 'description') else str(e),
                    'request_id': getattr(g, 'request_id', 'unknown')
                }), e.code
                
            return render_template('error.html', 
                                 error_code=f"HTTP {e.code}", 
                                 message=e.description if hasattr(e, 'description') else "An HTTP error occurred.",
                                 request_id=getattr(g, 'request_id', 'Initial')), e.code
                                 
        # Catch-all for non-RscpError exceptions
        log_exception(e, source="UnhandledCrasher")
        
        # Return JSON for API routes (no traceback exposure for security)
        if request.path.startswith('/api/'):
            from flask import jsonify
            return jsonify({
                'error': 'Internal server error',
                'request_id': getattr(g, 'request_id', 'unknown')
            }), 500
        
        return render_template('error.html', 
                             error_code="RSCP-999", 
                             message="Internal Application Error",
                             request_id=getattr(g, 'request_id', 'Initial')), 500
    
    # Filters
    from app.utils.helpers import local_time_filter
    app.jinja_env.filters['format_date'] = format_date_filter
    app.jinja_env.filters['local_time'] = local_time_filter
    
    # Reverse Proxy Support - Trust X-Forwarded-* headers
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    
    # Security Config - For HTTPS reverse proxy access
    # Set SESSION_COOKIE_SECURE based on environment (detect HTTPS via proxy headers)
    is_production = os.environ.get('FLASK_ENV') == 'production'
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=is_production,  # True in production with HTTPS
    )
    
    # CSRF & Security Headers
    from app.services.csrf import generate_csrf_token, verify_csrf_token
    
    @app.before_request
    def require_login():
        """Global authentication check - all routes require login except whitelist."""
        try:
            from flask_login import current_user, logout_user
            import time
            
            # Whitelist: Routes that don't require authentication
            public_paths = [
                '/',  # Index will handle its own redirect
                '/login',
                '/logout', 
                '/setup',
                '/static/',
                '/favicon.ico',
                '/api/public/',  # Public storefront API (uses API key auth)
            ]
            
            # Check if current path is public
            for public_path in public_paths:
                if request.path == public_path or request.path.startswith(public_path):
                    return None
            
            # Check if user is authenticated
            if not current_user.is_authenticated:
                # Redirect to login with 'next' parameter to return after login
                return redirect(url_for('auth.login', next=request.url))
            
            # Check session timeout (if enabled)
            from app.services.data_manager import load_config
            config = load_config() or {}
            timeout_enabled = config.get('SESSION_TIMEOUT_ENABLED', False)
            timeout_minutes = config.get('SESSION_TIMEOUT_MINUTES', 30)
            
            if timeout_enabled and timeout_minutes > 0:
                login_time = session.get('login_time')
                if login_time:
                    elapsed = time.time() - login_time
                    if elapsed > (timeout_minutes * 60):
                        logout_user()
                        session.clear()
                        flash('Session expired due to inactivity. Please log in again.', 'info')
                        return redirect(url_for('auth.login'))
                    # Update activity time on each request
                    session['login_time'] = time.time()
                    
        except Exception as e:
            # If any error occurs checking auth, redirect to login
            import logging
            logging.getLogger(__name__).error(f"Auth check error: {e}")
            return redirect(url_for('auth.login'))
    
    @app.before_request
    def check_csrf():
        # Skip CSRF for login/logout/setup (no session may exist yet)
        # Skip CSRF for /api/ routes (they use API key auth, not sessions)
        # Skip CSRF for JSON-only federation routes
        exempt_paths = ['/login', '/logout', '/setup', 
                       '/inventory/request-transfer', '/inventory/federated-search']
        if any(request.path == p for p in exempt_paths) or request.path.startswith('/static') or request.path.startswith('/api/'):
            return
        verify_csrf_token()
        
    @app.after_request
    def security_headers(response):
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        # Content Security Policy - Prevent XSS attacks
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdn.socket.io; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https: http:; "
            "connect-src 'self' ws: wss:"
        )
        if request.is_secure or request.headers.get('X-Forwarded-Proto') == 'https':
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
            
        # Additional Security Headers (Middleware)
        from app.services.security_middleware import add_security_headers
        response = add_security_headers(response)
        
        return response

    # Authentication (Flask-Login)
    try:
        from flask_login import LoginManager
        login_manager = LoginManager()
        login_manager.login_view = 'auth.login'
        login_manager.login_message_category = 'warning'
        login_manager.init_app(app)

        # Initialize User Loader
        from app.services.auth import load_user
        @login_manager.user_loader
        def user_loader_callback(user_id):
            return load_user(user_id)


        # Register Auth Blueprint
        from app.routes.auth import auth_bp
        app.register_blueprint(auth_bp)

    except Exception as e:
        logging.critical(f"CRITICAL AUTH ERROR: Could not initialize Authentication system. Error: {e}")
        # Start without Auth for debugging purposes
        pass

    # Load version from VERSION file

    # Load version from VERSION file
    version_file = os.path.join(BASE_DIR, 'VERSION')
    try:
        with open(version_file, 'r') as f:
            app_version = f.read().strip()
    except IOError:
        app_version = "unknown"
    
    @app.context_processor
    def inject_globals():
        c = load_config()
        org = c.get('ORG_NAME', '') if c else ''
        trim_enabled = c.get('AUTO_TRIM', False) if c else False
        inventory_enabled = c.get('INVENTORY_ENABLED', False) if c else False
        pos_enabled = c.get('POS_ENABLED', False) if c else False
        
        # Get inventory count if enabled (uses shared request connection)
        inventory_count = 0
        if inventory_enabled:
            try:
                from app.services.db import get_request_db
                conn = get_request_db()
                result = conn.execute('SELECT COUNT(*) as cnt FROM inventory_items WHERE COALESCE(is_legacy, 0) = 0').fetchone()
                inventory_count = result['cnt'] if result else 0
                # No need to close - teardown handles it
            except Exception:
                pass  # Inventory table may not exist yet
        
        # Favicon from static folder
        favicon = "/static/favicon.png"
        
        # Get user info from Flask-Login's current_user instead of session
        from flask_login import current_user
        local_user = current_user.username if current_user.is_authenticated else None
        is_admin = current_user.is_admin if current_user.is_authenticated else False
        
        from app.utils.helpers import guess_shipper
        from app.utils.permissions import has_permission
        
        return {
            'app_name': "RSCP",
            'app_version': app_version,
            'org_name': org,
            'favicon': favicon,
            'trim_enabled': trim_enabled,
            'inventory_enabled': inventory_enabled,
            'inventory_count': inventory_count,
            'pos_enabled': pos_enabled,
            'is_admin': is_admin,
            'local_user': local_user,
            'csrf_token': generate_csrf_token,
            'guess_shipper': guess_shipper,
            'has_permission': has_permission,
            'config': c  # Full config for templates that need it
        }
        
    return app
