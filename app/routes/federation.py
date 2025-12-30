"""
Federation API Module
Handles communication between linked RSCP instances.
"""
import logging
import uuid
import json
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, g
from flask_login import login_required, current_user
from functools import wraps

from app.services.db import get_db_connection
from app.services.data_manager import load_config, save_config

logger = logging.getLogger(__name__)

federation_bp = Blueprint('federation', __name__, url_prefix='/api/federation')

# Transfer expiration time (72 hours)
TRANSFER_EXPIRATION_HOURS = 72


def require_api_key(f):
    """Decorator to require valid API key for federation endpoints."""
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.headers.get('X-API-Key')
        if not api_key:
            return jsonify({'error': 'Missing API key'}), 401
        
        conn = get_db_connection()
        try:
            peer = conn.execute(
                "SELECT * FROM federation_peers WHERE api_key = ? AND status = 'active'",
                (api_key,)
            ).fetchone()
            
            if not peer:
                return jsonify({'error': 'Invalid or inactive API key'}), 403
            
            # Update last_seen
            conn.execute(
                "UPDATE federation_peers SET last_seen = CURRENT_TIMESTAMP WHERE id = ?",
                (peer['id'],)
            )
            conn.commit()
            
            g.peer = dict(peer)
            return f(*args, **kwargs)
        finally:
            conn.close()
    
    return decorated


def generate_api_key():
    """Generate a new API key."""
    return str(uuid.uuid4())


# ============================================
# Public Endpoints (authenticated via API key)
# ============================================

@federation_bp.route('/ping', methods=['GET'])
@require_api_key
def ping():
    """Health check endpoint. Returns instance info."""
    config = load_config()
    return jsonify({
        'status': 'ok',
        'name': config.get('ORG_NAME', 'RSCP'),
        'location_prefix': config.get('LOCATION_PREFIX', 'RSCP'),
        'version': _get_version()
    })


@federation_bp.route('/search', methods=['POST'])
@require_api_key
def search():
    """Search inventory on this instance."""
    data = request.get_json()
    query = data.get('query', '').strip()
    
    if not query or len(query) < 2:
        return jsonify({'error': 'Query must be at least 2 characters'}), 400
    
    config = load_config()
    
    # Check if cross-search is enabled
    if not config.get('FEDERATION_CROSS_SEARCH_ENABLED', False):
        return jsonify({'error': 'Cross-search disabled on this instance'}), 403
    
    conn = get_db_connection()
    try:
        items = conn.execute('''
            SELECT id, sku, name, quantity, sell_price, image_url, 
                   location_area, location_aisle, location_shelf, location_bin
            FROM inventory_items 
            WHERE name LIKE ? OR sku LIKE ? OR secondary_ids LIKE ?
            LIMIT 50
        ''', (f'%{query}%', f'%{query}%', f'%{query}%')).fetchall()
        
        results = []
        for item in items:
            results.append({
                'sku': item['sku'],
                'name': item['name'],
                'quantity': item['quantity'],
                'price': item['sell_price'],
                'image_url': item['image_url'],
                'location': ' / '.join(filter(None, [
                    item['location_area'], item['location_aisle'],
                    item['location_shelf'], item['location_bin']
                ])),
                'source': config.get('LOCATION_PREFIX', 'RSCP')
            })
        
        return jsonify({
            'source': config.get('LOCATION_PREFIX', 'RSCP'),
            'source_name': config.get('ORG_NAME', 'RSCP'),
            'results': results
        })
    finally:
        conn.close()


@federation_bp.route('/items/<sku>', methods=['GET'])
@require_api_key
def get_item(sku):
    """Get item details by SKU."""
    conn = get_db_connection()
    try:
        item = conn.execute(
            'SELECT * FROM inventory_items WHERE sku = ?', (sku,)
        ).fetchone()
        
        if not item:
            return jsonify({'error': 'Item not found'}), 404
        
        return jsonify(dict(item))
    finally:
        conn.close()


@federation_bp.route('/transfer/request', methods=['POST'])
@require_api_key
def receive_transfer_request():
    """Receive a transfer request from a peer."""
    data = request.get_json()
    
    required = ['sku', 'item_data', 'quantity', 'requested_by']
    for field in required:
        if field not in data:
            return jsonify({'error': f'Missing required field: {field}'}), 400
    
    conn = get_db_connection()
    try:
        # Calculate expiration (72 hours from now)
        expires_at = datetime.now() + timedelta(hours=TRANSFER_EXPIRATION_HOURS)
        
        conn.execute('''
            INSERT INTO federation_transfers 
            (direction, peer_id, item_sku, item_data, quantity, status, 
             requested_by, expires_at, notes)
            VALUES ('incoming', ?, ?, ?, ?, 'pending', ?, ?, ?)
        ''', (
            g.peer['id'],
            data['sku'],
            json.dumps(data['item_data']),
            data['quantity'],
            data['requested_by'],
            expires_at.isoformat(),
            data.get('notes', '')
        ))
        conn.commit()
        
        transfer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        
        # In-app notification for incoming transfer
        try:
            from app.services.data_manager import load_config
            from app.routes.notifications import create_notification
            conf = load_config() or {}
            
            if conf.get('NOTIFY_FEDERATION', False):
                item_data = data['item_data']
                item_name = item_data.get('name', data['sku'])
                create_notification(
                    user_id=None,
                    title=f"🔗 Incoming Transfer: {item_name}",
                    message=f"Qty: {data['quantity']} from {data['requested_by']} - Expires in 72h",
                    notification_type='info',
                    link="/inventory/federation/admin"
                )
        except Exception as e:
            logger.error(f"Federation notification error: {e}")
        
        return jsonify({
            'status': 'pending',
            'transfer_id': transfer_id,
            'expires_at': expires_at.isoformat()
        })
    finally:
        conn.close()


