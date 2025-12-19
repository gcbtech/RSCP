import os
import json
import logging
import datetime
import subprocess
import pandas as pd
import zipfile
import io
from flask import Blueprint, render_template, request, redirect, url_for, session, flash, send_file, make_response

# Services (New DB/Auth/Migration interfaces)
from app.services.db import get_db_connection, DB_PATH
from app.services.auth import load_users, create_user, delete_user, update_user_password, BASE_DIR
from app.services.data_manager import load_config, sync_manifest, MANIFEST_FILE, CONFIG_FILE
from app.services.file_handler import atomic_write
from app.services.migration import ensure_db_ready  # Used in import backup to re-init DB

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')
logger = logging.getLogger(__name__)

# --- HELPERS ---
def save_config_value(key, value):
    conf = load_config() or {}
    conf[key] = value
    try:
        with atomic_write(CONFIG_FILE, 'w') as f:
            json.dump(conf, f, indent=4)
    except Exception as e:
        logger.error(f"Save config error: {e}")

# --- ROUTES ---

@admin_bp.route('/')
def admin_panel():
    if not session.get('is_admin'):
        return redirect(url_for('main.login', mode='admin'))
        
    # Sync Manifest first to ensure fresh data
    sync_manifest()
    
    users = load_users()
    
    # Sorting / Pagination parameters
    sort_by = request.args.get('sort_by', 'recent')
    order = request.args.get('order', 'desc')
    page = int(request.args.get('page', 1))
    per_page = 50
    offset = (page - 1) * per_page
    
    # Map sort keys to SQL columns
    sort_map = {
        'date': 'date_expected',
        'status': 'status', # Alphabetical status? checking legacy: custom sort logic existed.
        'name': 'item_name',
        'recent': 'id' # Proxy for "Added recently"
    }
    sql_sort = sort_map.get(sort_by, 'id')
    sql_order = 'DESC' if order == 'desc' else 'ASC'
    
    # Custom Status Sorting (received checks date_scanned)
    # SQL can sort by CASE WHEN... but keeping it simple for now.
    
    # Custom Status Sorting (received checks date_scanned)
    # SQL can sort by CASE WHEN... but keeping it simple for now.
    
    from app.services.data_manager import get_analytics_stats
    graph_data = get_analytics_stats(14)
    
    conn = get_db_connection()
    try:
        # Total Count
        total = conn.execute("SELECT count(*) as c FROM packages").fetchone()['c']
        total_pages = (total + per_page - 1) // per_page
        
        # Query
        query = f"SELECT * FROM packages ORDER BY {sql_sort} {sql_order} LIMIT ? OFFSET ?"
        # Status sort might need refinement if 'received' logic is complex. 
        # For now, sorting by 'status' string.
        
        rows = conn.execute(query, (per_page, offset)).fetchall()
        
        # Convert to dict list for template (template expects specific keys like 'qty', 'date')
        packages = []
        for r in rows:
            d = dict(r)
            # Map DB columns to Template expectations (Legacy Compat)
            d['qty'] = d['quantity']
            d['date'] = d['date_expected']
            d['image'] = d['image_url']
            d['name'] = d['item_name']
            
            # Helper for 'status' display if needed
            if d['date_scanned']:
                d['scanned'] = True
                # In template, if scanned, it shows "Received" usually.
            else:
                d['scanned'] = False
            
            packages.append(d)
            
    finally:
        conn.close()

    if request.args.get('partial'):
         return render_template('_admin_table_rows.html', 
                               packages=packages, 
                               date_format=load_config().get('DATE_FORMAT', 'US'))

    return render_template('admin.html', 
                           packages=packages, 
                           users=users,
                           current_page=page,
                           total_pages=total_pages,
                           sort_by=sort_by,
                           order=order,
                           graph_data=graph_data,
                           config=load_config())

# --- PACKAGES ACTIONS ---

