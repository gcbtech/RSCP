"""
Inventory Items Module
CRUD routes for inventory items. Business rules live in
app.services.inventory (items, stock, ledger) and app.services.item_images.
"""
import base64
import json
import logging
import sqlite3

from flask import request, redirect, url_for, flash, render_template, jsonify
from flask_login import current_user
from app.utils.permissions import require_permission

from app.routes.inventory import inventory_bp, CATEGORY_CODES
from app.routes.inventory.core import generate_sku, validate_location
from app.services import inventory, item_images, money
from app.services.db import get_db_connection
from app.services.data_manager import load_config
# Re-exported: other modules (system_migrations) and tests import these here.
from app.services.item_images import (  # noqa: F401
    MAX_IMAGE_SIZE, JPEG_QUALITY, THUMB_SIZE,
    optimize_image, get_thumbnail_url, generate_thumbnail,
)

logger = logging.getLogger(__name__)

QUICK_ADD_DEFAULT_AREA = 'Retail'  # Pre-selected Quick Add area (used only if it's a configured area)


@inventory_bp.route('/low-stock')
def low_stock_report():
    """Show report of items with low stock or out of stock."""
    conn = get_db_connection()
    try:
        return render_template('inventory/low_stock.html', items=inventory.low_stock_items(conn))
    finally:
        conn.close()


def _bulk_edit_changes(form):
    """Bulk edit form -> {column: value} for the fields that were filled in
    (invalid numbers are ignored, as before)."""
    changes = {}
    for col in ('location_area', 'location_aisle', 'location_shelf', 'location_bin'):
        val = form.get(col, '').strip()
        if val:
            changes[col] = val
    for col in ('buy_price', 'sell_price'):
        val = form.get(col, '').strip()
        if val:
            cents = money.parse(val)
            if cents is not None:
                changes[f'{col}_cents'] = cents
    keywords = form.get('keywords', '').strip()
    if keywords:
        changes['keywords'] = keywords
    threshold = form.get('alert_threshold', '').strip()
    if threshold:
        try:
            changes['alert_threshold'] = int(threshold)
        except ValueError:
            pass
    for col in ('addon_1', 'addon_2'):
        val = form.get(col, '').strip()
        if val in ('0', '1'):
            changes[col] = int(val)
    return changes


@inventory_bp.route('/bulk-edit', methods=['POST'])
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
            changes = _bulk_edit_changes(request.form)
            if not changes:
                flash("No changes specified.")
                return redirect(url_for('inventory.list_items'))
            count = inventory.bulk_update_items(conn, item_ids, changes)
            flash(f"Successfully updated {count} items.")
            return redirect(url_for('inventory.list_items'))

        # Rendering Phase (Selection Confirmation)
        placeholders = ','.join('?' * len(item_ids))
        items = conn.execute(f'SELECT id, sku, name FROM inventory_items WHERE id IN ({placeholders})', item_ids).fetchall()
        return render_template('inventory/bulk_edit.html', items=items, item_ids=item_ids, config=load_config())

    except Exception as e:
        logger.error(f"Bulk edit error: {e}")
        flash(f"Error during bulk edit: {e}")
        return redirect(url_for('inventory.list_items'))
    finally:
        conn.close()


@inventory_bp.route('/items')
def list_items():
    """List all inventory items with sorting, pagination, and search support."""
    search_query = request.args.get('q', '').strip()
    show_legacy = request.args.get('show_legacy', '0') == '1'
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 100))

    conn = get_db_connection()
    try:
        items, total, sort_by, order = inventory.list_items(
            conn, sort_by=request.args.get('sort', 'name'), order=request.args.get('order'),
            search=search_query, show_legacy=show_legacy, page=page, per_page=per_page)
    finally:
        conn.close()

    total_pages = (total + per_page - 1) // per_page if total > 0 else 1

    # Attach pre-generated thumbnails (path check only; generation happens at upload time)
    items_with_thumbs = []
    for item in items:
        item_dict = dict(item)
        item_dict['thumbnail_url'] = get_thumbnail_url(item_dict['image_url']) if item_dict.get('image_url') else None
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
                           show_legacy=show_legacy,
                           quick_add_default_area=QUICK_ADD_DEFAULT_AREA)


