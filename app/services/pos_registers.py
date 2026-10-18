"""
POS Register & Peripheral Service (pairing v3)

The register (the browser tab running /pos/) is the hub: it owns the
authoritative cart, stored in pos_registers keyed by the register's
persistent localStorage ID. Peripherals pair to exactly one register
with a role:

    display  - read-only view of the register's cart (customer display)
    scanner  - may mutate the register's cart (staff handheld; the staff
               member must also be logged in normally)

Pairing is persistent and survives reboots on both sides: the peripheral
keeps a bearer token in localStorage, the server keeps only the SHA-256
hash, and nothing expires until explicitly unpaired. A customer display
that is powered off overnight reconnects on its own in the morning.

All functions take an open sqlite3 connection (typically get_request_db())
and commit their own writes.
"""
import hashlib
import json
import logging
import random
import re
import secrets
import string
import uuid

logger = logging.getLogger(__name__)

# Pairing codes shown on an unpaired peripheral expire after this many
# minutes; the peripheral auto-refreshes its code before then.
PAIRING_CODE_TTL_MINUTES = 10

# A peripheral is considered connected if it polled within this window.
# Displays poll every ~1.2s (with backoff to 5s), so 15s absorbs blips
# without the status badge flapping.
CONNECTED_WINDOW_SECONDS = 15

# last_seen is written at most this often per peripheral; polls in between
# are pure reads (they don't even open a write transaction). Must stay well
# under CONNECTED_WINDOW_SECONDS minus the slowest poll interval (5s).
LAST_SEEN_WRITE_SECONDS = 4

EMPTY_CART = {'items': [], 'discount_amount': 0, 'discount_type': None, 'discount_reason': ''}

VALID_ROLES = ('display', 'scanner')


def empty_cart():
    """A fresh empty cart dict (never share the module-level constant)."""
    return json.loads(json.dumps(EMPTY_CART))


def hash_token(token):
    """SHA-256 hex digest of a peripheral bearer token (tokens are stored hashed)."""
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


# =====================================================================
# Registers
# =====================================================================

def touch_register(conn, register_id, friendly_name=None):
    """Upsert a register row, refreshing last_seen and optionally its name."""
    if friendly_name:
        conn.execute(
            '''INSERT INTO pos_registers (register_id, friendly_name, last_seen)
               VALUES (?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(register_id) DO UPDATE SET
                   friendly_name = excluded.friendly_name,
                   last_seen = CURRENT_TIMESTAMP''',
            (register_id, friendly_name)
        )
    else:
        conn.execute(
            '''INSERT INTO pos_registers (register_id, last_seen)
               VALUES (?, CURRENT_TIMESTAMP)
               ON CONFLICT(register_id) DO UPDATE SET last_seen = CURRENT_TIMESTAMP''',
            (register_id,)
        )
    conn.commit()


_LINE_ID_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,31}$')


def new_line_id():
    """A short, stable id for a cart line. Always starts with a letter so it
    can never be mistaken for a list index in a URL."""
    return 'L' + uuid.uuid4().hex[:11]


def ensure_line_ids(cart):
    """Give every cart line a line_id. Returns True if any line was changed.

    Lines saved before line ids existed get a DETERMINISTIC id derived from
    their position and content, so every worker computes the same id for the
    same stored cart (a rendered page and the request it posts agree even if
    nothing has persisted the backfill yet). The first mutation then saves
    the ids, after which they never change.
    """
    changed = False
    seen = set()
    for i, item in enumerate(cart.get('items') or []):
        if not isinstance(item, dict):
            continue
        lid = item.get('line_id')
        if not isinstance(lid, str) or not _LINE_ID_RE.match(lid) or lid in seen:
            basis = f"{i}|{item.get('sku')}|{item.get('name')}|{item.get('inventory_item_id')}"
            lid = 'B' + hashlib.sha1(basis.encode('utf-8')).hexdigest()[:11]
            item['line_id'] = lid
            changed = True
        seen.add(lid)
    return changed


def normalize_cart(cart):
    """Repair a cart dict in place so downstream code never KeyErrors."""
    if not isinstance(cart, dict):
        cart = empty_cart()
    if not isinstance(cart.get('items'), list):
        cart['items'] = []
    cart.setdefault('discount_amount', 0)
    cart.setdefault('discount_type', None)
    cart.setdefault('discount_reason', '')
    ensure_line_ids(cart)
    return cart


