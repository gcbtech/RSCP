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
        try:
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
        except Exception as e:
            logger.error(f"require_api_key error: {e}")
            return jsonify({'error': f'Authentication error: {str(e)}'}), 500
    
    return decorated


def generate_api_key():
    """Generate a new API key."""
    return str(uuid.uuid4())


# ============================================
# Public Endpoints (authenticated via API key)
# ============================================

@federation_bp.route('/debug', methods=['GET'])
def federation_debug():
    """Debug endpoint (no auth) to check federation status."""
    try:
        config = load_config()
        
        conn = get_db_connection()
        try:
            # Check if table exists
            table_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='federation_peers'"
            ).fetchone()
            
            # Check if inventory_items exists
            inv_check = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='inventory_items'"
            ).fetchone()
            
            return jsonify({
                'status': 'ok',
                'cross_search_enabled': config.get('FEDERATION_CROSS_SEARCH_ENABLED', False),
                'location_prefix': config.get('LOCATION_PREFIX', 'RSCP'),
                'federation_peers_table_exists': table_check is not None,
                'inventory_items_table_exists': inv_check is not None
            })
        finally:
            conn.close()
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@federation_bp.route('/debug-peers', methods=['GET'])
def debug_peers():
    """Debug endpoint to check peers and their status."""
    try:
        conn = get_db_connection()
        try:
            peers = conn.execute('SELECT name, url, status, api_key, remote_api_key FROM federation_peers').fetchall()
            
            # Mask API keys for security (show first 8 chars)
            result = []
            for p in peers:
                result.append({
                    'name': p['name'],
                    'url': p['url'],
                    'status': p['status'],
                    'api_key_preview': p['api_key'][:8] + '...' if p['api_key'] else None,
                    'remote_api_key_set': p['remote_api_key'] is not None
                })
            
            return jsonify({'peers': result, 'count': len(result)})
        finally:
            conn.close()
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@federation_bp.route('/debug-auth', methods=['GET', 'POST'])
def debug_auth():
    """Debug endpoint to test API key authentication."""
    try:
        # Get key from header or query param
        api_key = request.headers.get('X-API-Key') or request.args.get('key', '')
        
        if not api_key:
            return jsonify({
                'error': 'No key provided',
                'usage': 'Send X-API-Key header or ?key=YOUR_KEY'
            })
        
        conn = get_db_connection()
        try:
            # Check if key exists
            peer = conn.execute(
                "SELECT id, name, status, api_key FROM federation_peers WHERE api_key = ?",
                (api_key,)
            ).fetchone()
            
            if not peer:
                # Show what keys we DO have
                all_keys = conn.execute("SELECT api_key FROM federation_peers").fetchall()
                return jsonify({
                    'error': 'Key not found',
                    'key_preview': api_key[:8] + '...',
                    'existing_keys': [k['api_key'][:8] + '...' for k in all_keys]
                })
            
            # Check if active
            if peer['status'] != 'active':
                return jsonify({
                    'error': 'Peer not active',
                    'peer_name': peer['name'],
                    'status': peer['status']
                })
            
            return jsonify({
                'success': True,
                'peer_name': peer['name'],
                'status': peer['status'],
                'key_matches': True
            })
        finally:
            conn.close()
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