def _item_form(form):
    """Fields shared by the add and edit forms, as entered (strings)."""
    return {
        'sku': form.get('sku', '').strip(),
        'name': form.get('name', '').strip(),
        'location_area': form.get('location_area', '').strip(),
        'location_aisle': form.get('location_aisle', '').strip(),
        'location_shelf': form.get('location_shelf', '').strip(),
        'location_bin': form.get('location_bin', '').strip(),
        'asin': form.get('asin', '').strip() or None,
        'buy_price': form.get('buy_price', '').strip(),
        'sell_price': form.get('sell_price', '').strip(),
        'supplier': form.get('supplier', '').strip() or None,
        'first_stock_date': form.get('first_stock_date', '').strip() or None,
        'resupply_interval': form.get('resupply_interval', '').strip(),
        'keywords': form.get('keywords', '').strip(),
        'notes': form.get('notes', '').strip(),
        'description': form.get('description', '').strip() or None,
        'source_url': form.get('source_url', '').strip() or None,
        'upc': form.get('upc', '').strip(),
        'part_number': form.get('part_number', '').strip(),
        'addon_1': form.get('addon_1') == 'on',
        'addon_2': form.get('addon_2') == 'on',
    }


def _convert_numbers(f, form):
    """Price / interval / threshold strings -> numbers (ValueError on junk).
    Prices become buy_price_cents / sell_price_cents."""
    for col in ('buy_price', 'sell_price'):
        raw = f[col]
        cents = money.parse(raw) if raw else None
        if raw and cents is None:
            raise ValueError(f'invalid price {raw!r}')
        f[f'{col}_cents'] = cents
    f['resupply_interval'] = int(f['resupply_interval']) if f['resupply_interval'] else None
    f['alert_threshold'] = int(form.get('alert_threshold', 0) or 0)
    return f


def _add_prefill(f, category, quantity, with_sku=True):
    """Re-render data for the add form after a failed submit."""
    prefill = {
        'name': f['name'],
        'asin': f['asin'] or '',
        'quantity': quantity,
        'image_url': request.form.get('image_url', ''),
        'source_url': request.form.get('source_url', ''),
        'location_area': f['location_area'],
        'location_aisle': f['location_aisle'],
        'location_shelf': f['location_shelf'],
        'location_bin': f['location_bin'],
        'buy_price': f['buy_price'],
        'sell_price': f['sell_price'],
        'supplier': f['supplier'],
        'first_stock_date': f['first_stock_date'],
        'resupply_interval': f['resupply_interval'],
        'keywords': f['keywords'],
        'alert_enabled': request.form.get('alert_enabled') == 'on',
        'alert_threshold': request.form.get('alert_threshold', 0),
        'tracking': request.form.get('source_tracking'),
        'description': f['description'],
        'notes': f['notes'],
    }
    if with_sku:
        prefill = {'sku': f['sku'], 'category': category, **prefill}
    return prefill


@inventory_bp.route('/add', methods=['GET', 'POST'])
@require_permission('inventory.manage')
def add_item():
    """Add a new inventory item."""
    if request.method == 'POST':
        f = _item_form(request.form)
        category = request.form.get('category', 'ATO').strip()
        quantity = int(request.form.get('quantity', 0))
        secondary_ids = {'upc': request.form.get('upc'), 'part_number': request.form.get('part_number')}

        if not f['name']:
            flash("Name is required.")
            return render_template('inventory/add.html', prefill=_add_prefill(f, category, quantity),
                                   secondary_ids=secondary_ids, categories=CATEGORY_CODES)

        if request.form.get('quick_add'):
            # Default location for Quick Add if not specified
            if not any([f['location_area'], f['location_aisle'], f['location_shelf'], f['location_bin']]):
                area_options = (load_config() or {}).get('LOCATION_OPTIONS', {}).get('area', [])
                f['location_area'] = QUICK_ADD_DEFAULT_AREA if QUICK_ADD_DEFAULT_AREA in area_options else 'General'

        if not validate_location(f['location_area'], f['location_aisle'], f['location_shelf'], f['location_bin']):
            flash("At least one location field is required.")
            return render_template('inventory/add.html', prefill=_add_prefill(f, category, quantity),
                                   secondary_ids=secondary_ids, categories=CATEGORY_CODES)

        if not f['sku']:
            f['sku'] = generate_sku(category)
        sku = f['sku']

        # TRACK_FIRST_STOCK_DATE: an empty first-stock date means today
        if not f['first_stock_date'] and load_config().get('TRACK_FIRST_STOCK_DATE'):
            from datetime import date
            f['first_stock_date'] = date.today().isoformat()
        try:
            _convert_numbers(f, request.form)
        except (TypeError, ValueError):
            flash("Error adding item: prices, interval and alert threshold must be numbers.")
            return redirect(url_for('inventory.add_item'))
        f['quantity'] = quantity

        conn = get_db_connection()
        try:
            f['image_url'] = (item_images.save_cover_upload(request.files.get('image'), sku)
                              or request.form.get('image_url', '').strip() or None)
            f['additional_images'] = item_images.save_gallery_uploads(
                request.files.getlist('additional_images'), sku)
            inventory.create_item(conn, f, current_user.username)

            flash(f"Item added: {sku}")

            next_url = request.args.get('next') or request.form.get('next')
            if next_url:
                if next_url == 'scan':
                    return redirect(url_for('main.scan_page', mode='receiving'))
                return redirect(next_url)

            if load_config().get('INVENTORY_AUTO_COPY_SKU'):
                return redirect(url_for('inventory.list_items', new_sku=sku))
            return redirect(url_for('inventory.list_items'))

        except inventory.DuplicateSku:
            flash(f"Error: SKU '{sku}' already exists. Please use a different SKU or let the system generate one.", "danger")
            return render_template('inventory/add.html', prefill=_add_prefill(f, category, quantity, with_sku=False),
                                   categories=CATEGORY_CODES)
        except sqlite3.IntegrityError as e:
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
    return render_template('inventory/add.html', prefill=prefill, categories=CATEGORY_CODES)


