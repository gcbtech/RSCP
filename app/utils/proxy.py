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
    _warned = set()

    def __init__(self, wsgi_app, trusted=None):
        self.raw_app = wsgi_app
        # Scheme/host/prefix come from the nearest proxy; the client address
        # is resolved by _client_addr below (handles chained proxies).
        self.proxied_app = ProxyFix(wsgi_app, x_for=0, x_proto=1, x_host=1, x_prefix=1)
        self.networks = _parse_networks(trusted if trusted is not None else DEFAULT_TRUSTED_PROXIES)

    def _is_trusted(self, addr):
        try:
            ip = ipaddress.ip_address(addr)
        except (ValueError, TypeError):
            return False
        return any(ip in net for net in self.networks)

    def _client_addr(self, environ, peer):
        """Walk X-Forwarded-For from the right, skipping trusted proxies.

        Works for one hop (cloudflared -> RSCP) and for chains
        (cloudflared -> nginx -> RSCP) as long as every proxy in the chain is
        listed; the first untrusted address is the real client.
        """
        hops = [h.strip() for h in environ.get('HTTP_X_FORWARDED_FOR', '').split(',') if h.strip()]
        for hop in reversed(hops):
            if not self._is_trusted(hop):
                return hop
        return hops[0] if hops else peer

    def __call__(self, environ, start_response):
        peer = environ.get('REMOTE_ADDR')
        if self._is_trusted(peer):
            client = self._client_addr(environ, peer)
            environ['werkzeug.proxy_fix.orig_remote_addr'] = peer
            environ['REMOTE_ADDR'] = client
            return self.proxied_app(environ, start_response)
        # Traffic relayed by Cloudflare/cloudflared from a peer we don't trust:
        # every tunnelled user will look like this one IP. Say so once.
        if 'HTTP_CF_CONNECTING_IP' in environ and peer not in self._warned:
            self._warned.add(peer)
            logger.warning(f"[Proxy] Cloudflare-relayed request from untrusted peer {peer}; "
                           f"all tunnel users will appear as {peer}. If this is your cloudflared "
                           f"host, add it under Admin > Settings > Reverse Proxy / Tunnel.")
        return self.raw_app(environ, start_response)


def get_client_ip():
    """The requesting client's address, for rate limits and logs.

    TrustedProxyFix has already rewritten REMOTE_ADDR from X-Forwarded-For
    when (and only when) the peer is a trusted proxy, so this is the one
    place to ask; never read X-Forwarded-For directly.
    """
    from flask import request
    return request.remote_addr or 'unknown'
