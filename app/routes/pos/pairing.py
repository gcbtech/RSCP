"""
POS Terminal Pairing Routes
Handles terminal session creation, pairing, and unpairing.
"""
import logging
import json
import random
import string
from flask import request, redirect, url_for, flash, render_template, jsonify, session
from flask_login import current_user, login_required

from app.routes.pos import pos_bp
from app.services.db import get_db_connection, get_request_db

logger = logging.getLogger(__name__)


def generate_pairing_code(length=6):
    """Generate a random numeric pairing code."""
    return ''.join(random.choices(string.digits, k=length))


@pos_bp.route('/terminal/start', methods=['POST'])
@login_required
def start_terminal_session():
    """
    Start a new terminal session and become the main terminal.
    Returns a pairing code that other terminals can use to connect.
    """
    data = request.get_json() or {}
    conn = get_db_connection()
    try:
        # Use terminal_id from request (localStorage) or fall back to Flask session
        terminal_id = data.get('terminal_id') or session.get('_id', request.cookies.get('session', ''))
        
        existing = conn.execute(
            'SELECT session_code, terminal_type FROM pos_paired_terminals WHERE flask_session_id = ?',
            (terminal_id,)
        ).fetchone()
        
        if existing:
            return jsonify({
                'success': False,
                'message': f'Already paired to session {existing["session_code"]} as {existing["terminal_type"]}'
            }), 400
        
        # Generate unique code
        for _ in range(10):  # Try up to 10 times
            code = generate_pairing_code()
            exists = conn.execute(
                'SELECT 1 FROM pos_terminal_sessions WHERE session_code = ?', (code,)
            ).fetchone()
            if not exists:
                break
        else:
            return jsonify({'success': False, 'message': 'Could not generate unique code'}), 500
        
        # Get current cart from session to initialize shared cart
        current_cart = session.get('pos_cart', {'items': []})
        
        # Create terminal session
        conn.execute(
            'INSERT INTO pos_terminal_sessions (session_code, cart_data) VALUES (?, ?)',
            (code, json.dumps(current_cart))
        )
        
        # Register this terminal as main
        conn.execute(
            'INSERT INTO pos_paired_terminals (session_code, terminal_type, flask_session_id, user_id) VALUES (?, ?, ?, ?)',
            (code, 'main', terminal_id, current_user.id)
        )
        
        conn.commit()
        
        # Store pairing info in Flask session
        session['pos_pairing'] = {
            'session_code': code,
            'terminal_type': 'main'
        }
        session.modified = True
        
        logger.info(f"Terminal session started: {code} by user {current_user.username}")
        
        return jsonify({
            'success': True,
            'session_code': code,
            'terminal_type': 'main'
        })
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Error starting terminal session: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        conn.close()


@pos_bp.route('/terminal/pair', methods=['POST'])
@login_required
def pair_terminal():
    """
    Pair this terminal to an existing session as customer or staff.
    """
    data = request.get_json() or request.form
    session_code = data.get('session_code', '').strip()
    terminal_type = data.get('terminal_type', 'staff')  # 'customer' or 'staff'
    
    if not session_code:
        return jsonify({'success': False, 'message': 'Session code required'}), 400
    
    if terminal_type not in ['customer', 'staff']:
        return jsonify({'success': False, 'message': 'Invalid terminal type'}), 400
    
    conn = get_db_connection()
    try:
        # Verify session exists
        sess_row = conn.execute(
            'SELECT id FROM pos_terminal_sessions WHERE session_code = ?',
            (session_code,)
        ).fetchone()
        
        if not sess_row:
            return jsonify({'success': False, 'message': 'Invalid session code'}), 404
        
        # Use terminal_id from request (localStorage) or fall back to Flask session
        terminal_id = data.get('terminal_id') or session.get('_id', request.cookies.get('session', ''))
        
        # Check if already paired
        existing = conn.execute(
            'SELECT session_code FROM pos_paired_terminals WHERE flask_session_id = ?',
            (terminal_id,)
        ).fetchone()
        
        if existing:
            return jsonify({
                'success': False,
                'message': f'Already paired to session {existing["session_code"]}. Unpair first.'
            }), 400
        
        # Register this terminal
        conn.execute(
            'INSERT INTO pos_paired_terminals (session_code, terminal_type, flask_session_id, user_id) VALUES (?, ?, ?, ?)',
            (session_code, terminal_type, terminal_id, current_user.id)
        )
        conn.commit()
        
        # Store in Flask session
        session['pos_pairing'] = {
            'session_code': session_code,
            'terminal_type': terminal_type
        }
        session.modified = True
        
        logger.info(f"Terminal paired: {session_code} as {terminal_type} by {current_user.username}")
        
        return jsonify({
            'success': True,
            'session_code': session_code,
            'terminal_type': terminal_type
        })
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Error pairing terminal: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        conn.close()


