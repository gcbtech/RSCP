"""
Cloudflare Access single sign-on (off by default).

Remote staff reach RSCP through a cloudflared tunnel guarded by Cloudflare
Access. Access adds a signed JWT to every request it proxies, in the
`Cf-Access-Jwt-Assertion` header. When CF_ACCESS_ENABLED is on, the login
guard (app/__init__.py) verifies that token and signs the matching RSCP user
in, so staff don't have to log in a second time.

Trust model: headers are trivially spoofable by anyone who can reach RSCP
directly (e.g. http://<ip>:5000 on the LAN); the token's RS256 signature is
not. Only the signature is trusted, checked against the team's public keys
fetched over HTTPS from https://<team>.cloudflareaccess.com/cdn-cgi/access/certs.
On top of that the token must be:
  - alg RS256 (never 'none' or HS*: an HMAC "signature" keyed with the public
    key would otherwise be forgeable),
  - for this Access application (aud contains CF_ACCESS_AUD, which is required),
  - issued by this team (iss == https://<team domain>),
  - current (exp required; exp / nbf / iat checked with LEEWAY seconds of
    clock skew).
The checks are done here with the `cryptography` package (RSASSA-PKCS1-v1_5
with SHA-256); no JOSE library is involved.

The `email` claim is mapped to an existing account whose email matches
(case-insensitive): no fallback to the username, no auto-provisioning. An
optional domain allowlist applies (CF_ACCESS_ALLOWED_DOMAINS).

Tokens are never logged; failures log a short reason only.

The keys are cached per process for JWKS_TTL seconds. A token signed with an
unknown `kid` (Cloudflare rotates keys) triggers a refetch, at most once per
JWKS_MIN_REFRESH seconds so that garbage kids cannot make RSCP hammer
Cloudflare.
"""
import base64
import json
import logging
import re
import threading
import time

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

logger = logging.getLogger(__name__)

HEADER = 'Cf-Access-Jwt-Assertion'
TEAM_DOMAIN_SUFFIX = '.cloudflareaccess.com'

ALGORITHMS = ['RS256']      # the only one accepted; see verify_token()
MIN_KEY_BITS = 2048         # smaller RSA keys in the JWKS are ignored
LEEWAY = 60                 # seconds of clock skew allowed on exp/nbf/iat
MAX_TOKEN_BYTES = 16384     # anything bigger is not an Access token
FETCH_TIMEOUT = 5           # seconds, for the JWKS request
JWKS_TTL = 3600             # seconds a fetched key set is trusted without refetch
JWKS_MIN_REFRESH = 30       # seconds between fetches (unknown kid / failures)

# After an explicit logout this cookie stops the header from signing the
# browser straight back in. It is cleared by the next explicit sign-in
# (password, or the "Sign in with Cloudflare Access" button) and expires
# on its own after LOGOUT_SUPPRESS_SECONDS. Forging it only *prevents*
# automatic sign-in, so it needs no signature.
SUPPRESS_COOKIE = 'rscp_cf_logged_out'
LOGOUT_SUPPRESS_SECONDS = 12 * 3600

_jwks_cache = {}            # team domain -> {'keys': {kid: RSAPublicKey}, 'fetched': t, 'attempted': t}
_jwks_lock = threading.Lock()


class CfAccessError(Exception):
    """Why a request could not be signed in. `reason` is safe to log; `public`
    is safe to show the user on the login page."""

    def __init__(self, reason, public=None):
        super().__init__(reason)
        self.reason = reason
        self.public = public or 'Your Cloudflare Access sign-in could not be verified.'
        self.claims = None


# --- settings -----------------------------------------------------------------

_HOST_LABEL = re.compile(r'^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$')


def normalize_team_domain(value):
    """'gcbcomputers', 'https://gcbcomputers.cloudflareaccess.com/' ->
    'gcbcomputers.cloudflareaccess.com'. '' stays ''. Raises ValueError.

    Restricted to *.cloudflareaccess.com: the keys that RSCP trusts are
    fetched from this host, so it must be Cloudflare's."""
    v = (value or '').strip().lower()
    if not v:
        return ''
    v = re.sub(r'^https://', '', v).split('/', 1)[0]
    if '.' not in v:
        v += TEAM_DOMAIN_SUFFIX
    labels = v.split('.')
    if (not v.endswith(TEAM_DOMAIN_SUFFIX) or len(labels) != 3
            or not all(_HOST_LABEL.match(label) for label in labels)):
        raise ValueError('enter your team domain, e.g. yourteam.cloudflareaccess.com')
    return v


def validate_aud(value):
    v = (value or '').strip()
    if v and not re.fullmatch(r'[A-Za-z0-9_-]{16,256}', v):
        raise ValueError('paste the Application Audience (AUD) tag exactly as Cloudflare shows it')
    return v


