"""
Admin Packages Routes
Handles package CRUD operations and the main admin panel view.
"""
import os
import datetime
import logging
import pandas as pd
from flask import render_template, request, redirect, url_for, session, flash, jsonify, has_app_context
from flask_login import current_user

from app.routes.admin import admin_bp, require_admin, save_config_value
from app.services.db import get_db_connection, get_request_db
from app.services.auth import load_users
from app.services.data_manager import load_config, sync_manifest, get_analytics_stats, MANIFEST_FILE
from app.services.file_handler import atomic_write

logger = logging.getLogger(__name__)


@admin_bp.route('/')
def admin_panel():
    """Main admin panel view with package listing."""
    if not current_user.is_authenticated:
        return redirect(url_for('auth.login', next=request.url))
    if not current_user.is_admin:
        flash("Administrator access required. Please login with an Admin account.")
        return redirect(url_for('auth.login', next=request.url, force=True))
        
    # Sync Manifest first to ensure fresh data
    sync_manifest()
    
    users = load_users()
    
    # Sorting / Pagination parameters
    sort_by = request.args.get('sort_by', 'recent')
    order = request.args.get('order', 'desc')
    page = int(request.args.get('page', 1))
    per_page = 50
    offset = (page - 1) * per_page
    
    # Search query (form uses 'search', not 'q')
    search_query = request.args.get('search', '').strip()
    
    # Map sort keys to SQL columns
    sort_map = {
        'date': 'date_expected',
        'status': 'status',
        'name': 'item_name',
        'recent': 'id'
    }
    sql_sort = sort_map.get(sort_by, 'id')
    # Security: Validate column is in allowed whitelist to prevent SQL injection
    ALLOWED_SORT_COLUMNS = {'date_expected', 'status', 'item_name', 'id'}
    if sql_sort not in ALLOWED_SORT_COLUMNS:
        sql_sort = 'id'
    sql_order = 'DESC' if order == 'desc' else 'ASC'
    
    graph_data = get_analytics_stats(14)
    
    close_conn = False
    if has_app_context():
        conn = get_request_db()
    else:
        conn = get_db_connection()
        close_conn = True
    try:
        # Query
        base_query = "SELECT * FROM packages"
        count_query = "SELECT count(*) as c FROM packages"
        params = []
        
        if search_query:
            # Add search filter
            # Logic: Tracking LIKE %q% OR item_name LIKE %q% OR status LIKE %q%
            where_clause = " WHERE tracking_number LIKE ? OR item_name LIKE ? OR status LIKE ?"
            base_query += where_clause
            count_query += where_clause
            s_param = f"%{search_query}%"
            params = [s_param, s_param, s_param]
            
        # Total Count (with filter)
        total = conn.execute(count_query, params).fetchone()['c']
        total_pages = (total + per_page - 1) // per_page
        
        # Final Query with Sort/Limit
        # Note: params currently has 3 search terms if search is active
        # We need to add limit/offset to params
        # For date sorting, push 'received' status to the bottom so unreceived packages show first
        if sql_sort == 'date_expected':
            full_query = f"{base_query} ORDER BY (CASE WHEN status = 'received' THEN 1 ELSE 0 END) ASC, {sql_sort} {sql_order} LIMIT ? OFFSET ?"
        else:
            full_query = f"{base_query} ORDER BY {sql_sort} {sql_order} LIMIT ? OFFSET ?"
        params.extend([per_page, offset])
        
        rows = conn.execute(full_query, params).fetchall()
        
        # Convert to dict list for template
        packages = []
        for r in rows:
            d = dict(r)
            # Map DB columns to Template expectations (Legacy Compat)
            d['qty'] = d['quantity']
            d['date'] = d['date_expected']
            d['image'] = d['image_url']
            d['name'] = d['item_name']
            
            # Helper for 'status' display
            if d['date_scanned']:
                d['scanned'] = True
            else:
                d['scanned'] = False
            
            packages.append(d)
            
    finally:
        if close_conn:
            conn.close()

    if request.args.get('partial'):
        return render_template('_admin_table_rows.html', 
                               packages=packages, 
                               date_format=load_config().get('DATE_FORMAT', 'US'))

    config = load_config()
    # search_query is already defined above
    
    # Load POS settings from database for POS tab
    pos_settings = {}
    try:
        from app.routes.pos.core import get_pos_setting
        pos_settings = {
            'TAX_RATE': float(get_pos_setting('TAX_RATE', 0)),
            'CASH_DISCOUNT_ENABLED': get_pos_setting('CASH_DISCOUNT_ENABLED', 'false'),
            'CASH_DISCOUNT_TYPE': get_pos_setting('CASH_DISCOUNT_TYPE', 'percent'),
            'CASH_DISCOUNT_AMOUNT': float(get_pos_setting('CASH_DISCOUNT_AMOUNT', 0) or 0),
            'RECEIPT_STORE_NAME': get_pos_setting('RECEIPT_STORE_NAME', ''),
            'RECEIPT_HEADER': get_pos_setting('RECEIPT_HEADER', ''),
            'RECEIPT_FOOTER': get_pos_setting('RECEIPT_FOOTER', ''),
            'POS_EMAIL_HOST': get_pos_setting('POS_EMAIL_HOST', ''),
            'POS_EMAIL_PORT': get_pos_setting('POS_EMAIL_PORT', '587'),
            'POS_EMAIL_USER': get_pos_setting('POS_EMAIL_USER', ''),
            'POS_EMAIL_RECIPIENTS': get_pos_setting('POS_EMAIL_RECIPIENTS', ''),
        }
    except Exception as e:
        logger.warning(f"Error loading POS settings for admin panel: {e}")
    
    return render_template('admin.html', 
                           packages=packages, 
                           users=users,
                           current_page=page,
                           total_pages=total_pages,
                           sort_by=sort_by,
                           order=order,
                           search_query=search_query,
                           date_format=config.get('DATE_FORMAT', 'US') if config else 'US',
                           graph_data=graph_data,
                           config=config,
                           pos_settings=pos_settings,
                           inventory_enabled=config.get('INVENTORY_ENABLED', False) if config else False)


