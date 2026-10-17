from flask import render_template, request, flash, redirect, url_for, current_app
from app.routes.admin import admin_bp, require_admin, load_config
from app.services.data_manager import save_config

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
            
            # Domain allowlist (comma separated -> list of '@domain')
            domains_str = request.form.get('sso_allowed_domains', '').strip()
            allowed_domains = []
            for d in domains_str.split(','):
                d = d.strip().lower()
                if d:
                    allowed_domains.append(d if d.startswith('@') else '@' + d)

            allow_new = request.form.get('sso_allow_new_users') == 'on'

            # SSO without a domain allowlist would admit any account the
            # identity provider vouches for (e.g. any Google account).
            if sso_enabled and not allowed_domains:
                flash('SSO needs at least one allowed domain before it can be enabled.', 'error')
                return redirect(url_for('admin.admin_panel', tab='settings'))

            updates = {
                'SSO_ENABLED': sso_enabled,
                'SSO_CLIENT_ID': client_id,
                'SSO_DISCOVERY_URL': discovery_url,
                'SSO_ALLOWED_DOMAINS': allowed_domains,
                'SSO_ALLOW_NEW_USERS': allow_new,
            }
            # The secret is never sent to the browser, so a blank field means
            # "keep the current one".
            if client_secret:
                updates['SSO_CLIENT_SECRET'] = client_secret
            save_config(updates)
            
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