def find_line(cart, line_id):
    """The cart line with this line_id, or None (never falls back to an index)."""
    if not line_id:
        return None
    return next((i for i in cart.get('items', []) if i.get('line_id') == line_id), None)


def _parse_cart(register_id, raw):
    try:
        cart = json.loads(raw) if raw else empty_cart()
    except (ValueError, TypeError):
        logger.warning(f"Corrupt cart_json for register {register_id}; resetting")
        cart = empty_cart()
    return normalize_cart(cart)


def read_register_cart(conn, register_id):
    """Return (cart, version) WITHOUT creating the row or committing, so it
    is safe inside a caller's transaction. A missing row reads as an empty
    cart at version 0."""
    row = conn.execute(
        'SELECT cart_json, cart_version FROM pos_registers WHERE register_id = ?',
        (register_id,)
    ).fetchone()
    if row is None:
        return empty_cart(), 0
    return _parse_cart(register_id, row['cart_json']), row['cart_version']


def write_register_cart(conn, register_id, cart):
    """Write a cart and bump its version WITHOUT committing (the caller owns
    the transaction). Returns the new version."""
    conn.execute(
        '''INSERT INTO pos_registers (register_id, cart_json, cart_version, last_seen)
           VALUES (?, ?, 1, CURRENT_TIMESTAMP)
           ON CONFLICT(register_id) DO UPDATE SET
               cart_json = excluded.cart_json,
               cart_version = pos_registers.cart_version + 1,
               last_seen = CURRENT_TIMESTAMP''',
        (register_id, json.dumps(cart))
    )
    row = conn.execute(
        'SELECT cart_version FROM pos_registers WHERE register_id = ?',
        (register_id,)
    ).fetchone()
    return row['cart_version'] if row else 1


def load_register_cart(conn, register_id):
    """Return (cart_dict, version) for a register, creating the row if needed."""
    row = conn.execute(
        'SELECT cart_json, cart_version FROM pos_registers WHERE register_id = ?',
        (register_id,)
    ).fetchone()

    if row is None:
        conn.execute(
            'INSERT OR IGNORE INTO pos_registers (register_id) VALUES (?)',
            (register_id,)
        )
        conn.commit()
        return empty_cart(), 0

    return _parse_cart(register_id, row['cart_json']), row['cart_version']


def save_register_cart(conn, register_id, cart):
    """Persist a register's cart and bump its version. Returns the new version.

    Blind overwrite (last write wins): request handlers must use
    mutate_register_cart() instead. Kept for tests and one-off tools.
    """
    if conn.in_transaction:
        conn.commit()
    version = write_register_cart(conn, register_id, cart)
    conn.commit()
    return version


def begin_immediate(conn):
    """Start a write transaction now (takes SQLite's write lock up front, so
    concurrent writers from other worker processes queue behind us instead
    of interleaving). Commits any implicit transaction left open first."""
    if conn.in_transaction:
        conn.commit()
    conn.execute('BEGIN IMMEDIATE')


class CartVersionMismatch(Exception):
    """The cart changed since the client rendered it (expected_version)."""

    def __init__(self, cart, version):
        super().__init__('cart_version_mismatch')
        self.cart = cart
        self.version = version


def mutate_register_cart(conn, register_id, fn, expected_version=None):
    """Atomically apply fn to a register's cart: load -> fn(cart) -> save, all
    inside ONE BEGIN IMMEDIATE transaction on the register row.

    Every cart change from every worker process goes through here, so two
    devices acting on the same sale serialize and neither change is lost.
    fn mutates the cart dict in place and may return a value; it may also
    write other tables through `conn` (they commit atomically with the
    cart). If fn raises, nothing is written. If fn leaves the cart unchanged
    the version is NOT bumped (no needless peripheral refreshes).

    With expected_version, raises CartVersionMismatch (writing nothing) if
    the cart is no longer at that version -- for all-or-nothing actions such
    as "clear the cart I am looking at".

    Returns (cart, version, fn_result).
    """
    begin_immediate(conn)
    try:
        cart, version = read_register_cart(conn, register_id)
        if expected_version is not None and expected_version != version:
            raise CartVersionMismatch(cart, version)
        before = json.dumps(cart, sort_keys=True)
        result = fn(cart)
        cart = normalize_cart(cart)
        if json.dumps(cart, sort_keys=True) != before:
            version = write_register_cart(conn, register_id, cart)
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    return cart, version, result


