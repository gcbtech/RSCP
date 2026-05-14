"""
WebSocket Service for POS Terminal Pairing
Provides real-time cart synchronization between paired terminals.
"""
import logging
import json
from flask import session, request
from flask_socketio import SocketIO, emit, join_room, leave_room, rooms

logger = logging.getLogger(__name__)

# SocketIO instance - initialized in create_app()
socketio = None


def init_socketio(app):
    """Initialize SocketIO with the Flask app."""
    global socketio
    
    # Use eventlet for async support
    socketio = SocketIO(
        app,
        cors_allowed_origins="*",
        async_mode='eventlet',
        logger=False,
        engineio_logger=False
    )
    
    # Register event handlers
    @socketio.on('connect')
    def handle_connect():
        logger.debug(f"Client connected: {request.sid}")
    
    @socketio.on('disconnect')
    def handle_disconnect():
        logger.debug(f"Client disconnected: {request.sid}")
        # Clean up paired terminal record if exists
        try:
            from app.services.db import get_db_connection
            conn = get_db_connection()
            conn.execute(
                'DELETE FROM pos_paired_terminals WHERE flask_session_id = ?',
                (request.sid,)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error cleaning up terminal on disconnect: {e}")
    
    @socketio.on('join_session')
    def handle_join_session(data):
        """Join a terminal pairing session room."""
        session_code = data.get('session_code')
        if session_code:
            join_room(session_code)
            logger.info(f"Client {request.sid} joined room {session_code}")
            # Do not emit cart_update here; client handles initial load via HTTP

    
    @socketio.on('leave_session')
    def handle_leave_session(data):
        """Leave a terminal pairing session room."""
        session_code = data.get('session_code')
        if session_code:
            leave_room(session_code)
            logger.info(f"Client {request.sid} left room {session_code}")
    
    @socketio.on('cart_modified')
    def handle_cart_modified(data):
        """Broadcast cart update to all terminals in the session."""
        session_code = data.get('session_code')
        cart = data.get('cart', {})
        
        if session_code:
            # Save to database
            try:
                from app.services.db import get_db_connection
                conn = get_db_connection()
                conn.execute(
                    'UPDATE pos_terminal_sessions SET cart_data = ?, last_activity = CURRENT_TIMESTAMP WHERE session_code = ?',
                    (json.dumps(cart), session_code)
                )
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"Error saving shared cart: {e}")
            
            # Broadcast to all terminals in the room (except sender)
            emit('cart_update', {'cart': cart}, room=session_code, include_self=False)
            logger.debug(f"Cart update broadcast to room {session_code}")
    
    return socketio


def broadcast_cart_update(session_code: str, cart: dict):
    """
    Broadcast a cart update to all terminals in a session.
    Called from regular HTTP routes when cart changes.
    """
    global socketio
    if socketio:
        socketio.emit('cart_update', {'cart': cart}, room=session_code)
        logger.debug(f"Cart broadcast to {session_code}")


def get_socketio():
    """Get the SocketIO instance."""
    global socketio
    return socketio