@federation_bp.route('/debug-search', methods=['GET', 'POST'])
def debug_search():
    """Debug search endpoint (no auth) to test search logic."""
    try:
        # Get query from either GET or POST
        if request.method == 'POST':
            data = request.get_json() or {}
            query = data.get('query', '')
        else:
            query = request.args.get('q', '')
        
        if not query or len(query) < 2:
            return jsonify({'error': 'Query must be at least 2 chars', 'received_query': query})
        
        config = load_config()
        
        if not config.get('FEDERATION_CROSS_SEARCH_ENABLED', False):
            return jsonify({'error': 'Cross-search disabled'})
        
        conn = get_db_connection()
        try:
            search_pattern = f'%{query}%'
            items = conn.execute('''
                SELECT id, sku, name, quantity, sell_price, image_url
                FROM inventory_items 
                WHERE name LIKE ? OR sku LIKE ?
                LIMIT 10
            ''', (search_pattern, search_pattern)).fetchall()
            
            results = [{'sku': i['sku'], 'name': i['name'], 'qty': i['quantity']} for i in items]
            
            return jsonify({
                'success': True,
                'query': query,
                'result_count': len(results),
                'results': results
            })
        finally:
            conn.close()
    except Exception as e:
        import traceback
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


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
    try:
        data = request.get_json()
        query = data.get('query', '').strip() if data else ''
        
        if not query or len(query) < 2:
            return jsonify({'error': 'Query must be at least 2 characters'}), 400
        
        config = load_config()
        
        # Check if cross-search is enabled
        if not config.get('FEDERATION_CROSS_SEARCH_ENABLED', False):
            return jsonify({'error': 'Cross-search disabled on this instance'}), 403
        
        conn = get_db_connection()
        try:
            # Search only name and sku for maximum compatibility
            search_pattern = f'%{query}%'
            items = conn.execute('''
                SELECT id, sku, name, quantity, sell_price, image_url, 
                       location_area, location_aisle, location_shelf, location_bin
                FROM inventory_items 
                WHERE name LIKE ? OR sku LIKE ?
                LIMIT 50
            ''', (search_pattern, search_pattern)).fetchall()
            
            results = []
            for item in items:
                results.append({
                    'sku': item['sku'] or '',
                    'name': item['name'] or '',
                    'quantity': item['quantity'] or 0,
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
    except Exception as e:
        logger.error(f"Federation search error: {e}")
        return jsonify({'error': str(e), 'results': []}), 500


@federation_bp.route('/items/<sku>', methods=['GET'])
@require_api_key
def get_item(sku):
    """Get item details by SKU, including base64 image data for transfers."""
    import base64
    
    conn = get_db_connection()
    try:
        item = conn.execute(
            'SELECT * FROM inventory_items WHERE sku = ?', (sku,)
        ).fetchone()
        
        if not item:
            return jsonify({'error': 'Item not found'}), 404
        
        result = dict(item)
        
        # Include base64 image data if image exists
        if item['image_url']:
            try:
                # Convert relative URL to file path
                image_path = item['image_url']
                if image_path.startswith('/static/'):
                    from app.services.db import BASE_DIR
                    image_path = os.path.join(BASE_DIR, image_path.lstrip('/'))
                
                if os.path.exists(image_path):
                    with open(image_path, 'rb') as f:
                        image_data = base64.b64encode(f.read()).decode('utf-8')
                    # Get file extension
                    ext = os.path.splitext(image_path)[1].lower()
                    result['image_base64'] = image_data
                    result['image_extension'] = ext
                    logger.info(f"Included image data for {sku} ({len(image_data)} bytes)")
            except Exception as e:
                logger.warning(f"Could not read image for {sku}: {e}")
        
        return jsonify(result)
    finally:
        conn.close()


@federation_bp.route('/transfer/request', methods=['POST'])
@require_api_key
def receive_transfer_request():
    """Receive a transfer request from a peer.
    
    When peer B requests an item from us (A), we create an OUTGOING transfer
    because we will be sending the item OUT to B.
    """
    data = request.get_json()
    
    required = ['sku', 'item_data', 'quantity', 'requested_by']
    for field in required:
        if field not in data:
            return jsonify({'error': f'Missing required field: {field}'}), 400
    
    conn = get_db_connection()
    try:
        # Calculate expiration (72 hours from now)
        expires_at = datetime.now() + timedelta(hours=TRANSFER_EXPIRATION_HOURS)
        
        # Direction is 'outgoing' because we (source) are sending the item out
        conn.execute('''
            INSERT INTO federation_transfers 
            (direction, peer_id, item_sku, item_data, quantity, status, 
             requested_by, expires_at, notes)
            VALUES ('outgoing', ?, ?, ?, ?, 'pending', ?, ?, ?)
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


@federation_bp.route('/transfer/complete', methods=['POST'])
@require_api_key
def receive_transfer_complete():
    """Receive notification that a transfer has been completed by the source.
    
    The source instance calls this when they approve an outgoing transfer.
    We add the item to our inventory.
    """
    data = request.get_json()
    
    required = ['sku', 'item_data', 'quantity']
    for field in required:
        if field not in data:
            return jsonify({'error': f'Missing required field: {field}'}), 400
    
    sku = data['sku']
    item_data = data['item_data']
    quantity = data['quantity']
    source = data.get('source', 'Unknown')
    
    # Save image if provided
    local_image_url = None
    if item_data.get('image_base64'):
        try:
            import base64
            import uuid
            from app.services.db import BASE_DIR
            
            image_data = base64.b64decode(item_data['image_base64'])
            ext = item_data.get('image_extension', '.jpg')
            filename = f"fed_{uuid.uuid4().hex}{ext}"
            
            upload_dir = os.path.join(BASE_DIR, 'static', 'uploads', 'items')
            os.makedirs(upload_dir, exist_ok=True)
            
            image_path = os.path.join(upload_dir, filename)
            with open(image_path, 'wb') as f:
                f.write(image_data)
            
            local_image_url = f"/static/uploads/items/{filename}"
            logger.info(f"Saved transferred image for {sku}: {local_image_url}")
        except Exception as e:
            logger.warning(f"Could not save transferred image: {e}")
    
    # Use local image URL if we saved one, otherwise use the original (won't work but preserves data)
    final_image_url = local_image_url or item_data.get('image_url')
    
    conn = get_db_connection()
    try:
        # Check if item exists locally
        existing_item = conn.execute(
            'SELECT id, quantity FROM inventory_items WHERE sku = ?', (sku,)
        ).fetchone()
        
        if existing_item:
            new_quantity = existing_item['quantity'] + quantity
            conn.execute(
                'UPDATE inventory_items SET quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                (new_quantity, existing_item['id'])
            )
            item_id = existing_item['id']
            
            # Update image if we got a new one and the existing item doesn't have one
            if local_image_url:
                conn.execute(
                    'UPDATE inventory_items SET image_url = ? WHERE id = ? AND (image_url IS NULL OR image_url = "")',
                    (local_image_url, existing_item['id'])
                )
        else:
            # Create new item
            conn.execute('''
                INSERT INTO inventory_items (
                    sku, name, quantity, buy_price, sell_price,
                    location_area, location_aisle, location_shelf, location_bin,
                    supplier, asin, keywords, image_url, source_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                sku,
                item_data.get('name'),
                quantity,
                item_data.get('buy_price', 0),
                item_data.get('sell_price', 0),
                item_data.get('location_area'),
                item_data.get('location_aisle'),
                item_data.get('location_shelf'),
                item_data.get('location_bin'),
                item_data.get('supplier'),
                item_data.get('asin'),
                item_data.get('keywords'),
                final_image_url,
                item_data.get('source_url')
            ))
            item_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        
        # Log transaction
        conn.execute('''
            INSERT INTO inventory_transactions 
            (inventory_item_id, quantity_change, reason, user_id, source_tracking)
            VALUES (?, ?, ?, 'federation', ?)
        ''', (item_id, quantity, f'Federation Transfer from {source}', f'peer:{g.peer["id"]}'))
        
        # Update any outgoing transfer we have for this
        conn.execute('''
            UPDATE federation_transfers 
            SET status = 'completed', approved_at = CURRENT_TIMESTAMP
            WHERE item_sku = ? AND peer_id = ? AND direction = 'outgoing' AND status = 'pending'
        ''', (sku, g.peer['id']))
        
        conn.commit()
        
        logger.info(f"Received transfer of {quantity}x {sku} from {source}")
        
        return jsonify({
            'success': True,
            'item_id': item_id,
            'message': f'Received {quantity}x {sku}'
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
    remote_api_key_raw = data.get('remote_api_key', '').strip() or None
    from app.services.security import encrypt
    remote_api_key = encrypt(remote_api_key_raw) if remote_api_key_raw else None
    
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
    from app.services.security import encrypt
    
    if not remote_key:
        return jsonify({'error': 'Remote API key is required'}), 400
    
    conn = get_db_connection()
    try:
        conn.execute('''
            UPDATE federation_peers 
            SET remote_api_key = ?, status = 'active'
            WHERE id = ?
        ''', (encrypt(remote_key), peer_id))
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
        from app.services.security import decrypt
        remote_key = decrypt(peer['remote_api_key'])
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
    """Approve a pending transfer (admin only).
    
    For OUTGOING transfers: We decrease our quantity and notify the requester.
    For INCOMING transfers: We add the item to our inventory.
    """
    import requests as http_requests
    
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
        
        item_data = json.loads(transfer['item_data'])
        sku = item_data.get('sku')
        quantity = transfer['quantity']
        
        logger.info(f"approve_transfer: id={transfer_id}, direction={transfer['direction']}, sku={sku}, qty={quantity}")
        
        if transfer['direction'] == 'outgoing':
            # OUTGOING: We are the source, decrease our quantity
            logger.info(f"Processing OUTGOING transfer - will DECREASE quantity")
            
            # Find and update local item
            local_item = conn.execute(
                'SELECT id, quantity FROM inventory_items WHERE sku = ?', (sku,)
            ).fetchone()
            
            if not local_item:
                return jsonify({'error': f'Item {sku} not found in local inventory'}), 404
            
            logger.info(f"Local item found: id={local_item['id']}, current_qty={local_item['quantity']}")
            
            if local_item['quantity'] < quantity:
                return jsonify({'error': f'Insufficient quantity. Have {local_item["quantity"]}, need {quantity}'}), 400
            
            # Decrease local quantity
            new_quantity = local_item['quantity'] - quantity
            logger.info(f"Updating quantity: {local_item['quantity']} - {quantity} = {new_quantity}")
            
            conn.execute(
                'UPDATE inventory_items SET quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                (new_quantity, local_item['id'])
            )
            
            # Log outgoing transaction (negative quantity change)
            conn.execute('''
                INSERT INTO inventory_transactions 
                (inventory_item_id, quantity_change, reason, user_id, source_tracking)
                VALUES (?, ?, 'Federation Transfer Out', ?, ?)
            ''', (local_item['id'], -quantity, current_user.username, f'transfer:{transfer_id}'))
            
            # Get peer info to notify them
            peer = conn.execute(
                'SELECT * FROM federation_peers WHERE id = ?', (transfer['peer_id'],)
            ).fetchone()
            
            # Notify the requesting peer that their item is ready
            if peer and peer['remote_api_key']:
                try:
                    http_requests.post(
                        f"{peer['url']}/api/federation/transfer/complete",
                        headers={'X-API-Key': peer['remote_api_key']},
                        json={
                            'sku': sku,
                            'item_data': item_data,
                            'quantity': quantity,
                            'source': load_config().get('LOCATION_PREFIX', 'RSCP')
                        },
                        timeout=10
                    )
                except Exception as e:
                    logger.warning(f"Could not notify peer of transfer completion: {e}")
            
            message = f"Transfer approved - sent {quantity} of {sku} (remaining: {new_quantity})"
            item_id = local_item['id']
            
        else:
            # INCOMING: We are the destination, add to our inventory
            existing_item = conn.execute(
                'SELECT id, quantity FROM inventory_items WHERE sku = ?', (sku,)
            ).fetchone()
            
            if existing_item:
                new_quantity = existing_item['quantity'] + quantity
                conn.execute(
                    'UPDATE inventory_items SET quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                    (new_quantity, existing_item['id'])
                )
                item_id = existing_item['id']
                message = f"Transfer approved - added {quantity} to existing item (new total: {new_quantity})"
            else:
                # Create new item
                conn.execute('''
                    INSERT INTO inventory_items (
                        sku, name, quantity, buy_price, sell_price,
                        location_area, location_aisle, location_shelf, location_bin,
                        supplier, asin, keywords, secondary_ids, description,
                        image_url, source_url
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    sku,
                    item_data.get('name'),
                    quantity,
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
                item_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                message = 'Transfer approved and new item created'
            
            # Log incoming transaction
            conn.execute('''
                INSERT INTO inventory_transactions 
                (inventory_item_id, quantity_change, reason, user_id, source_tracking)
                VALUES (?, ?, 'Federation Transfer In', ?, ?)
            ''', (item_id, quantity, current_user.username, f'transfer:{transfer_id}'))
        
        # Update transfer status
        conn.execute('''
            UPDATE federation_transfers 
            SET status = 'approved', approved_by = ?, approved_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (current_user.username, transfer_id))
        
        conn.commit()
        
        return jsonify({
            'success': True,
            'item_id': item_id,
            'message': message
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