def get_register_name(conn, register_id):
    row = conn.execute(
        'SELECT friendly_name FROM pos_registers WHERE register_id = ?',
        (register_id,)
    ).fetchone()
    return row['friendly_name'] if row else 'Register'


# =====================================================================
# Pairing codes
# =====================================================================

def create_pairing_code(conn, peripheral_id, role):
    """Mint a 6-digit pairing code for an unpaired peripheral.

    Re-requesting replaces the peripheral's previous code (the peripheral
    auto-refreshes before the TTL lapses so the code on screen is never
    stale). Returns the code, or None if a unique one couldn't be minted.
    """
    if role not in VALID_ROLES:
        raise ValueError('invalid_role')

    # Opportunistic cleanup of expired codes
    conn.execute(
        "DELETE FROM pos_pairing_codes WHERE datetime(created_at) < datetime('now', ?)",
        (f'-{PAIRING_CODE_TTL_MINUTES} minutes',)
    )

    for _ in range(20):
        code = ''.join(random.choices(string.digits, k=6))
        exists = conn.execute(
            'SELECT 1 FROM pos_pairing_codes WHERE code = ?', (code,)
        ).fetchone()
        if not exists:
            # REPLACE also evicts this peripheral's previous code (peripheral_id UNIQUE)
            conn.execute(
                '''INSERT OR REPLACE INTO pos_pairing_codes
                   (code, peripheral_id, requested_role, created_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)''',
                (code, peripheral_id, role)
            )
            conn.commit()
            return code

    conn.commit()
    return None


def claim_pairing_code(conn, code, register_id, register_name, user_id=None):
    """Register-side: claim a peripheral's pairing code, binding it to this
    register and minting its bearer token.

    Returns {'peripheral_id', 'role'}. Raises ValueError('invalid_code')
    for unknown/expired codes.

    The raw token is parked in pending_token for one-time delivery to the
    peripheral via get_pairing_status(); the peripheral acks receipt with
    ack_token_delivery(), after which only the hash remains.
    """
    row = conn.execute(
        '''SELECT peripheral_id, requested_role FROM pos_pairing_codes
           WHERE code = ? AND datetime(created_at) >= datetime('now', ?)''',
        (code, f'-{PAIRING_CODE_TTL_MINUTES} minutes')
    ).fetchone()

    if not row:
        raise ValueError('invalid_code')

    peripheral_id = row['peripheral_id']
    role = row['requested_role']
    token = secrets.token_hex(32)
    default_name = 'Customer Display' if role == 'display' else 'Staff Scanner'

    # Re-pairing a known peripheral (e.g. moving it to another register)
    # replaces its row and rotates its token.
    conn.execute(
        '''INSERT OR REPLACE INTO pos_peripherals
           (peripheral_id, register_id, role, token_hash, pending_token,
            friendly_name, paired_by, paired_at, last_seen)
           VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, NULL)''',
        (peripheral_id, register_id, role, hash_token(token), token,
         default_name, user_id)
    )
    conn.execute('DELETE FROM pos_pairing_codes WHERE code = ?', (code,))

    # Make sure the register exists and carries its friendly name
    conn.execute(
        '''INSERT INTO pos_registers (register_id, friendly_name)
           VALUES (?, ?)
           ON CONFLICT(register_id) DO UPDATE SET friendly_name = excluded.friendly_name''',
        (register_id, register_name or 'Register')
    )
    conn.commit()

    return {'peripheral_id': peripheral_id, 'role': role}


def get_pairing_status(conn, peripheral_id):
    """Peripheral-side: has my code been claimed yet?

    While the token is pending delivery it is included in the response;
    the peripheral must then call ack_token_delivery(). If the peripheral
    is paired but the token was already delivered (and it lost it), it
    must request a fresh code and re-pair.
    """
    row = conn.execute(
        '''SELECT p.pending_token, p.role, p.register_id, r.friendly_name
           FROM pos_peripherals p
           LEFT JOIN pos_registers r ON r.register_id = p.register_id
           WHERE p.peripheral_id = ?''',
        (peripheral_id,)
    ).fetchone()

    if not row:
        return {'paired': False}

    result = {
        'paired': True,
        'role': row['role'],
        'register_name': row['friendly_name'] or 'Register',
    }
    if row['pending_token']:
        result['token'] = row['pending_token']
    return result