def get_config():
    from app.services import settings as store
    domains = store.get('CF_ACCESS_ALLOWED_DOMAINS') or []
    return {
        'enabled': bool(store.get('CF_ACCESS_ENABLED')),
        'team_domain': store.get('CF_ACCESS_TEAM_DOMAIN') or '',
        'aud': store.get('CF_ACCESS_AUD') or '',
        'allowed_domains': [d.lower() if d.startswith('@') else '@' + d.lower() for d in domains],
    }


def is_enabled():
    try:
        conf = get_config()
    except Exception:
        return False
    return conf['enabled'] and bool(conf['team_domain']) and bool(conf['aud'])


# --- signing keys -------------------------------------------------------------

def certs_url(team_domain):
    return f'https://{team_domain}/cdn-cgi/access/certs'


def _fetch_jwks(team_domain):
    """The team's JSON Web Key Set (dict). Network call; mocked in tests."""
    resp = requests.get(certs_url(team_domain), timeout=FETCH_TIMEOUT, allow_redirects=False)
    resp.raise_for_status()
    return resp.json()


def _b64url_decode(segment):
    """Strict base64url (RFC 7515, unpadded) -> bytes. Raises ValueError."""
    if not isinstance(segment, str) or not re.fullmatch(r'[A-Za-z0-9_-]*', segment) \
            or len(segment) % 4 == 1:
        raise ValueError('not base64url')
    return base64.urlsafe_b64decode(segment + '=' * (-len(segment) % 4))


def _b64url_uint(segment):
    value = _b64url_decode(segment)
    if not value:
        raise ValueError('empty integer')
    return int.from_bytes(value, 'big')


def _rsa_public_key(jwk):
    """An RSAPublicKey from a JWK's n and e (any private members are ignored)."""
    n = _b64url_uint(jwk.get('n'))
    e = _b64url_uint(jwk.get('e'))
    if n.bit_length() < MIN_KEY_BITS:
        raise ValueError('RSA key too small')
    return rsa.RSAPublicNumbers(e, n).public_key()


def _parse_jwks(data):
    """{kid: RSAPublicKey} of the RS256 signing keys in a JWKS document."""
    keys = {}
    for jwk in (data or {}).get('keys') or []:
        if not isinstance(jwk, dict) or jwk.get('kty') != 'RSA' or not isinstance(jwk.get('kid'), str):
            continue
        if not jwk['kid']:
            continue
        if jwk.get('use') not in (None, 'sig') or jwk.get('alg') not in (None, 'RS256'):
            continue
        try:
            keys[jwk['kid']] = _rsa_public_key(jwk)
        except Exception:
            continue
    return keys


def _signing_key(team_domain, kid):
    """The cached public key `kid` of `team_domain`, fetching or refreshing the
    key set when it is missing, stale, or lacks that kid. Raises CfAccessError."""
    with _jwks_lock:
        now = time.monotonic()
        entry = _jwks_cache.get(team_domain)
        if entry and kid in entry['keys'] and now - entry['fetched'] < JWKS_TTL:
            return entry['keys'][kid]
        if entry and now - entry['attempted'] < JWKS_MIN_REFRESH:
            # Fetched (or tried) moments ago: answer from what we have.
            if kid in entry['keys']:
                return entry['keys'][kid]
            raise CfAccessError('token signed with an unknown key')

        if entry is None:
            entry = _jwks_cache[team_domain] = {'keys': {}, 'fetched': 0.0, 'attempted': 0.0}
        entry['attempted'] = now
        try:
            keys = _parse_jwks(_fetch_jwks(team_domain))
        except Exception as e:
            keys = None
            logger.warning(f'[CF Access] Could not fetch signing keys from {certs_url(team_domain)}: '
                           f'{type(e).__name__}')
        if keys:
            entry['keys'] = keys
            entry['fetched'] = now
        elif keys is not None:
            logger.warning(f'[CF Access] {certs_url(team_domain)} returned no usable RSA keys')
        if kid in entry['keys']:
            # A key fetched earlier is still better than locking everyone out
            # while Cloudflare is unreachable.
            return entry['keys'][kid]
        raise CfAccessError('token signed with an unknown key' if keys else
                            'could not load Cloudflare Access signing keys')


def clear_cache():
    with _jwks_lock:
        _jwks_cache.clear()


# --- verification -------------------------------------------------------------

def _json_segment(segment):
    """A token segment decoded to a JSON object (dict). Raises CfAccessError."""
    try:
        value = json.loads(_b64url_decode(segment))
    except Exception:
        raise CfAccessError('malformed token') from None
    if not isinstance(value, dict):
        raise CfAccessError('malformed token')
    return value


def _numeric_claim(claims, name, required=False):
    """claims[name] as a number (a JWT NumericDate), None when absent. Raises CfAccessError."""
    value = claims.get(name)
    if value is None:
        if required:
            raise CfAccessError(f'token rejected (missing {name})')
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CfAccessError(f'token rejected (invalid {name})')
    return value