@admin_bp.route('/add_manual_item', methods=['POST'])
def add_manual_item():
    if not session.get('is_admin'): return "Unauthorized", 401
    
    tracking = request.form.get('tracking', '').strip()
    name = request.form.get('name', '').strip()
    date_input = request.form.get('date', '')
    is_priority = request.form.get('priority') == 'on' 
    
    if tracking and name:
        conn = get_db_connection()
        try:
            # Date Status Logic
            status = 'pending'
            today = datetime.date.today()
            if date_input:
                try:
                    dt_obj = datetime.datetime.strptime(date_input, '%Y-%m-%d').date()
                    if dt_obj == today: status = 'expected'
                    elif dt_obj < today: status = 'past_due'
                except: pass
            
            conn.execute('''
                INSERT INTO packages (tracking_number, item_name, date_expected, quantity, status, source, priority, manual_date)
                VALUES (?, ?, ?, 1, ?, 'manual', ?, ?)
            ''', (tracking, name, date_input, status, 1 if is_priority else 0, date_input))
            conn.commit()
            flash(f"Added {name}")
        except Exception as e:
            flash(f"Error adding item: {e}")
        finally:
            conn.close()
            
    return redirect(url_for('admin.admin_panel'))

@admin_bp.route('/delete_package/<tracking>', methods=['POST'])
def delete_package_from_db(tracking):
    if not session.get('is_admin'): return "Unauthorized", 401
    
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM packages WHERE tracking_number = ?", (tracking,))
        conn.commit()
    except Exception as e:
        logger.error(f"Delete error: {e}")
    finally:
        conn.close()
    
    # Also remove from Manifest if present? (Legacy behavior)
    if os.path.exists(MANIFEST_FILE):
        try:
            df = pd.read_csv(MANIFEST_FILE, dtype=str)
            df = df[df['TrackingNumber'].astype(str).str.strip() != tracking]
            with atomic_write(MANIFEST_FILE, 'w') as f:
                df.to_csv(f, index=False)
        except: pass
        
    return redirect(url_for('admin.admin_panel'))

@admin_bp.route('/toggle_priority/<tracking>', methods=['POST'])
def toggle_priority(tracking):
    if not session.get('is_admin'): return "Unauthorized", 401
    
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
    if not session.get('is_admin'): return "Unauthorized", 401
    
    new_date = request.form.get('new_date')
    if new_date:
        conn = get_db_connection()
        try:
            # Update manual_date AND recalculate status?
            # Or just update manual_date. sync_manifest handles status calc next time.
            # But we want immediate feedback.
            conn.execute("UPDATE packages SET manual_date = ?, date_expected = ? WHERE tracking_number = ?", 
                         (new_date, new_date, tracking))
            conn.commit()
            sync_manifest() # Trigger status update check
        except Exception as e:
            logger.error(f"Set date error: {e}")
        finally:
            conn.close()
            
    return redirect(url_for('admin.admin_panel'))

@admin_bp.route('/set_status/<tracking>/<status>')
def set_status(tracking, status):
    if not session.get('is_admin'): return "Unauthorized", 401
    
    # Supported statuses: received, returned, refunded, return_pending, past_due, expected
    conn = get_db_connection()
    try:
        if status == 'received':
             # Logic is usually handled by 'receive' action, but this is a manual override link?
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
    if not session.get('is_admin'): return "Unauthorized", 401
    
    action = request.form.get('action_type')
    trackings = request.form.getlist('trackings')
    
    if not trackings: 
        return redirect(url_for('admin.admin_panel'))
        
    conn = get_db_connection()
    try:
        if action == 'mark_received':
             for t in trackings:
                 # Log receipt needs ID lookup, log_receipt helper does this.
                 # But we can do bulk update.
                 # Let's simple loop.
                 # log_receipt updates DB status too.
                 # Import log_receipt? No, circular import risk if data_manager imports admin? No admin imports data_manager.
                 from app.services.data_manager import log_receipt
                 # Assumption: log_receipt is available.
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
             # Remove from history, reset status/scanned
             for t in trackings:
                  # Get Package ID
                  res = conn.execute("SELECT id FROM packages WHERE tracking_number=?", (t,)).fetchone()
                  if res:
                      pid = res['id']
                      conn.execute("DELETE FROM history WHERE package_id=? AND action='received'", (pid,))
                      conn.execute("UPDATE packages SET status='expected', date_scanned=NULL WHERE id=?", (pid,))
             conn.commit()
             
    except Exception as e:
        logger.error(f"Bulk action error: {e}")
    finally:
        conn.close()
        
    return redirect(url_for('admin.admin_panel'))


# --- USER ACTIONS ---