@pos_bp.route('/terminal/rejoin', methods=['POST'])
@login_required
def rejoin_terminal():
    """
    Rejoin a terminal to an existing session.
    Clears any stale pairing first, then re-pairs.
    Used for auto-reconnection when a terminal still has a saved session code.
    """
    data = request.get_json() or {}
    session_code = data.get('session_code', '').strip()
    terminal_type = data.get('terminal_type', 'staff')
    terminal_id = data.get('terminal_id') or session.get('_id', request.cookies.get('session', ''))
    
    if not session_code:
        return jsonify({'success': False, 'message': 'Session code required'}), 400
    
    if terminal_type not in ['customer', 'staff']:
        return jsonify({'success': False, 'message': 'Invalid terminal type'}), 400
    
    conn = get_db_connection()
    try:
        # Verify session still exists
        sess_row = conn.execute(
            'SELECT id FROM pos_terminal_sessions WHERE session_code = ?',
            (session_code,)
        ).fetchone()
        
        if not sess_row:
            # Session no longer exists — tell caller to discard saved code
            session.pop('pos_pairing', None)
            session.modified = True
            return jsonify({
                'success': False,
                'message': 'Session no longer exists',
                'discard': True
            }), 404
        
        # Clean up any existing pairing for this terminal (stale or current)
        conn.execute(
            'DELETE FROM pos_paired_terminals WHERE flask_session_id = ?',
            (terminal_id,)
        )
        
        # Re-register this terminal to the session
        conn.execute(
            'INSERT INTO pos_paired_terminals (session_code, terminal_type, flask_session_id, user_id) VALUES (?, ?, ?, ?)',
            (session_code, terminal_type, terminal_id, current_user.id)
        )
        conn.commit()
        
        # Update Flask session
        session['pos_pairing'] = {
            'session_code': session_code,
            'terminal_type': terminal_type
        }
        session.modified = True
        
        logger.info(f"Terminal rejoined session {session_code} as {terminal_type} by {current_user.username}")
        
        return jsonify({
            'success': True,
            'session_code': session_code,
            'terminal_type': terminal_type
        })
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Error rejoining terminal: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        conn.close()