@inventory_bp.route('/edit/<int:item_id>', methods=['GET', 'POST'])
@require_permission('inventory.manage')
def edit_item(item_id):
    """Edit an existing inventory item."""
    conn = get_db_connection()

    if request.method == 'POST':
        try:
            f = _item_form(request.form)

            # Pagination state
            page = request.form.get('page')
            page = int(page) if page and page.isdigit() else 1
            sort_by = request.form.get('sort', 'name')
            order = request.form.get('order', 'asc')
            search_query = request.form.get('q', '')
            back = url_for('inventory.edit_item', item_id=item_id, page=page, sort=sort_by, order=order, q=search_query)

            if not f['sku']:
                flash("SKU is required.")
                return redirect(back)
            if not f['name']:
                flash("Name is required.")
                return redirect(back)
            if not validate_location(f['location_area'], f['location_aisle'], f['location_shelf'], f['location_bin']):
                flash("At least one location field is required.")
                return redirect(back)
            if inventory.sku_in_use(conn, f['sku'], exclude_id=item_id):
                flash("SKU already exists for another item.")
                return redirect(back)

            _convert_numbers(f, request.form)
            f['is_legacy'] = request.form.get('is_legacy') == 'on'

            current = inventory.get_item(conn, item_id)
            cover, gallery = item_images.apply_gallery_edits(
                current['image_url'] if current else None,
                item_images.parse_gallery(current['additional_images'] if current else None),
                delete_images=request.form.getlist('delete_images'),
                form_image_url=request.form.get('image_url', '').strip(),
                swap_cover=request.form.get('swap_cover', '').strip())
            # A newly uploaded cover file wins over a fetched URL and a swap
            cover = item_images.save_cover_upload(request.files.get('image'), f['sku']) or cover
            gallery += item_images.save_gallery_uploads(request.files.getlist('additional_images'), f['sku'])
            f['image_url'], f['additional_images'] = cover, gallery

            inventory.update_item(conn, item_id, f)

            flash("Item updated.")
            return redirect(url_for('inventory.list_items', page=page, sort=sort_by, order=order, q=search_query))

        except Exception as e:
            logger.error(f"Error updating inventory item: {e}")
            flash(f"Error updating item: {e}")
            page = request.form.get('page') or 1
            sort_by = request.form.get('sort') or 'name'
            order = request.form.get('order') or 'asc'
            search_query = request.form.get('q') or ''
            return redirect(url_for('inventory.edit_item', item_id=item_id, page=page, sort=sort_by, order=order, q=search_query))
        finally:
            conn.close()

    # GET - Load item for editing
    try:
        item = inventory.get_item(conn, item_id)
        if not item:
            flash("Item not found.")
            return redirect(url_for('inventory.list_items'))

        transactions = inventory.item_transactions(conn, item_id)

        secondary_ids = {}
        try:
            if 'secondary_ids' in item.keys() and item['secondary_ids']:
                secondary_ids = json.loads(item['secondary_ids'])
        except json.JSONDecodeError:
            pass  # Invalid JSON in secondary_ids

        additional_images_list = item_images.parse_gallery(
            item['additional_images'] if 'additional_images' in item.keys() else None)

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
@require_permission('inventory.manage')
def delete_item(item_id):
    """Delete an inventory item."""
    conn = get_db_connection()
    try:
        inventory.delete_item(conn, item_id)
        flash("Item deleted.")
    except Exception as e:
        logger.error(f"Error deleting inventory item: {e}")
        flash(f"Error: {e}")
    finally:
        conn.close()

    return redirect(request.referrer or url_for('inventory.list_items'))