@admin_bp.route('/add_user', methods=['POST'])
def add_user_action():
    if not session.get('is_admin'): return "Unauthorized", 401
    username = request.form.get('username', '').strip()
    pin = request.form.get('pin', '').strip()
    
    if not username:
        flash("Username required.")
        return redirect(url_for('admin.admin_panel'))
    
    try:
        from werkzeug.security import generate_password_hash
        # Logic: User wants "Optional PIN" and "No Password".
        # So PIN becomes the password.
        
        pin_hash = generate_password_hash(pin) if pin else None
        # Store PIN hash in password_hash too so standard login works
        pw_hash = pin_hash 
        
        if create_user(username, pw_hash, is_admin=False, pin_hash=pin_hash):
            flash(f"User {username} created.")
        else:
            flash("Error creating user (already exists?)")
    except Exception as e:
        logger.error(f"Add User Error: {e}")
        flash(f"System error adding user: {e}")
        
    return redirect(url_for('admin.admin_panel'))

@admin_bp.route('/delete_user/<username>', methods=['POST'])
def delete_user_action(username):
    if not session.get('is_admin'): return "Unauthorized", 401
    if username == session.get('user'):
        flash("Cannot delete yourself.")
    else:
        delete_user(username)
        flash(f"User {username} deleted.")
    return redirect(url_for('admin.admin_panel'))


# --- SYSTEM ACTIONS ---

@admin_bp.route('/reset_users')
def reset_users_route():
    # Only for recovery? Or debug?
    return redirect(url_for('admin.admin_panel'))

@admin_bp.route('/clear_history', methods=['POST'])
def clear_history():
    if not session.get('is_admin'): return "Unauthorized", 401
    # Delete all History rows?
    conn = get_db_connection()
    conn.execute("DELETE FROM history")
    conn.commit()
    conn.close()
    return redirect(url_for('admin.admin_panel'))

