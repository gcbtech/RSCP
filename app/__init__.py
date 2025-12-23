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
from app.utils.helpers import format_date_filter
from app.services.data_manager import load_config, BASE_DIR
from app.services.migration import ensure_db_ready
from app.services.logger import log_exception

def create_app():
    # TODO: Remove debug print statement for production
    print("DEBUG: Starting create_app...")
    # Logging Setup
    from logging.handlers import RotatingFileHandler
    
    file_handler = RotatingFileHandler(
        os.path.join(BASE_DIR, 'app.log'), 
        maxBytes=10*1024*1024, # 10MB
        backupCount=5
    )
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] [REQ_%(thread)d] %(message)s' 
    ))
    file_handler.setLevel(logging.INFO)
    
    # Root logger
    logging.getLogger().addHandler(file_handler)
    logging.getLogger().setLevel(logging.INFO)
    
    # DB Migration (Async)
    import threading
    def run_db_init():
        try:
            ensure_db_ready()
        except Exception as e:
            logging.error(f"Startup DB Init Error: {e}")
            
    threading.Thread(target=run_db_init, daemon=True).start()

    app = Flask(__name__, template_folder='../templates', static_folder='../static')
    
    # Request Correlation ID
    import uuid
    @app.before_request
    def add_request_id():
        g.request_id = str(uuid.uuid4())[:8] # Short UUID
        g.user_id = session.get('user', 'Guest')

    # Config
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
        except:
            pass  # DB not ready yet, first run
        secret_key = 'temp_setup_key'
    
    app.secret_key = secret_key
    app.permanent_session_lifetime = timedelta(days=7)
    
    # File Upload Size Limit (32MB)
    app.config['MAX_CONTENT_LENGTH'] = 32 * 1024 * 1024
    
    # Rate Limiter (attached to app, used in routes)
    limiter = Limiter(
        key_func=get_remote_address,
        app=app,
        default_limits=[],
        storage_uri="memory://"
    )
    app.limiter = limiter  # Make accessible to blueprints
    
    # Database connection cleanup (for request-scoped connections)
    from app.services.db import init_app as init_db_app
    init_db_app(app)
    
    # Blueprints
    app.register_blueprint(main_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(inventory_bp)
    
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
        # Catch-all for non-RscpError exceptions
        log_exception(e, source="UnhandledCrasher")
        return render_template('error.html', 
                             error_code="RSCP-999", 
                             message="Internal Application Error",
                             request_id=getattr(g, 'request_id', 'Initial')), 500
    
    # Filters
    app.jinja_env.filters['format_date'] = format_date_filter
    
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
    def check_csrf():
        # Skip CSRF for setup wizard (no session exists yet)
        if request.path == '/setup':
            return
        verify_csrf_token()
        
    @app.after_request
    def security_headers(response):
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        # Content Security Policy - Prevent XSS attacks
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data: https: http:; "
            "connect-src 'self'"
        )
        if request.is_secure or request.headers.get('X-Forwarded-Proto') == 'https':
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
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
    except:
        app_version = "unknown"
    
    @app.context_processor
    def inject_globals():
        c = load_config()
        org = c.get('ORG_NAME', '') if c else ''
        trim_enabled = c.get('AUTO_TRIM', False) if c else False
        inventory_enabled = c.get('INVENTORY_ENABLED', False) if c else False
        
        # Get inventory count if enabled (uses shared request connection)
        inventory_count = 0
        if inventory_enabled:
            try:
                from app.services.db import get_request_db
                conn = get_request_db()
                result = conn.execute('SELECT COUNT(*) as cnt FROM inventory_items').fetchone()
                inventory_count = result['cnt'] if result else 0
                # No need to close - teardown handles it
            except:
                pass
        
        # Favicon from static folder
        favicon = "/static/favicon.png"
        
        return {
            'app_name': "RSCP",
            'app_version': app_version,
            'org_name': org,
            'favicon': favicon,
            'trim_enabled': trim_enabled,
            'inventory_enabled': inventory_enabled,
            'inventory_count': inventory_count,
            'is_admin': session.get('is_admin', False),
            'local_user': session.get('user', None),
            'csrf_token': generate_csrf_token
        }
        
    return app
