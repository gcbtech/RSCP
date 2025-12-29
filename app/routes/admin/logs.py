"""
Admin Log Management Routes
Handles viewing, clearing, and exporting error logs.
"""
import io
import csv
import logging
from flask import render_template, redirect, url_for, flash, make_response

from app.routes.admin import admin_bp, require_admin
from app.services.db import get_db_connection

logger = logging.getLogger(__name__)


@admin_bp.route('/logs')
def admin_logs():
    """View error logs."""
    error = require_admin()
    if error:
        return error
    
    conn = get_db_connection()
    logs = conn.execute("SELECT * FROM error_logs ORDER BY timestamp DESC LIMIT 200").fetchall()
    conn.close()
    
    return render_template('admin_logs.html', logs=logs)


@admin_bp.route('/logs/clear', methods=['POST'])
def clear_logs():
    """Clear all error logs."""
    error = require_admin()
    if error:
        return error
    
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM error_logs")
        conn.commit()
        flash("All error logs have been cleared.")
    except Exception as e:
        logger.error(f"Error clearing logs: {e}")
        flash(f"Error clearing logs: {e}")
    finally:
        conn.close()
        
    return redirect(url_for('admin.admin_logs'))


@admin_bp.route('/logs/export')
def export_logs():
    """Export error logs as CSV."""
    error = require_admin()
    if error:
        return error
    
    conn = get_db_connection()
    logs = conn.execute("SELECT * FROM error_logs ORDER BY timestamp DESC").fetchall()
    conn.close()
    
    si = io.StringIO()
    cw = csv.writer(si)
    # Header
    cw.writerow(['ID', 'Timestamp', 'Level', 'Source', 'Message', 'Trace', 'User', 'Status'])
    
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
