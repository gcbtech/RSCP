"""
Inventory Items Module
CRUD operations for inventory items.
"""
import os
import io
import time
import logging
import sqlite3
from flask import request, redirect, url_for, session, flash, render_template, jsonify
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
import base64
import uuid

from app.routes.inventory import inventory_bp, CATEGORY_CODES
from app.routes.inventory.core import generate_sku, validate_location
from app.services.db import get_db_connection
from app.services.data_manager import BASE_DIR, load_config

logger = logging.getLogger(__name__)

# Image optimization settings
MAX_IMAGE_SIZE = (1280, 720)  # Max resolution (720p)
JPEG_QUALITY = 85


def optimize_image(image_data, max_size=MAX_IMAGE_SIZE, quality=JPEG_QUALITY):
    """
    Optimize image by resizing and compressing.
    
    Args:
        image_data: bytes or file-like object
        max_size: tuple (width, height) - max dimensions
        quality: JPEG quality (1-100)
        
    Returns:
        bytes: Optimized image data as JPEG
    """
    try:
        from PIL import Image
        
        # Open image
        if hasattr(image_data, 'read'):
            img = Image.open(image_data)
        else:
            img = Image.open(io.BytesIO(image_data))
        
        # Convert to RGB if necessary (for PNG with transparency)
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')
        
        # Resize if larger than max_size
        img.thumbnail(max_size, Image.Resampling.LANCZOS)
        
        # Save to bytes
        output = io.BytesIO()
        img.save(output, format='JPEG', quality=quality, optimize=True)
        return output.getvalue()
        
    except ImportError:
        logger.warning("PIL not installed, skipping image optimization")
        if hasattr(image_data, 'read'):
            return image_data.read()
        return image_data
    except Exception as e:
        logger.error(f"Image optimization failed: {e}")
        if hasattr(image_data, 'read'):
            return image_data.read()
        return image_data


THUMB_SIZE = (40, 40)

def generate_thumbnail(image_url):
    """
    Generate a 40x40 thumbnail for an image.
    
    Args:
        image_url: URL path like /static/uploads/inventory/image.jpg
        
    Returns:
        str: Thumbnail URL path, or None if generation failed
    """
    if not image_url or image_url.startswith('http'):
        return None  # Skip external URLs
    
    try:
        from PIL import Image
        
        # Extract filename from URL
        if '/static/uploads/inventory/' not in image_url:
            return None
            
        filename = image_url.split('/static/uploads/inventory/')[-1]
        source_path = os.path.join(BASE_DIR, 'static', 'uploads', 'inventory', filename)
        
        if not os.path.exists(source_path):
            return None
        
        # Create thumbs directory if needed
        thumb_dir = os.path.join(BASE_DIR, 'static', 'uploads', 'inventory', 'thumbs')
        os.makedirs(thumb_dir, exist_ok=True)
        
        # Generate thumbnail filename
        base, ext = os.path.splitext(filename)
        thumb_filename = f"{base}_thumb.jpg"
        thumb_path = os.path.join(thumb_dir, thumb_filename)
        
        # Skip if thumbnail already exists and is newer than source
        if os.path.exists(thumb_path):
            if os.path.getmtime(thumb_path) >= os.path.getmtime(source_path):
                return f"/static/uploads/inventory/thumbs/{thumb_filename}"
        
        # Generate thumbnail
        with Image.open(source_path) as img:
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')
            img.thumbnail(THUMB_SIZE, Image.Resampling.LANCZOS)
            img.save(thumb_path, format='JPEG', quality=80, optimize=True)
        
        return f"/static/uploads/inventory/thumbs/{thumb_filename}"
        
    except ImportError:
        logger.warning("PIL not installed, cannot generate thumbnail")
        return None
    except Exception as e:
        logger.error(f"Thumbnail generation failed for {image_url}: {e}")
        return None


