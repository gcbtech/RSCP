"""
Security Middleware
Adds strict security headers to all responses.
"""

def add_security_headers(response):
    """
    Add additional security headers to the response.
    Registered as an after_request callback.
    """
    # Prevent information leakage
    # Note: simple_server (Flask dev) might overwrite Server, but Gunicorn usually respects this
    response.headers['X-Powered-By'] = 'RSCP' 
    response.headers['Server'] = 'RSCP'
    
    # Referrer policy - protect user privacy logic
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    
    # Permissions policy (disable privileges we don't need)
    # Camera IS needed for barcode scanning (if using webcam) - so allow it self
    # Geolocation not needed
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=(self)'
    
    # Block FLoC / cohort tracking (legacy checking)
    response.headers['Permissions-Policy'] += ', interest-cohort=()'
    
    return response
