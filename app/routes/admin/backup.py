"""
Admin Backup Routes
Handles backup export/import, system toggles, and notification settings.
"""
import os
import io
import json
import logging
import datetime
import zipfile
from flask import request, redirect, url_for, session, flash, send_file, current_app, jsonify

from app.routes.admin import admin_bp, require_admin, save_config_value
from app.services.db import DB_PATH
from app.services.auth import BASE_DIR
from app.services.data_manager import load_config, MANIFEST_FILE, CONFIG_FILE

logger = logging.getLogger(__name__)


@admin_bp.route('/export_backup')
def export_backup():
    """Export system backup as ZIP file."""
    from flask_login import current_user
    if not current_user.is_authenticated or not current_user.is_admin:
        return redirect(url_for('auth.login'))
    
    files_to_save = [CONFIG_FILE, DB_PATH, MANIFEST_FILE]
    
    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f_path in files_to_save:
            if os.path.exists(f_path):
                zf.write(f_path, os.path.basename(f_path))
    
    memory_file.seek(0)
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    return send_file(memory_file, download_name=f"rscp_sqlite_backup_{date_str}.zip", as_attachment=True)


@admin_bp.route('/import_backup', methods=['POST'])
def import_backup():
    """Import system backup from ZIP file."""
    error = require_admin()
    if error:
        return error
    
    file = request.files.get('backup_file')
    force_restore = request.form.get('force_restore') == 'true'
    
    if not file or not file.filename.endswith('.zip'):
        flash("Please select a valid .zip backup file.")
        return redirect(url_for('admin.admin_panel'))
    
    try:
        with zipfile.ZipFile(file, 'r') as zf:
            # Validate backup contents
            allowed_files = ['config.json', 'rscp.db', 'manifest.csv']
            backup_files = zf.namelist()
            
            # Check for unexpected files
            for member in backup_files:
                if member not in allowed_files:
                    flash(f"Invalid backup: Unexpected file '{member}' found.")
                    return redirect(url_for('admin.admin_panel'))
            
            # Check SECRET_KEY match (if config.json is in backup)
            if 'config.json' in backup_files and not force_restore:
                try:
                    backup_config = json.loads(zf.read('config.json').decode('utf-8'))
                    current_config = load_config()
                    
                    backup_key = backup_config.get('SECRET_KEY', '')
                    current_key = current_config.get('SECRET_KEY', '')
                    
                    if backup_key and current_key and backup_key != current_key:
                        flash("⚠️ WARNING: This backup has a different SECRET_KEY. All current sessions will be invalidated. To proceed, check 'Force Restore' and try again.")
                        return redirect(url_for('admin.admin_panel'))
                except Exception as e:
                    logger.warning(f"Could not validate backup config: {e}")
            
            # Perform restore
            for member in backup_files:
                if member in allowed_files:
                    # Security: Validate target path stays within BASE_DIR
                    target_path = os.path.normpath(os.path.join(BASE_DIR, member))
                    if not target_path.startswith(os.path.normpath(BASE_DIR)):
                        flash(f"Security error: Invalid path detected.")
                        return redirect(url_for('admin.admin_panel'))
                    zf.extract(member, BASE_DIR)
            
            flash("✅ System restored from backup successfully. Please log in again.")
            session.clear()
            
    except Exception as e:
        flash(f"Restore failed: {str(e)}")
        
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/toggle_trim', methods=['POST'])
def toggle_trim():
    """Toggle auto-trim setting."""
    error = require_admin()
    if error:
        return error
    new_state = request.form.get('trim_state') == 'on'
    save_config_value('AUTO_TRIM', new_state)
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/toggle_inventory', methods=['POST'])
def toggle_inventory():
    """Toggle inventory module on/off."""
    error = require_admin()
    if error:
        return error
    new_state = request.form.get('enabled') == 'on'
    save_config_value('INVENTORY_ENABLED', new_state)
    flash(f"Inventory module {'enabled' if new_state else 'disabled'}.")
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/save_margin_settings', methods=['POST'])
def save_margin_settings():
    """Save preferred margin settings for inventory pricing."""
    error = require_admin()
    if error:
        return error
    
    enabled = request.form.get('margin_enabled') == 'on'
    try:
        percent = float(request.form.get('margin_percent', 20))
    except ValueError:
        percent = 20
    
    save_config_value('PREFERRED_MARGIN_ENABLED', enabled)
    save_config_value('PREFERRED_MARGIN_PERCENT', percent)
    
    # Save Margin/Markup Preference
    margin_type = request.form.get('margin_type', 'margin')
    if margin_type not in ['markup', 'margin']:
        margin_type = 'margin'
    save_config_value('PREFERRED_MARGIN_TYPE', margin_type)
    
    flash(f"Margin settings saved. Basis: {margin_type.title()}, Target: {percent}%.")
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/save_inventory_general', methods=['POST'])
def save_inventory_general():
    """Save general inventory settings."""
    error = require_admin()
    if error:
        return error
    
    auto_copy = request.form.get('auto_copy_sku') == 'on'
    quick_add = request.form.get('quick_add_enabled') == 'on'
    
    save_config_value('INVENTORY_AUTO_COPY_SKU', auto_copy)
    save_config_value('INVENTORY_QUICK_ADD', quick_add)
    
    flash("General inventory settings saved.")
    return redirect(url_for('admin.admin_panel', tab='inventory'))


