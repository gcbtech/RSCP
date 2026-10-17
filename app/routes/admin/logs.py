"""
Admin Log Management Routes
Handles viewing, clearing, and exporting error logs.

Two sources: the `error_logs` table (errors and user reports the app records)
and, on request, the service's own output from the systemd journal. RSCP
writes no log files of its own.
"""
import io
import os
import csv
import shutil
import logging
import subprocess
from flask import render_template, redirect, url_for, flash, make_response, request

from app.routes.admin import admin_bp, require_admin
from app.services.db import get_db_connection

logger = logging.getLogger(__name__)

JOURNAL_UNIT = os.environ.get('RSCP_SYSTEMD_UNIT', 'rscp')
JOURNAL_TIMEOUT = 5  # seconds
JOURNAL_MAX_LINES = 2000

# journalctl exits 0 but shows only the caller's own messages when the user
# may not read the system journal; it says so on stderr.
_NO_ACCESS_HINTS = ('not seeing messages from other users', 'insufficient permissions',
                    'no journal files were opened')


def read_journal(lines=200):
    """Last `lines` journal entries for the RSCP unit.

    Returns (text, note): text is the output or None; note explains why the
    journal is missing or incomplete, else None. Never raises.
    """
    lines = max(10, min(int(lines), JOURNAL_MAX_LINES))
    exe = shutil.which('journalctl')
    if not exe:
        return None, 'journalctl is not available on this host (not running under systemd).'
    try:
        result = subprocess.run(
            [exe, '-u', JOURNAL_UNIT, '-n', str(lines), '--no-pager', '-o', 'short-iso'],
            capture_output=True, text=True, timeout=JOURNAL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None, f'journalctl did not answer within {JOURNAL_TIMEOUT} seconds.'
    except OSError as e:
        return None, f'Could not run journalctl: {e}'

    stderr = (result.stderr or '').strip()
    no_access = any(h in stderr.lower() for h in _NO_ACCESS_HINTS)
    note = None
    if no_access:
        note = ('The RSCP service user cannot read the system journal, so service '
                'messages are missing. Add it to the systemd-journal group '
                '(sudo usermod -aG systemd-journal <service user>) and restart RSCP.')
    elif result.returncode != 0:
        note = f'journalctl failed: {stderr.splitlines()[0] if stderr else "exit code " + str(result.returncode)}'
    text = result.stdout if result.stdout.strip() else None
    return text, note


@admin_bp.route('/logs')
def admin_logs():
    """View error logs with filtering and pagination."""
    # Filters
    level_filter = request.args.get('level', '')
    source_filter = request.args.get('source', '')
    page = max(1, request.args.get('page', 1, type=int) or 1)
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

        # Service log from the journal, only when asked for (?journal=<lines>).
        journal_lines = request.args.get('journal', type=int)
        journal_text = journal_note = None
        if journal_lines:
            journal_text, journal_note = read_journal(journal_lines)

        return render_template('admin_logs.html',
                               journal_lines=journal_lines,
                               journal_text=journal_text,
                               journal_note=journal_note,
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