@inventory_bp.route('/items')
def list_items():
    """List all inventory items with sorting, pagination, and search support."""
    # Get sort parameters from query string
    sort_by = request.args.get('sort', 'name')
    order = request.args.get('order', 'asc')
    search_query = request.args.get('q', '').strip()
    
    # Pagination parameters
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 100))
    offset = (page - 1) * per_page
    
    # Whitelist allowed columns to prevent SQL injection
    allowed_columns = ['name', 'sku', 'quantity', 'location_area', 'buy_price', 'sell_price', 'created_at', 'updated_at']
    if sort_by not in allowed_columns:
        sort_by = 'name'
    
    # Validate order direction
    order_dir = 'ASC' if order.lower() == 'asc' else 'DESC'
    
    conn = get_db_connection()
    try:
        # Build search condition
        if search_query:
            search_pattern = f'%{search_query}%'
            search_clause = '''WHERE COALESCE(name, '') LIKE ? 
                               OR COALESCE(sku, '') LIKE ? 
                               OR COALESCE(location_area, '') LIKE ?'''
            search_params = (search_pattern, search_pattern, search_pattern)
            
            # Get total count with search filter
            total = conn.execute(f'SELECT COUNT(*) FROM inventory_items {search_clause}', search_params).fetchone()[0]
            
            items = conn.execute(f'''
                SELECT * FROM inventory_items 
                {search_clause}
                ORDER BY {sort_by} {order_dir}
                LIMIT ? OFFSET ?
            ''', search_params + (per_page, offset)).fetchall()
        else:
            # No search - get all
            total = conn.execute('SELECT COUNT(*) FROM inventory_items').fetchone()[0]
            
            items = conn.execute(f'''
                SELECT * FROM inventory_items 
                ORDER BY {sort_by} {order_dir}
                LIMIT ? OFFSET ?
            ''', (per_page, offset)).fetchall()
        
        total_pages = (total + per_page - 1) // per_page if total > 0 else 1
        
        # Generate thumbnails for list display
        items_with_thumbs = []
        for item in items:
            item_dict = dict(item)
            if item_dict.get('image_url'):
                item_dict['thumbnail_url'] = generate_thumbnail(item_dict['image_url'])
            else:
                item_dict['thumbnail_url'] = None
            items_with_thumbs.append(item_dict)
        
        if request.args.get('partial'):
            return render_template('inventory/_list_rows.html', items=items_with_thumbs)
            
        return render_template('inventory/list.html', 
                               items=items_with_thumbs, 
                               sort_by=sort_by, 
                               order=order,
                               page=page,
                               per_page=per_page,
                               total_pages=total_pages,
                               total_items=total,
                               search_query=search_query)
    finally:
        conn.close()