@admin_bp.route('/toggle_pos', methods=['POST'])
def toggle_pos():
    """Toggle POS module on/off."""
    error = require_admin()
    if error:
        return error
    new_state = request.form.get('enabled') == 'on'
    save_config_value('POS_ENABLED', new_state)
    flash(f"POS module {'enabled' if new_state else 'disabled'}.")
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/toggle_timeclock', methods=['POST'])
def toggle_timeclock():
    """Toggle Timeclock module on/off."""
    error = require_admin()
    if error:
        return error
    enabled = request.form.get('enabled') == 'on'
    save_config_value('TIMECLOCK_ENABLED', enabled)
    flash(f"Timeclock module {'enabled' if enabled else 'disabled'}.")
    return redirect(url_for('admin.admin_panel'))

@admin_bp.route('/update_timeclock_settings', methods=['POST'])
def update_timeclock_settings():
    error = require_admin()
    if error:
        return error
    try:
        grace = int(request.form.get('grace_period', 15))
        save_config_value('TIMECLOCK_GRACE_PERIOD', grace)
        flash("Timeclock settings updated.")
    except ValueError:
        flash("Invalid grace period.")
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/save_location_labels', methods=['POST'])
def save_location_labels():
    """Save custom inventory location labels."""
    error = require_admin()
    if error:
        return error
    
    location_labels = {
        'area': request.form.get('location_label_1', 'Area').strip() or 'Area',
        'aisle': request.form.get('location_label_2', 'Aisle').strip() or 'Aisle',
        'shelf': request.form.get('location_label_3', 'Shelf').strip() or 'Shelf',
        'bin': request.form.get('location_label_4', 'Bin').strip() or 'Bin',
    }
    
    save_config_value('LOCATION_LABELS', location_labels)
    flash("Location labels updated successfully.")
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/save_item_addons', methods=['POST'])
def save_item_addons():
    """Save item addon settings (warranty/disclaimer text)."""
    error = require_admin()
    if error:
        return error
    
    # Addon 1
    save_config_value('ITEM_ADDON_1_ENABLED', request.form.get('addon_1_enabled') == 'on')
    save_config_value('ITEM_ADDON_1_LABEL', request.form.get('addon_1_label', '').strip())
    save_config_value('ITEM_ADDON_1_TEXT', request.form.get('addon_1_text', '').strip())
    
    # Addon 2
    save_config_value('ITEM_ADDON_2_ENABLED', request.form.get('addon_2_enabled') == 'on')
    save_config_value('ITEM_ADDON_2_LABEL', request.form.get('addon_2_label', '').strip())
    save_config_value('ITEM_ADDON_2_TEXT', request.form.get('addon_2_text', '').strip())
    
    flash("Item addons saved successfully.")
    return redirect(url_for('admin.admin_panel', tab='inventory'))


