"""
Inventory Items Module
CRUD operations for inventory items.
"""
import os
import io
import time
import logging
import sqlite3
import json
from flask import request, redirect, url_for, session, flash, render_template, jsonify
from flask_login import login_required, current_user
from app.utils.permissions import require_permission
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


@inventory_bp.route('/low-stock')
@login_required
def low_stock_report():
    """Show report of items with low stock or out of stock."""
    conn = get_db_connection()
    try:
        # Exclude legacy items from low stock report
        items = conn.execute('''
            SELECT * FROM inventory_items 
            WHERE COALESCE(is_legacy, 0) = 0
            AND (quantity <= 0 OR (quantity <= alert_threshold AND alert_threshold > 0))
            ORDER BY quantity ASC, name ASC
        ''').fetchall()
        return render_template('inventory/low_stock.html', items=items)
    finally:
        conn.close()


@inventory_bp.route('/bulk-edit', methods=['POST'])
@login_required
@require_permission('inventory.manage')
def bulk_edit():
    """Handle bulk editing of inventory items."""
    item_ids = request.form.getlist('item_ids')
    if not item_ids:
        flash("No items selected.")
        return redirect(url_for('inventory.list_items'))
        
    conn = get_db_connection()
    try:
        if request.form.get('confirm_bulk_edit'):
            # Processing Phase
            count = 0
            
            # Prepare update query dynamically based on provided fields
            fields = []
            params = []
            
            # Helper to add field if present
            def add_if_present(field_name, col_name, convert_func=None):
                val = request.form.get(field_name, '').strip()
                if val:
                    if convert_func:
                        try:
                            val = convert_func(val)
                        except ValueError:
                            return # Skip invalid
                    fields.append(f"{col_name} = ?")
                    params.append(val)
            
            add_if_present('location_area', 'location_area')
            add_if_present('location_aisle', 'location_aisle')
            add_if_present('location_shelf', 'location_shelf')
            add_if_present('location_bin', 'location_bin')
            
            add_if_present('buy_price', 'buy_price', float)
            add_if_present('sell_price', 'sell_price', float)
            add_if_present('keywords', 'keywords')
            
            # Special logic for Threshold to handle 0 properly
            threshold_val = request.form.get('alert_threshold', '').strip()
            if threshold_val:
                try:
                    thresh = int(threshold_val)
                    fields.append("alert_threshold = ?")
                    params.append(thresh)
                    # Auto-update alert_enabled based on rule
                    fields.append("alert_enabled = ?")
                    params.append(1 if thresh > 0 else 0)
                except ValueError:
                    pass

            # Addons (allow 0 or 1, strictly)
            addon1_val = request.form.get('addon_1', '').strip()
            if addon1_val in ['0', '1']:
                fields.append("addon_1 = ?")
                params.append(int(addon1_val))
                
            addon2_val = request.form.get('addon_2', '').strip()
            if addon2_val in ['0', '1']:
                fields.append("addon_2 = ?")
                params.append(int(addon2_val))
            
            if not fields:
                flash("No changes specified.")
                return redirect(url_for('inventory.list_items'))
                
            fields.append("updated_at = CURRENT_TIMESTAMP")
            
            # Execute Update
            query = f"UPDATE inventory_items SET {', '.join(fields)} WHERE id = ?"
            
            for item_id in item_ids:
                conn.execute(query, params + [item_id])
                count += 1
                
            conn.commit()
            flash(f"Successfully updated {count} items.")
            return redirect(url_for('inventory.list_items'))
        
        else:
            # Rendering Phase (Selection Confirmation)
            placeholders = ','.join('?' * len(item_ids))
            items = conn.execute(f'SELECT id, sku, name FROM inventory_items WHERE id IN ({placeholders})', item_ids).fetchall()
            
            # Config for dropdowns
            from app.services.data_manager import load_config
            config = load_config()
            
            return render_template('inventory/bulk_edit.html', items=items, item_ids=item_ids, config=config)
            
    except Exception as e:
        logger.error(f"Bulk edit error: {e}")
        flash(f"Error during bulk edit: {e}")
        return redirect(url_for('inventory.list_items'))
    finally:
        conn.close()


