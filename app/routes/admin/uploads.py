"""
Admin File Upload Routes
Handles CSV/Excel package imports with column mapping.
Two-step flow: Preview → Confirm. Confirmed rows are upserted straight into
the packages table (see app/services/package_imports.py); there is no
manifest file any more.
"""
import logging
import secrets
import time
import os
from flask import request, redirect, url_for, flash, session, render_template
from flask_login import current_user

from app.routes.admin import admin_bp, require_admin
from app.services import package_imports

logger = logging.getLogger(__name__)

# Temp storage for preview data (file-based for large datasets)
from app.paths import TEMP_DIR as PREVIEW_TEMP_DIR

def detect_column_mapping(headers):
    """Auto-detect column mappings based on column names."""
    mapping = {'tracking': None, 'name': None, 'date': None, 'quantity': None, 'asin': None, 'image': None, 'product_url': None}

    lower_cols = {}
    for c in headers:
        lower_cols.setdefault(c.lower(), c)

    # Tracking detection (Enhanced for eBay)
    tracking_hints = [
        'trackingnumber', 'tracking number', 'tracking_number', 
        'tracking', 'carrier tracking', 'tracking #', 
        'carrier tracking #', 'carrier tracking number'
    ]
    
    for hint in tracking_hints:
        if hint in lower_cols:
            mapping['tracking'] = lower_cols[hint]
            break

    # Name detection
    name_hints = ['item name', 'itemname', 'title', 'item title', 'product name', 'description', 'item description']
    for hint in name_hints:
        if hint in lower_cols:
            mapping['name'] = lower_cols[hint]
            break
    
    # Date detection
    date_hints = ['expected delivery date', 'estimated delivery date', 'expected date', 'promise date', 
                  'purchase date', 'order date', 'orderdate', 'ship date', 'date']
    for hint in date_hints:
        if hint in lower_cols:
            mapping['date'] = lower_cols[hint]
            break
    
    # Quantity detection
    qty_hints = ['item quantity', 'order quantity', 'quantity', 'qty', 'units', 'count', 'amount', 'ordered']
    for hint in qty_hints:
        if hint in lower_cols:
            mapping['quantity'] = lower_cols[hint]
            break
    
    # ASIN detection
    asin_hints = ['asin', 'product asin', 'amazon asin']
    for hint in asin_hints:
        if hint in lower_cols:
            mapping['asin'] = lower_cols[hint]
            break
    
    # Image detection
    image_hints = ['image', 'image url', 'photo', 'photo url', 'picture', 'thumbnail']
    for hint in image_hints:
        if hint in lower_cols:
            mapping['image'] = lower_cols[hint]
            break
    
    # Product URL detection
    url_hints = ['product url', 'item url', 'store url', 'listing url', 'url', 'link', 'product link']
    for hint in url_hints:
        if hint in lower_cols:
            mapping['product_url'] = lower_cols[hint]
            break
            
    return mapping


def generate_warnings(headers, rows, mapping):
    """Generate warnings about data quality issues."""
    warnings = []

    if not mapping['tracking']:
        warnings.append("⚠️ No tracking number column detected - this is required!")
    else:
        # Rows that end up with no tracking (an order ID can stand in for one)
        _, skipped = package_imports.items_from_mapping(headers, rows, mapping)
        if skipped > 0:
            warnings.append(f"{skipped} rows have empty tracking numbers, these items will NOT be imported!")
    
    if not mapping['name']:
        warnings.append("⚠️ No item name column detected - this is required!")
    
    if not mapping['date']:
        warnings.append("No date column detected - all items will show 'Pending'")
    
    return warnings


def _preview_rows(headers, rows, limit=15):
    return [dict(zip(headers, r + [''] * (len(headers) - len(r)))) for r in rows[:limit]]


def run_import(headers, rows, mapping, filename):
    """Upsert the mapped rows into packages and flash a summary."""
    items, skipped = package_imports.items_from_mapping(headers, rows, mapping)
    added, updated = package_imports.import_items(
        items, 'upload', filename, current_user.username)
    unchanged = len(items) - added - updated
    msg = f"✅ Imported {filename}: {added} added, {updated} updated, {unchanged} unchanged"
    if skipped:
        msg += f", {skipped} skipped (no tracking number)"
    flash(msg + ".")
    return added, updated


