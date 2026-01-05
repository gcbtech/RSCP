"""
Admin File Upload Routes
Handles CSV/Excel manifest file imports with column mapping.
Two-step flow: Preview → Confirm
"""
import logging
import pandas as pd
import json
import time
import os
from flask import request, redirect, url_for, flash, session, render_template

from app.routes.admin import admin_bp, require_admin
from app.services.data_manager import sync_manifest, MANIFEST_FILE, BASE_DIR

logger = logging.getLogger(__name__)

# Temp storage for preview data (file-based for large datasets)
PREVIEW_TEMP_DIR = os.path.join(BASE_DIR, 'temp')


def detect_column_mapping(df):
    """Auto-detect column mappings based on column names."""
    mapping = {'tracking': None, 'name': None, 'date': None, 'quantity': None, 'asin': None, 'image': None, 'product_url': None}
    
    lower_cols = {c.lower(): c for c in df.columns}
    
    # Tracking detection
    tracking_hints = ['tracking number', 'tracking_number', 'tracking', 'carrier tracking', 'tracking #', 'carrier tracking #', 'carrier tracking number']
    for hint in tracking_hints:
        if hint in lower_cols:
            mapping['tracking'] = lower_cols[hint]
            break
    
    # Name detection
    name_hints = ['item name', 'title', 'item title', 'product name', 'description', 'item description']
    for hint in name_hints:
        if hint in lower_cols:
            mapping['name'] = lower_cols[hint]
            break
    
    # Date detection (prioritize expected dates)
    date_hints = ['expected delivery date', 'estimated delivery date', 'expected date', 'promise date', 
                  'purchase date', 'order date', 'ship date', 'date']
    for hint in date_hints:
        if hint in lower_cols:
            mapping['date'] = lower_cols[hint]
            break
    
    # Quantity detection
    qty_hints = ['order quantity', 'quantity', 'qty', 'units', 'count', 'amount', 'ordered']
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


def generate_warnings(df, mapping):
    """Generate warnings about data quality issues."""
    warnings = []
    
    if not mapping['tracking']:
        warnings.append("⚠️ No tracking number column detected - this is required!")
    else:
        # Check for empty tracking numbers
        track_col = mapping['tracking']
        if track_col in df.columns:
            # Convert to string first to handle numeric columns
            col_as_str = df[track_col].astype(str)
            empty_count = df[track_col].isna().sum() + (col_as_str.isin(['', 'nan', 'None'])).sum()
            if empty_count > 0:
                warnings.append(f"{empty_count} rows have empty tracking numbers, these items will NOT be imported!")
    
    if not mapping['name']:
        warnings.append("⚠️ No item name column detected - this is required!")
    
    if not mapping['date']:
        warnings.append("No date column detected - all items will show 'Pending'")
    
    return warnings


def process_import_directly(df, mapping, filename):
    """Process import directly without preview (auto-confirm mode)."""
    try:
        # Build rename map from detected mapping
        rename_map = {}
        if mapping.get('tracking'):
            rename_map[mapping['tracking']] = 'TrackingNumber'
        if mapping.get('name'):
            rename_map[mapping['name']] = 'ItemName'
        if mapping.get('date'):
            rename_map[mapping['date']] = 'Date'
        if mapping.get('quantity'):
            rename_map[mapping['quantity']] = 'Quantity'
        if mapping.get('image'):
            rename_map[mapping['image']] = 'Image'
        if mapping.get('product_url'):
            rename_map[mapping['product_url']] = 'ProductURL'
        
        df.rename(columns=rename_map, inplace=True)
        
        # Generate image URLs from ASIN if present
        if mapping.get('asin') and mapping['asin'] in df.columns:
            if 'Image' not in df.columns:
                df['Image'] = None
            
            for index, row in df.iterrows():
                asin = str(row.get(mapping['asin'], '')).strip().upper()
                if asin and asin.lower() not in ['nan', 'none', ''] and len(asin) >= 10:
                    current_img = str(row.get('Image', ''))
                    if pd.isna(row.get('Image')) or current_img.lower() in ['nan', 'none'] or not current_img.strip():
                        df.at[index, 'Image'] = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SX200_.jpg"
        
        # Save to manifest
        df.to_csv(MANIFEST_FILE, index=False)
        
        # Sync to database
        sync_manifest()
        
        flash(f"✅ Auto-imported {len(df)} packages from {filename}")
        return redirect(url_for('admin.admin_panel'))
        
    except Exception as e:
        logger.error(f"Auto-import error: {e}")
        flash(f"Import error: {e}")
        return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/upload_file', methods=['POST'])