@admin_bp.route('/save_location_options', methods=['POST'])
def save_location_options():
    """Save location dropdown options for each field."""
    error = require_admin()
    if error:
        return error
    
    # Parse comma-separated values into arrays
    def parse_options(raw):
        if not raw:
            return []
        return [opt.strip() for opt in raw.split(',') if opt.strip()]
    
    location_options = {
        'area': parse_options(request.form.get('location_options_area', '')),
        'aisle': parse_options(request.form.get('location_options_aisle', '')),
        'shelf': parse_options(request.form.get('location_options_shelf', '')),
        'bin': parse_options(request.form.get('location_options_bin', '')),
    }
    
    allow_custom = request.form.get('location_allow_custom') == 'on'
    
    save_config_value('LOCATION_OPTIONS', location_options)
    save_config_value('LOCATION_ALLOW_CUSTOM', allow_custom)
    
    flash("Location options saved successfully.")
    return redirect(url_for('admin.admin_panel', tab='inventory'))


@admin_bp.route('/save_pos_approval_settings', methods=['POST'])
def save_pos_approval_settings():
    """Save POS manager approval settings."""
    error = require_admin()
    if error:
        return error
    
    try:
        max_discount_percent = float(request.form.get('max_discount_percent', 10))
    except ValueError:
        max_discount_percent = 10
    
    try:
        max_discount_amount = float(request.form.get('max_discount_amount', 20))
    except ValueError:
        max_discount_amount = 20
    
    require_manager_refund = request.form.get('require_manager_refund') == 'on'
    
    save_config_value('POS_MAX_DISCOUNT_PERCENT', max_discount_percent)
    save_config_value('POS_MAX_DISCOUNT_AMOUNT', max_discount_amount)
    save_config_value('POS_REQUIRE_MANAGER_REFUND', require_manager_refund)
    
    flash("POS manager approval settings saved.")
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/toggle_date_format', methods=['POST'])
def toggle_date_format():
    """Toggle between US and UK date formats."""
    error = require_admin()
    if error:
        return error
    c = load_config() or {}
    current = c.get('DATE_FORMAT', 'US')
    new_fmt = 'UK' if current == 'US' else 'US'
    save_config_value('DATE_FORMAT', new_fmt)
    return redirect(url_for('admin.admin_panel'))


@admin_bp.route('/save_notifications', methods=['POST'])
def save_notifications():
    """Save webhook notification settings for both Receiving and Inventory."""
    error = require_admin()
    if error:
        return error
    
    from app.utils.helpers import obscure_string
    key = current_app.secret_key
    
    # Receiving Webhook (Priority package alerts)
    url_receiving = request.form.get('webhook_url_receiving', '').strip()
    enabled_receiving = request.form.get('webhook_enabled_receiving') == 'on'
    
    if url_receiving and url_receiving != "********":
        enc_url = obscure_string(url_receiving, key)
        save_config_value('WEBHOOK_URL', enc_url)
    save_config_value('WEBHOOK_ENABLED', enabled_receiving)
    
    # Inventory Webhook (Low stock alerts)
    url_inventory = request.form.get('webhook_url_inventory', '').strip()
    enabled_inventory = request.form.get('webhook_enabled_inventory') == 'on'
    
    if url_inventory and url_inventory != "********":
        enc_url = obscure_string(url_inventory, key)
        save_config_value('WEBHOOK_URL_INVENTORY', enc_url)
    save_config_value('WEBHOOK_ENABLED_INVENTORY', enabled_inventory)
    
    flash("Notification settings saved.")
    return redirect(url_for('admin.admin_panel', tab='settings'))