@pos_bp.route('/terminal/unpair', methods=['POST'])
@login_required
def unpair_terminal():
    """
    Unpair this terminal from the current session.
    If this is the main terminal, the entire session is closed.
    """
    data = request.get_json() or {}
    pairing = session.get('pos_pairing')
    if not pairing:
        return jsonify({'success': True, 'message': 'Not currently paired'})
    
    session_code = pairing.get('session_code')
    terminal_type = pairing.get('terminal_type')
    # Use terminal_id from request (localStorage) or fall back to Flask session
    terminal_id = data.get('terminal_id') or session.get('_id', request.cookies.get('session', ''))
    
    conn = get_db_connection()
    try:
        if terminal_type == 'main':
            # Main terminal closing - notify all paired terminals and delete session
            # Broadcast session_closed event via WebSocket
            try:
                from app.services.websocket import get_socketio
                socketio = get_socketio()
                if socketio:
                    socketio.emit('session_closed', {'reason': 'Main terminal disconnected'}, room=session_code)
            except Exception as e:
                logger.warning(f"Could not broadcast session close: {e}")
            
            # Delete all paired terminals and session
            conn.execute('DELETE FROM pos_paired_terminals WHERE session_code = ?', (session_code,))
            conn.execute('DELETE FROM pos_terminal_sessions WHERE session_code = ?', (session_code,))
            logger.info(f"Terminal session {session_code} closed by main terminal")
        else:
            # Non-main terminal just removes itself
            conn.execute(
                'DELETE FROM pos_paired_terminals WHERE session_code = ? AND flask_session_id = ?',
                (session_code, terminal_id)
            )
            logger.info(f"Terminal unpaired from {session_code}")
        
        conn.commit()
        
        # Clear Flask session pairing
        session.pop('pos_pairing', None)
        session.modified = True
        
        return jsonify({'success': True, 'message': 'Unpaired successfully'})
        
    except Exception as e:
        conn.rollback()
        logger.error(f"Error unpairing terminal: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        conn.close()


@pos_bp.route('/terminal/status')
@login_required
def terminal_status():
    """
    Get current terminal pairing status and paired customer displays.
    """
    pairing = session.get('pos_pairing')
    staff_terminal_id = request.args.get('terminal_id')
    
    # Check database for paired customer displays for this staff terminal
    customer_displays = []
    if staff_terminal_id:
        conn = get_request_db()
        try:
            rows = conn.execute(
                """SELECT customer_terminal_id, customer_last_seen 
                   FROM pos_customer_display_pairings 
                   WHERE staff_terminal_id = ?""",
                (staff_terminal_id,)
            ).fetchall()
            
            from datetime import datetime
            for r in rows:
                connected = False
                if r['customer_last_seen']:
                    try:
                        # SQLite CURRENT_TIMESTAMP is UTC, customer_last_seen is UTC
                        last_seen_dt = datetime.strptime(r['customer_last_seen'], '%Y-%m-%d %H:%M:%S')
                        utcnow = datetime.utcnow()
                        delta = (utcnow - last_seen_dt).total_seconds()
                        connected = delta < 8 # Active in the last 8 seconds
                    except Exception as ts_err:
                        logger.warning(f"Error parsing last_seen timestamp: {ts_err}")
                customer_displays.append({
                    'id': r['customer_terminal_id'],
                    'connected': connected
                })
        except Exception as db_err:
            logger.error(f"Error loading paired displays for status: {db_err}")
    
    if not pairing:
        return jsonify({
            'paired': False,
            'customer_displays': customer_displays
        })
    
    session_code = pairing.get('session_code')
    terminal_type = pairing.get('terminal_type')
    
    conn = get_request_db()
    terminals = conn.execute(
        '''SELECT terminal_type, user_id, connected_at 
           FROM pos_paired_terminals 
           WHERE session_code = ?''',
        (session_code,)
    ).fetchall()
    
    return jsonify({
        'paired': True,
        'session_code': session_code,
        'terminal_type': terminal_type,
        'connected_terminals': [
            {'type': t['terminal_type'], 'user_id': t['user_id']}
            for t in terminals
        ],
        'customer_displays': customer_displays
    })


@pos_bp.route('/pair')
@login_required
def pairing_page():
    """
    Page to enter pairing code and select terminal type.
    """
    return render_template('pos/pair.html')


@pos_bp.route('/customer-display')
def customer_display():
    """
    Customer-facing display page (read-only cart view).
    Public access; pairing is validated in the client using the browser's persistent terminal ID and token.
    """
    return render_template('pos/customer_display.html')


@pos_bp.route('/api/display/request-code', methods=['POST'])
def request_display_code():
    """Called by an unpaired customer display to generate a unique 6-digit display code."""
    data = request.get_json() or {}
    customer_terminal_id = data.get('customer_terminal_id')
    if not customer_terminal_id:
        return jsonify({'success': False, 'message': 'customer_terminal_id required'}), 400
    
    import random
    import string
    conn = get_db_connection()
    try:
        # Clean up stale codes older than 10 minutes
        conn.execute("DELETE FROM pos_display_pairing_codes WHERE datetime(created_at) < datetime('now', '-10 minutes')")
        
        # Generate unique code
        for _ in range(10):
            code = ''.join(random.choices(string.digits, k=6))
            exists = conn.execute("SELECT 1 FROM pos_display_pairing_codes WHERE pairing_code = ?", (code,)).fetchone()
            if not exists:
                break
        else:
            return jsonify({'success': False, 'message': 'Failed to generate unique code'}), 500
        
        # Insert pairing code
        conn.execute(
            "INSERT OR REPLACE INTO pos_display_pairing_codes (pairing_code, customer_terminal_id) VALUES (?, ?)",
            (code, customer_terminal_id)
        )
        conn.commit()
        return jsonify({'success': True, 'pairing_code': code})
    except Exception as e:
        conn.rollback()
        logger.error(f"Error requesting display code: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        conn.close()


@pos_bp.route('/api/register/pair-display', methods=['POST'])
@login_required
def pair_display_from_register():
    """Called by the staff register to pair a customer display by typing its 6-digit code."""
    data = request.get_json() or {}
    pairing_code = data.get('pairing_code', '').strip().replace(' ', '')
    staff_terminal_id = data.get('staff_terminal_id')
    staff_friendly_name = data.get('staff_friendly_name', 'Register')
    
    if not pairing_code or not staff_terminal_id:
        return jsonify({'success': False, 'message': 'pairing_code and staff_terminal_id required'}), 400
        
    conn = get_db_connection()
    try:
        # Find customer display for this pairing code
        code_row = conn.execute(
            "SELECT customer_terminal_id FROM pos_display_pairing_codes WHERE pairing_code = ?",
            (pairing_code,)
        ).fetchone()
        
        if not code_row:
            return jsonify({'success': False, 'message': 'Invalid or expired pairing code'}), 404
            
        customer_terminal_id = code_row['customer_terminal_id']
        
        # Generate a secure 64-character token
        import secrets
        customer_terminal_token = secrets.token_hex(32)
        
        # Save pairing relationship persistently
        conn.execute(
            """INSERT OR REPLACE INTO pos_customer_display_pairings 
               (customer_terminal_id, customer_terminal_token, staff_terminal_id, staff_friendly_name) 
               VALUES (?, ?, ?, ?)""",
            (customer_terminal_id, customer_terminal_token, staff_terminal_id, staff_friendly_name)
        )
        
        # Remove temporary code
        conn.execute("DELETE FROM pos_display_pairing_codes WHERE pairing_code = ?", (pairing_code,))
        
        # Register active staff register friendly name
        conn.execute(
            "INSERT OR REPLACE INTO pos_active_terminals (terminal_id, friendly_name) VALUES (?, ?)",
            (staff_terminal_id, staff_friendly_name)
        )
        
        conn.commit()
        
        # Broadcast pairing event via WebSocket to notify the customer display instantly!
        try:
            from app.services.websocket import get_socketio
            socketio = get_socketio()
            if socketio:
                # Emit to the customer display's own terminal room
                socketio.emit('display_paired', {
                    'customer_terminal_token': customer_terminal_token,
                    'staff_terminal_id': staff_terminal_id,
                    'staff_friendly_name': staff_friendly_name
                }, room=customer_terminal_id)
        except Exception as ws_err:
            logger.warning(f"Could not broadcast pairing success: {ws_err}")
            
        return jsonify({
            'success': True,
            'message': 'Successfully paired customer display!',
            'customer_terminal_id': customer_terminal_id
        })
    except Exception as e:
        conn.rollback()
        logger.error(f"Error pairing display from register: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        conn.close()


@pos_bp.route('/api/display/check-paired')
def check_display_paired():
    """Allows customer display to check if it has been paired (polling fallback)."""
    customer_terminal_id = request.args.get('customer_terminal_id')
    if not customer_terminal_id:
        return jsonify({'success': False, 'message': 'customer_terminal_id required'}), 400
        
    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT customer_terminal_token, staff_terminal_id, staff_friendly_name FROM pos_customer_display_pairings WHERE customer_terminal_id = ?",
            (customer_terminal_id,)
        ).fetchone()
        
        if row:
            return jsonify({
                'success': True,
                'paired': True,
                'customer_terminal_token': row['customer_terminal_token'],
                'staff_terminal_id': row['staff_terminal_id'],
                'staff_friendly_name': row['staff_friendly_name']
            })
        return jsonify({'success': True, 'paired': False})
    except Exception as e:
        logger.error(f"Error checking display pairing: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        conn.close()


@pos_bp.route('/api/terminal/heartbeat', methods=['POST'])
def terminal_heartbeat():
    """Updates friendly name and active timestamp for staff register."""
    data = request.get_json() or {}
    terminal_id = data.get('terminal_id')
    friendly_name = data.get('friendly_name', 'Register')
    
    if not terminal_id:
        return jsonify({'success': False, 'message': 'terminal_id required'}), 400
        
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO pos_active_terminals (terminal_id, friendly_name, last_seen) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (terminal_id, friendly_name)
        )
        # Also update friendly name in pairings if exists
        conn.execute(
            "UPDATE pos_customer_display_pairings SET staff_friendly_name = ? WHERE staff_terminal_id = ?",
            (friendly_name, terminal_id)
        )
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        logger.error(f"Error saving terminal heartbeat: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        conn.close()


@pos_bp.route('/api/customer-display/heartbeat', methods=['POST'])
def customer_display_heartbeat():
    """Updates customer display last active timestamp."""
    data = request.get_json() or {}
    customer_terminal_token = data.get('customer_terminal_token')
    
    if not customer_terminal_token:
        return jsonify({'success': False, 'message': 'customer_terminal_token required'}), 400
        
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE pos_customer_display_pairings SET customer_last_seen = CURRENT_TIMESTAMP WHERE customer_terminal_token = ?",
            (customer_terminal_token,)
        )
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        conn.rollback()
        logger.error(f"Error updating customer display heartbeat: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        conn.close()


@pos_bp.route('/api/customer-display/cart')
def customer_display_cart():
    """Secure, token-authenticated public cart endpoint for customer displays."""
    customer_terminal_token = request.args.get('customer_terminal_token')
    if not customer_terminal_token:
        return jsonify({'success': False, 'message': 'customer_terminal_token required'}), 400
        
    conn = get_db_connection()
    try:
        # Find paired register terminal ID
        pairing_row = conn.execute(
            "SELECT staff_terminal_id, staff_friendly_name FROM pos_customer_display_pairings WHERE customer_terminal_token = ?",
            (customer_terminal_token,)
        ).fetchone()
        
        if not pairing_row:
            return jsonify({'success': False, 'message': 'Invalid pairing or display not linked', 'unpaired': True}), 403
            
        staff_terminal_id = pairing_row['staff_terminal_id']
        staff_friendly_name = pairing_row['staff_friendly_name']
        
        # Look up active session code for this register terminal
        session_row = conn.execute(
            """SELECT pt.session_code, s.cart_data 
               FROM pos_paired_terminals pt
               JOIN pos_terminal_sessions s ON pt.session_code = s.session_code
               WHERE pt.flask_session_id = ? AND pt.terminal_type = 'main'""",
            (staff_terminal_id,)
        ).fetchone()
        
        # If no active session found, return empty cart with paired status
        if not session_row:
            return jsonify({
                'success': True,
                'paired': True,
                'staff_terminal_id': staff_terminal_id,
                'staff_friendly_name': staff_friendly_name,
                'cart': {'items': []}
            })
            
        # We have active session, load cart and compute totals
        import json
        from app.routes.pos.core import get_tax_rate, get_pos_setting, calculate_tax, round_money, calculate_percentage
        
        session_code = session_row['session_code']
        try:
            cart = json.loads(session_row['cart_data'])
        except:
            cart = {'items': []}
            
        items = cart.get('items', [])
        subtotal = sum(item.get('line_total', 0) for item in items)
        
        order_discount = 0
        if cart.get('discount_amount') and cart.get('discount_type'):
            if cart['discount_type'] == 'percent':
                order_discount = calculate_percentage(subtotal, cart['discount_amount'])
            else:
                order_discount = cart['discount_amount']
                
        coupon_discount = (cart.get('applied_coupon') or {}).get('discount', 0) or 0
        discounted_subtotal = max(0, subtotal - order_discount - coupon_discount)
        
        tax_rate = get_tax_rate()
        tax_amount = calculate_tax(discounted_subtotal)
        card_total = round(discounted_subtotal + tax_amount, 2)
        
        cash_discount_enabled = get_pos_setting('CASH_DISCOUNT_ENABLED', 'false') == 'true'
        cash_discount_value = 0
        if cash_discount_enabled:
            cd_amount = float(get_pos_setting('CASH_DISCOUNT_AMOUNT', '0') or 0)
            cd_type = get_pos_setting('CASH_DISCOUNT_TYPE', 'percent')
            if cd_amount > 0:
                if cd_type == 'percent':
                    cash_discount_value = calculate_percentage(discounted_subtotal, cd_amount)
                else:
                    cash_discount_value = round_money(min(cd_amount, discounted_subtotal))
                    
        cash_total = round(card_total - cash_discount_value, 2)
        
        return jsonify({
            'success': True,
            'paired': True,
            'staff_terminal_id': staff_terminal_id,
            'staff_friendly_name': staff_friendly_name,
            'session_code': session_code,
            'cart': {
                'items': items,
                'subtotal': round(subtotal, 2),
                'order_discount': round(order_discount, 2),
                'coupon_discount': round(coupon_discount, 2),
                'tax_rate_pct': round(tax_rate * 100, 2),
                'tax_amount': tax_amount,
                'card_total': card_total,
                'cash_discount_enabled': cash_discount_enabled,
                'cash_discount_value': cash_discount_value,
                'cash_total': cash_total
            }
        })
    except Exception as e:
        logger.error(f"Error getting customer display cart: {e}")
        return jsonify({'success': False, 'message': str(e)}), 500
    finally:
        conn.close()
