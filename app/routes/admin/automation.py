"""
Admin Automation Routes
Handles settings for email ingestion and other automation features.
"""
import logging
from flask import request, redirect, url_for, flash
from app.routes.admin import admin_bp, require_admin, save_config_value

logger = logging.getLogger(__name__)

@admin_bp.route('/save_automation', methods=['POST'])
def save_automation():
    """Save email automation/ingestion settings."""
    error = require_admin()
    if error:
        return error
        
    imap_server = request.form.get('imap_server', '').strip()
    email_user = request.form.get('email_user', '').strip()
    email_pass = request.form.get('email_pass', '').strip()
    ingest_enabled = request.form.get('ingest_enabled') == 'on'
    
    updates = {
        'IMAP_SERVER': imap_server,
        'EMAIL_USER': email_user,
        'EMAIL_INGEST_ENABLED': ingest_enabled
    }
    
    # Only update password if provided (don't overwrite with empty string if user left it blank)
    if email_pass:
        updates['EMAIL_PASS'] = email_pass
        
    try:
        for key, value in updates.items():
            save_config_value(key, value)
            
        flash("Automation settings saved successfully.")
    except Exception as e:
        logger.error(f"Save Automation Error: {e}")
        flash(f"Error saving settings: {e}")
        
    # The Automation card lives in the admin page's Settings tab.
    return redirect(url_for('admin.admin_panel', tab='settings'))

@admin_bp.route('/run_email_ingest', methods=['POST'])
def run_email_ingest():
    """Manually trigger email ingest."""
    error = require_admin()
    if error: return error
    
    from app.services.data_manager import sync_email_ingest
    result = sync_email_ingest()
    
    if result['status'] == 'success':
        flash(f"Success! {result['message']}", "success")
    elif result['status'] == 'skipped':
        flash(f"Skipped: {result['message']}", "warning")
    else:
        flash(f"Error: {result['message']}", "danger")
        
    return redirect(url_for('admin.admin_panel', tab='settings'))
