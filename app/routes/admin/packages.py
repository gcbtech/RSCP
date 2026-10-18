"""
Admin Packages Routes
Handles package CRUD operations and the main admin panel view.
"""
import logging
from flask import render_template, request, redirect, url_for, session, flash, jsonify, has_app_context
from flask_login import current_user

from app.routes.admin import admin_bp, require_admin, save_config_value
from app.services.db import get_db_connection, get_request_db
from app.services.auth import load_users
from app.services.data_manager import load_config, get_analytics_stats
from app.services import receiving

logger = logging.getLogger(__name__)


@admin_bp.route('/')
def admin_panel():
    """Main admin panel view with package listing."""
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
    
    from app.services.settings import template_view
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
                           config=template_view(config),  # never render secrets
                           pos_settings=pos_settings,
                           inventory_enabled=config.get('INVENTORY_ENABLED', False) if config else False)


def _conn():
    """Request-scoped connection inside a request, else a fresh one
    (second value: whether the caller must close it)."""
    if has_app_context():
        return get_request_db(), False
    return get_db_connection(), True


@admin_bp.route('/add_manual_item', methods=['POST'])
def add_manual_item():
    """Add a package manually."""
    tracking = request.form.get('tracking', '').strip()
    name = request.form.get('name', '').strip()
    sku = request.form.get('sku', '').strip() or None
    date_input = request.form.get('date', '')
    is_priority = request.form.get('priority') == 'on'

    if tracking and name:
        conn, close_conn = _conn()
        try:
            receiving.add_manual_package(conn, tracking, name, sku, date_input, is_priority,
                                         current_user.username)

            # In-app notification for new package
            try:
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
    sku = request.form.get('sku', '').strip() or None
    conn, close_conn = _conn()
    try:
        receiving.set_package_sku(conn, package_id, sku)
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
    try:
        qty = int(request.form.get('quantity', '1'))
        if qty < 0:
            raise ValueError("Negative quantity")
    except ValueError:
        return jsonify({'success': False, 'error': 'Invalid Quantity'}), 400

    conn, close_conn = _conn()
    try:
        receiving.set_package_quantity(conn, package_id, qty)
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
    conn, close_conn = _conn()
    try:
        pkg = receiving.delete_package(conn, package_id)
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

    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/toggle_priority/<tracking>', methods=['POST'])
def toggle_priority(tracking):
    """Toggle priority flag on a package."""
    conn, close_conn = _conn()
    try:
        receiving.toggle_priority(conn, tracking)
    except Exception as e:
        logger.error(f"Toggle priority error: {e}")
    finally:
        if close_conn:
            conn.close()
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/set_date/<tracking>', methods=['POST'])
def set_date(tracking):
    """Set expected date for a package."""
    new_date = request.form.get('new_date')
    if new_date:
        conn, close_conn = _conn()
        try:
            receiving.set_expected_date(conn, tracking, new_date)
        except Exception as e:
            logger.error(f"Set date error: {e}")
        finally:
            if close_conn:
                conn.close()

    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/set_status/<tracking>/<status>')
def set_status(tracking, status):
    """Set status for a package."""
    conn, close_conn = _conn()
    try:
        receiving.set_status(conn, tracking, status)
    except Exception as e:
        logger.error(f"Set status error: {e}")
    finally:
        if close_conn:
            conn.close()

    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/bulk_action', methods=['POST'])
def bulk_action():
    """Perform bulk actions on selected packages."""
    action = request.form.get('action_type')
    trackings = request.form.getlist('trackings')

    if not trackings:
        return redirect(url_for('admin.admin_panel'))

    conn, close_conn = _conn()
    try:
        if action == 'mark_received':
            receiving.bulk_receive(conn, trackings, current_user.username, conf=load_config())
        elif action == 'mark_late':
            receiving.mark_late(conn, trackings)
        elif action == 'delete':
            receiving.delete_by_tracking(conn, trackings)
        elif action == 'unreceive':
            receiving.unreceive(conn, trackings)
    except Exception as e:
        logger.error(f"Bulk action error: {e}")
    finally:
        if close_conn:
            conn.close()

    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/clear_history', methods=['POST'])
def clear_history():
    """Clear all history records."""
    conn, close_conn = _conn()
    try:
        receiving.clear_history(conn)
    finally:
        if close_conn:
            conn.close()
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/set_tracking/<int:package_id>', methods=['POST'])
def set_tracking(package_id):
    """Update package Tracking Number."""
    new_tracking = request.form.get('tracking', '').strip()
    if not new_tracking:
        return jsonify({'success': False, 'error': 'Empty tracking number'}), 400

    conn, close_conn = _conn()
    try:
        if not receiving.set_package_tracking(conn, package_id, new_tracking):
            return jsonify({'success': False, 'error': 'Package not found'}), 404
        return {'success': True}
    except receiving.TrackingConflict as e:
        return jsonify({'success': False, 'error': str(e)}), 409
    except Exception as e:
        logger.error(f"Error setting Tracking: {e}")
        return {'success': False, 'error': str(e)}, 500
    finally:
        if close_conn:
            conn.close()
