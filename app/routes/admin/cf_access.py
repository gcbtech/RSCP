"""
Admin > Settings > Cloudflare Access: the "Test" button.

GET /admin/cf-access/test reports whether the admin's own current request
carried a valid Cloudflare Access JWT and which RSCP user it maps to, using the
saved team domain and AUD tag (whether or not the feature is enabled yet). It
signs nobody in and never returns the token.
"""
import datetime

from flask import jsonify, request

from app.routes.admin import admin_bp
from app.services import cf_access


@admin_bp.route('/cf-access/test')
def cf_access_test():
    conf = cf_access.get_config()
    result = {
        'enabled': conf['enabled'],
        'configured': bool(conf['team_domain'] and conf['aud']),
        'team_domain': conf['team_domain'],
        'header_present': bool(request.headers.get(cf_access.HEADER)),
        'valid': False,
        'email': None,
        'user': None,
        'expires': None,
        'error': None,
    }
    try:
        user, email, claims = cf_access.check_request(request, conf)
    except cf_access.CfAccessError as e:
        result['error'] = e.reason
        # A valid token whose email has no account is still worth reporting.
        claims = getattr(e, 'claims', None)
    else:
        result.update(valid=True, email=email, user=user.username)
    if claims and isinstance(claims.get('exp'), (int, float)):
        result['expires'] = datetime.datetime.fromtimestamp(
            claims['exp'], datetime.timezone.utc).isoformat(timespec='seconds')
    result['would_sign_in'] = bool(result['valid'] and result['enabled'] and result['configured'])
    return jsonify(result)