def process_import_directly(headers, rows, mapping, filename):
    """Process import directly without preview (auto-confirm mode)."""
    try:
        run_import(headers, rows, mapping, filename)
    except Exception as e:
        logger.error(f"Auto-import error: {e}")
        flash(f"Import error: {e}")
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/upload_file', methods=['POST'])
def upload_preview():
    """Step 1: Upload file and show preview with editable column mapping."""
    file = request.files.get('file')
    if not file or file.filename == '':
        flash("No file selected.")
        return redirect(url_for('admin.admin_panel'))
    
    try:
        # Parse file (csv module / openpyxl; every cell becomes a string)
        headers, rows = package_imports.read_table(file.read(), file.filename)

        if not rows:
            flash("File is empty.")
            return redirect(url_for('admin.admin_panel'))

        # Auto-detect mappings
        mapping = detect_column_mapping(headers)

        # Generate warnings
        warnings = generate_warnings(headers, rows, mapping)
        
        # Check for auto-confirm setting
        from app.services.data_manager import load_config
        config = load_config()
        auto_confirm = config.get('AUTO_CONFIRM_IMPORT', False)
        
        # Auto-confirm if: setting enabled AND both tracking AND name columns detected
        if auto_confirm and mapping['tracking'] and mapping['name']:
            # Directly process the import without preview
            return process_import_directly(headers, rows, mapping, file.filename)

        preview_rows = _preview_rows(headers, rows)

        # Save full data to temp file for confirmation step
        if not os.path.exists(PREVIEW_TEMP_DIR):
            os.makedirs(PREVIEW_TEMP_DIR)

        temp_file = os.path.join(PREVIEW_TEMP_DIR, f"preview_{secrets.token_hex(8)}.json")
        package_imports.save_table(temp_file, headers, rows)

        # Store MINIMAL info in session (cookie size limit)
        session['upload_preview'] = {
            'filename': file.filename,
            'total_rows': len(rows),
            'temp_file': temp_file,
            'timestamp': time.time(),
            # Store mapping/warnings in session as they are small and needed for re-render
            'mapping': mapping,
            'warnings': warnings
        }
        
        # Pass FULL data to template
        template_data = session['upload_preview'].copy()
        template_data['original_columns'] = headers
        template_data['preview_rows'] = preview_rows

        return render_template('admin/upload_preview.html', preview=template_data)

    except package_imports.ImportFileError as e:
        flash(str(e))
        return redirect(url_for('admin.admin_panel'))
    except Exception as e:
        logger.error(f"Upload preview error: {e}")
        flash(f"Error reading file: {e}")
        return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/upload_confirm', methods=['POST'])
def upload_confirm():
    """Step 2: Apply the import with user-selected column mappings."""
    preview = session.get('upload_preview')
    if not preview or not os.path.exists(preview.get('temp_file', '')):
        flash("Preview expired. Please upload the file again.")
        return redirect(url_for('admin.admin_panel'))
    
    # Check timeout (10 minutes)
    if time.time() - preview['timestamp'] > 600:
        flash("Preview expired. Please upload the file again.")
        return redirect(url_for('admin.admin_panel'))
    
    try:
        # Load the temp file
        headers, rows = package_imports.load_table(preview['temp_file'])

        # Get user-selected column mappings
        mapping = {
            'tracking': request.form.get('col_tracking', '').strip(),
            'name': request.form.get('col_name', '').strip(),
            'date': request.form.get('col_date', '').strip(),
            'asin': request.form.get('col_asin', '').strip(),
            'image': request.form.get('col_image', '').strip(),
            'product_url': request.form.get('col_product_url', '').strip(),
            'quantity': request.form.get('col_quantity', '').strip(),
        }
        mapping = {k: (v if v in headers else None) for k, v in mapping.items()}

        for key, label in (('tracking', 'Tracking number'), ('name', 'Item name')):
            if not mapping[key]:
                flash(f"{label} column is required.")
                # Re-hydrate template data
                template_data = preview.copy()
                template_data['original_columns'] = headers
                template_data['preview_rows'] = _preview_rows(headers, rows)
                return render_template('admin/upload_preview.html', preview=template_data)

        # Upsert into packages (one transaction)
        run_import(headers, rows, mapping, preview.get('filename'))

        # Cleanup temp file
        try:
            os.remove(preview['temp_file'])
        except OSError:
            pass  # Temp file already removed

        # Clear session
        session.pop('upload_preview', None)

        return redirect(url_for('admin.admin_panel'))

    except Exception as e:
        logger.error(f"Upload confirm error: {e}")
        flash(f"Import error: {e}")
        return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/upload_cancel')
def upload_cancel():
    """Cancel the upload preview and cleanup."""
    preview = session.pop('upload_preview', None)
    if preview and preview.get('temp_file'):
        try:
            os.remove(preview['temp_file'])
        except OSError:
            pass  # Temp file already removed
    flash("Import cancelled.")
    return redirect(url_for('admin.admin_panel'))




