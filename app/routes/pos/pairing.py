"""
POS Pairing Routes (v3 - register-hub architecture)

One pairing flow for every peripheral type:

  1. An unpaired peripheral (customer display or staff scanner) shows a
     6-digit code (POST /api/pairing/request-code, auto-refreshed before
     the TTL lapses).
  2. Staff type that code into the register's pairing menu
     (POST /api/register/claim-code), which binds the peripheral to that
     register and mints a persistent bearer token.
  3. The peripheral picks the token up (GET /api/pairing/status), stores
     it in localStorage, acks receipt (POST /api/pairing/ack), and from
     then on polls GET /api/peripheral/poll for the register's cart.

Reconnection is automatic and unattended: the token never expires, only
its SHA-256 hash is stored server-side, and polling doubles as presence.
A display powered off overnight resumes on its own in the morning. The
only thing that ends a pairing is an explicit unpair (from either side),
which the peripheral detects as an authoritative 403 {revoked: true}.

Sync transport is short polling with a version number — deliberately no
WebSockets: this app deploys under multiple sync gunicorn workers, where
Socket.IO cannot deliver cross-worker events without a message queue.
A changed=false poll is a single indexed row read.
"""
import logging
from flask import request, render_template, jsonify, session
from flask_login import current_user, login_required

from app.routes.pos import pos_bp
from app.services.db import get_request_db
from app.services import pos_registers

logger = logging.getLogger(__name__)


# =====================================================================
# Customer display page (public kiosk)
# =====================================================================

@pos_bp.route('/customer-display')
def customer_display():
    """Customer-facing display page (read-only cart view).

    Public: pairing is established with a code + bearer token, not a
    login session, so an unattended kiosk never gets logged out.
    """
    return render_template('pos/customer_display.html')


# =====================================================================
# Peripheral-side pairing endpoints (public)
# =====================================================================

@pos_bp.route('/api/pairing/request-code', methods=['POST'])
def pairing_request_code():
    """An unpaired peripheral requests a fresh 6-digit pairing code.

    Called on the pairing screen and re-called before the code's TTL
    lapses, so the code on screen is always claimable.
    """
    data = request.get_json() or {}
    peripheral_id = (data.get('peripheral_id') or '').strip()
    role = (data.get('role') or 'display').strip()

    if not peripheral_id:
        return jsonify({'success': False, 'message': 'peripheral_id required'}), 400
    if role not in pos_registers.VALID_ROLES:
        return jsonify({'success': False, 'message': 'Invalid role'}), 400

    try:
        code = pos_registers.create_pairing_code(get_request_db(), peripheral_id, role)
        if not code:
            return jsonify({'success': False, 'message': 'Could not generate a unique code'}), 500
        return jsonify({
            'success': True,
            'pairing_code': code,
            # Client refreshes at ~80% of the TTL
            'ttl_seconds': pos_registers.PAIRING_CODE_TTL_MINUTES * 60,
        })
    except Exception as e:
        logger.error(f"Error creating pairing code: {e}")
        return jsonify({'success': False, 'message': 'Server error'}), 500


@pos_bp.route('/api/pairing/status')
def pairing_status():
    """Peripheral polls this while its code is on screen: 'am I paired yet?'

    The bearer token is included exactly while it is pending delivery;
    the peripheral must store it and call /api/pairing/ack.
    """
    peripheral_id = (request.args.get('peripheral_id') or '').strip()
    if not peripheral_id:
        return jsonify({'success': False, 'message': 'peripheral_id required'}), 400

    try:
        status = pos_registers.get_pairing_status(get_request_db(), peripheral_id)
        status['success'] = True
        return jsonify(status)
    except Exception as e:
        logger.error(f"Error checking pairing status: {e}")
        return jsonify({'success': False, 'message': 'Server error'}), 500


@pos_bp.route('/api/pairing/ack', methods=['POST'])
def pairing_ack():
    """Peripheral confirms it stored its token; the plaintext copy is wiped."""
    data = request.get_json() or {}
    peripheral_id = (data.get('peripheral_id') or '').strip()
    token = (data.get('token') or '').strip()

    if not peripheral_id or not token:
        return jsonify({'success': False, 'message': 'peripheral_id and token required'}), 400

    try:
        pos_registers.ack_token_delivery(get_request_db(), peripheral_id, token)
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error acking token delivery: {e}")
        return jsonify({'success': False, 'message': 'Server error'}), 500


# =====================================================================
# Peripheral runtime endpoints (public, bearer-token authenticated)
# =====================================================================

def _peripheral_token():
    """Bearer token from header (preferred) or query string."""
    return request.headers.get('X-Pairing-Token') or request.args.get('token') or ''