@admin_bp.route('/save_inapp_notifications', methods=['POST'])
def save_inapp_notifications():
    """Save in-app notification toggle settings."""
    error = require_admin()
    if error:
        return error
    
    # Get form values
    low_stock = request.form.get('notify_low_stock') == 'on'
    oos = request.form.get('notify_oos') == 'on'
    federation = request.form.get('notify_federation') == 'on'
    priority_pkg = request.form.get('notify_priority_packages') == 'on'
    normal_pkg = request.form.get('notify_normal_packages') == 'on'
    
    logger.info(f"Saving in-app notifications: low_stock={low_stock}, oos={oos}, federation={federation}, priority={priority_pkg}, normal={normal_pkg}")
    
    # Save each toggle setting
    save_config_value('NOTIFY_LOW_STOCK', low_stock)
    save_config_value('NOTIFY_OOS', oos)
    save_config_value('NOTIFY_FEDERATION', federation)
    save_config_value('NOTIFY_PRIORITY_PACKAGES', priority_pkg)
    save_config_value('NOTIFY_NORMAL_PACKAGES', normal_pkg)
    
    flash("In-app notification settings saved.")
    return redirect(url_for('admin.admin_panel', tab='settings'))


@admin_bp.route('/save_session_timeout', methods=['POST'])
def save_session_timeout():
    """Save session timeout settings."""
    error = require_admin()
    if error:
        return error
    
    enabled = request.form.get('timeout_enabled') == 'on'
    minutes = int(request.form.get('timeout_minutes', 30))
    
    save_config_value('SESSION_TIMEOUT_ENABLED', enabled)
    save_config_value('SESSION_TIMEOUT_MINUTES', minutes)
    
    if enabled:
        flash(f"Session timeout enabled: {minutes} minutes of inactivity.")
    else:
        flash("Session timeout disabled.")
    
    return redirect(url_for('admin.admin_panel', tab='settings'))


@admin_bp.route('/save_auto_confirm', methods=['POST'])
def save_auto_confirm():
    """Save auto-confirm import setting."""
    error = require_admin()
    if error:
        return jsonify({"error": "Unauthorized"}), 403
    
    try:
        data = request.get_json() or {}
        enabled = data.get('enabled', False)
        
        save_config_value('AUTO_CONFIRM_IMPORT', enabled)
        
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error saving auto-confirm setting: {e}")
        return jsonify({"error": str(e)}), 500


@admin_bp.route('/federation/prefix', methods=['POST'])
def save_federation_prefix():
    """Save the location prefix for SKU federation."""
    error = require_admin()
    if error:
        return jsonify({"error": "Unauthorized"}), 403
    
    try:
        data = request.get_json() or {}
        prefix = data.get('prefix', '').upper().strip()
        
        # Validate prefix
        if len(prefix) != 4 or not prefix.isalnum():
            return jsonify({"error": "Prefix must be exactly 4 alphanumeric characters"}), 400
        
        # TODO: Check uniqueness across linked peers
        
        save_config_value('LOCATION_PREFIX', prefix)
        
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"Error saving federation prefix: {e}")
        return jsonify({"error": str(e)}), 500


@admin_bp.route('/federation/settings', methods=['POST'])
def save_federation_settings():
    """Save federation settings (cross-search toggle)."""
    error = require_admin()
    if error:
        flash("Admin access required", "danger")
        return redirect(url_for('admin.admin_panel', tab='federation'))
    
    try:
        cross_search_enabled = 'cross_search_enabled' in request.form
        
        save_config_value('FEDERATION_CROSS_SEARCH_ENABLED', cross_search_enabled)
        
        flash("Federation settings saved", "success")
        return redirect(url_for('admin.admin_panel', tab='federation'))
    except Exception as e:
        logger.error(f"Error saving federation settings: {e}")
        flash(f"Error: {e}", "danger")
        return redirect(url_for('admin.admin_panel', tab='federation'))
