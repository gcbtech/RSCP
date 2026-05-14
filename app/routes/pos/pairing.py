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
    Get current terminal pairing status.
    """
    pairing = session.get('pos_pairing')
    
    if not pairing:
        return jsonify({
            'paired': False
        })
    
    session_code = pairing.get('session_code')
    terminal_type = pairing.get('terminal_type')
    
    # Get list of connected terminals
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
        ]
    })


@pos_bp.route('/pair')
@login_required
def pairing_page():
    """
    Page to enter pairing code and select terminal type.
    """
    return render_template('pos/pair.html')


@pos_bp.route('/customer-display')
@login_required
def customer_display():
    """
    Customer-facing display page (read-only cart view).
    Must be paired first.
    """
    pairing = session.get('pos_pairing')
    
    if not pairing or pairing.get('terminal_type') != 'customer':
        flash('Please pair as a customer display first.')
        return redirect(url_for('pos.pairing_page'))
    
    return render_template('pos/customer_display.html',
                           session_code=pairing.get('session_code'))
