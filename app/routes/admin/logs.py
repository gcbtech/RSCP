"""
Admin Log Management Routes
Handles viewing, clearing, and exporting error logs.
"""
import io
import csv
import logging
from flask import render_template, redirect, url_for, flash, make_response, request

from app.routes.admin import admin_bp, require_admin
from app.services.db import get_db_connection

logger = logging.getLogger(__name__)


@admin_bp.route('/logs')
def admin_logs():
    """View error logs with filtering and pagination."""
    error = require_admin()
    if error:
        return error
    
    # Filters
    level_filter = request.args.get('level', '')
    source_filter = request.args.get('source', '')
    page = int(request.args.get('page', 1))
    per_page = 50
    
    conn = get_db_connection()
    try:
        # Build query with filters
        query = "SELECT * FROM error_logs WHERE 1=1"
        params = []
        
        if level_filter:
            query += " AND level = ?"
            params.append(level_filter)
        
        if source_filter:
            query += " AND source LIKE ?"
            params.append(f"%{source_filter}%")
        
        # Count total for pagination
        count_query = query.replace("SELECT *", "SELECT COUNT(*) as cnt")
        total = conn.execute(count_query, params).fetchone()['cnt']
        total_pages = max(1, (total + per_page - 1) // per_page)
        
        # Add ordering and pagination
        query += " ORDER BY timestamp DESC LIMIT ? OFFSET ?"
        params.extend([per_page, (page - 1) * per_page])
        
        logs = conn.execute(query, params).fetchall()
        
        # Get unique levels and sources for filter dropdowns
        levels = conn.execute("SELECT DISTINCT level FROM error_logs WHERE level IS NOT NULL ORDER BY level").fetchall()
        sources = conn.execute("SELECT DISTINCT source FROM error_logs WHERE source IS NOT NULL ORDER BY source").fetchall()
        
        return render_template('admin_logs.html',
                               logs=logs,
                               levels=[l['level'] for l in levels],
                               sources=[s['source'] for s in sources],
                               current_page=page,
                               total_pages=total_pages,
                               total_logs=total,
                               filter_level=level_filter,
                               filter_source=source_filter)
    finally:
        conn.close()


@admin_bp.route('/logs/clear', methods=['POST'])
def clear_logs():
    """Clear old error logs (older than 7 days)."""
    error = require_admin()
    if error:
        return error
    
    conn = get_db_connection()
    try:
        result = conn.execute(
            "DELETE FROM error_logs WHERE timestamp < datetime('now', '-7 days')"
        )
        conn.commit()
        deleted = result.rowcount
        if deleted > 0:
            flash(f"Cleared {deleted} log entries older than 7 days.")
        else:
            flash("No logs older than 7 days to clear.")
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