@federation_bp.route('/transfer/<int:transfer_id>/status', methods=['GET'])
@require_api_key
def get_transfer_status(transfer_id):
    """Check status of a transfer request."""
    conn = get_db_connection()
    try:
        transfer = conn.execute(
            'SELECT * FROM federation_transfers WHERE id = ?', (transfer_id,)
        ).fetchone()
        
        if not transfer:
            return jsonify({'error': 'Transfer not found'}), 404
        
        return jsonify({
            'transfer_id': transfer['id'],
            'status': transfer['status'],
            'approved_by': transfer['approved_by'],
            'approved_at': transfer['approved_at'],
            'expires_at': transfer['expires_at']
        })
    finally:
        conn.close()


@federation_bp.route('/prefixes', methods=['GET'])
@require_api_key
def get_prefixes():
    """Get all known location prefixes in the federation."""
    config = load_config()
    my_prefix = config.get('LOCATION_PREFIX', 'RSCP')
    
    conn = get_db_connection()
    try:
        peers = conn.execute(
            "SELECT location_prefix FROM federation_peers WHERE status = 'active'"
        ).fetchall()
        
        prefixes = [my_prefix] + [p['location_prefix'] for p in peers if p['location_prefix']]
        
        return jsonify({'prefixes': list(set(prefixes))})
    finally:
        conn.close()


# ============================================
# Admin Endpoints (authenticated via session)
# ============================================

@federation_bp.route('/admin/peers', methods=['GET'])
@login_required
def list_peers():
    """List all federation peers (admin only)."""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    
    conn = get_db_connection()
    try:
        peers = conn.execute('SELECT * FROM federation_peers ORDER BY name').fetchall()
        return jsonify({'peers': [dict(p) for p in peers]})
    finally:
        conn.close()


@federation_bp.route('/admin/peers', methods=['POST'])
@login_required
def add_peer():
    """Add a new federation peer (admin only)."""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    
    data = request.get_json()
    
    if not data.get('name') or not data.get('url'):
        return jsonify({'error': 'Name and URL are required'}), 400
    
    # Generate API key for this peer (they use this to call US)
    api_key = generate_api_key()
    
    # The remote_api_key is the key THEY give US to call THEM
    remote_api_key = data.get('remote_api_key', '').strip() or None
    
    conn = get_db_connection()
    try:
        conn.execute('''
            INSERT INTO federation_peers (name, url, api_key, remote_api_key, status, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (data['name'], data['url'].rstrip('/'), api_key, remote_api_key, 
              'active' if remote_api_key else 'pending', current_user.username))
        conn.commit()
        
        peer_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        
        return jsonify({
            'id': peer_id,
            'api_key': api_key,
            'message': 'Peer added. Share YOUR API key with them, and enter THEIR API key to complete linking.'
        })
    finally:
        conn.close()


@federation_bp.route('/admin/peers/<int:peer_id>', methods=['DELETE'])
@login_required
def remove_peer(peer_id):
    """Remove a federation peer (admin only)."""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM federation_peers WHERE id = ?', (peer_id,))
        conn.commit()
        return jsonify({'success': True})
    finally:
        conn.close()


@federation_bp.route('/admin/peers/<int:peer_id>/remote-key', methods=['POST'])
@login_required
def set_remote_key(peer_id):
    """Set the remote API key for a federation peer (admin only)."""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    
    data = request.get_json()
    remote_key = data.get('remote_api_key', '').strip()
    
    if not remote_key:
        return jsonify({'error': 'Remote API key is required'}), 400
    
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE federation_peers 
            SET remote_api_key = ?, status = 'active'
            WHERE id = ?
        ''', (remote_key, peer_id))
        conn.commit()
        return jsonify({'success': True, 'message': 'Remote API key saved. You can now test the connection.'})
    finally:
        conn.close()


