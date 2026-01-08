"""
Admin Packages Routes
Handles package CRUD operations and the main admin panel view.
"""
import os
import datetime
import logging
import pandas as pd
from flask import render_template, request, redirect, url_for, session, flash
from flask_login import current_user

from app.routes.admin import admin_bp, require_admin, save_config_value
from app.services.db import get_db_connection
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
    
    conn = get_db_connection()
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
    date_input = request.form.get('date', '')
    is_priority = request.form.get('priority') == 'on' 
    
    if tracking and name:
        conn = get_db_connection()
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
                INSERT INTO packages (tracking_number, item_name, date_expected, quantity, status, source, priority, manual_date)
                VALUES (?, ?, ?, 1, ?, 'manual', ?, ?)
            ''', (tracking, name, date_input, status, 1 if is_priority else 0, date_input))
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
            conn.close()
            
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/delete_package/<tracking>', methods=['POST'])
def delete_package_from_db(tracking):
    """Delete a package."""
    error = require_admin()
    if error:
        return error
    
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM packages WHERE tracking_number = ?", (tracking,))
        conn.commit()
    except Exception as e:
        logger.error(f"Delete error: {e}")
    finally:
        conn.close()
    
    # Also remove from Manifest if present
    if os.path.exists(MANIFEST_FILE):
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
    
    conn = get_db_connection()
    try:
        conn.execute("UPDATE packages SET priority = NOT COALESCE(priority, 0) WHERE tracking_number = ?", (tracking,))
        conn.commit()
    except Exception as e:
        logger.error(f"Toggle priority error: {e}")
    finally:
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
        conn = get_db_connection()
        try:
            conn.execute("UPDATE packages SET manual_date = ?, date_expected = ? WHERE tracking_number = ?", 
                         (new_date, new_date, tracking))
            conn.commit()
            sync_manifest()  # Trigger status update check
        except Exception as e:
            logger.error(f"Set date error: {e}")
        finally:
            conn.close()
            
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/set_status/<tracking>/<status>')
def set_status(tracking, status):
    """Set status for a package."""
    error = require_admin()
    if error:
        return error
    
    conn = get_db_connection()
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
        
    conn = get_db_connection()
    try:
        if action == 'mark_received':
            for t in trackings:
                from app.services.data_manager import log_receipt
                log_receipt(t, "Bulk Item", "1", session.get('user', 'Admin'))
                 
        elif action == 'mark_late':
            placeholders = ','.join(['?']*len(trackings))
            conn.execute(f"UPDATE packages SET status='past_due' WHERE tracking_number IN ({placeholders})", trackings)
            conn.commit()
             
        elif action == 'delete':
            placeholders = ','.join(['?']*len(trackings))
            conn.execute(f"DELETE FROM packages WHERE tracking_number IN ({placeholders})", trackings)
            conn.commit()
             
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
        conn.close()
        
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/clear_history', methods=['POST'])
def clear_history():
    """Clear all history records."""
    error = require_admin()
    if error:
        return error
    
    conn = get_db_connection()
    conn.execute("DELETE FROM history")
    conn.commit()
    conn.close()
    return redirect(url_for('admin.admin_panel'))
