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
from app.routes.timeclock import timeclock_bp
from app.routes.public_api import public_api_bp
from app.utils.helpers import format_date_filter
from app.services.data_manager import load_config, BASE_DIR
from app import paths
from app.services.migration import run_migrations
from app.services.logger import log_exception, configure_logging
# Imported eagerly so authlib.jose is loaded before anything (e.g. a test that
# stubs out `authlib`) can shadow it.
from app.services import cf_access  # noqa: F401

def _template_config(conf):
    """Settings minus credentials, for templates. Secrets are the registry's
    secret settings plus any key named like one (SECRET/PASS/TOKEN/...).
    Templates can still ask whether a secret is set via <KEY>_SET
    (e.g. config.SSO_CLIENT_SECRET_SET)."""
    from app.services.settings import template_view
    return template_view(conf)


def create_app(test_config=None):
    # Clean up any stale update status file on startup
    try:
        status_file = os.path.join(paths.DATA_DIR, 'update_status.json')
        if os.path.exists(status_file):
            os.remove(status_file)
    except Exception:
        pass

    # Logging: stderr only (journald under systemd), with request id and
    # user on every line. No per-process log files in the code directory.
    configure_logging()
    
    # Schema migrations run synchronously, before this worker serves anything.
    # Workers serialise on a file lock, so only the first one does real work;
    # a failure raises and stops the boot rather than serving a partial schema.
    run_migrations()

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
        # Production: never run with a guessable fallback key. The installer
        # writes one; if it is somehow missing, generate and persist one.
        from app.services.data_manager import ensure_secret_key
        secret_key = ensure_secret_key()

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
    
    # Login, POS PIN/badge, manager-override and pairing-code throttling use
    # app/services/throttle.py (shared across workers via SQLite);
    # Flask-Limiter's memory:// store is per-process.

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
    
    # Uploaded files live in the data directory but keep their historical
    # /static/uploads/... URLs. This rule is more specific than Flask's
    # /static/<path> rule, so it wins for those paths.
    if paths.USES_DATA_DIR:
        from flask import send_from_directory

        def uploaded_file(filename):
            return send_from_directory(paths.UPLOADS_DIR, filename, max_age=604800)

        app.add_url_rule('/static/uploads/<path:filename>', 'uploaded_file', uploaded_file)

    # Blueprints
    app.register_blueprint(main_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(inventory_bp)
    app.register_blueprint(pos_bp)
    app.register_blueprint(timeclock_bp)
    app.register_blueprint(public_api_bp)
    from app.routes.settings import settings_bp
    app.register_blueprint(settings_bp)
    
    # Notifications API
    from app.routes.notifications import notifications_bp
    app.register_blueprint(notifications_bp)
    
    # POS pairing endpoints are rate-limited in app/routes/pos/pairing.py
    # (shared across workers via services/throttle).

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

    # {{ value|js }} -> a JavaScript string/number literal that is safe inside
    # an HTML attribute such as onclick="f({{ value|js }})". Plain HTML
    # escaping is NOT enough there: the browser decodes &#39; back to ' before
    # running the handler, so '{{ name }}' lets a name containing a quote
    # inject script.
    import json as _json
    from markupsafe import escape as _escape
    app.jinja_env.filters['js'] = lambda value: _escape(_json.dumps(value))
    
    # Reverse proxy support: honour X-Forwarded-* only from configured proxies
    # (TRUSTED_PROXIES setting; default loopback, e.g. a local cloudflared).
    from app.utils.proxy import TrustedProxyFix
    app.wsgi_app = TrustedProxyFix(app.wsgi_app, (load_config() or {}).get('TRUSTED_PROXIES'))

    # Cookies. Secure-only cookies would break plain-http LAN access
    # (http://<ip>:5000), so they are opt-in: the SECURE_COOKIES setting or
    # FLASK_ENV=production. The remember-me cookie gets the same treatment as
    # the session cookie instead of Flask-Login's 365-day, no-SameSite default.
    secure_cookies = bool((load_config() or {}).get('SECURE_COOKIES')) or \
        os.environ.get('FLASK_ENV') == 'production'
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=secure_cookies,
        REMEMBER_COOKIE_HTTPONLY=True,
        REMEMBER_COOKIE_SAMESITE='Lax',
        REMEMBER_COOKIE_SECURE=secure_cookies,
        REMEMBER_COOKIE_DURATION=timedelta(days=30),
    )
    
    # CSRF & Security Headers
    from app.services.csrf import generate_csrf_token, verify_csrf_token
    
    @app.before_request
    def require_login():
        """Global authentication check - all routes require login except whitelist."""
        try:
            from flask_login import current_user, logout_user
            from flask import redirect, url_for, flash
            import time

            # Public paths that must match EXACTLY. The index page renders or
            # redirects for itself. (Historically '/' was matched with
            # startswith, which — since every path starts with '/' — silently
            # disabled this entire guard.)
            public_exact = {'/', '/favicon.ico', '/healthz'}

            # Public path PREFIXES: truly public pages, plus endpoints that
            # authenticate themselves (API key / pairing token) and therefore
            # must not be bounced to the login page by the session guard.
            #   /login*, /logout, /setup     - auth + first-run wizard
            #   /static/                     - assets
            #   /api/public/                 - storefront API (API key auth)
            #   /pos/customer-display + /pos/api/{pairing,peripheral,
            #     customer-display}/         - unattended POS displays, which
            #                                  authenticate by pairing token so
            #                                  an expired staff session can
            #                                  never take a display down
            public_prefixes = (
                '/login', '/logout', '/setup', '/static/',
                '/api/public/',
                '/pos/customer-display',
                '/pos/api/pairing/', '/pos/api/peripheral/',
                '/pos/api/customer-display/',
            )

            is_public = request.path in public_exact or request.path.startswith(public_prefixes)

            # Cloudflare Access: a verified Access JWT signs the matching user
            # in before they would be sent to the login page. Tried on page
            # loads only (GET/HEAD), on protected paths plus '/' and '/login'
            # (where a tunnelled user lands); failures fall through to the
            # normal flow. See app/services/cf_access.py.
            if (request.method in ('GET', 'HEAD')
                    and (not is_public or request.path in ('/', '/login'))
                    and request.headers.get(cf_access.HEADER)
                    and not current_user.is_authenticated):
                from app.routes.auth import cf_access_login
                if cf_access_login() and request.path == '/login':
                    from app.utils.helpers import is_safe_redirect
                    next_page = request.args.get('next')
                    return redirect(next_page if is_safe_redirect(next_page) else url_for('main.index'))

            if is_public:
                return None

            # Check if user is authenticated
            if not current_user.is_authenticated:
                # JSON callers get a 401 they can act on, not a login page
                from app.utils.permissions import wants_json, unauthenticated_response
                if wants_json():
                    return unauthenticated_response(as_json=True)
                # Redirect to login with 'next' parameter to return after login
                return redirect(url_for('auth.login', next=request.url))
            
            # Check session timeout (if enabled)
            from app.services.data_manager import load_config
            config = load_config() or {}
            timeout_enabled = config.get('SESSION_TIMEOUT_ENABLED', False)
            timeout_minutes = config.get('SESSION_TIMEOUT_MINUTES', 30)
            
            if timeout_enabled and timeout_minutes > 0:
                login_time = session.get('login_time')
                if not login_time:
                    # Session restored from a remember-me cookie: start the
                    # inactivity clock now rather than never timing out.
                    session['login_time'] = time.time()
                elif login_time:
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
        # Skip CSRF for login/logout/setup (no session may exist yet) and for
        # the public API, which authenticates with an API key, not a cookie.
        # Every other route, including session-authenticated /api/* JSON
        # endpoints, must send the token (form field or X-CSRFToken header).
        exempt_paths = ['/login', '/logout', '/setup']
        if request.path in exempt_paths or request.path.startswith(('/static', '/api/public/')):
            return
        verify_csrf_token()
        
    @app.after_request
    def security_headers(response):
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        # Content Security Policy - Prevent XSS attacks
        # No 'unsafe-inline' for scripts: pages carry no inline JavaScript
        # (static/js/pages/*.js + data-action handlers, see rscp-common.js),
        # so an injected <script> or onclick= cannot run.
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; "
            "script-src 'self' https://cdn.jsdelivr.net; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; "
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
    version_file = paths.VERSION_FILE
    try:
        with open(version_file, 'r') as f:
            app_version = f.read().strip()
    except IOError:
        app_version = "unknown"

    @app.route('/healthz')
    def healthz():
        """Unauthenticated liveness check for the updater and monitoring:
        200 + version when the app can read its database, else 503."""
        from flask import jsonify
        from app.services.db import get_db_connection
        try:
            conn = get_db_connection()
            try:
                conn.execute('SELECT count(*) FROM sqlite_master').fetchone()
            finally:
                conn.close()
        except Exception:
            return jsonify(status='error', version=app_version), 503
        return jsonify(status='ok', version=app_version)
    
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
        from app.utils.helpers import guess_shipper
        from app.utils.permissions import has_permission
        # Kept for old templates; new ones use has_permission(current_user, 'admin.manage')
        is_admin = has_permission(current_user, 'admin.manage')
        
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
            'config': _template_config(c),
        }
        
    return app
