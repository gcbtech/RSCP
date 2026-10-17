"""
Admin Automation Routes
Manual email ingest trigger. (Its settings are saved by app/routes/settings.py.)
"""
import logging
from flask import request, redirect, url_for, flash
from app.routes.admin import admin_bp, require_admin

logger = logging.getLogger(__name__)

@admin_bp.route('/run_email_ingest', methods=['POST'])
def run_email_ingest():
    """Manually trigger email ingest."""
    from app.services.data_manager import sync_email_ingest
    result = sync_email_ingest()
    
    if result['status'] == 'success':
        flash(f"Success! {result['message']}", "success")
    elif result['status'] == 'skipped':
        flash(f"Skipped: {result['message']}", "warning")
    else:
        flash(f"Error: {result['message']}", "danger")
        
    return redirect(url_for('admin.admin_panel', tab='settings'))
