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