@inventory_bp.route('/items')
@login_required
def list_items():
    """List all inventory items with sorting, pagination, and search support."""
    # Get sort parameters from query string
    sort_by = request.args.get('sort', 'name')
    search_query = request.args.get('q', '').strip()
    show_legacy = request.args.get('show_legacy', '0') == '1'
    
    # Pagination parameters
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 100))
    offset = (page - 1) * per_page
    
    # Whitelist allowed columns to prevent SQL injection
    allowed_columns = ['name', 'sku', 'quantity', 'location_area', 'buy_price', 'sell_price', 'created_at', 'updated_at']
    if sort_by not in allowed_columns:
        sort_by = 'name'
        
    default_order = 'desc' if sort_by == 'created_at' else 'asc'
    order = request.args.get('order', default_order)
    
    # Validate order direction
    order_dir = 'ASC' if order.lower() == 'asc' else 'DESC'
    
    # Legacy filter clause
    legacy_clause = "" if show_legacy else "AND COALESCE(is_legacy, 0) = 0"
    
    conn = get_db_connection()
    try:
        # Build search condition
        if search_query:
            tokens = search_query.strip().split()
            search_conditions = []
            search_params_list = []
            
            for token in tokens:
                pattern = f'%{token}%'
                search_conditions.append('''
                    (COALESCE(name, '') LIKE ? 
                     OR COALESCE(sku, '') LIKE ? 
                     OR COALESCE(location_area, '') LIKE ?
                     OR COALESCE(secondary_ids, '') LIKE ?)
                ''')
                search_params_list.extend([pattern, pattern, pattern, pattern])
            
            search_clause = "WHERE (" + " AND ".join(search_conditions) + f") {legacy_clause}"
            search_params = tuple(search_params_list)
            
            # Get total count with search filter
            total = conn.execute(f'SELECT COUNT(*) FROM inventory_items {search_clause}', search_params).fetchone()[0]
            
            items = conn.execute(f'''
                SELECT *,
                (SELECT SUM(quantity) FROM packages p 
                 WHERE p.sku = inventory_items.sku 
                 AND p.status NOT IN ('received', 'refunded', 'return_pending', 'returned', 'archived')
                ) as incoming_count
                FROM inventory_items 
                {search_clause}
                ORDER BY {sort_by} {order_dir}
                LIMIT ? OFFSET ?
            ''', search_params + (per_page, offset)).fetchall()
        else:
            # No search - get all (applying legacy filter)
            where_clause = "WHERE 1=1 " + legacy_clause
            total = conn.execute(f'SELECT COUNT(*) FROM inventory_items {where_clause}').fetchone()[0]
            
            items = conn.execute(f'''
                SELECT *,
                (SELECT SUM(quantity) FROM packages p 
                 WHERE p.sku = inventory_items.sku 
                 AND p.status NOT IN ('received', 'refunded', 'return_pending', 'returned', 'archived')
                ) as incoming_count
                FROM inventory_items 
                {where_clause}
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
            return render_template('inventory/_list_rows.html', 
                                   items=items_with_thumbs,
                                   sort_by=sort_by,
                                   order=order,
                                   page=page,
                                   search_query=search_query,
                                   show_legacy=show_legacy)
            
        return render_template('inventory/list.html', 
                               items=items_with_thumbs, 
                               sort_by=sort_by, 
                               order=order,
                               page=page,
                               per_page=per_page,
                               total_pages=total_pages,
                               total_items=total,
                               search_query=search_query,
                               show_legacy=show_legacy)
    finally:
        conn.close()


@inventory_bp.route('/add', methods=['GET', 'POST'])
@login_required
@require_permission('inventory.manage')
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
        resupply_interval = request.form.get('resupply_interval', '').strip()
        keywords = request.form.get('keywords', '').strip()
        notes = request.form.get('notes', '').strip()
        description = request.form.get('description', '').strip() or None
        
        if not name:
            flash("Name is required.")
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
                'description': description,
                'notes': notes,
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
                'description': description,
                'notes': notes,
            }
            secondary_ids = {'upc': request.form.get('upc'), 'part_number': request.form.get('part_number')}
            return render_template('inventory/add.html', prefill=prefill, secondary_ids=secondary_ids, categories=CATEGORY_CODES)
        
        if not sku:
            sku = generate_sku(category)
            
        # Check specific config to auto-set first stock date
        conf = load_config()
        if not first_stock_date and conf.get('TRACK_FIRST_STOCK_DATE'):
            from datetime import date
            first_stock_date = date.today().isoformat()
        
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

            # Handle additional gallery images
            additional_images_list = []
            if 'additional_images' in request.files:
                gallery_files = request.files.getlist('additional_images')
                for i, file in enumerate(gallery_files):
                    if file and file.filename:
                        optimized_data = optimize_image(file)
                        unique_name = f"{secure_filename(sku)}_gallery_{i}_{int(time.time())}_{uuid.uuid4().hex[:6]}.jpg"
                        
                        upload_folder = os.path.join(BASE_DIR, 'static', 'uploads', 'inventory')
                        if not os.path.exists(upload_folder):
                            os.makedirs(upload_folder)
                            
                        with open(os.path.join(upload_folder, unique_name), 'wb') as f:
                            f.write(optimized_data)
                        img_path = url_for('static', filename=f'uploads/inventory/{unique_name}')
                        additional_images_list.append(img_path)

            # If cover is empty, the first uploaded gallery image automatically becomes the primary cover
            if not image_url and additional_images_list:
                image_url = additional_images_list.pop(0)

            source_url = request.form.get('source_url', '').strip() or None

            if asin:
                if not image_url:
                    image_url = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SX200_.jpg"
                if not source_url:
                    source_url = f"https://www.amazon.com/dp/{asin}"
            
            alert_threshold = int(request.form.get('alert_threshold', 0) or 0)
            # User Rule: Enabled if > 0, Disabled if 0
            alert_enabled = True if alert_threshold > 0 else False
            
            # Collect secondary IDs (UPC, part number, etc.)
            secondary_ids = {}
            upc = request.form.get('upc', '').strip()
            part_number = request.form.get('part_number', '').strip()
            if upc:
                secondary_ids['upc'] = upc
            if part_number:
                secondary_ids['part_number'] = part_number
            secondary_ids_json = json.dumps(secondary_ids) if secondary_ids else None
            
            # Get addon selections
            addon_1 = 1 if request.form.get('addon_1') == 'on' else 0
            addon_2 = 1 if request.form.get('addon_2') == 'on' else 0
            
            conn.execute('''
                INSERT INTO inventory_items 
                (sku, name, quantity, location_area, location_aisle, location_shelf, location_bin,
                 asin, image_url, source_url, buy_price, sell_price, supplier, first_stock_date, 
                 resupply_interval, alert_enabled, alert_threshold, secondary_ids, keywords, addon_1, addon_2, description, additional_images, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (sku, name, quantity, location_area or None, location_aisle or None, 
                  location_shelf or None, location_bin or None, asin, image_url, source_url, 
                  buy_price, sell_price, supplier, first_stock_date, resupply_interval,
                  1 if alert_enabled else 0, alert_threshold, secondary_ids_json, keywords, addon_1, addon_2, description, json.dumps(additional_images_list) if additional_images_list else None, notes))
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
            
            # Check for redirect (next) param
            next_url = request.args.get('next') or request.form.get('next')
            if next_url:
                if next_url == 'scan':
                    return redirect(url_for('main.scan_page', mode='receiving'))
                return redirect(next_url)
            
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
                    'first_stock_date': first_stock_date,
                    'resupply_interval': resupply_interval,
                    'keywords': keywords,
                    'alert_enabled': request.form.get('alert_enabled') == 'on',
                    'alert_threshold': request.form.get('alert_threshold', 0),
                    'tracking': request.form.get('source_tracking'),
                    'description': description,
                    'notes': notes,
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
@login_required
@require_permission('inventory.manage')
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
            resupply_interval = request.form.get('resupply_interval', '').strip()
            keywords = request.form.get('keywords', '').strip()
            notes = request.form.get('notes', '').strip()
            
            # Pagination state
            page = request.form.get('page')
            page = int(page) if page and page.isdigit() else 1
            sort_by = request.form.get('sort', 'name')
            order = request.form.get('order', 'asc')
            search_query = request.form.get('q', '')

            if not sku:
                flash("SKU is required.")
                return redirect(url_for('inventory.edit_item', item_id=item_id, page=page, sort=sort_by, order=order, q=search_query))
            
            if not name:
                flash("Name is required.")
                return redirect(url_for('inventory.edit_item', item_id=item_id, page=page, sort=sort_by, order=order, q=search_query))
            
            if not validate_location(location_area, location_aisle, location_shelf, location_bin):
                flash("At least one location field is required.")
                return redirect(url_for('inventory.edit_item', item_id=item_id, page=page, sort=sort_by, order=order, q=search_query))
            
            existing = conn.execute('SELECT id FROM inventory_items WHERE sku = ? AND id != ?', 
                                   (sku, item_id)).fetchone()
            if existing:
                flash("SKU already exists for another item.")
                return redirect(url_for('inventory.edit_item', item_id=item_id, page=page, sort=sort_by, order=order, q=search_query))
            
            buy_price = float(buy_price) if buy_price else None
            sell_price = float(sell_price) if sell_price else None
            resupply_interval = int(resupply_interval) if resupply_interval else None
            source_url = request.form.get('source_url', '').strip() or None
            alert_threshold = int(request.form.get('alert_threshold', 0) or 0)
            # User Rule: Enabled if > 0, Disabled if 0
            alert_enabled = True if alert_threshold > 0 else False
            
            # Get description from form
            description = request.form.get('description', '').strip() or None

            # Fetch current state from DB
            current_item = conn.execute('SELECT image_url, additional_images FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
            image_url = current_item['image_url'] if current_item else None
            additional_images_json = current_item['additional_images'] if current_item else None
            additional_images_list = []
            if additional_images_json:
                try:
                    additional_images_list = json.loads(additional_images_json)
                except json.JSONDecodeError:
                    pass

            # Handle deleted images
            deleted_images = request.form.getlist('delete_images')
            for img in deleted_images:
                if img in additional_images_list:
                    additional_images_list.remove(img)
                    # Safely delete file from static folder
                    if img.startswith('/static/'):
                        file_path = os.path.join(BASE_DIR, img.lstrip('/'))
                        if os.path.exists(file_path):
                            try:
                                os.remove(file_path)
                                # Check if a thumbnail exists and delete it too
                                thumb_url = generate_thumbnail(img)
                                if thumb_url and thumb_url.startswith('/static/'):
                                    thumb_path = os.path.join(BASE_DIR, thumb_url.lstrip('/'))
                                    if os.path.exists(thumb_path):
                                        os.remove(thumb_path)
                            except Exception as e:
                                logger.error(f"Error deleting image file: {e}")

            # Check if a new image URL was fetched/submitted via the form
            form_image_url = request.form.get('image_url', '').strip()
            if form_image_url:
                image_url = form_image_url

            # Handle cover swap
            swap_cover = request.form.get('swap_cover', '').strip()
            if swap_cover and swap_cover in additional_images_list:
                old_cover = image_url
                image_url = swap_cover
                additional_images_list.remove(swap_cover)
                if old_cover:
                    # Add old cover to additional images
                    additional_images_list.append(old_cover)

            # Check if a new cover file was uploaded (takes priority over fetched URL and swap)
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

            # Handle new gallery uploads
            if 'additional_images' in request.files:
                gallery_files = request.files.getlist('additional_images')
                for i, file in enumerate(gallery_files):
                    if file and file.filename:
                        optimized_data = optimize_image(file)
                        unique_name = f"{secure_filename(sku)}_gallery_{i}_{int(time.time())}_{uuid.uuid4().hex[:6]}.jpg"
                        
                        upload_folder = os.path.join(BASE_DIR, 'static', 'uploads', 'inventory')
                        if not os.path.exists(upload_folder):
                            os.makedirs(upload_folder)
                            
                        with open(os.path.join(upload_folder, unique_name), 'wb') as f:
                            f.write(optimized_data)
                        img_path = url_for('static', filename=f'uploads/inventory/{unique_name}')
                        additional_images_list.append(img_path)

            # Fallback: if not image_url and additional_images_list:
            if not image_url and additional_images_list:
                image_url = additional_images_list.pop(0)

            if asin:
                if not image_url:
                    image_url = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SX200_.jpg"
                # Note: Do NOT auto-generate source_url on edit - respect user's decision to clear it

            # Collect secondary IDs (UPC, part number, etc.)
            secondary_ids = {}
            upc = request.form.get('upc', '').strip()
            part_number = request.form.get('part_number', '').strip()
            if upc:
                secondary_ids['upc'] = upc
            if part_number:
                secondary_ids['part_number'] = part_number
            secondary_ids_json = json.dumps(secondary_ids) if secondary_ids else None

            # Get addon selections
            addon_1 = 1 if request.form.get('addon_1') == 'on' else 0
            addon_2 = 1 if request.form.get('addon_2') == 'on' else 0
            
            # Get legacy status
            is_legacy = 1 if request.form.get('is_legacy') == 'on' else 0

            conn.execute('''
                UPDATE inventory_items SET
                    sku = ?, name = ?, location_area = ?, location_aisle = ?, 
                    location_shelf = ?, location_bin = ?, asin = ?,
                    buy_price = ?, sell_price = ?, supplier = ?,
                    first_stock_date = ?, resupply_interval = ?, source_url = ?,
                    image_url = ?, alert_enabled = ?, alert_threshold = ?,
                    secondary_ids = ?, keywords = ?, addon_1 = ?, addon_2 = ?, 
                    description = ?, additional_images = ?, notes = ?, 
                    is_legacy = ?, updated_at = CURRENT_TIMESTAMP
                 WHERE id = ?
             ''', (sku, name, location_area or None, location_aisle or None,
                   location_shelf or None, location_bin or None, asin,
                   buy_price, sell_price, supplier, first_stock_date,
                   resupply_interval, source_url, image_url, 1 if alert_enabled else 0, 
                   alert_threshold, secondary_ids_json, keywords, addon_1, addon_2, 
                   description, json.dumps(additional_images_list) if additional_images_list else None, notes, is_legacy, item_id))
            conn.commit()
            
            flash("Item updated.")
            return redirect(url_for('inventory.list_items', page=page, sort=sort_by, order=order, q=search_query))
            
        except Exception as e:
            logger.error(f"Error updating inventory item: {e}")
            flash(f"Error updating item: {e}")
            try:
                page = request.form.get('page') or 1
                sort_by = request.form.get('sort') or 'name'
                order = request.form.get('order') or 'asc'
                search_query = request.form.get('q') or ''
            except Exception:
                page = 1
                sort_by = 'name'
                order = 'asc'
                search_query = ''
            return redirect(url_for('inventory.edit_item', item_id=item_id, page=page, sort=sort_by, order=order, q=search_query))
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
        secondary_ids = {}
        try:
            if 'secondary_ids' in item.keys() and item['secondary_ids']:
                secondary_ids = json.loads(item['secondary_ids'])
        except json.JSONDecodeError:
            pass  # Invalid JSON in secondary_ids

        # Parse additional images for template
        additional_images_list = []
        if 'additional_images' in item.keys() and item['additional_images']:
            try:
                additional_images_list = json.loads(item['additional_images'])
            except json.JSONDecodeError:
                pass
        
        # Capture pagination state
        pagination_state = {
            'page': request.args.get('page', 1),
            'sort': request.args.get('sort', 'name'),
            'order': request.args.get('order', 'asc'),
            'q': request.args.get('q', '')
        }
        
        return render_template('inventory/add.html', 
                               item=item, 
                               transactions=transactions,
                               edit_mode=True,
                               categories=CATEGORY_CODES,
                               secondary_ids=secondary_ids,
                               additional_images_list=additional_images_list,
                               pagination_state=pagination_state)
    finally:
        conn.close()


@inventory_bp.route('/delete/<int:item_id>', methods=['POST'])
@login_required
@require_permission('inventory.manage')
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
    
    return redirect(request.referrer or url_for('inventory.list_items'))


@inventory_bp.route('/toggle-legacy/<int:item_id>', methods=['POST'])
@login_required
@require_permission('inventory.manage')
def toggle_legacy(item_id):
    """Toggle the legacy status of an inventory item."""
    conn = get_db_connection()
    try:
        # Get current status
        item = conn.execute('SELECT is_legacy, name FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
        if not item:
            flash("Item not found.")
            return redirect(request.referrer or url_for('inventory.list_items'))
        
        current = item['is_legacy'] or 0
        new_status = 0 if current else 1
        
        conn.execute('UPDATE inventory_items SET is_legacy = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?', 
                     (new_status, item_id))
        conn.commit()
        
        status_text = "marked as Legacy" if new_status else "restored to Active"
        flash(f"'{item['name']}' {status_text}.")
    except Exception as e:
        logger.error(f"Error toggling legacy status: {e}")
        flash(f"Error: {e}")
    finally:
        conn.close()
    
    return redirect(request.referrer or url_for('inventory.list_items'))


@inventory_bp.route('/transaction/delete/<int:tx_id>', methods=['POST'])
@login_required
@require_permission('inventory.manage')
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
@login_required
def adjust_quantity(item_id):
    """Adjust quantity for an inventory item."""
    import threading
    
    quantity_change = int(request.form.get('quantity_change', 0))
    reason = request.form.get('reason', 'Sold/Consumed').strip()
    source_tracking = request.form.get('source_tracking', '').strip() or None
    
    if quantity_change == 0:
        flash("No change specified.")
        return redirect(request.referrer or url_for('inventory.list_items'))
    
    conn = get_db_connection()
    try:
        if quantity_change < 0:
            current = conn.execute('SELECT quantity FROM inventory_items WHERE id = ?', (item_id,)).fetchone()
            if current and (current['quantity'] + quantity_change) < 0:
                flash(f"Cannot reduce by {abs(quantity_change)} - only {current['quantity']} in stock.")
                conn.close()
                return redirect(request.referrer or url_for('inventory.list_items'))
        
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
    
    return redirect(request.referrer or url_for('inventory.list_items'))


@inventory_bp.route('/add_stock/<int:item_id>', methods=['GET', 'POST'])
@login_required
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
@login_required
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
@require_permission('inventory.manage')
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
            from app.services.security import decrypt
            remote_key = decrypt(peer.get('remote_api_key'))
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
        from app.services.security import decrypt
        remote_key = decrypt(peer['remote_api_key'])
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

