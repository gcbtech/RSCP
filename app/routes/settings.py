"""
The one route that saves settings: POST /admin/settings/save.

Form posts carry `group` (a group from app.services.settings.GROUPS), the
setting keys as field names, and optionally `next` (a local URL to return
to). JSON posts send {"group": ..., "values": {KEY: value}} and get JSON back.

Each setting declares the permission needed to change it (POS settings need
pos.manage, most others admin), so this blueprint deliberately sits outside
the admin blueprint and does its own per-setting checks.
"""
import logging

from flask import Blueprint, request, redirect, url_for, flash, jsonify, abort
from flask_login import current_user

from app.services import settings as settings_store
from app.services.settings import SettingsError

logger = logging.getLogger(__name__)

settings_bp = Blueprint('settings', __name__, url_prefix='/admin/settings')


def _return_url(group):
    nxt = request.form.get('next') or request.args.get('next') or ''
    if nxt.startswith('/') and not nxt.startswith(('//', '/\\')):
        return nxt
    tab = settings_store.GROUPS.get(group, (None, None))[1]
    return url_for('admin.admin_panel', tab=tab) if tab else url_for('admin.admin_panel')


def save_settings_from_request(group=None):
    """Validate and save one settings group from the current request."""
    as_json = request.is_json
    if as_json:
        data = request.get_json(silent=True) or {}
        group = group or data.get('group')
        source = data.get('values') or {}
    else:
        group = group or request.form.get('group')
        source = request.form

    def fail(message, status):
        if as_json:
            return jsonify({'success': False, 'error': message}), status
        if status == 403:
            abort(403, description=message)
        flash(message, 'error')
        return redirect(_return_url(group))

    if not current_user.is_authenticated:
        return fail('Please log in.', 401)
    if group not in settings_store.GROUPS:
        return fail('Unknown settings group.', 400)

    # The caller must be allowed to edit this group at all, and every setting
    # it actually changes.
    group_keys = [k for k, s in settings_store.settings_in_group(group) if s.editable]
    if not any(settings_store.user_can_edit(current_user, k) for k in group_keys):
        logger.warning(f"Settings: {current_user.username} denied for group '{group}'")
        return fail('You do not have permission to change these settings.', 403)

    try:
        updates = settings_store.parse_submission(group, source, from_form=not as_json)
    except SettingsError as e:
        return fail(str(e), 400)

    denied = [k for k in updates if not settings_store.user_can_edit(current_user, k)]
    if denied:
        logger.warning(f"Settings: {current_user.username} denied for {denied}")
        return fail('You do not have permission to change these settings.', 403)

    try:
        settings_store.set_many(updates, user=current_user.username)
    except SettingsError as e:
        return fail(str(e), 400)

    restart = sorted({settings_store.SETTINGS[k].label for k in updates if settings_store.SETTINGS[k].restart})
    logger.info(f"Settings: {current_user.username} saved {sorted(updates)}")
    if as_json:
        return jsonify({'success': True, 'saved': sorted(updates), 'restart_required': bool(restart)})
    flash('Settings saved.', 'success')
    if restart:
        flash(f"Restart RSCP for these changes to take effect: {', '.join(restart)}.", 'warning')
    return redirect(_return_url(group))


@settings_bp.route('/save', methods=['POST'])
def save():
    return save_settings_from_request()