@admin_bp.route('/upload_file', methods=['POST'])
def upload_file():
    if not session.get('is_admin'): return "Unauthorized", 401
    
    file = request.files.get('file')
    if not file or file.filename == '':
        return redirect(url_for('admin.admin_panel'))
        
    try:
        import pandas as pd
        
        # Determine File Type
        if file.filename.endswith('.xlsx'):
            df = pd.read_excel(file)
        else:
            # Try different encodings for CSV
            try:
                df = pd.read_csv(file, encoding='utf-8')
            except:
                file.seek(0)
                df = pd.read_csv(file, encoding='cp1252')
        
        # Manual Column Mapping (Overrides)
        manual_track = request.form.get('col_tracking', '').strip()
        manual_name = request.form.get('col_name', '').strip()
        manual_date = request.form.get('col_date', '').strip()
        
        # PRIORITIZE DATES
        # Check for better date columns before general map
        priority_dates = ['expected delivery date', 'estimated delivery date', 'expected date', 'promise date']
        found_priority_date = False
        
        lower_cols = {c.lower(): c for c in df.columns}
        
        for p in priority_dates:
            if p in lower_cols:
                # Rename this priority column to 'Date'
                real_col = lower_cols[p]
                df.rename(columns={real_col: 'Date'}, inplace=True)
                found_priority_date = True
                break
        
        # Column Normalization Map
        # Target: TrackingNumber, ItemName, Date, Quantity, Image
        col_map = {
            # Tracking
            'Tracking number': 'TrackingNumber', 'tracking_number': 'TrackingNumber', 'Tracking': 'TrackingNumber',
            'Carrier Tracking #': 'TrackingNumber', 'Carrier Tracking Number': 'TrackingNumber', 'Tracking #': 'TrackingNumber',
            # Name
            'Item name': 'ItemName', 'Title': 'ItemName', 'Item Title': 'ItemName', 'Product Name': 'ItemName',
            'Item Description': 'ItemName', 'Description': 'ItemName',
            # Date (Fallbacks if no priority date found)
            'Purchase Date': 'Date', 'Order Date': 'Date', 'Shipment Date': 'Date', 'Ship Date': 'Date',
            # Quantity
            'Quantity': 'Quantity', 'Qty': 'Quantity',
            # Image
            'Image URL': 'Image', 'Photo': 'Image'
        }
        
        # If we found a priority date, remove 'Date' targets from map to avoid overwriting or duplicates
        if found_priority_date:
            col_map = {k: v for k, v in col_map.items() if v != 'Date'}
        elif 'Date' in df.columns:
             # If a column specifically named "Date" exists, assume it is the right one and don't rename others to Date
             col_map = {k: v for k, v in col_map.items() if v != 'Date'}
        
        # Apply Manual Overrides
        if manual_track and manual_track in df.columns:
            df.rename(columns={manual_track: 'TrackingNumber'}, inplace=True)
        if manual_name and manual_name in df.columns:
             df.rename(columns={manual_name: 'ItemName'}, inplace=True)
        if manual_date and manual_date in df.columns:
             df.rename(columns={manual_date: 'Date'}, inplace=True)
        
        # Rename columns (case insensitive search?)
        # Better: Normalize current columns to lower, then map?
        # Simple approach: Rename matching keys
        df.rename(columns=col_map, inplace=True)

        # CHECK FOR ASIN -> IMAGE GENERATION
        # Look for a column named 'ASIN' (case-insensitive) that wasn't renamed to anything else
        asin_col = None
        for c in df.columns:
            if c.lower() == 'asin':
                asin_col = c
                break
        
        if asin_col:
            total_rows = len(df)
            logger.info(f"[Upload] Found ASIN column: {asin_col}. Total Rows: {total_rows}")
            
            # Generate Image column if it doesn't exist or fill missing
            if 'Image' not in df.columns:
                df['Image'] = None
            
            gen_count = 0
            skip_no_asin = 0
            skip_bad_asin = 0
            skip_has_img = 0
            
            # Iterate and set image url
            for index, row in df.iterrows():
                try:
                    asin = str(row[asin_col]).strip().upper()
                    if asin.lower() == 'nan' or asin.lower() == 'none' or asin == '':
                         asin = ""
                    
                    # Check if Image is effectively empty
                    current_img = str(row.get('Image', ''))
                    is_empty = pd.isna(row['Image']) or current_img.lower() == 'nan' or current_img.lower() == 'none' or current_img.strip() == ''
                    
                    if not asin:
                        skip_no_asin += 1
                        continue
                        
                    if len(asin) < 10:
                        skip_bad_asin += 1
                        if skip_bad_asin <= 5:
                             logger.info(f"[Upload] Row {index} Skipped: ASIN too short '{asin}'")
                        continue

                    if not is_empty:
                        skip_has_img += 1
                        if skip_has_img <= 5:
                             logger.info(f"[Upload] Row {index} Skipped: Has Image '{current_img}'")
                        continue
                        
                    # If we got here, we generate
                    df.at[index, 'Image'] = f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SX200_.jpg"
                    gen_count += 1
                    
                except Exception as e:
                    logger.error(f"[Upload] Error gen image for row {index}: {e}")
                    continue
            
            logger.info(f"[Upload] Result: Generated={gen_count}, Skipped(NoASIN)={skip_no_asin}, Skipped(BadASIN)={skip_bad_asin}, Skipped(HasImg)={skip_has_img}")
        else:
            logger.info("[Upload] No ASIN column found in file.")
        
        # Ensure TrackingNumber exists
        if 'TrackingNumber' not in df.columns:
            # Fallback: finding first column with "track" in it?
            found = False
            for c in df.columns:
                lower_c = c.lower()
                if 'track' in lower_c and ('number' in lower_c or '#' in c):
                     df.rename(columns={c: 'TrackingNumber'}, inplace=True)
                     found = True
                     break
            if not found:
                # Last ditch: Look for just "Tracking" if it's the only one
                for c in df.columns:
                    if c.lower() == 'tracking':
                        df.rename(columns={c: 'TrackingNumber'}, inplace=True)
                        found = True
                        break
                        
            if not found:
                raise Exception("Could not find 'Tracking Number' column in file.")

        # Ensure ItemName exists
        if 'ItemName' not in df.columns:
             for c in df.columns:
                if 'title' in c.lower() or 'item' in c.lower() or 'product' in c.lower():
                     df.rename(columns={c: 'ItemName'}, inplace=True)
                     break
        
        # Save Standardized Manifest
        df.to_csv(MANIFEST_FILE, index=False)
        
        # Trigger Sync
        sync_manifest()
        
        flash(f"Manifest processed successfully. {len(df)} rows found.")
        
    except Exception as e:
        flash(f"Upload error: {e}")
        
    return redirect(url_for('admin.admin_panel'))