def verify_token(token, team_domain, aud, now=None):
    """Verified claims (dict) of an Access JWT. Raises CfAccessError.

    In order: shape; header (alg exactly RS256, a kid, no 'crit' extensions);
    the RS256 signature with the team's cached key for that kid; then the
    claims: exp (required), nbf and iat (when present) with LEEWAY seconds of
    skew, iss == https://<team domain>, aud contains `aud`.
    """
    if not team_domain or not aud:
        raise CfAccessError('Cloudflare Access is not configured (team domain and AUD tag are required)')
    if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_BYTES or token.count('.') != 2:
        raise CfAccessError('malformed token')
    header_b64, payload_b64, signature_b64 = token.split('.')
    header = _json_segment(header_b64)
    # Only RS256: never 'none' (no signature) or HS* (an HMAC keyed with the
    # public key, which anyone can compute), nor any other variant.
    alg = header.get('alg')
    if not isinstance(alg, str) or alg not in ALGORITHMS:
        raise CfAccessError(f"unsupported algorithm {str(alg)[:20]!r}")
    if 'crit' in header:
        raise CfAccessError('token rejected (unsupported critical header)')
    kid = header.get('kid')
    if not kid or not isinstance(kid, str):
        raise CfAccessError('token has no key id')
    try:
        signature = _b64url_decode(signature_b64)
    except ValueError:
        raise CfAccessError('malformed token') from None
    if not signature:
        raise CfAccessError('malformed token')

    key = _signing_key(team_domain, kid)
    try:
        key.verify(signature, f'{header_b64}.{payload_b64}'.encode('ascii'),
                   padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        raise CfAccessError('token rejected (bad signature)') from None
    except Exception as e:
        raise CfAccessError(f'token rejected ({type(e).__name__})') from None

    # The signature is good, so the claims are Cloudflare's. Check they are
    # current and meant for this team and this Access application.
    claims = _json_segment(payload_b64)
    now = time.time() if now is None else now
    exp = _numeric_claim(claims, 'exp', required=True)
    if now > exp + LEEWAY:
        raise CfAccessError('token rejected (expired)')
    nbf = _numeric_claim(claims, 'nbf')
    if nbf is not None and now + LEEWAY < nbf:
        raise CfAccessError('token rejected (not yet valid)')
    iat = _numeric_claim(claims, 'iat')
    if iat is not None and now + LEEWAY < iat:
        raise CfAccessError('token rejected (issued in the future)')

    if claims.get('iss') != f'https://{team_domain}':
        raise CfAccessError('token rejected (iss)')
    token_aud = claims.get('aud')
    token_aud = token_aud if isinstance(token_aud, list) else [token_aud]
    if aud not in [a for a in token_aud if isinstance(a, str)]:
        raise CfAccessError('token rejected (aud)')
    return claims


def user_for_claims(claims, allowed_domains):
    """The existing RSCP user whose account email is the token's email."""
    from app.services.auth import User

    email = claims.get('email')
    if not isinstance(email, str) or '@' not in email:
        # e.g. a service-token JWT (common_name, no email)
        raise CfAccessError('token has no email claim')
    email = email.strip().lower()
    domain = '@' + email.rsplit('@', 1)[-1]
    if allowed_domains and domain not in allowed_domains:
        raise CfAccessError(f'email domain {domain} is not allowed',
                            public='Your email domain is not allowed to sign in to RSCP.')
    # Account email only (case-insensitive). Never the username:
    # "admin@<domain>" must not become the local "admin" account.
    from app.services.db import get_db_connection
    conn = get_db_connection()
    try:
        rows = conn.execute('SELECT id FROM users WHERE LOWER(TRIM(email)) = ?', (email,)).fetchall()
    finally:
        conn.close()
    if len(rows) > 1:
        raise CfAccessError(f'{len(rows)} RSCP accounts share the email {email}',
                            public='More than one RSCP account has your email. Ask an administrator.')
    user = User.get(rows[0]['id']) if rows else None
    if user is None:
        raise CfAccessError(f'no RSCP account has the email {email}',
                            public=f'No RSCP account is linked to {email}. Ask an administrator '
                                   f'to set it as your account email, or sign in with your password.')
    return user, email


def check_request(req, conf=None):
    """(user, email, claims) for the Access token on `req`. Ignores the
    enabled flag (the admin Test button uses it before enabling); callers
    check is_enabled(). Raises CfAccessError."""
    conf = conf or get_config()
    token = req.headers.get(HEADER)
    if not token:
        raise CfAccessError('no Cloudflare Access token on this request',
                            public='This request did not come through Cloudflare Access.')
    claims = verify_token(token.strip(), conf['team_domain'], conf['aud'])
    try:
        user, email = user_for_claims(claims, conf['allowed_domains'])
    except CfAccessError as e:
        e.claims = claims  # verified, just not mappable (for the admin Test button)
        raise
    return user, email, claims