@inventory_bp.route('/add', methods=['GET', 'POST'])
def add_item():
    """Add a new inventory item."""
    if request.method == 'POST':
        sku = request.form.get('sku', '').strip()
        category = request.form.get('category', 'ATO').strip()
        name = request.form.get('name', '').strip()
        quantity = int(request.form.get('quantity', 0))
        
        location_area = request.form.get('location_area', '').strip()
        location_aisle = request.form.get('location_aisle', '').strip()
        location_shelf = request.form.get('location_shelf', '').strip()
        location_bin = request.form.get('location_bin', '').strip()
        
        asin = request.form.get('asin', '').strip() or None
        buy_price = request.form.get('buy_price', '').strip()
        sell_price = request.form.get('sell_price', '').strip()
        supplier = request.form.get('supplier', '').strip() or None
        first_stock_date = request.form.get('first_stock_date', '').strip() or None
        resupply_interval = request.form.get('resupply_interval', '').strip()
        keywords = request.form.get('keywords', '').strip()
        
        if not name:
            flash("Name is required.")
            # Preserve form data on validation error
            # Preserve form data on validation error
            prefill = {
                'sku': sku,
                'category': category,
                'name': name,
                'asin': asin or '',
                'quantity': quantity,
                'image_url': request.form.get('image_url', ''),
                'source_url': request.form.get('source_url', ''),
                'location_area': location_area,
                'location_aisle': location_aisle,
                'location_shelf': location_shelf,
                'location_bin': location_bin,
                'buy_price': buy_price,
                'sell_price': sell_price,
                'supplier': supplier,
                'first_stock_date': first_stock_date,
                'resupply_interval': resupply_interval,
                'keywords': keywords,
                'alert_enabled': request.form.get('alert_enabled') == 'on',
                'alert_threshold': request.form.get('alert_threshold', 0),
                'tracking': request.form.get('source_tracking'),
            }
            # Add secondary IDs for prefill
            secondary_ids = {'upc': request.form.get('upc'), 'part_number': request.form.get('part_number')}
            return render_template('inventory/add.html', prefill=prefill, secondary_ids=secondary_ids, categories=CATEGORY_CODES)
        
        if request.form.get('quick_add'):
            # Default location for Quick Add if not specified
            if not any([location_area, location_aisle, location_shelf, location_bin]):
                location_area = 'General'
        
        if not validate_location(location_area, location_aisle, location_shelf, location_bin):
            flash("At least one location field is required.")
            # Preserve form data on validation error
            # Preserve form data on validation error
            prefill = {
                'sku': sku,
                'category': category,
                'name': name,
                'asin': asin or '',
                'quantity': quantity,
                'image_url': request.form.get('image_url', ''),
                'source_url': request.form.get('source_url', ''),
                'location_area': location_area,
                'location_aisle': location_aisle,
                'location_shelf': location_shelf,
                'location_bin': location_bin,
                'buy_price': buy_price,
                'sell_price': sell_price,
                'supplier': supplier,
                'first_stock_date': first_stock_date,
                'resupply_interval': resupply_interval,
                'keywords': keywords,
                'alert_enabled': request.form.get('alert_enabled') == 'on',
                'alert_threshold': request.form.get('alert_threshold', 0),
                'tracking': request.form.get('source_tracking'),
            }
            secondary_ids = {'upc': request.form.get('upc'), 'part_number': request.form.get('part_number')}
            return render_template('inventory/add.html', prefill=prefill, secondary_ids=secondary_ids, categories=CATEGORY_CODES)
        
        if not sku:
            sku = generate_sku(category)
        
        buy_price = float(buy_price) if buy_price else None
        sell_price = float(sell_price) if sell_price else None
        resupply_interval = int(resupply_interval) if resupply_interval else None
        
        conn = get_db_connection()
        try:
            image_url = request.form.get('image_url', '').strip() or None
            
            if 'image' in request.files:
                file = request.files['image']
                if file and file.filename:
                    # Optimize image (resize and compress)
                    optimized_data = optimize_image(file)
                    unique_name = f"{secure_filename(sku)}_{int(time.time())}.jpg"
                    
                    upload_folder = os.path.join(BASE_DIR, 'static', 'uploads', 'inventory')
                    if not os.path.exists(upload_folder):
                        os.makedirs(upload_folder)
                        
                    with open(os.path.join(upload_folder, unique_name), 'wb') as f:
                        f.write(optimized_data)
                    image_url = url_for('static', filename=f'uploads/inventory/{unique_name}')

            source_url = request.form.get('source_url', '').strip() or None

            if asin:
                if not image_url:
                    image_url = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SX200_.jpg"
                if not source_url:
                    source_url = f"https://www.amazon.com/dp/{asin}"
            
            alert_enabled = request.form.get('alert_enabled') == 'on'
            alert_threshold = int(request.form.get('alert_threshold', 0) or 0)
            
            # Collect secondary IDs (UPC, part number, etc.)
            import json
            secondary_ids = {}
            upc = request.form.get('upc', '').strip()
            part_number = request.form.get('part_number', '').strip()
            if upc:
                secondary_ids['upc'] = upc
            if part_number:
                secondary_ids['part_number'] = part_number
            secondary_ids_json = json.dumps(secondary_ids) if secondary_ids else None
            
            conn.execute('''
                INSERT INTO inventory_items 
                (sku, name, quantity, location_area, location_aisle, location_shelf, location_bin,
                 asin, image_url, source_url, buy_price, sell_price, supplier, first_stock_date, 
                 resupply_interval, alert_enabled, alert_threshold, secondary_ids, keywords)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (sku, name, quantity, location_area or None, location_aisle or None, 
                  location_shelf or None, location_bin or None, asin, image_url, source_url, 
                  buy_price, sell_price, supplier, first_stock_date, resupply_interval,
                  1 if alert_enabled else 0, alert_threshold, secondary_ids_json, keywords))
            conn.commit()
            
            item_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
            if quantity > 0:
                conn.execute('''
                    INSERT INTO inventory_transactions 
                    (inventory_item_id, quantity_change, reason, user_id)
                    VALUES (?, ?, ?, ?)
                ''', (item_id, quantity, 'Initial Stock', session.get('user')))
                conn.commit()
            
            flash(f"Item added: {sku}")
            
            # Check for Auto-Copy SKU setting
            conf = load_config()
            if conf.get('INVENTORY_AUTO_COPY_SKU'):
                return redirect(url_for('inventory.list_items', new_sku=sku))
                
            return redirect(url_for('inventory.list_items'))
        
        except sqlite3.IntegrityError as e:
            # Handle duplicate SKU error
            if 'UNIQUE constraint failed' in str(e) and 'sku' in str(e).lower():
                flash(f"Error: SKU '{sku}' already exists. Please use a different SKU or let the system generate one.", "danger")
                # Preserve form data for re-entry
                prefill = {
                    'name': name,
                    'asin': asin or '',
                    'quantity': quantity,
                    'image_url': request.form.get('image_url', ''),
                    'source_url': request.form.get('source_url', ''),
                    'location_area': location_area,
                    'location_aisle': location_aisle,
                    'location_shelf': location_shelf,
                    'location_bin': location_bin,
                    'buy_price': buy_price,
                    'sell_price': sell_price,
                    'supplier': supplier,
                }
                return render_template('inventory/add.html', prefill=prefill, categories=CATEGORY_CODES)
            else:
                logger.error(f"Database integrity error: {e}")
                flash(f"Database error: {e}")
            
        except Exception as e:
            logger.error(f"Error adding inventory item: {e}")
            flash(f"Error adding item: {e}")
        finally:
            conn.close()
    
    # GET - Show form with optional prefill from query params
    prefill = {
        'asin': request.args.get('asin', ''),
        'tracking': request.args.get('tracking', ''),
        'quantity': request.args.get('qty', '1'),
        'image_url': request.args.get('image_url', ''),
        'name': request.args.get('name', ''),
        'source_url': request.args.get('source_url', ''),
    }
    
    return render_template('inventory/add.html', 
                           prefill=prefill, 
                           categories=CATEGORY_CODES)


@inventory_bp.route('/edit/<int:item_id>', methods=['GET', 'POST'])
def edit_item(item_id):
    """Edit an existing inventory item."""
    conn = get_db_connection()
    
    if request.method == 'POST':
        try:
            sku = request.form.get('sku', '').strip()
            name = request.form.get('name', '').strip()
            location_area = request.form.get('location_area', '').strip()
            location_aisle = request.form.get('location_aisle', '').strip()
            location_shelf = request.form.get('location_shelf', '').strip()
            location_bin = request.form.get('location_bin', '').strip()
            
            asin = request.form.get('asin', '').strip() or None
            buy_price = request.form.get('buy_price', '').strip()
            sell_price = request.form.get('sell_price', '').strip()
            supplier = request.form.get('supplier', '').strip() or None
            first_stock_date = request.form.get('first_stock_date', '').strip() or None
            resupply_interval = request.form.get('resupply_interval', '').strip()
            keywords = request.form.get('keywords', '').strip()
            
            if not sku:
                flash("SKU is required.")
                return redirect(url_for('inventory.edit_item', item_id=item_id))
            
            if not name:
                flash("Name is required.")
                return redirect(url_for('inventory.edit_item', item_id=item_id))
            
            if not validate_location(location_area, location_aisle, location_shelf, location_bin):
                flash("At least one location field is required.")
                return redirect(url_for('inventory.edit_item', item_id=item_id))
            
            existing = conn.execute('SELECT id FROM inventory_items WHERE sku = ? AND id != ?', 
                                   (sku, item_id)).fetchone()
            if existing:
                flash("SKU already exists for another item.")
                return redirect(url_for('inventory.edit_item', item_id=item_id))
            
            buy_price = float(buy_price) if buy_price else None
            sell_price = float(sell_price) if sell_price else None
            resupply_interval = int(resupply_interval) if resupply_interval else None
            source_url = request.form.get('source_url', '').strip() or None
            alert_enabled = request.form.get('alert_enabled') == 'on'
            alert_threshold = int(request.form.get('alert_threshold', 0) or 0)
            
            current_img = conn.execute('SELECT image_url FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
            image_url = current_img['image_url'] if current_img else None
            
            # Check if a new image URL was fetched/submitted via the form
            form_image_url = request.form.get('image_url', '').strip()
            if form_image_url:
                image_url = form_image_url
            
            # Check if a file was uploaded (takes priority over fetched URL)
            if 'image' in request.files:
                file = request.files['image']
                if file and file.filename:
                    # Optimize image (resize and compress)
                    optimized_data = optimize_image(file)
                    unique_name = f"{secure_filename(sku)}_{int(time.time())}.jpg"
                    
                    upload_folder = os.path.join(BASE_DIR, 'static', 'uploads', 'inventory')
                    if not os.path.exists(upload_folder):
                        os.makedirs(upload_folder)
                        
                    with open(os.path.join(upload_folder, unique_name), 'wb') as f:
                        f.write(optimized_data)
                    image_url = url_for('static', filename=f'uploads/inventory/{unique_name}')

            if asin:
                if not image_url:
                    image_url = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SX200_.jpg"
                # Note: Do NOT auto-generate source_url on edit - respect user's decision to clear it

            # Collect secondary IDs (UPC, part number, etc.)
            import json
            secondary_ids = {}
            upc = request.form.get('upc', '').strip()
            part_number = request.form.get('part_number', '').strip()
            if upc:
                secondary_ids['upc'] = upc
            if part_number:
                secondary_ids['part_number'] = part_number
            secondary_ids_json = json.dumps(secondary_ids) if secondary_ids else None

            conn.execute('''
                UPDATE inventory_items SET
                    sku = ?, name = ?, location_area = ?, location_aisle = ?, 
                    location_shelf = ?, location_bin = ?, asin = ?,
                    buy_price = ?, sell_price = ?, supplier = ?,
                    first_stock_date = ?, resupply_interval = ?, source_url = ?,
                    image_url = ?, alert_enabled = ?, alert_threshold = ?,
                    secondary_ids = ?, keywords = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (sku, name, location_area or None, location_aisle or None,
                  location_shelf or None, location_bin or None, asin,
                  buy_price, sell_price, supplier, first_stock_date,
                  resupply_interval, source_url, image_url, 1 if alert_enabled else 0, 
                  alert_threshold, secondary_ids_json, keywords, item_id))
            conn.commit()
            
            flash("Item updated.")
            return redirect(url_for('inventory.list_items'))
            
        except Exception as e:
            logger.error(f"Error updating inventory item: {e}")
            flash(f"Error updating item: {e}")
        finally:
            conn.close()
    
    # GET - Load item for editing
    try:
        item = conn.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
        if not item:
            flash("Item not found.")
            return redirect(url_for('inventory.list_items'))
        
        transactions = conn.execute('''
            SELECT * FROM inventory_transactions 
            WHERE inventory_item_id = ? 
            ORDER BY created_at DESC LIMIT 50
        ''', (item_id,)).fetchall()
        
        # Parse secondary IDs for template
        import json
        secondary_ids = {}
        try:
            if 'secondary_ids' in item.keys() and item['secondary_ids']:
                secondary_ids = json.loads(item['secondary_ids'])
        except json.JSONDecodeError:
            pass  # Invalid JSON in secondary_ids
        
        return render_template('inventory/add.html', 
                               item=item, 
                               transactions=transactions,
                               edit_mode=True,
                               categories=CATEGORY_CODES,
                               secondary_ids=secondary_ids)
    finally:
        conn.close()