@pos_bp.route('/api/peripheral/poll')
def peripheral_poll():
    """Versioned cart poll for displays and scanners. Doubles as presence.

    Returns 403 {revoked: true} ONLY when the token is authoritatively
    unknown (unpaired) — never for transient errors — so clients know the
    difference between 'keep retrying' and 'drop to pairing screen'.
    """
    token = _peripheral_token()
    if not token:
        return jsonify({'success': False, 'message': 'pairing token required'}), 400

    try:
        since = int(request.args.get('since', -1))
    except (TypeError, ValueError):
        since = -1

    try:
        conn = get_request_db()
        peripheral = pos_registers.resolve_peripheral(conn, token)
        if not peripheral:
            return jsonify({'success': False, 'revoked': True,
                            'message': 'Pairing revoked or unknown'}), 403

        cart, version = pos_registers.load_register_cart(conn, peripheral['register_id'])

        if version == since:
            return jsonify({'success': True, 'changed': False, 'version': version})

        from app.routes.pos.core import compute_cart_totals
        return jsonify({
            'success': True,
            'changed': True,
            'version': version,
            'role': peripheral['role'],
            'register_name': peripheral['register_name'] or 'Register',
            'cart': compute_cart_totals(cart),
        })
    except Exception as e:
        logger.error(f"Error in peripheral poll: {e}")
        # 500 (not 403): the client keeps its credentials and retries
        return jsonify({'success': False, 'message': 'Server error'}), 500


@pos_bp.route('/api/peripheral/unpair', methods=['POST'])
def peripheral_unpair():
    """A peripheral unpairs itself (staff escape control on the device)."""
    token = _peripheral_token()
    if not token:
        data = request.get_json() or {}
        token = (data.get('token') or '').strip()
    if not token:
        return jsonify({'success': False, 'message': 'pairing token required'}), 400

    try:
        removed = pos_registers.unpair_peripheral(get_request_db(), token=token)
        return jsonify({'success': True, 'removed': removed})
    except Exception as e:
        logger.error(f"Error unpairing peripheral: {e}")
        return jsonify({'success': False, 'message': 'Server error'}), 500


# =====================================================================
# Register-side endpoints (staff login required)
# =====================================================================

@pos_bp.route('/api/register/hello', methods=['POST'])
@login_required
def register_hello():
    """The sales page announces itself: upserts the register row and binds
    the register id to the Flask session so plain form posts resolve to
    the same cart as AJAX calls."""
    data = request.get_json() or {}
    register_id = (data.get('register_id') or '').strip()
    friendly_name = (data.get('friendly_name') or '').strip() or None

    if not register_id:
        return jsonify({'success': False, 'message': 'register_id required'}), 400

    try:
        pos_registers.touch_register(get_request_db(), register_id, friendly_name)
        session['terminal_id'] = register_id
        session.modified = True
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error in register hello: {e}")
        return jsonify({'success': False, 'message': 'Server error'}), 500


@pos_bp.route('/api/register/claim-code', methods=['POST'])
@login_required
def register_claim_code():
    """Staff type a peripheral's 6-digit code into the register to pair it."""
    data = request.get_json() or {}
    code = (data.get('code') or '').replace(' ', '').strip()
    register_id = (data.get('register_id') or '').strip()
    friendly_name = (data.get('friendly_name') or '').strip() or 'Register'

    if not code or not register_id:
        return jsonify({'success': False, 'message': 'code and register_id required'}), 400

    try:
        conn = get_request_db()
        result = pos_registers.claim_pairing_code(
            conn, code, register_id, friendly_name,
            user_id=getattr(current_user, 'id', None)
        )
        session['terminal_id'] = register_id
        session.modified = True

        role_label = 'Customer display' if result['role'] == 'display' else 'Staff scanner'
        logger.info(
            f"{role_label} {result['peripheral_id']} paired to register "
            f"{register_id} by {getattr(current_user, 'username', '?')}"
        )
        return jsonify({
            'success': True,
            'role': result['role'],
            'peripheral_id': result['peripheral_id'],
            'message': f'{role_label} paired successfully!',
        })
    except ValueError:
        return jsonify({'success': False, 'message': 'Invalid or expired pairing code'}), 404
    except Exception as e:
        logger.error(f"Error claiming pairing code: {e}")
        return jsonify({'success': False, 'message': 'Server error'}), 500


@pos_bp.route('/api/register/peripherals')
@login_required
def register_peripherals():
    """Everything paired to this register, with live connected status."""
    register_id = (request.args.get('register_id') or
                   request.headers.get('X-Terminal-Id') or '').strip()
    if not register_id:
        return jsonify({'success': False, 'message': 'register_id required'}), 400

    try:
        peripherals = pos_registers.list_peripherals(get_request_db(), register_id)
        return jsonify({'success': True, 'peripherals': peripherals})
    except Exception as e:
        logger.error(f"Error listing peripherals: {e}")
        return jsonify({'success': False, 'message': 'Server error'}), 500