@inventory_bp.route('/toggle-legacy/<int:item_id>', methods=['POST'])
@require_permission('inventory.manage')
def toggle_legacy(item_id):
    """Toggle the legacy status of an inventory item."""
    conn = get_db_connection()
    try:
        name, new_status = inventory.toggle_legacy(conn, item_id)
        status_text = "marked as Legacy" if new_status else "restored to Active"
        flash(f"'{name}' {status_text}.")
    except inventory.ItemNotFound:
        flash("Item not found.")
    except Exception as e:
        logger.error(f"Error toggling legacy status: {e}")
        flash(f"Error: {e}")
    finally:
        conn.close()

    return redirect(request.referrer or url_for('inventory.list_items'))


@inventory_bp.route('/transaction/delete/<int:tx_id>', methods=['POST'])
@require_permission('inventory.manage')
def delete_transaction(tx_id):
    """Delete a transaction and revert the stock change."""
    item_id = None
    conn = get_db_connection()
    try:
        item_id = inventory.revert_transaction(conn, tx_id)
        flash("Transaction reverted.")
    except inventory.TransactionNotFound:
        flash("Transaction not found.")
    except Exception as e:
        logger.error(f"Error deleting transaction: {e}")
        flash("Error reverting transaction.")
    finally:
        conn.close()

    if item_id is None:
        return redirect(request.referrer or url_for('inventory.list_items'))
    return redirect(request.referrer or url_for('inventory.edit_item', item_id=item_id))


@inventory_bp.route('/adjust/<int:item_id>', methods=['POST'])
def adjust_quantity(item_id):
    """Adjust quantity for an inventory item."""
    quantity_change = int(request.form.get('quantity_change', 0))
    reason = request.form.get('reason', 'Sold/Consumed').strip()
    source_tracking = request.form.get('source_tracking', '').strip() or None

    if quantity_change == 0:
        flash("No change specified.")
        return redirect(request.referrer or url_for('inventory.list_items'))

    conn = get_db_connection()
    try:
        inventory.adjust_stock(conn, item_id, quantity_change, reason, current_user.username, source_tracking)
        if quantity_change < 0:
            inventory.notify_if_low_stock(conn, item_id, load_config())
        action = "Added" if quantity_change > 0 else "Removed"
        flash(f"{action} {abs(quantity_change)} units.")
    except inventory.InsufficientStock as e:
        flash(f"Cannot reduce by {abs(e.requested)} - only {e.available} in stock.")
    except inventory.ItemNotFound:
        flash("Item not found.")
    except Exception as e:
        logger.error(f"Error adjusting quantity: {e}")
        flash(f"Error: {e}")
    finally:
        conn.close()

    return redirect(request.referrer or url_for('inventory.list_items'))


@inventory_bp.route('/add_stock/<int:item_id>', methods=['GET', 'POST'])
def add_stock(item_id):
    """Confirmation page for adding stock to existing inventory item from scan."""
    conn = get_db_connection()
    try:
        item = inventory.get_item(conn, item_id)
        if not item:
            flash("Item not found.")
            return redirect(url_for('main.scan_page', mode='receive'))

        if request.method == 'POST':
            qty_to_add = int(request.form.get('qty', 1))
            tracking = request.form.get('tracking', '')
            if qty_to_add < 1:
                flash("Quantity to add must be at least 1.")
                return redirect(url_for('inventory.add_stock', item_id=item_id))
            inventory.adjust_stock(conn, item_id, qty_to_add, inventory.REASON_RECEIVED_SCAN,
                                   current_user.username, tracking)
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
@require_permission('inventory.manage')
def upload_photo():
    """Upload photo from camera."""
    data = request.json
    if not data or 'image' not in data:
        return jsonify({'error': 'No image data'}), 400

    try:
        # Image data comes as "data:image/jpeg;base64,..."
        image_data = data['image'].split(',')[1] if ',' in data['image'] else data['image']
        photo_url = item_images.save_camera_photo(base64.b64decode(image_data))
        return jsonify({'url': photo_url})
    except Exception as e:
        logger.error(f"Photo upload failed: {e}")
        return jsonify({'error': str(e)}), 500


@inventory_bp.route('/export_csv')
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
            money.to_dollars(item['buy_price_cents']),
            money.to_dollars(item['sell_price_cents']),
            item['supplier']
        ])
        
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=inventory_export.csv"
    output.headers["Content-type"] = "text/csv"
    return output