def upload_preview():
    """Step 1: Upload file and show preview with editable column mapping."""
    error = require_admin()
    if error:
        return error
    
    file = request.files.get('file')
    if not file or file.filename == '':
        flash("No file selected.")
        return redirect(url_for('admin.admin_panel'))
    
    upload_mode = request.form.get('upload_mode', 'append')
    
    try:
        # Parse file
        if file.filename.endswith('.xlsx'):
            df = pd.read_excel(file)
        else:
            try:
                df = pd.read_csv(file, encoding='utf-8')
            except UnicodeDecodeError:
                file.seek(0)
                df = pd.read_csv(file, encoding='cp1252')
        
        if df.empty:
            flash("File is empty.")
            return redirect(url_for('admin.admin_panel'))
        
        # Auto-detect mappings
        mapping = detect_column_mapping(df)
        
        # Generate warnings
        warnings = generate_warnings(df, mapping)
        
        # Check for auto-confirm setting
        from app.services.data_manager import load_config
        config = load_config()
        auto_confirm = config.get('AUTO_CONFIRM_IMPORT', False)
        
        # Auto-confirm if: setting enabled AND both tracking AND name columns detected
        if auto_confirm and mapping['tracking'] and mapping['name']:
            # Directly process the import without preview
            return process_import_directly(df, mapping, file.filename)
        
        # Prepare preview data - convert all values to strings to avoid JSON serialization issues
        preview_df = df.head(15).fillna('')
        for col in preview_df.columns:
            preview_df[col] = preview_df[col].astype(str)
        preview_rows = preview_df.to_dict('records')
        
        # Save full data to temp file for confirmation step
        if not os.path.exists(PREVIEW_TEMP_DIR):
            os.makedirs(PREVIEW_TEMP_DIR)
        
        temp_file = os.path.join(PREVIEW_TEMP_DIR, f"preview_{int(time.time())}.csv")
        df.to_csv(temp_file, index=False)
        
        # Store MINIMAL info in session (cookie size limit)
        session['upload_preview'] = {
            'filename': file.filename,
            'total_rows': len(df),
            'temp_file': temp_file,
            'mode': upload_mode,
            'timestamp': time.time(),
            # Store mapping/warnings in session as they are small and needed for re-render
            'mapping': mapping,
            'warnings': warnings
        }
        
        # Pass FULL data to template
        template_data = session['upload_preview'].copy()
        template_data['original_columns'] = list(df.columns)
        template_data['preview_rows'] = preview_rows
        
        return render_template('admin/upload_preview.html', preview=template_data)
        
    except Exception as e:
        logger.error(f"Upload preview error: {e}")
        flash(f"Error reading file: {e}")
        return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/upload_confirm', methods=['POST'])
def upload_confirm():
    """Step 2: Apply the import with user-selected column mappings."""
    error = require_admin()
    if error:
        return error
    
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
        df = pd.read_csv(preview['temp_file'])
        
        # Get user-selected column mappings
        col_tracking = request.form.get('col_tracking', '').strip()
        col_name = request.form.get('col_name', '').strip()
        col_date = request.form.get('col_date', '').strip()
        col_asin = request.form.get('col_asin', '').strip()
        col_image = request.form.get('col_image', '').strip()
        col_product_url = request.form.get('col_product_url', '').strip()
        col_quantity = request.form.get('col_quantity', '').strip()
        
        if not col_tracking or col_tracking not in df.columns:
            flash("Tracking number column is required.")
            # Re-hydrate template data
            template_data = preview.copy()
            template_data['original_columns'] = list(df.columns)
            preview_df = df.head(15).fillna('')
            for col in preview_df.columns:
                preview_df[col] = preview_df[col].astype(str)
            template_data['preview_rows'] = preview_df.to_dict('records')
            
            return render_template('admin/upload_preview.html', preview=template_data)
        
        if not col_name or col_name not in df.columns:
            flash("Item name column is required.")
            # Re-hydrate template data
            template_data = preview.copy()
            template_data['original_columns'] = list(df.columns)
            preview_df = df.head(15).fillna('')
            for col in preview_df.columns:
                preview_df[col] = preview_df[col].astype(str)
            template_data['preview_rows'] = preview_df.to_dict('records')
            
            return render_template('admin/upload_preview.html', preview=template_data)
        
        # Rename columns to standard names
        rename_map = {}
        if col_tracking:
            rename_map[col_tracking] = 'TrackingNumber'
        if col_name:
            rename_map[col_name] = 'ItemName'
        if col_date:
            rename_map[col_date] = 'Date'
        if col_quantity:
            rename_map[col_quantity] = 'Quantity'
        if col_image:
            rename_map[col_image] = 'Image'
        if col_product_url:
            rename_map[col_product_url] = 'ProductURL'
        # Note: ASIN doesn't need renaming, we use it directly for image generation
        
        df.rename(columns=rename_map, inplace=True)
        
        # Generate image URLs from ASIN if user selected an ASIN column
        if col_asin and col_asin in df.columns:
            if 'Image' not in df.columns:
                df['Image'] = None
            
            for index, row in df.iterrows():
                asin = str(row.get(col_asin, '')).strip().upper()
                if asin and asin.lower() not in ['nan', 'none', ''] and len(asin) >= 10:
                    current_img = str(row.get('Image', ''))
                    if pd.isna(row.get('Image')) or current_img.lower() in ['nan', 'none'] or not current_img.strip():
                        df.at[index, 'Image'] = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SX200_.jpg"
        
        # Save to manifest
        upload_mode = preview.get('mode', 'replace')
        file_exists = os.path.exists(MANIFEST_FILE)
        
        mode = 'a' if (upload_mode == 'append' and file_exists) else 'w'
        write_header = not (upload_mode == 'append' and file_exists)
        
        df.to_csv(MANIFEST_FILE, mode=mode, header=write_header, index=False)
        
        # Sync to database
        sync_manifest()
        
        # Cleanup temp file
        try:
            os.remove(preview['temp_file'])
        except OSError:
            pass  # Temp file already removed
        
        # Clear session
        session.pop('upload_preview', None)
        
        flash(f"✅ Successfully imported {len(df)} packages.")
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




