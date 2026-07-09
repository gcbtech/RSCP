"""
Inventory Audit Module
Routes for inventory audit sessions.
"""
import logging
from flask import request, redirect, url_for, session, flash, render_template, jsonify
from flask_login import login_required, current_user

from app.routes.inventory import inventory_bp
from app.services.db import get_db_connection

logger = logging.getLogger(__name__)


@inventory_bp.route('/audit')
@login_required
def audit_dashboard():
    """Audit dashboard showing active and past sessions."""
    conn = get_db_connection()
    active_session = conn.execute("SELECT * FROM audit_sessions WHERE status = 'active' ORDER BY start_time DESC LIMIT 1").fetchone()
    past_sessions = conn.execute("SELECT * FROM audit_sessions WHERE status = 'completed' ORDER BY end_time DESC LIMIT 10").fetchall()
    conn.close()
    return render_template('inventory/audit_dashboard.html', active_session=active_session, past_sessions=past_sessions)


@inventory_bp.route('/audit/start', methods=['POST'])
@login_required
def start_audit():
    """Start a new audit session."""
    mode = request.form.get('mode')  # 'item' or 'shelf'
    user = session.get('user', 'Unknown')
    
    if mode not in ['item', 'shelf']:
        flash("Invalid audit mode.")
        return redirect(url_for('inventory.audit_dashboard'))

    conn = get_db_connection()

    # Enforce a single active audit at a time. Without this guard, tapping a
    # mode card while an audit is already in progress spawns a second concurrent
    # session, splitting one logical audit across multiple reports (the user
    # then has to resume+finish each fragment in turn). See audit_dashboard,
    # which only ever surfaces the newest active session.
    existing = conn.execute(
        "SELECT * FROM audit_sessions WHERE status = 'active' ORDER BY start_time DESC LIMIT 1"
    ).fetchone()
    if existing:
        existing_id = existing['id']
        existing_mode = existing['mode']
        conn.close()
        if existing_mode == mode:
            # Same mode: just resume the in-progress audit rather than forking.
            flash("Resumed your in-progress audit.")
            return redirect(url_for('inventory.audit_live', session_id=existing_id))
        # Different mode: don't silently mix counts. Send them back to the
        # dashboard to explicitly resume or finish the existing audit first.
        flash(f"You have an in-progress {existing_mode.capitalize()} audit. "
              f"Resume or finish it before starting a new one.")
        return redirect(url_for('inventory.audit_dashboard'))

    cursor = conn.cursor()
    cursor.execute("INSERT INTO audit_sessions (user_id, mode) VALUES (?, ?)", (user, mode))
    session_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return redirect(url_for('inventory.audit_live', session_id=session_id))


@inventory_bp.route('/audit/live/<int:session_id>')
@login_required
def audit_live(session_id):
    """Live audit scanning page."""
    conn = get_db_connection()
    sess = conn.execute("SELECT * FROM audit_sessions WHERE id = ?", (session_id,)).fetchone()
    
    if not sess or sess['status'] != 'active':
        conn.close()
        flash("Invalid or completed session.")
        return redirect(url_for('inventory.audit_dashboard'))
        
    conn.close()
    return render_template('inventory/audit_live.html', audit_session=sess)


@inventory_bp.route('/audit/scan', methods=['POST'])
@login_required
def audit_scan():
    """Process an audit scan (API)."""
    data = request.json
    session_id = data.get('session_id')
    barcode = data.get('barcode').strip()
    
    conn = get_db_connection()
    
    # Verify Session
    sess = conn.execute("SELECT * FROM audit_sessions WHERE id = ?", (session_id,)).fetchone()
    if not sess or sess['status'] != 'active':
        conn.close()
        return jsonify({'error': 'Invalid session'}), 400
        
    # Find Item by SKU or Secondary ID (UPC, Part Number)
    # The secondary_ids column stores JSON, e.g., {"upc": "123", "part_number": "ABC"}
    # We use a LIKE query to find the barcode within the JSON string.
    item = conn.execute('''
        SELECT * FROM inventory_items 
        WHERE sku = ? 
        OR secondary_ids LIKE ?
    ''', (barcode, f'%"{barcode}"%')).fetchone()
        
    if not item:
        conn.close()
        return jsonify({'error': 'Item not found in inventory'}), 404
        
    # Check for existing record in this session
    record = conn.execute("SELECT * FROM audit_records WHERE session_id = ? AND item_id = ?", 
                          (session_id, item['id'])).fetchone()
                          
    response_data = {
        'item': dict(item),
        'mode': sess['mode'],
        'prev_count': record['counted_qty'] if record else 0
    }
    
    conn.close()
    return jsonify(response_data)