@admin_bp.route('/add_manual_item', methods=['POST'])
def add_manual_item():
    """Add a package manually."""
    error = require_admin()
    if error:
        return error
    
    tracking = request.form.get('tracking', '').strip()
    name = request.form.get('name', '').strip()
    sku = request.form.get('sku', '').strip() or None
    date_input = request.form.get('date', '')
    is_priority = request.form.get('priority') == 'on' 
    
    if tracking and name:
        close_conn = False
        if has_app_context():
            conn = get_request_db()
        else:
            conn = get_db_connection()
            close_conn = True
        try:
            # Date Status Logic
            # "Pending" is removed. Default is 'on_time' until it is past due.
            status = 'on_time' 
            today = datetime.date.today()
            if date_input:
                try:
                    dt_obj = datetime.datetime.strptime(date_input, '%Y-%m-%d').date()
                    if dt_obj == today: status = 'expected'
                    elif dt_obj < today: status = 'past_due'
                except ValueError:
                    pass  # Date parsing failed
            
            conn.execute('''
                INSERT INTO packages (tracking_number, item_name, date_expected, quantity, status, source, priority, manual_date, sku)
                VALUES (?, ?, ?, 1, ?, 'manual', ?, ?, ?)
            ''', (tracking, name, date_input, status, 1 if is_priority else 0, date_input, sku))
            conn.commit()
            
            # In-app notification for new package
            try:
                from app.services.data_manager import load_config
                from app.routes.notifications import create_notification
                conf = load_config() or {}
                
                should_notify = (is_priority and conf.get('NOTIFY_PRIORITY_PACKAGES', False)) or \
                                (not is_priority and conf.get('NOTIFY_NORMAL_PACKAGES', False))
                
                if should_notify:
                    priority_label = "🔴 PRIORITY" if is_priority else "📦"
                    create_notification(
                        user_id=None,
                        title=f"{priority_label} Package Added: {name}",
                        message=f"Tracking: {tracking}",
                        notification_type='warning' if is_priority else 'info',
                        link="/admin#packages"
                    )
            except Exception as e:
                logger.error(f"Package notification error: {e}")
            
            flash(f"Added {name}")
        except Exception as e:
            flash(f"Error adding item: {e}")
        finally:
            if close_conn:
                conn.close()
            
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/set_sku/<int:package_id>', methods=['POST'])
def set_package_sku(package_id):
    """Update package SKU."""
    error = require_admin()
    if error:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        
    sku = request.form.get('sku', '').strip() or None
    
    close_conn = False
    if has_app_context():
        conn = get_request_db()
    else:
        conn = get_db_connection()
        close_conn = True
    try:
        conn.execute("UPDATE packages SET sku = ? WHERE id = ?", (sku, package_id))
        conn.commit()
        return {'success': True}
    except Exception as e:
        logger.error(f"Error setting SKU: {e}")
        return {'success': False, 'error': str(e)}, 500
    finally:
        if close_conn:
            conn.close()