@admin_bp.route('/export_backup')
def export_backup():
    if not session.get('is_admin'): return redirect(url_for('main.login'))
    
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
    if not session.get('is_admin'): return "Unauthorized", 401
    
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
                        # Keys don't match - require confirmation
                        flash("⚠️ WARNING: This backup has a different SECRET_KEY. All current sessions will be invalidated. To proceed, check 'Force Restore' and try again.")
                        return redirect(url_for('admin.admin_panel'))
                except Exception as e:
                    logger.warning(f"Could not validate backup config: {e}")
            
            # Perform restore
            for member in backup_files:
                if member in allowed_files:
                    zf.extract(member, BASE_DIR)
            
            flash("✅ System restored from backup successfully. Please log in again.")
            session.clear()  # Clear session since SECRET_KEY may have changed
            
    except Exception as e:
        flash(f"Restore failed: {str(e)}")
        
    return redirect(url_for('admin.admin_panel'))

@admin_bp.route('/toggle_trim', methods=['POST'])
def toggle_trim():
    if not session.get('is_admin'): return "Unauthorized", 401
    new_state = request.form.get('trim_state') == 'on'
    save_config_value('AUTO_TRIM', new_state)
    return redirect(url_for('admin.admin_panel'))

@admin_bp.route('/toggle_date_format', methods=['POST'])
def toggle_date_format():
    if not session.get('is_admin'): return "Unauthorized", 401
    c = load_config() or {}
    current = c.get('DATE_FORMAT', 'US')
    new_fmt = 'UK' if current == 'US' else 'US'
    save_config_value('DATE_FORMAT', new_fmt)
    return redirect(url_for('admin.admin_panel'))

@admin_bp.route('/save_notifications', methods=['POST'])
def save_notifications():
    if not session.get('is_admin'): return "Unauthorized", 401
    
    from app.utils.helpers import obscure_string
    from flask import current_app
    
    url = request.form.get('webhook_url', '').strip()
    enabled = request.form.get('webhook_enabled') == 'on'
    
    # Save URL if provided (Obfuscated)
    if url:
        key = current_app.secret_key
        # If user types "********", ignore it (keep existing)
        if url != "********":
            enc_url = obscure_string(url, key)
            save_config_value('WEBHOOK_URL', enc_url)
    
    save_config_value('WEBHOOK_ENABLED', enabled)
    
    flash("Notification settings saved.")
    return redirect(url_for('admin.admin_panel'))

@admin_bp.route('/logs')
def admin_logs():
    if not session.get('is_admin'): return "Unauthorized", 401
    
    conn = get_db_connection()
    logs = conn.execute("SELECT * FROM error_logs ORDER BY timestamp DESC LIMIT 200").fetchall()
    conn.close()
    
    return render_template('admin_logs.html', logs=logs)

@admin_bp.route('/logs/export')
def export_logs():
    if not session.get('is_admin'): return "Unauthorized", 401
    
    conn = get_db_connection()
    logs = conn.execute("SELECT * FROM error_logs ORDER BY timestamp DESC").fetchall()
    conn.close()
    
    import io
    import csv 
    
    si = io.StringIO()
    cw = csv.writer(si)
    # Header
    cw.writerow(['ID', 'Timestamp', 'Level', 'Source', 'Message', 'Trace', 'User', 'Status']) # Status is still in DB, just expored
    
    for log in logs:
        cw.writerow([
            log['id'], 
            log['timestamp'], 
            log['level'], 
            log['source'], 
            log['message'], 
            log['trace'], 
            log['user_id'], 
            log['status']
        ])
        
    output = make_response(si.getvalue())
    output.headers["Content-Disposition"] = "attachment; filename=error_logs.csv"
    output.headers["Content-type"] = "text/csv"
    return output

# --- UPDATE SYSTEM ---
import shutil
import tempfile
import requests

GITHUB_REPO = "gcbtech/RSCP"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}"
GITHUB_ZIP_URL = f"https://github.com/{GITHUB_REPO}/archive/refs/heads/main.zip"

# Files that should never be overwritten during updates
PROTECTED_FILES = [
    'config.json',
    'rscp.db', 
    'manifest.csv',
    'app.log',
]

# Directories that should never be overwritten
PROTECTED_DIRS = [
    'venv',
    '__pycache__',
    '.pytest_cache',
]

def get_current_version():
    """Get current installed version."""
    version_file = os.path.join(BASE_DIR, 'VERSION')
    if os.path.exists(version_file):
        with open(version_file, 'r') as f:
            return f.read().strip()
    return "unknown"

