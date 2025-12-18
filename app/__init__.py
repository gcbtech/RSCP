from flask import Flask, session, g, request
import os
import logging
from datetime import timedelta
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Import Routes
from app.routes.main import main_bp
from app.routes.admin import admin_bp
from app.utils.helpers import format_date_filter
from app.services.data_manager import load_config, BASE_DIR
from app.services.migration import ensure_db_ready
from app.services.logger import log_exception

def create_app():
    # Logging Setup
    logging.basicConfig(
        filename=os.path.join(BASE_DIR, 'app.log'),
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # DB Migration (Async)
    import threading
    def run_db_init():
        try:
            ensure_db_ready()
        except Exception as e:
            logging.error(f"Startup DB Init Error: {e}")
            
    threading.Thread(target=run_db_init, daemon=True).start()

    app = Flask(__name__, template_folder='../templates', static_folder='../static')
    
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
    
    # Blueprints
    app.register_blueprint(main_bp)
    app.register_blueprint(admin_bp)
    
    # Global Error Handler
    @app.errorhandler(500)
    def internal_error(error):
        log_exception(error, source="System")
        return "Internal Server Error - Admins have been notified.", 500
        
    @app.errorhandler(Exception)
    def unhandled_exception(e):
        # Catch-all for non-500 exceptions if they propagate here
        log_exception(e, source="System (Unhandled)")
        return "Internal Application Error", 500
    
    # Filters
    app.jinja_env.filters['format_date'] = format_date_filter
    
    # Reverse Proxy Support - Trust X-Forwarded-* headers
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    
    # Security Config - For HTTPS reverse proxy access
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='None',  # Required for cross-site cookies with Secure
        SESSION_COOKIE_SECURE=True,      # Required for HTTPS access
    )
    
    # CSRF & Security Headers
    from app.services.csrf import generate_csrf_token, verify_csrf_token
    
    @app.before_request
    def check_csrf():
        verify_csrf_token()
        
    @app.after_request
    def security_headers(response):
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        # HSTS: Enable when behind HTTPS (most cloud providers handle TLS)
        if request.is_secure or request.headers.get('X-Forwarded-Proto') == 'https':
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        return response

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
        # Favicon from static folder
        favicon = "/static/favicon.png"
        
        return {
            'app_name': "RSCP",
            'app_version': app_version,
            'org_name': org,
            'favicon': favicon,
            'trim_enabled': trim_enabled,
            'is_admin': session.get('is_admin', False),
            'local_user': session.get('user', None),
            'csrf_token': generate_csrf_token
        }
        
    return app