def ack_token_delivery(conn, peripheral_id, token):
    """Peripheral confirms it stored its token; clear the plaintext copy."""
    conn.execute(
        '''UPDATE pos_peripherals SET pending_token = NULL
           WHERE peripheral_id = ? AND token_hash = ?''',
        (peripheral_id, hash_token(token))
    )
    conn.commit()


# =====================================================================
# Peripheral auth & presence
# =====================================================================

def resolve_peripheral(conn, token, touch=True):
    """Look up a peripheral by its bearer token. Returns the row or None.

    When touch=True, refreshes last_seen, at most once per
    LAST_SEEN_WRITE_SECONDS: the lookup itself says whether the stored value
    is due, so the other ~2 of every 3 polls (1.2s polling) issue no UPDATE
    and take no write lock at all.
    """
    if not token:
        return None

    window = f'-{LAST_SEEN_WRITE_SECONDS} seconds'
    row = conn.execute(
        '''SELECT p.peripheral_id, p.register_id, p.role, p.friendly_name,
                  r.friendly_name AS register_name,
                  (p.last_seen IS NULL OR datetime(p.last_seen) < datetime('now', ?)) AS seen_due
           FROM pos_peripherals p
           LEFT JOIN pos_registers r ON r.register_id = p.register_id
           WHERE p.token_hash = ?''',
        (window, hash_token(token))
    ).fetchone()

    if row and touch and row['seen_due']:
        # The guard repeats the check: concurrent polls from other workers
        # may have refreshed it meanwhile.
        conn.execute(
            '''UPDATE pos_peripherals SET last_seen = CURRENT_TIMESTAMP
               WHERE peripheral_id = ?
                 AND (last_seen IS NULL OR datetime(last_seen) < datetime('now', ?))''',
            (row['peripheral_id'], window)
        )
        conn.commit()

    return row


def _utc_iso(value):
    """SQLite CURRENT_TIMESTAMP text ('YYYY-MM-DD HH:MM:SS', UTC) -> ISO 8601
    with a Z, so a browser parses it as UTC."""
    if not value:
        return None
    return str(value).replace(' ', 'T')[:19] + 'Z'


def list_peripherals(conn, register_id):
    """All peripherals paired to a register, with live connection status.

    seen_ago_s is computed here, from the server's clock, so the "last seen"
    text is right even when the register's own clock is off."""
    rows = conn.execute(
        '''SELECT peripheral_id, role, friendly_name, paired_at, last_seen,
                  CASE WHEN last_seen IS NOT NULL
                        AND datetime(last_seen) >= datetime('now', ?)
                       THEN 1 ELSE 0 END AS connected,
                  CAST(strftime('%s', 'now') - strftime('%s', last_seen) AS INTEGER) AS seen_ago_s
           FROM pos_peripherals
           WHERE register_id = ?
           ORDER BY role, paired_at''',
        (f'-{CONNECTED_WINDOW_SECONDS} seconds', register_id)
    ).fetchall()

    return [
        {
            'peripheral_id': r['peripheral_id'],
            'role': r['role'],
            'friendly_name': r['friendly_name'] or ('Customer Display' if r['role'] == 'display' else 'Staff Scanner'),
            'connected': bool(r['connected']),
            'paired_at': r['paired_at'],
            'last_seen': _utc_iso(r['last_seen']),
            'seen_ago_s': None if r['seen_ago_s'] is None else max(0, r['seen_ago_s']),
        }
        for r in rows
    ]


def unpair_peripheral(conn, peripheral_id=None, token=None, register_id=None):
    """Remove a pairing. Callable from either side:

    - register UI passes peripheral_id (optionally scoped by register_id)
    - the peripheral itself passes its own token

    Returns True if a row was deleted.
    """
    cur = None
    if token:
        cur = conn.execute(
            'DELETE FROM pos_peripherals WHERE token_hash = ?',
            (hash_token(token),)
        )
    elif peripheral_id and register_id:
        cur = conn.execute(
            'DELETE FROM pos_peripherals WHERE peripheral_id = ? AND register_id = ?',
            (peripheral_id, register_id)
        )
    elif peripheral_id:
        cur = conn.execute(
            'DELETE FROM pos_peripherals WHERE peripheral_id = ?',
            (peripheral_id,)
        )
    conn.commit()
    return bool(cur and cur.rowcount)
