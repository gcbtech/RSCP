"""
Reverse-proxy header handling that only trusts actual proxies.

werkzeug's ProxyFix rewrites REMOTE_ADDR/scheme/host from X-Forwarded-*
headers unconditionally. Gunicorn listens on 0.0.0.0:5000, so any client that
reaches that port directly could forge X-Forwarded-For and pick a fresh IP for
every request, defeating per-IP rate limits and polluting the login log.

TrustedProxyFix applies ProxyFix only when the TCP peer is a configured
proxy (config.json TRUSTED_PROXIES: IPs or CIDRs; default loopback, which is
where a local cloudflared/nginx connects from). Everyone else is taken at face
value.
"""
import ipaddress
import logging

from werkzeug.middleware.proxy_fix import ProxyFix

logger = logging.getLogger(__name__)

DEFAULT_TRUSTED_PROXIES = ('127.0.0.1/32', '::1/128')


def _parse_networks(entries):
    networks = []
    for entry in entries or ():
        try:
            networks.append(ipaddress.ip_network(str(entry).strip(), strict=False))
        except ValueError:
            logger.warning(f"[Proxy] Ignoring invalid TRUSTED_PROXIES entry: {entry!r}")
    return networks


class TrustedProxyFix:
    def __init__(self, wsgi_app, trusted=None):
        self.raw_app = wsgi_app
        self.proxied_app = ProxyFix(wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
        self.networks = _parse_networks(trusted if trusted is not None else DEFAULT_TRUSTED_PROXIES)

    def _is_trusted(self, addr):
        try:
            ip = ipaddress.ip_address(addr)
        except (ValueError, TypeError):
            return False
        return any(ip in net for net in self.networks)

    def __call__(self, environ, start_response):
        if self._is_trusted(environ.get('REMOTE_ADDR')):
            return self.proxied_app(environ, start_response)
        return self.raw_app(environ, start_response)