@pos_bp.route('/api/register/unpair', methods=['POST'])
@login_required
def register_unpair():
    """Unpair a peripheral from the register's device list."""
    data = request.get_json() or {}
    peripheral_id = (data.get('peripheral_id') or '').strip()
    register_id = (data.get('register_id') or '').strip()

    if not peripheral_id:
        return jsonify({'success': False, 'message': 'peripheral_id required'}), 400

    try:
        removed = pos_registers.unpair_peripheral(
            get_request_db(),
            peripheral_id=peripheral_id,
            register_id=register_id or None,
        )
        if removed:
            logger.info(f"Peripheral {peripheral_id} unpaired by {getattr(current_user, 'username', '?')}")
        return jsonify({'success': True, 'removed': removed})
    except Exception as e:
        logger.error(f"Error unpairing peripheral: {e}")
        return jsonify({'success': False, 'message': 'Server error'}), 500


@pos_bp.route('/api/register/poll')
@login_required
def register_poll():
    """Lightweight version check so the register picks up cart changes
    made by paired scanners; the page fetches /pos/cart/fragment when the
    version moves."""
    register_id = (request.headers.get('X-Terminal-Id') or
                   request.args.get('register_id') or
                   session.get('terminal_id') or '').strip()
    if not register_id:
        return jsonify({'success': False, 'message': 'register identity required'}), 400

    try:
        since = int(request.args.get('since', -1))
    except (TypeError, ValueError):
        since = -1

    try:
        _cart, version = pos_registers.load_register_cart(get_request_db(), register_id)
        return jsonify({'success': True, 'changed': version != since, 'version': version})
    except Exception as e:
        logger.error(f"Error in register poll: {e}")
        return jsonify({'success': False, 'message': 'Server error'}), 500


# =====================================================================
# Staff scanner page (login required; pairing selects the register)
# =====================================================================

@pos_bp.route('/scan')
@login_required
def scan_page():
    """Mobile line-busting page for handheld scanners (e.g. FZ-N1)."""
    return render_template('pos/scan.html')


# =====================================================================
# Back-compat shims for pre-v3 display pages
# ---------------------------------------------------------------------
# Display tablets that were powered on across the upgrade still run the
# old customer_display.html until their next reload. These two endpoints
# keep those pages alive (their tokens were migrated), so the fleet
# upgrades itself as each tablet reboots. Safe to delete once every
# display has cycled.
# =====================================================================

@pos_bp.route('/api/customer-display/cart')
def legacy_display_cart():
    """Old display page cart endpoint (token in query string)."""
    token = request.args.get('customer_terminal_token')
    if not token:
        return jsonify({'success': False, 'message': 'customer_terminal_token required'}), 400

    try:
        conn = get_request_db()
        peripheral = pos_registers.resolve_peripheral(conn, token)
        if not peripheral:
            return jsonify({'success': False, 'unpaired': True,
                            'message': 'Invalid pairing or display not linked'}), 403

        cart, _version = pos_registers.load_register_cart(conn, peripheral['register_id'])
        from app.routes.pos.core import compute_cart_totals
        return jsonify({
            'success': True,
            'paired': True,
            'staff_terminal_id': peripheral['register_id'],
            'staff_friendly_name': peripheral['register_name'] or 'Register',
            'session_code': None,
            'cart': compute_cart_totals(cart),
        })
    except Exception as e:
        logger.error(f"Error in legacy display cart shim: {e}")
        return jsonify({'success': False, 'message': 'Server error'}), 500


@pos_bp.route('/api/customer-display/heartbeat', methods=['POST'])
def legacy_display_heartbeat():
    """Old display page heartbeat: now just refreshes presence."""
    data = request.get_json() or {}
    token = data.get('customer_terminal_token')
    if not token:
        return jsonify({'success': False, 'message': 'customer_terminal_token required'}), 400

    try:
        peripheral = pos_registers.resolve_peripheral(get_request_db(), token)
        return jsonify({'success': peripheral is not None})
    except Exception as e:
        logger.error(f"Error in legacy heartbeat shim: {e}")
        return jsonify({'success': False, 'message': 'Server error'}), 500


@pos_bp.route('/api/terminal/heartbeat', methods=['POST'])
def legacy_terminal_heartbeat():
    """Old sales-page heartbeat: keeps pre-v3 register tabs working
    (register row upsert + session binding for form posts)."""
    data = request.get_json() or {}
    terminal_id = (data.get('terminal_id') or '').strip()
    friendly_name = (data.get('friendly_name') or '').strip() or None

    if not terminal_id:
        return jsonify({'success': False, 'message': 'terminal_id required'}), 400

    try:
        pos_registers.touch_register(get_request_db(), terminal_id, friendly_name)
        session['terminal_id'] = terminal_id
        session.modified = True
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"Error in legacy terminal heartbeat shim: {e}")
        return jsonify({'success': False, 'message': 'Server error'}), 500
