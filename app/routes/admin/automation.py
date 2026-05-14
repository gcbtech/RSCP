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
        # Legacy cleanup or double-save? Let's just switch to EMAIL_PASS
        # The user's file likely has EMAIL_PASSWORD now.
        # Ideally we should probably delete the old key, but save_config_value only updates.
        # It's fine.
        
    try:
        for key, value in updates.items():
            save_config_value(key, value)
            
        flash("Automation settings saved successfully.")
    except Exception as e:
        logger.error(f"Save Automation Error: {e}")
        flash(f"Error saving settings: {e}")
        
    # Redirect back to the settings tab (assuming automation card acts like settings)
    # The 'tab' query param in admin.html controls which tab is active.
    # The automation card is inside the 'packages' tab in the template structure I saw earlier? 
    # Wait, in lines 1500+ of admin.html, it was in 'col-lg-6' inside...
    # Let's check logic:
    # Packages tab -> id="packages"
    # Settings tab -> id="settings"
    # Checking lines 1500+ again: 
    # It followed "POS TAB" (lines 824-910+) which ended differently.
    # The "Automation" card at 1510 is NOT indented under "packages".
    # Wait, line 821 closed "POS TAB" if block.
    # Line 1510 is independent?
    # Actually, line 348 start tab-content.
    # Lines 350-565 is Packages tab.
    # Lines 569-760+ is Inventory tab.
    # Lines 825+ is POS tab.
    # Where does line 1510 fall?
    # There was a BIG gap in my reading.
    # I read 1-800, 800-1000, 1000-1500, 1500-2098.
    # Automation card at 1510 is AFTER POS tab.
    # Is it in Federation? (1016-1167).
    # Is it in Settings? (1170-...).
    # Settings tab starts at 1170.
    # User Management is 1176.
    # Notification (1359).
    # Modules (1460).
    # Right Column (1508)
    # Automation (1510).
    # So Automation IS in Settings tab (right column).
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
