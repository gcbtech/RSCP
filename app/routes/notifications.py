"""
Notification System Module
Provides in-app notifications for users with bell icon and dropdown.
"""
import logging
from flask import Blueprint, jsonify, request, g
from flask_login import login_required, current_user

from app.services.db import get_db_connection

logger = logging.getLogger(__name__)

notifications_bp = Blueprint('notifications', __name__, url_prefix='/api/notifications')


# =============================================================================
# Helper Functions
# =============================================================================

def create_notification(user_id, title, message=None, notification_type='info', link=None):
    """
    Create a notification for a user.
    
    Args:
        user_id: Target user ID (None for all users/admins)
        title: Short notification title
        message: Optional longer message
        notification_type: 'info', 'warning', 'success', 'error'
        link: Optional URL to link to
    
    Returns:
        notification_id or None on failure
    """
    conn = get_db_connection()
    try:
        cursor = conn.execute('''
            INSERT INTO notifications (user_id, title, message, type, link)
            VALUES (?, ?, ?, ?, ?)
        ''', (user_id, title, message, notification_type, link))
        conn.commit()
        return cursor.lastrowid
    except Exception as e:
        logger.error(f"Failed to create notification: {e}")
        return None
    finally:
        conn.close()


def get_unread_count(user_id):
    """Get count of unread notifications for a user."""
    conn = get_db_connection()
    try:
        # Get notifications for this user OR for all users (user_id = NULL)
        result = conn.execute('''
            SELECT COUNT(*) as cnt FROM notifications 
            WHERE (user_id = ? OR user_id IS NULL) AND is_read = 0
        ''', (user_id,)).fetchone()
        return result['cnt'] if result else 0
    except Exception as e:
        logger.error(f"Failed to get unread count: {e}")
        return 0
    finally:
        conn.close()


# =============================================================================
# API Endpoints
# =============================================================================

@notifications_bp.route('/list')
@login_required
def list_notifications():
    """Get notifications for current user."""
    limit = min(int(request.args.get('limit', 20)), 100)
    include_read = request.args.get('include_read', 'false').lower() == 'true'
    
    conn = get_db_connection()
    try:
        query = '''
            SELECT * FROM notifications 
            WHERE (user_id = ? OR user_id IS NULL)
        '''
        params = [current_user.id]
        
        if not include_read:
            query += ' AND is_read = 0'
        
        query += ' ORDER BY created_at DESC LIMIT ?'
        params.append(limit)
        
        notifications = conn.execute(query, params).fetchall()
        
        return jsonify({
            'notifications': [dict(n) for n in notifications],
            'unread_count': get_unread_count(current_user.id)
        })
    finally:
        conn.close()


@notifications_bp.route('/count')
@login_required
def count_notifications():
    """Get unread notification count for current user."""
    return jsonify({'count': get_unread_count(current_user.id)})


@notifications_bp.route('/<int:notification_id>/read', methods=['POST'])
@login_required
def mark_read(notification_id):
    """Mark a notification as read."""
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE notifications 
            SET is_read = 1, read_at = CURRENT_TIMESTAMP 
            WHERE id = ? AND (user_id = ? OR user_id IS NULL)
        ''', (notification_id, current_user.id))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()


@notifications_bp.route('/read-all', methods=['POST'])
@login_required
def mark_all_read():
    """Mark all notifications as read for current user."""
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE notifications 
            SET is_read = 1, read_at = CURRENT_TIMESTAMP 
            WHERE (user_id = ? OR user_id IS NULL) AND is_read = 0
        ''', (current_user.id,))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()


@notifications_bp.route('/<int:notification_id>', methods=['DELETE'])
@login_required
def delete_notification(notification_id):
    """Delete a notification."""
    conn = get_db_connection()
    try:
        conn.execute('''
            DELETE FROM notifications 
            WHERE id = ? AND (user_id = ? OR user_id IS NULL)
        ''', (notification_id, current_user.id))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()