@inventory_bp.route('/delete/<int:item_id>', methods=['POST'])
def delete_item(item_id):
    """Delete an inventory item."""
    conn = get_db_connection()
    try:
        conn.execute('DELETE FROM inventory_transactions WHERE inventory_item_id = ?', (item_id,))
        conn.execute('DELETE FROM inventory_items WHERE id = ?', (item_id,))
        conn.commit()
        flash("Item deleted.")
    except Exception as e:
        logger.error(f"Error deleting inventory item: {e}")
        flash(f"Error: {e}")
    finally:
        conn.close()
    
    return redirect(url_for('inventory.list_items'))


@inventory_bp.route('/transaction/delete/<int:tx_id>', methods=['POST'])
def delete_transaction(tx_id):
    """Delete a transaction and revert the stock change."""
    conn = get_db_connection()
    try:
        tx = conn.execute('SELECT * FROM inventory_transactions WHERE id = ?', (tx_id,)).fetchone()
        if not tx:
            flash("Transaction not found.")
            return redirect(request.referrer or url_for('inventory.list_items'))
        
        change = tx['quantity_change']
        item_id = tx['inventory_item_id']
        
        conn.execute('UPDATE inventory_items SET quantity = quantity - ? WHERE id = ?', (change, item_id))
        conn.execute('DELETE FROM inventory_transactions WHERE id = ?', (tx_id,))
        conn.commit()
        
        flash("Transaction reverted.")
    except Exception as e:
        logger.error(f"Error deleting transaction: {e}")
        flash("Error reverting transaction.")
    finally:
        conn.close()
    
    return redirect(request.referrer or url_for('inventory.edit_item', item_id=item_id))