def get_latest_version():
    """Check GitHub for latest version."""
    try:
        # Get VERSION file content from GitHub
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/VERSION"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return response.text.strip()
    except Exception as e:
        logger.error(f"Error checking latest version: {e}")
    return None

@admin_bp.route('/check_update')
def check_update():
    """Check if updates are available from GitHub."""
    if not session.get('is_admin'):
        return {"error": "Admin required"}, 403
    
    try:
        current_version = get_current_version()
        latest_version = get_latest_version()
        
        if not latest_version:
            return {"error": "Could not check for updates. Please try again later."}, 500
        
        # Compare versions
        update_available = latest_version != current_version
        
        return {
            "current_version": current_version,
            "latest_version": latest_version,
            "update_available": update_available,
            "status": f"Current: {current_version}, Latest: {latest_version}"
        }
    except Exception as e:
        logger.error(f"Update check error: {e}")
        return {"error": str(e)}, 500


@admin_bp.route('/update', methods=['POST'])
def perform_update():
    """Download and apply latest updates from GitHub."""
    if not session.get('is_admin'):
        flash("Admin access required")
        return redirect(url_for('admin.admin_panel'))
    
    try:
        old_version = get_current_version()
        
        # Download ZIP from GitHub
        logger.info("Downloading update from GitHub...")
        response = requests.get(GITHUB_ZIP_URL, timeout=60, stream=True)
        if response.status_code != 200:
            flash(f"Failed to download update: HTTP {response.status_code}")
            return redirect(url_for('admin.admin_panel'))
        
        # Save to temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.zip') as tmp_file:
            for chunk in response.iter_content(chunk_size=8192):
                tmp_file.write(chunk)
            tmp_zip_path = tmp_file.name
        
        try:
            # Extract to temp directory
            with tempfile.TemporaryDirectory() as tmp_dir:
                with zipfile.ZipFile(tmp_zip_path, 'r') as zip_ref:
                    zip_ref.extractall(tmp_dir)
                
                # Find the extracted folder (GitHub adds -main suffix)
                extracted_dirs = [d for d in os.listdir(tmp_dir) if os.path.isdir(os.path.join(tmp_dir, d))]
                if not extracted_dirs:
                    flash("Update failed: Invalid archive structure")
                    return redirect(url_for('admin.admin_panel'))
                
                source_dir = os.path.join(tmp_dir, extracted_dirs[0])
                
                # Copy files, skipping protected ones
                files_updated = 0
                for root, dirs, files in os.walk(source_dir):
                    # Skip protected directories
                    dirs[:] = [d for d in dirs if d not in PROTECTED_DIRS]
                    
                    rel_path = os.path.relpath(root, source_dir)
                    dest_path = os.path.join(BASE_DIR, rel_path) if rel_path != '.' else BASE_DIR
                    
                    # Create directory if needed
                    if not os.path.exists(dest_path):
                        os.makedirs(dest_path)
                    
                    for file in files:
                        # Skip protected files
                        if file in PROTECTED_FILES:
                            continue
                        
                        src_file = os.path.join(root, file)
                        dst_file = os.path.join(dest_path, file)
                        
                        try:
                            shutil.copy2(src_file, dst_file)
                            files_updated += 1
                        except Exception as e:
                            logger.warning(f"Could not update {file}: {e}")
                
                logger.info(f"Updated {files_updated} files")
        
        finally:
            # Clean up temp zip file
            if os.path.exists(tmp_zip_path):
                os.unlink(tmp_zip_path)
        
        # Install any new dependencies
        venv_pip = os.path.join(BASE_DIR, 'venv', 'bin', 'pip')
        if os.path.exists(venv_pip):
            subprocess.run(
                [venv_pip, 'install', '-r', 'requirements.txt', '-q'],
                cwd=BASE_DIR,
                capture_output=True,
                timeout=120
            )
        
        new_version = get_current_version()
        
        flash(f"Update successful! {old_version} → {new_version}. Please restart the service for changes to take effect.")
        logger.info(f"RSCP updated from {old_version} to {new_version}")
        
    except requests.RequestException as e:
        logger.error(f"Download error: {e}")
        flash(f"Failed to download update: {str(e)}")
    except Exception as e:
        logger.error(f"Update error: {e}")
        flash(f"Update failed: {str(e)}")
    
    return redirect(url_for('admin.admin_panel'))