@admin_bp.route('/set_quantity/<int:package_id>', methods=['POST'])
def set_package_quantity(package_id):
    """Update package Quantity."""
    error = require_admin()
    if error:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        
    try:
        qty = int(request.form.get('quantity', '1'))
        if qty < 0: raise ValueError("Negative quantity")
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid Quantity'}), 400
    
    close_conn = False
    if has_app_context():
        conn = get_request_db()
    else:
        conn = get_db_connection()
        close_conn = True
    try:
        conn.execute("UPDATE packages SET quantity = ? WHERE id = ?", (qty, package_id))
        conn.commit()
        return {'success': True}
    except Exception as e:
        logger.error(f"Error setting Quantity: {e}")
        return {'success': False, 'error': str(e)}, 500
    finally:
        if close_conn:
            conn.close()


@admin_bp.route('/delete_package/<int:package_id>', methods=['POST'])
def delete_package_from_db(package_id):
    """Delete a package."""
    error = require_admin()
    if error:
        return error
    
    close_conn = False
    if has_app_context():
        conn = get_request_db()
    else:
        conn = get_db_connection()
        close_conn = True
    tracking = None
    try:
        # Get package details for logging
        pkg = conn.execute("SELECT tracking_number, item_name FROM packages WHERE id = ?", (package_id,)).fetchone()
        
        conn.execute("DELETE FROM packages WHERE id = ?", (package_id,))
        # Also delete history for this specific package ID
        conn.execute("DELETE FROM history WHERE package_id = ?", (package_id,))
        conn.commit()
        
        if pkg:
            tracking = pkg['tracking_number']
            logger.info(f"Deleted package {tracking} ({pkg['item_name']})")
            flash(f"Deleted package {tracking}", 'success')
            
    except Exception as e:
        logger.error(f"Delete error: {e}")
        flash(f"Error deleting package: {e}", 'error')
    finally:
        if close_conn:
            conn.close()
    
    # Also remove from Manifest if present
    if tracking and os.path.exists(MANIFEST_FILE):
        try:
            df = pd.read_csv(MANIFEST_FILE, dtype=str)
            df = df[df['TrackingNumber'].astype(str).str.strip() != tracking]
            with atomic_write(MANIFEST_FILE, 'w') as f:
                df.to_csv(f, index=False)
        except Exception as e:
            logger.warning(f"Could not remove package from manifest: {e}")
        
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/toggle_priority/<tracking>', methods=['POST'])
def toggle_priority(tracking):
    """Toggle priority flag on a package."""
    error = require_admin()
    if error:
        return error
    
    close_conn = False
    if has_app_context():
        conn = get_request_db()
    else:
        conn = get_db_connection()
        close_conn = True
    try:
        conn.execute("UPDATE packages SET priority = NOT COALESCE(priority, 0) WHERE tracking_number = ?", (tracking,))
        conn.commit()
    except Exception as e:
        logger.error(f"Toggle priority error: {e}")
    finally:
        if close_conn:
            conn.close()
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/set_date/<tracking>', methods=['POST'])
def set_date(tracking):
    """Set expected date for a package."""
    error = require_admin()
    if error:
        return error
    
    new_date = request.form.get('new_date')
    if new_date:
        close_conn = False
        if has_app_context():
            conn = get_request_db()
        else:
            conn = get_db_connection()
            close_conn = True
        try:
            conn.execute("UPDATE packages SET manual_date = ?, date_expected = ? WHERE tracking_number = ?", 
                         (new_date, new_date, tracking))
            conn.commit()
            sync_manifest()  # Trigger status update check
        except Exception as e:
            logger.error(f"Set date error: {e}")
        finally:
            if close_conn:
                conn.close()
            
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/set_status/<tracking>/<status>')
def set_status(tracking, status):
    """Set status for a package."""
    error = require_admin()
    if error:
        return error
    
    close_conn = False
    if has_app_context():
        conn = get_request_db()
    else:
        conn = get_db_connection()
        close_conn = True
    try:
        if status == 'received':
            conn.execute("UPDATE packages SET status='received', date_scanned=CURRENT_TIMESTAMP WHERE tracking_number=?", (tracking,))
        elif status == 'refunded':
            today = datetime.date.today().strftime('%Y-%m-%d')
            conn.execute("UPDATE packages SET status='refunded', refund_date=? WHERE tracking_number=?", (today, tracking))
        else:
            conn.execute("UPDATE packages SET status=? WHERE tracking_number=?", (status, tracking))
        conn.commit()
    except Exception as e:
        logger.error(f"Set status error: {e}")
    finally:
        if close_conn:
            conn.close()
        
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/bulk_action', methods=['POST'])
def bulk_action():
    """Perform bulk actions on selected packages."""
    error = require_admin()
    if error:
        return error
    
    action = request.form.get('action_type')
    trackings = request.form.getlist('trackings')
    
    if not trackings: 
        return redirect(url_for('admin.admin_panel'))
        
    close_conn = False
    if has_app_context():
        conn = get_request_db()
    else:
        conn = get_db_connection()
        close_conn = True
    try:
        if action == 'mark_received':
            for t in trackings:
                from app.services.data_manager import log_receipt
                log_receipt(t, "Bulk Item", "1", session.get('user', 'Admin'), conn=conn)
                 
        elif action == 'mark_late':
            placeholders = ','.join(['?']*len(trackings))
            conn.execute(f"UPDATE packages SET status='past_due' WHERE tracking_number IN ({placeholders})", trackings)
            conn.commit()
             
        elif action == 'delete':
            placeholders = ','.join(['?']*len(trackings))
            conn.execute(f"DELETE FROM packages WHERE tracking_number IN ({placeholders})", trackings)
            conn.commit()
            
            # Also remove from manifest.csv to prevent re-sync
            try:
                if os.path.exists(MANIFEST_FILE):
                    df = pd.read_csv(MANIFEST_FILE)
                    original_len = len(df)
                    
                    # Find ALL tracking columns (eBay has multiple like TrackingNumber, TrackingNumber.1)
                    track_cols = [col for col in df.columns if 'tracking' in col.lower()]
                    
                    if track_cols:
                        # Create a mask: row should be removed if ANY tracking column matches
                        # Normalize Excel-quoted values like ="12345" -> 12345
                        mask = pd.Series([False] * len(df))
                        for col in track_cols:
                            # Normalize: remove ="..." wrapping and quotes
                            normalized = df[col].astype(str).str.replace(r'^="?', '', regex=True).str.replace(r'"$', '', regex=True).str.strip()
                            mask = mask | normalized.isin(trackings)
                        
                        df = df[~mask]
                        if len(df) < original_len:
                            df.to_csv(MANIFEST_FILE, index=False)
                            logger.info(f"Removed {original_len - len(df)} items from manifest.csv")
            except Exception as csv_e:
                logger.error(f"Error removing from manifest: {csv_e}")
             
        elif action == 'unreceive':
            for t in trackings:
                # Get ALL packages with this tracking number (not just one)
                rows = conn.execute("SELECT id FROM packages WHERE tracking_number=?", (t,)).fetchall()
                for row in rows:
                    pid = row['id']
                    conn.execute("DELETE FROM history WHERE package_id=? AND action='received'", (pid,))
                    conn.execute("UPDATE packages SET status='expected', date_scanned=NULL WHERE id=?", (pid,))
            conn.commit()
             
    except Exception as e:
        logger.error(f"Bulk action error: {e}")
    finally:
        if close_conn:
            conn.close()
        
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/clear_history', methods=['POST'])
def clear_history():
    """Clear all history records."""
    error = require_admin()
    if error:
        return error
    
    close_conn = False
    if has_app_context():
        conn = get_request_db()
    else:
        conn = get_db_connection()
        close_conn = True
    try:
        conn.execute("DELETE FROM history")
        conn.commit()
    finally:
        if close_conn:
            conn.close()
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/set_tracking/<int:package_id>', methods=['POST'])
def set_tracking(package_id):
    """Update package Tracking Number."""
    error = require_admin()
    if error:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        
    new_tracking = request.form.get('tracking', '').strip()
    if not new_tracking:
        return jsonify({'success': False, 'error': 'Empty tracking number'}), 400
        
    close_conn = False
    if has_app_context():
        conn = get_request_db()
    else:
        conn = get_db_connection()
        close_conn = True
    try:
        # Get old tracking first
        old_pkg = conn.execute("SELECT tracking_number, item_name FROM packages WHERE id = ?", (package_id,)).fetchone()
        if not old_pkg:
            return jsonify({'success': False, 'error': 'Package not found'}), 404
            
        old_tracking = old_pkg['tracking_number']
        
        # Check if new tracking already exists (to avoid unique constraint fail)
        exists = conn.execute("SELECT id FROM packages WHERE tracking_number = ? AND id != ?", (new_tracking, package_id)).fetchone()
        if exists:
             return jsonify({'success': False, 'error': 'Tracking number already exists'}), 409
 
        conn.execute("UPDATE packages SET tracking_number = ? WHERE id = ?", (new_tracking, package_id))
        conn.commit()
        
        # Update Manifest if exists
        if os.path.exists(MANIFEST_FILE):
            try:
                # Use pandas like in delete/bulk_action
                # We need to find the row with old_tracking and update it
                df = pd.read_csv(MANIFEST_FILE, dtype=str)
                
                # Find columns that might hold the tracking number
                track_cols = [col for col in df.columns if 'tracking' in col.lower()]
                
                updated = False
                for col in track_cols:
                    # Clean comparisons
                    # If we find a match, update it
                    # Note: This might be tricky if multiple rows have the same tracking (unlikely for manifest, but possible)
                    # We will update ALL occurrences of the old tracking in the manifest
                    
                    # Normalize column for comparison
                    normalized = df[col].astype(str).str.replace(r'^="?', '', regex=True).str.replace(r'"$', '', regex=True).str.strip()
                    
                    mask = normalized == old_tracking
                    if mask.any():
                        # Update the original column
                        # If existing format was ="...", we should probably keep it? 
                        # For simplicity, just write the raw number. sync_manifest handles raw numbers fine.
                        df.loc[mask, col] = new_tracking
                        updated = True
                        
                if updated:
                    with atomic_write(MANIFEST_FILE, 'w') as f:
                        df.to_csv(f, index=False)
                    logger.info(f"Updated manifest tracking from {old_tracking} to {new_tracking}")
                    
            except Exception as e:
                logger.error(f"Error updating manifest tracking: {e}")
                
        return {'success': True}
    except Exception as e:
        logger.error(f"Error setting Tracking: {e}")
        return {'success': False, 'error': str(e)}, 500
    finally:
        if close_conn:
            conn.close()