@inventory_bp.route('/adjust/<int:item_id>', methods=['POST'])
def adjust_quantity(item_id):
    """Adjust quantity for an inventory item."""
    import threading
    
    quantity_change = int(request.form.get('quantity_change', 0))
    reason = request.form.get('reason', 'Sold/Consumed').strip()
    source_tracking = request.form.get('source_tracking', '').strip() or None
    
    if quantity_change == 0:
        flash("No change specified.")
        return redirect(url_for('inventory.list_items'))
    
    conn = get_db_connection()
    try:
        if quantity_change < 0:
            current = conn.execute('SELECT quantity FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
            if current and (current['quantity'] + quantity_change) < 0:
                flash(f"Cannot reduce by {abs(quantity_change)} - only {current['quantity']} in stock.")
                conn.close()
                return redirect(url_for('inventory.list_items'))
        
        conn.execute('''
            UPDATE inventory_items 
            SET quantity = quantity + ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (quantity_change, item_id))
        
        conn.execute('''
            INSERT INTO inventory_transactions 
            (inventory_item_id, quantity_change, reason, user_id, source_tracking)
            VALUES (?, ?, ?, ?, ?)
        ''', (item_id, quantity_change, reason, session.get('user'), source_tracking))
        
        conn.commit()
        
        # Check for stock alert
        if quantity_change < 0:
            from app.services.data_manager import load_config
            from app.utils.helpers import reveal_string
            
            item = conn.execute('''
                SELECT name, sku, quantity, alert_enabled, alert_threshold 
                FROM inventory_items WHERE id = ?
            ''', (item_id,)).fetchone()
            
            if item and item['alert_enabled']:
                new_qty = item['quantity']
                threshold = item['alert_threshold'] or 0
                
                if new_qty <= threshold:
                    conf = load_config()
                    
                    # In-app notifications
                    is_oos = new_qty <= 0
                    notify_oos = conf.get('NOTIFY_OOS', False)
                    notify_low = conf.get('NOTIFY_LOW_STOCK', False)
                    
                    if (is_oos and notify_oos) or (not is_oos and notify_low):
                        try:
                            from app.routes.notifications import create_notification
                            status = "OUT OF STOCK" if is_oos else f"LOW STOCK ({new_qty})"
                            title = f"📦 {status}: {item['name']}"
                            message = f"SKU: {item['sku']} - {new_qty} remaining (threshold: {threshold})"
                            create_notification(
                                user_id=None,  # All users
                                title=title,
                                message=message,
                                notification_type='warning' if not is_oos else 'error',
                                link=f"/inventory/edit/{item_id}"
                            )
                        except Exception as e:
                            logger.error(f"In-app notification error: {e}")
                    
                    # Webhook notification (existing)
                    if conf.get('WEBHOOK_ENABLED_INVENTORY') and conf.get('WEBHOOK_URL_INVENTORY'):
                        def send_stock_alert():
                            try:
                                import urllib.request
                                import json
                                
                                url = reveal_string(conf['WEBHOOK_URL_INVENTORY'], conf.get('SECRET_KEY', 'dev_key'))
                                if not url.startswith('http'):
                                    return
                                
                                status = "OUT OF STOCK" if new_qty == 0 else f"LOW STOCK ({new_qty} remaining)"
                                msg = f"📦 **{status}**: {item['name']} (SKU: {item['sku']})"
                                
                                payload = {"content": msg, "text": msg}
                                req = urllib.request.Request(
                                    url,
                                    data=json.dumps(payload).encode('utf-8'),
                                    headers={'Content-Type': 'application/json', 'User-Agent': 'RSCP-Bot'}
                                )
                                urllib.request.urlopen(req, timeout=5)
                                logger.info(f"Stock alert sent for {item['sku']}")
                            except Exception as e:
                                logger.error(f"Stock alert webhook error: {e}")
                        
                        threading.Thread(target=send_stock_alert, daemon=True).start()
        
        action = "Added" if quantity_change > 0 else "Removed"
        flash(f"{action} {abs(quantity_change)} units.")
        
    except Exception as e:
        logger.error(f"Error adjusting quantity: {e}")
        flash(f"Error: {e}")
    finally:
        conn.close()
    
    return redirect(url_for('inventory.list_items'))


@inventory_bp.route('/add_stock/<int:item_id>', methods=['GET', 'POST'])
def add_stock(item_id):
    """Confirmation page for adding stock to existing inventory item from scan."""
    conn = get_db_connection()
    try:
        item = conn.execute('SELECT * FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
        if not item:
            flash("Item not found.")
            return redirect(url_for('main.scan_page', mode='receive'))
        
        if request.method == 'POST':
            qty_to_add = int(request.form.get('qty', 1))
            tracking = request.form.get('tracking', '')
            
            new_qty = item['quantity'] + qty_to_add
            conn.execute('UPDATE inventory_items SET quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                         (new_qty, item_id))
            
            conn.execute('''
                INSERT INTO inventory_transactions (inventory_item_id, quantity_change, reason, source_tracking)
                VALUES (?, ?, ?, ?)
            ''', (item_id, qty_to_add, 'Received from Scan', tracking))
            
            conn.commit()
            return redirect(url_for('main.scan_page', mode='receive'))
        
        # GET: Show confirmation form
        qty_to_add = int(request.args.get('qty', 1))
        tracking = request.args.get('tracking', '')
        
        return render_template('inventory/add_stock.html', 
                               item=item, qty_to_add=qty_to_add, tracking=tracking)
    except Exception as e:
        logger.error(f"Error in add_stock: {e}")
        flash(f"Error: {e}")
        return redirect(url_for('main.scan_page', mode='receive'))
    finally:
        conn.close()
@inventory_bp.route('/upload_photo', methods=['POST'])
def upload_photo():
    """Upload photo from camera."""
    data = request.json
    if not data or 'image' not in data:
        return jsonify({'error': 'No image data'}), 400
        
    try:
        # Image data comes as "data:image/jpeg;base64,..."
        if ',' in data['image']:
            image_data = data['image'].split(',')[1]
        else:
            image_data = data['image']
            
        image_binary = base64.b64decode(image_data)
        
        # Optimize image (resize and compress)
        optimized_data = optimize_image(image_binary)
        
        filename = f"{uuid.uuid4().hex}.jpg"
        # Ensure directory exists
        upload_dir = os.path.join(BASE_DIR, 'static', 'uploads', 'inventory')
        os.makedirs(upload_dir, exist_ok=True)
        
        filepath = os.path.join(upload_dir, filename)
        with open(filepath, 'wb') as f:
            f.write(optimized_data)
            
        return jsonify({'url': f'/static/uploads/inventory/{filename}'})
    except Exception as e:
        logger.error(f"Photo upload failed: {e}")
        return jsonify({'error': str(e)}), 500

@inventory_bp.route('/export_csv')
@login_required
def export_inventory_csv():
    """Export inventory to CSV."""
    import csv
    import io
    from flask import make_response
    
    conn = get_db_connection()
    items = conn.execute('SELECT * FROM inventory_items ORDER BY sku').fetchall()
    conn.close()
    
    si = io.StringIO()
    cw = csv.writer(si)
    
    # Headers
    cw.writerow(['SKU', 'Name', 'Category', 'Quantity', 'Location', 'Buy Price', 'Sell Price', 'Supplier'])
    
    for item in items:
        # Construct location string
        locs = []
        if item['location_area']: locs.append(item['location_area'])
        if item['location_aisle']: locs.append(item['location_aisle'])
        if item['location_shelf']: locs.append(item['location_shelf'])
        if item['location_bin']: locs.append(item['location_bin'])
        location = ' '.join(locs)
        
        # Derive Category from SKU if possible
        sku = item['sku'] or ''
        parts = sku.split('-')
        cat_code = parts[1] if len(parts) >= 2 and parts[0] == 'RSCP' else 'Other'
        
        cw.writerow([
            item['sku'],
            item['name'],
            cat_code,
            item['quantity'],
            location,
            item['buy_price'],
            item['sell_price'],
            item['supplier']
        ])
        
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=inventory_export.csv"
    output.headers["Content-type"] = "text/csv"
    return output


@inventory_bp.route('/federated-search')
@login_required
def federated_search():
    """Search inventory across all linked instances."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import requests as http_requests
    
    query = request.args.get('q', '').strip()
    if len(query) < 2:
        return jsonify({'results': []})
    
    config = load_config()
    if not config.get('FEDERATION_CROSS_SEARCH_ENABLED'):
        return jsonify({'error': 'Cross-search disabled'}), 403
    
    conn = get_db_connection()
    try:
        peers = conn.execute(
            "SELECT * FROM federation_peers WHERE status = 'active'"
        ).fetchall()
    finally:
        conn.close()
    
    if not peers:
        return jsonify({'results': []})
    
    all_results = []
    debug_info = []
    
    def search_peer(peer):
        peer_name = peer.get('name', 'Unknown')
        try:
            # Use remote_api_key (THEIR key) to authenticate to THEM
            remote_key = peer.get('remote_api_key')
            if not remote_key:
                msg = f"Skipping {peer_name} - no remote API key configured"
                logger.warning(msg)
                return [], msg
            
            url = f"{peer['url']}/api/federation/search"
            key_preview = remote_key[:8] + '...' if remote_key else 'None'
            logger.info(f"Federated search to {peer_name}: {url} with key {key_preview}")
            
            response = http_requests.post(
                url,
                headers={'X-API-Key': remote_key},
                json={'query': query},
                timeout=5
            )
            
            logger.info(f"Response from {peer_name}: {response.status_code}")
            
            if response.status_code == 200:
                data = response.json()
                results = data.get('results', [])
                return results, f"{peer_name}: {len(results)} results"
            else:
                msg = f"{peer_name}: HTTP {response.status_code} (key: {key_preview}) - {response.text[:200]}"
                logger.warning(msg)
                return [], msg
        except Exception as e:
            msg = f"{peer_name}: Error - {e}"
            logger.warning(f"Federated search to {peer_name} failed: {e}")
            return [], msg
    
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(search_peer, dict(p)): p for p in peers}
        for future in as_completed(futures):
            results, debug_msg = future.result()
            all_results.extend(results)
            debug_info.append(debug_msg)
    
    return jsonify({
        'results': all_results,
        'debug': debug_info,
        'peers_checked': len(peers)
    })


@inventory_bp.route('/request-transfer', methods=['POST'])
@login_required
def request_transfer():
    """Request a transfer from another location."""
    import requests as http_requests
    from datetime import datetime, timedelta
    
    data = request.get_json()
    sku = data.get('sku')
    source_prefix = data.get('source')
    quantity = data.get('quantity', 1)
    
    if not sku or not source_prefix:
        return jsonify({'error': 'SKU and source are required'}), 400
    
    config = load_config()
    
    conn = get_db_connection()
    try:
        # Find the peer by prefix
        peer = conn.execute(
            "SELECT * FROM federation_peers WHERE location_prefix = ? AND status = 'active'",
            (source_prefix,)
        ).fetchone()
        
        if not peer:
            return jsonify({'error': f'No active peer with prefix {source_prefix}'}), 404
        
        # Check for remote API key
        remote_key = peer['remote_api_key']
        if not remote_key:
            return jsonify({'error': f'Peer {source_prefix} has no remote API key configured'}), 400
        
        # First, get the item details from the peer
        try:
            item_response = http_requests.get(
                f"{peer['url']}/api/federation/items/{sku}",
                headers={'X-API-Key': remote_key},
                timeout=5
            )
            if item_response.status_code != 200:
                return jsonify({'error': 'Item not found on remote'}), 404
            item_data = item_response.json()
        except Exception as e:
            return jsonify({'error': f'Could not reach {source_prefix}: {e}'}), 500
        
        # Send transfer request to peer
        try:
            transfer_response = http_requests.post(
                f"{peer['url']}/api/federation/transfer/request",
                headers={'X-API-Key': remote_key},
                json={
                    'sku': sku,
                    'item_data': item_data,
                    'quantity': quantity,
                    'requested_by': current_user.username
                },
                timeout=10
            )
            if transfer_response.status_code != 200:
                return jsonify({'error': 'Transfer request failed'}), 500
            
            result = transfer_response.json()
            
            # Log outgoing transfer locally
            expires_at = datetime.now() + timedelta(hours=72)
            conn.execute('''
                INSERT INTO federation_transfers 
                (direction, peer_id, item_sku, item_data, quantity, status, 
                 requested_by, expires_at, notes)
                VALUES ('outgoing', ?, ?, ?, ?, 'pending', ?, ?, ?)
            ''', (
                peer['id'],
                sku,
                json.dumps(item_data),
                quantity,
                current_user.username,
                expires_at.isoformat(),
                f"Remote transfer_id: {result.get('transfer_id')}"
            ))
            conn.commit()
            
            return jsonify({'success': True, 'transfer_id': result.get('transfer_id')})
            
        except Exception as e:
            return jsonify({'error': f'Transfer request failed: {e}'}), 500
    finally:
        conn.close()