@federation_bp.route('/admin/peers/<int:peer_id>/test', methods=['POST'])
@login_required
def test_peer_connection(peer_id):
    """Test connection to a federation peer (admin only)."""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    
    conn = get_db_connection()
    try:
        peer = conn.execute('SELECT * FROM federation_peers WHERE id = ?', (peer_id,)).fetchone()
        
        if not peer:
            return jsonify({'error': 'Peer not found'}), 404
        
        # Use remote_api_key (THEIR key) to call THEM
        remote_key = peer['remote_api_key']
        if not remote_key:
            return jsonify({
                'success': False,
                'error': 'No remote API key configured. Enter the API key from the remote instance.'
            })
        
        import requests
        try:
            response = requests.get(
                f"{peer['url']}/api/federation/ping",
                headers={'X-API-Key': remote_key},
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                
                # Update peer info
                conn.execute('''
                    UPDATE federation_peers 
                    SET status = 'active', last_seen = CURRENT_TIMESTAMP, location_prefix = ?
                    WHERE id = ?
                ''', (data.get('location_prefix'), peer_id))
                conn.commit()
                
                return jsonify({
                    'success': True,
                    'remote_name': data.get('name'),
                    'remote_prefix': data.get('location_prefix'),
                    'version': data.get('version')
                })
            else:
                return jsonify({
                    'success': False,
                    'error': f'Remote returned status {response.status_code}'
                })
        except requests.RequestException as e:
            return jsonify({'success': False, 'error': str(e)})
    finally:
        conn.close()


@federation_bp.route('/admin/transfers', methods=['GET'])
@login_required
def list_transfers():
    """List pending transfers (admin only)."""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    
    conn = get_db_connection()
    try:
        transfers = conn.execute('''
            SELECT t.*, p.name as peer_name 
            FROM federation_transfers t
            JOIN federation_peers p ON t.peer_id = p.id
            WHERE t.status = 'pending'
            ORDER BY t.requested_at DESC
        ''').fetchall()
        
        return jsonify({'transfers': [dict(t) for t in transfers]})
    finally:
        conn.close()


@federation_bp.route('/admin/transfers/<int:transfer_id>/approve', methods=['POST'])
@login_required
def approve_transfer(transfer_id):
    """Approve a pending transfer (admin only)."""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    
    conn = get_db_connection()
    try:
        transfer = conn.execute(
            'SELECT * FROM federation_transfers WHERE id = ? AND status = ?',
            (transfer_id, 'pending')
        ).fetchone()
        
        if not transfer:
            return jsonify({'error': 'Transfer not found or already processed'}), 404
        
        # Check expiration
        expires_at = datetime.fromisoformat(transfer['expires_at'])
        if datetime.now() > expires_at:
            conn.execute(
                "UPDATE federation_transfers SET status = 'expired' WHERE id = ?",
                (transfer_id,)
            )
            conn.commit()
            return jsonify({'error': 'Transfer has expired'}), 400
        
        # Process the transfer
        item_data = json.loads(transfer['item_data'])
        
        # Create the item locally
        conn.execute('''
            INSERT INTO inventory_items (
                sku, name, quantity, buy_price, sell_price,
                location_area, location_aisle, location_shelf, location_bin,
                supplier, asin, keywords, secondary_ids, description,
                image_url, source_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            item_data.get('sku'),
            item_data.get('name'),
            transfer['quantity'],
            item_data.get('buy_price', 0),
            item_data.get('sell_price', 0),
            item_data.get('location_area'),
            item_data.get('location_aisle'),
            item_data.get('location_shelf'),
            item_data.get('location_bin'),
            item_data.get('supplier'),
            item_data.get('asin'),
            item_data.get('keywords'),
            item_data.get('secondary_ids'),
            item_data.get('description'),
            item_data.get('image_url'),
            item_data.get('source_url')
        ))
        
        new_item_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        
        # Log transaction
        conn.execute('''
            INSERT INTO inventory_transactions 
            (inventory_item_id, quantity_change, reason, user_id, source_tracking)
            VALUES (?, ?, 'Federation Transfer', ?, ?)
        ''', (new_item_id, transfer['quantity'], current_user.username, f'transfer:{transfer_id}'))
        
        # Update transfer status
        conn.execute('''
            UPDATE federation_transfers 
            SET status = 'approved', approved_by = ?, approved_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (current_user.username, transfer_id))
        
        conn.commit()
        
        return jsonify({
            'success': True,
            'item_id': new_item_id,
            'message': 'Transfer approved and item created'
        })
    finally:
        conn.close()


@federation_bp.route('/admin/transfers/<int:transfer_id>/reject', methods=['POST'])
@login_required
def reject_transfer(transfer_id):
    """Reject a pending transfer (admin only)."""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403
    
    data = request.get_json() or {}
    reason = data.get('reason', '')
    
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE federation_transfers 
            SET status = 'rejected', approved_by = ?, approved_at = CURRENT_TIMESTAMP, notes = ?
            WHERE id = ? AND status = 'pending'
        ''', (current_user.username, reason, transfer_id))
        conn.commit()
        
        return jsonify({'success': True})
    finally:
        conn.close()


# ============================================
# Helper Functions
# ============================================

def _get_version():
    """Get RSCP version."""
    try:
        from app.services.data_manager import BASE_DIR
        import os
        version_file = os.path.join(BASE_DIR, 'VERSION')
        if os.path.exists(version_file):
            with open(version_file) as f:
                return f.read().strip()
    except IOError:
        pass
    return 'unknown'
