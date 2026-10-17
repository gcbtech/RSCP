"""
Admin Backup Routes
Handles backup export/import. (Settings are saved by app/routes/settings.py.)
"""
import os
import io
import json
import logging
import datetime
import zipfile
from flask import request, redirect, url_for, session, flash, send_file, current_app

from app.routes.admin import admin_bp, require_admin
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

            # A backup from before the settings table (schema v6) keeps its
            # settings in its config.json: apply them to the restored database.
            legacy_settings = None
            if restored_db and 'config.json' in backup_files and _schema_version(restored_db) < 6:
                legacy_settings = json.loads(zf.read('config.json').decode('utf-8'))

            try:
                if restored_db:
                    safety_copy = _restore_database(restored_db)
                    logger.info(f"Database restored from upload; previous copy saved to {safety_copy}")
            finally:
                if restored_db and os.path.exists(restored_db):
                    os.remove(restored_db)

            if legacy_settings:
                from app.services import settings as settings_store
                settings_store.invalidate()
                settings_store.set_lenient({k: v for k, v in legacy_settings.items()
                                            if k not in settings_store.BOOTSTRAP_KEYS}, user='restore')

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


def _schema_version(path):
    import sqlite3
    conn = sqlite3.connect(path)
    try:
        return conn.execute('PRAGMA user_version').fetchone()[0]
    finally:
        conn.close()


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
