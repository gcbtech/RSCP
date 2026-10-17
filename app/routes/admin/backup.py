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
    
    import tempfile
    import sqlite3
    from app.services.db import get_db_connection
    
    temp_db_path = None
    try:
        # Create a temp file path and immediately close file descriptor to release handles
        temp_fd, temp_db_path = tempfile.mkstemp(suffix=".db")
        os.close(temp_fd)
        
        # Connect to active DB and safely perform online backup to the temp DB file
        src_conn = get_db_connection()
        dest_conn = sqlite3.connect(temp_db_path)
        with dest_conn:
            src_conn.backup(dest_conn)
        dest_conn.close()
        src_conn.close()
        
        memory_file = io.BytesIO()
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            # Zip config from its active file (packages live in the database)
            for f_path in [CONFIG_FILE]:
                if os.path.exists(f_path):
                    zf.write(f_path, os.path.basename(f_path))
            
            # Zip the safe temporary backup file as 'rscp.db'
            if os.path.exists(temp_db_path):
                zf.write(temp_db_path, "rscp.db")
                
        memory_file.seek(0)
        date_str = datetime.datetime.now().strftime("%Y-%m-%d")
        return send_file(memory_file, download_name=f"rscp_sqlite_backup_{date_str}.zip", as_attachment=True)
        
    except Exception as e:
        logger.error(f"Failed to generate safe database backup: {e}")
        flash(f"Backup failed: {str(e)}", "danger")
        return redirect(url_for('admin.admin_panel'))
        
    finally:
        # Ensure temporary backup file is cleaned up
        if temp_db_path and os.path.exists(temp_db_path):
            try:
                os.remove(temp_db_path)
            except Exception as e:
                logger.warning(f"Could not clean up temporary backup file '{temp_db_path}': {e}")


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
            # manifest.csv: accepted so pre-3.0 backups restore, but ignored
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
            
            # Validate the database BEFORE touching anything live
            restored_db = None
            if 'rscp.db' in backup_files:
                restored_db = _extract_to_temp(zf, 'rscp.db', suffix='.db')
                problem = _check_backup_db(restored_db)
                if problem:
                    os.remove(restored_db)
                    flash(f"Restore refused: {problem}")
                    return redirect(url_for('admin.admin_panel'))

            try:
                if restored_db:
                    safety_copy = _restore_database(restored_db)
                    logger.info(f"Database restored from upload; previous copy saved to {safety_copy}")
            finally:
                if restored_db and os.path.exists(restored_db):
                    os.remove(restored_db)

            for name, target in (('config.json', CONFIG_FILE),):
                if name in backup_files:
                    _atomic_replace(target, zf.read(name))
            if 'config.json' in backup_files:
                load_config(force_reload=True)

            flash("✅ System restored from backup. A copy of the previous database was kept in the "
                  "backups folder. Restart RSCP if settings were restored. Please log in again.")
            session.clear()

    except Exception as e:
        logger.exception("Restore failed")
        flash(f"Restore failed: {str(e)}")

    return redirect(url_for('admin.admin_panel'))


def _extract_to_temp(zf, member, suffix=''):
    import tempfile
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, 'wb') as out:
        out.write(zf.read(member))
    return path


def _check_backup_db(path):
    """Return a reason string if the uploaded database is unusable, else None."""
    import sqlite3
    try:
        conn = sqlite3.connect(path)
        try:
            if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                return "the database in the backup is corrupt."
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'users' not in tables or 'packages' not in tables:
                return "the file is not an RSCP database."
            if conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0:
                return "the backup has no user accounts (you would be locked out)."
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        return f"the database could not be read ({e})."
    return None


def _restore_database(src_path):
    """Replace the live database's contents with `src_path`, safely.

    Overwriting rscp.db on disk while workers hold it open (WAL mode) can
    corrupt it: their -wal/-shm files still describe the old file. SQLite's
    online backup API copies into the live database through SQLite itself,
    under its locks, so every connection sees a consistent result. The
    current database is copied aside first. Returns the safety copy's path.
    """
    import sqlite3
    from app.services.migration import run_migrations

    live_path = os.path.realpath(DB_PATH)
    backup_dir = os.path.join(os.path.dirname(live_path), 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    safety_copy = os.path.join(backup_dir, f'pre-restore-{stamp}.db')

    live = sqlite3.connect(DB_PATH, timeout=30)
    try:
        dest = sqlite3.connect(safety_copy)
        with dest:
            live.backup(dest)
        dest.close()

        src = sqlite3.connect(src_path)
        try:
            src.backup(live)
        finally:
            src.close()
    finally:
        live.close()

    # An older backup may predate the current schema
    run_migrations(DB_PATH)
    return safety_copy


def _atomic_replace(target, data):
    """Write bytes to `target` (following symlinks into the shared data dir) atomically."""
    import tempfile
    real = os.path.realpath(target)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(real), prefix='.restore.')
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        if os.path.basename(real) == 'config.json':
            os.chmod(tmp, 0o600)
        os.replace(tmp, real)
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


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
    track_dates = request.form.get('track_first_stock_date') == 'on'
    
    save_config_value('INVENTORY_AUTO_COPY_SKU', auto_copy)
    save_config_value('INVENTORY_QUICK_ADD', quick_add)
    save_config_value('TRACK_FIRST_STOCK_DATE', track_dates)
    
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
    priority_pkg = request.form.get('notify_priority_packages') == 'on'
    normal_pkg = request.form.get('notify_normal_packages') == 'on'
    
    logger.info(f"Saving in-app notifications: low_stock={low_stock}, oos={oos}, priority={priority_pkg}, normal={normal_pkg}")
    
    # Save each toggle setting
    save_config_value('NOTIFY_LOW_STOCK', low_stock)
    save_config_value('NOTIFY_OOS', oos)
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