@inventory_bp.route('/audit/submit_count', methods=['POST'])
@login_required
def audit_submit_count():
    """Submit a count for an audit item."""
    data = request.json
    session_id = data.get('session_id')
    item_id = data.get('item_id')
    count = int(data.get('count'))
    
    conn = get_db_connection()
    
    item = conn.execute("SELECT * FROM inventory_items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        conn.close()
        return jsonify({'error': 'Item not found'}), 404
        
    record = conn.execute("SELECT * FROM audit_records WHERE session_id = ? AND item_id = ?", 
                          (session_id, item_id)).fetchone()
                          
    diff = count - item['quantity']
    
    if diff != 0:
        # Update live inventory immediately
        conn.execute("UPDATE inventory_items SET quantity = ? WHERE id = ?", (count, item_id))
        
        # Log the transaction
        user = session.get('user', 'Unknown')
        conn.execute('''
            INSERT INTO inventory_transactions (inventory_item_id, quantity_change, reason, user_id) 
            VALUES (?, ?, 'Live Audit Correction', ?)
        ''', (item_id, diff, user))

    if record:
        conn.execute("UPDATE audit_records SET counted_qty = ?, timestamp = CURRENT_TIMESTAMP WHERE id = ?", 
                     (count, record['id']))
    else:
        conn.execute('''
            INSERT INTO audit_records (session_id, item_id, sku, name, expected_qty, counted_qty)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (session_id, item_id, item['sku'], item['name'], item['quantity'], count))
        
    conn.commit()
    conn.close()
    
    return jsonify({'success': True})


@inventory_bp.route('/audit/history')
@login_required
def audit_history():
    """View completed audit sessions."""
    conn = get_db_connection()
    sessions = conn.execute("SELECT * FROM audit_sessions WHERE status='completed' ORDER BY end_time DESC").fetchall()
    conn.close()
    return render_template('inventory/audit_history.html', sessions=sessions)


@inventory_bp.route('/audit/finalize/<int:session_id>', methods=['POST'])
@login_required
def audit_finalize(session_id):
    """Finalize an audit session (counts are already applied live)."""
    conn = get_db_connection()
    
    # We no longer apply counts here because they are applied immediately upon submission.
    conn.execute("UPDATE audit_sessions SET status = 'completed', end_time = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()
    return redirect(url_for('inventory.audit_report', session_id=session_id))


@inventory_bp.route('/audit/report/<int:session_id>')
@login_required
def audit_report(session_id):
    """View audit session report."""
    conn = get_db_connection()
    sess = conn.execute("SELECT * FROM audit_sessions WHERE id = ?", (session_id,)).fetchone()
    
    records = conn.execute('''
        SELECT *, (counted_qty - expected_qty) as diff 
        FROM audit_records 
        WHERE session_id = ? 
        ORDER BY diff DESC
    ''', (session_id,)).fetchall()
    
    conn.close()
    return render_template('inventory/audit_report.html', audit_session=sess, records=records)


@inventory_bp.route('/audit/apply_fix/<int:record_id>', methods=['POST'])
@login_required
def audit_apply_fix(record_id):
    """Apply a single audit correction."""
    conn = get_db_connection()
    record = conn.execute("SELECT * FROM audit_records WHERE id = ?", (record_id,)).fetchone()
    
    if record:
        conn.execute("UPDATE inventory_items SET quantity = ? WHERE id = ?", 
                     (record['counted_qty'], record['item_id']))
        diff = record['counted_qty'] - record['expected_qty']
        conn.execute("INSERT INTO inventory_transactions (inventory_item_id, quantity_change, reason, user_id) VALUES (?, ?, 'Audit Correction', ?)", 
                     (record['item_id'], diff, session.get('user')))
                     
        flash(f"Updated stock for {record['name']} to {record['counted_qty']}")
        conn.commit()
        
    conn.close()
    return redirect(request.referrer)


@inventory_bp.route('/audit/export/<int:session_id>')
@login_required
def audit_export(session_id):
    """Export audit report as CSV."""
    import io
    import csv
    from flask import Response
    
    conn = get_db_connection()
    sess = conn.execute("SELECT * FROM audit_sessions WHERE id = ?", (session_id,)).fetchone()
    
    if not sess:
        flash("Audit session not found.")
        return redirect(url_for('inventory.audit_dashboard'))
    
    records = conn.execute('''
        SELECT sku, name, expected_qty, counted_qty, (counted_qty - expected_qty) as diff 
        FROM audit_records 
        WHERE session_id = ? 
        ORDER BY name
    ''', (session_id,)).fetchall()
    conn.close()
    
    # Generate CSV
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Header with audit info
    writer.writerow([f'Audit Report #{session_id}'])
    writer.writerow([f'Mode: {sess["mode"]}', f'User: {sess["user_id"]}'])
    writer.writerow([f'Started: {sess["start_time"]}', f'Ended: {sess["end_time"] or "In Progress"}'])
    writer.writerow([])  # Blank line
    
    # Column headers
    writer.writerow(['SKU', 'Item Name', 'Expected Qty', 'Counted Qty', 'Difference'])
    
    # Data rows
    for rec in records:
        writer.writerow([rec['sku'], rec['name'], rec['expected_qty'], rec['counted_qty'], rec['diff']])
    
    # Summary
    total_items = len(records)
    matched = sum(1 for r in records if r['diff'] == 0)
    discrepancies = total_items - matched
    writer.writerow([])
    writer.writerow(['Summary'])
    writer.writerow([f'Total Items: {total_items}', f'Matched: {matched}', f'Discrepancies: {discrepancies}'])
    
    output.seek(0)
    
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename=audit_report_{session_id}.csv'}
    )


@inventory_bp.route('/audit/delete/<int:session_id>', methods=['POST'])
@login_required
def delete_audit(session_id):
    """Delete a specific audit session (admin only)."""
    from flask_login import current_user
    from app.utils.permissions import has_permission
    
    if not current_user.is_authenticated or not has_permission(current_user, 'inventory.manage'):
        flash("Admin access required.", "error")
        return redirect(url_for('inventory.audit_history'))
    
    conn = get_db_connection()
    try:
        # Delete audit records first (foreign key constraint)
        conn.execute('DELETE FROM audit_records WHERE session_id = ?', (session_id,))
        # Delete the session
        conn.execute('DELETE FROM audit_sessions WHERE id = ?', (session_id,))
        conn.commit()
        flash(f"Audit session #{session_id} deleted.")
    except Exception as e:
        logger.error(f"Error deleting audit session {session_id}: {e}")
        flash(f"Error deleting session: {e}", "error")
    finally:
        conn.close()
    
    return redirect(url_for('inventory.audit_history'))


@inventory_bp.route('/audit/delete_all', methods=['POST'])
@login_required
def delete_all_audits():
    """Delete ALL audit sessions (admin only)."""
    from flask_login import current_user
    from app.utils.permissions import has_permission
    
    if not current_user.is_authenticated or not has_permission(current_user, 'inventory.manage'):
        flash("Admin access required.", "error")
        return redirect(url_for('inventory.audit_history'))
    
    conn = get_db_connection()
    try:
        # Delete all audit records first
        conn.execute('DELETE FROM audit_records')
        # Delete all sessions
        conn.execute('DELETE FROM audit_sessions')
        conn.commit()
        flash("All audit history deleted.")
    except Exception as e:
        logger.error(f"Error deleting all audits: {e}")
        flash(f"Error deleting audits: {e}", "error")
    finally:
        conn.close()
    
    return redirect(url_for('inventory.audit_history'))


@inventory_bp.route('/audit/export_all')
@login_required
def export_all_audits():
    """Export all audit sessions and records to a single CSV file."""
    from flask import Response
    import csv
    import io
    
    conn = get_db_connection()
    try:
        sessions = conn.execute('''
            SELECT * FROM audit_sessions 
            WHERE status = 'completed' 
            ORDER BY end_time DESC
        ''').fetchall()
        
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Header
        writer.writerow(['Session ID', 'Mode', 'User', 'Start Time', 'End Time', 'SKU', 'Item Name', 'Expected Qty', 'Counted Qty', 'Difference'])
        
        for sess in sessions:
            records = conn.execute('''
                SELECT ar.*, ii.sku, ii.name
                FROM audit_records ar
                LEFT JOIN inventory_items ii ON ar.inventory_item_id = ii.id
                WHERE ar.session_id = ?
            ''', (sess['id'],)).fetchall()
            
            for rec in records:
                diff = rec['counted_qty'] - rec['expected_qty']
                writer.writerow([
                    sess['id'], sess['mode'], sess['user_id'], 
                    sess['start_time'], sess['end_time'],
                    rec['sku'], rec['name'], rec['expected_qty'], rec['counted_qty'], diff
                ])
        
        output.seek(0)
        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': 'attachment; filename=all_audit_history.csv'}
        )
    except Exception as e:
        logger.error(f"Error exporting all audits: {e}")
        flash(f"Export error: {e}", "error")
        return redirect(url_for('inventory.audit_history'))
    finally:
        conn.close()
