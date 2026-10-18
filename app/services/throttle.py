"""
Cross-worker rate limiting backed by SQLite.

Gunicorn runs several worker processes, so in-memory counters (a dict, or
Flask-Limiter's memory:// storage) give each worker its own allowance and an
attacker gets N times the advertised limit. This ledger lives in the shared
database instead, so every worker sees the same counts.

Usage:
    if throttle.is_limited('login_ip', ip, limit=10, window=900):
        return 429
    ...on failure:  throttle.hit('login_ip', ip)
    ...on success:  throttle.clear('login_user', username)

Credential checks (PIN, password, badge, codes) use FailureLimit, which
bundles the per-IP and per-username buckets for one kind of check. The IP
must come from app.utils.proxy.get_client_ip (TrustedProxyFix), never from
X-Forwarded-For directly.
"""
import logging
import time

from app.services.db import get_db_connection

logger = logging.getLogger(__name__)

# Rows older than this are pruned opportunistically; no limit uses a longer window.
_MAX_WINDOW = 24 * 3600


def is_limited(bucket, key, limit, window):
    """True if `key` has `limit` or more hits in `bucket` within `window` seconds."""
    conn = get_db_connection()
    try:
        row = conn.execute(
            'SELECT COUNT(*) AS c FROM rate_events WHERE bucket = ? AND key = ? AND ts > ?',
            (bucket, str(key), time.time() - window),
        ).fetchone()
        return row['c'] >= limit
    except Exception as e:
        # Fail closed would lock every user out if the table is missing; log
        # loudly instead. The table is created by schema migration v4.
        logger.error(f"[Throttle] lookup failed for {bucket}: {e}")
        return False
    finally:
        conn.close()


def hit(bucket, key):
    """Record one event for `key` in `bucket`."""
    now = time.time()
    conn = get_db_connection()
    try:
        conn.execute('INSERT INTO rate_events (bucket, key, ts) VALUES (?, ?, ?)',
                     (bucket, str(key), now))
        # Cheap opportunistic cleanup (~1% of writes).
        if int(now * 1000) % 100 == 0:
            conn.execute('DELETE FROM rate_events WHERE ts < ?', (now - _MAX_WINDOW,))
        conn.commit()
    except Exception as e:
        logger.error(f"[Throttle] record failed for {bucket}: {e}")
    finally:
        conn.close()


def clear(bucket, key):
    """Forget all events for `key` in `bucket` (e.g. after a successful login)."""
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM rate_events WHERE bucket = ? AND key = ?', (bucket, str(key)))
        conn.commit()
    except Exception as e:
        logger.error(f"[Throttle] clear failed for {bucket}: {e}")
    finally:
        conn.close()


class FailureLimit:
    """Failed-attempt limits for one kind of credential check.

    Two dimensions, both counting failures only:

    * Per client IP: `ip_limit` failures within `ip_window` seconds refuse
      every further attempt from that IP, right or wrong, until the oldest
      failure ages out.
    * Per target username (optional, only where the form names a user):
      `user_limit` failures within `user_window` against one username, from
      any IP, refuse further attempts for that username -- except from an IP
      that has no failures of its own for this check within `user_window`.
      That keeps a cashier at a clean register able to sign in while someone
      elsewhere hammers their username, so failures from a different IP never
      lock out a correct sign-in; a guesser spreading attempts over many
      addresses gets one try per fresh IP per window.

    A success clears the username's failures but not the IP's: knowing one
    valid PIN must not reset an IP's budget for guessing the others.
    """

    def __init__(self, name, ip_limit, ip_window, user_limit=None, user_window=None):
        self.name = name
        self.ip_bucket = f'{name}_ip'
        self.user_bucket = f'{name}_user'
        self.ip_limit = ip_limit
        self.ip_window = ip_window
        self.user_limit = user_limit
        self.user_window = user_window

    @staticmethod
    def _user_key(username):
        return (username or '').strip().lower()

    def _tracks_user(self, username):
        return bool(self.user_limit and self._user_key(username))

    def refused(self, ip, username=None):
        """True if this attempt must be refused without checking the credential."""
        if is_limited(self.ip_bucket, ip, self.ip_limit, self.ip_window):
            return True
        if self._tracks_user(username) and is_limited(
                self.user_bucket, self._user_key(username), self.user_limit, self.user_window):
            # The username is under attack; only an IP with its own recent
            # failures is turned away.
            return is_limited(self.ip_bucket, ip, 1, self.user_window)
        return False

    def failed(self, ip, username=None):
        """Record one failed attempt."""
        hit(self.ip_bucket, ip)
        if self._tracks_user(username):
            hit(self.user_bucket, self._user_key(username))

    def succeeded(self, username=None):
        """Record a successful attempt: forget the username's failures."""
        if self._tracks_user(username):
            clear(self.user_bucket, self._user_key(username))
