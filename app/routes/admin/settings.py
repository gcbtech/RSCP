from flask import render_template, request, flash, redirect, url_for, current_app
from app.routes.admin import admin_bp, require_admin, save_config_value, load_config

@admin_bp.route('/settings', methods=['GET', 'POST'])
def settings():
    # 1. Auth Check
    auth_err = require_admin()
    if auth_err: return auth_err

    # 2. Handle Save
    if request.method == 'POST':
        try:
            # SSO Settings
            sso_enabled = request.form.get('sso_enabled') == 'on'
            client_id = request.form.get('sso_client_id', '').strip()
            client_secret = request.form.get('sso_client_secret', '').strip()
            discovery_url = request.form.get('sso_discovery_url', '').strip()
            
            # Domain Whitelist (comma separated -> list)
            domains_str = request.form.get('sso_allowed_domains', '').strip()
            allowed_domains = [d.strip() for d in domains_str.split(',') if d.strip()]
            
            allow_new = request.form.get('sso_allow_new_users') == 'on'
            
            # Save to Config
            save_config_value('SSO_ENABLED', sso_enabled)
            save_config_value('SSO_CLIENT_ID', client_id)
            save_config_value('SSO_CLIENT_SECRET', client_secret)
            save_config_value('SSO_DISCOVERY_URL', discovery_url)
            save_config_value('SSO_ALLOWED_DOMAINS', allowed_domains)
            save_config_value('SSO_ALLOW_NEW_USERS', allow_new)
            
            flash('Settings saved successfully.', 'success')
            
            # Re-register OAuth client if config changed
            # (Crude reload mechanism: just clear property logic? No, need to re-init)
            # In production, usually requires restart, but we can try to re-register on next request
            # by clearing current registration if possible. Authlib doesn't make this easy.
            # Ideally, we ask user to restart, or we rely on the fact that app.oauth reads keys?
            # Actually, `authlib` usually reads keys on request if using `fetch_token`, but `register` is valid at init.
            # We will just flash a message: "Restart required for SSO changes to take effect"
            flash('Note: You may need to restart the server for SSO changes to apply.', 'warning')
            
            return redirect(url_for('admin.admin_panel', tab='settings'))
            
        except Exception as e:
            flash(f"Error saving settings: {e}", "error")
            return redirect(url_for('admin.admin_panel', tab='settings'))

    # 3. Redirect to Admin Panel (GET)
    return redirect(url_for('admin.admin_panel', tab='settings'))
