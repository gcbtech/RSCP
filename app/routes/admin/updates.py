"""
Admin Update System Routes
Handles version checking and GitHub-based updates.
"""
import os
import re
import logging
import tempfile
import requests
import json
import time
import uuid
from flask import request, jsonify

from app import paths
from app.routes.admin import admin_bp
from app.services.auth import BASE_DIR

logger = logging.getLogger(__name__)

# GitHub Configuration
GITHUB_REPO = "gcbtech/RSCP"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}"

# Available branches for updates
BRANCHES = {
    'stable': {'name': 'main', 'display': 'Stable', 'warning': None},
    'beta': {'name': 'beta', 'display': 'Beta', 'warning': '⚠️ Beta versions may contain bugs and unstable features.'}
}

# Shown instead of update options on a 2.x-layout install (code and data in one
# directory, no RSCP_DATA_DIR): see the 3.0 section below.
LEGACY_LAYOUT_MESSAGE = (
    "This install still uses the RSCP 2.x folder layout (code and data together in "
    f"{paths.APP_DIR}), so it cannot update itself from this page, and scheduled jobs "
    "and nightly backups are not running. On the server, run the installer once as root "
    f"to move it to the 3.x layout: sudo bash install.sh --migrate-from {paths.APP_DIR} "
    "(README: \"Migrating a 2.x install\"). Updates work from this page again afterwards."
)


def get_version_url(branch='main'):
    """Get the raw VERSION file URL for a specific branch."""
    return f"https://raw.githubusercontent.com/{GITHUB_REPO}/{branch}/VERSION"

def get_current_version():
    """Get current installed version."""
    version_file = os.path.join(BASE_DIR, 'VERSION')
    if os.path.exists(version_file):
        with open(version_file, 'r') as f:
            return f.read().strip()
    return "unknown"


def get_latest_version(branch='main'):
    """Check GitHub for latest version on a specific branch."""
    try:
        url = get_version_url(branch)
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return response.text.strip()
    except Exception as e:
        logger.error(f"Error checking latest version for {branch}: {e}")
    return None


def version_is_newer(remote_version, local_version):
    """Compare semantic versions. Returns True if remote > local.
    
    Handles versions like: 1.16.5, 1.16.2, 1.15.0
    """
    try:
        def parse_version(v):
            if not v or v == "unknown":
                return (0, 0, 0)
            parts = v.strip().split('.')
            return tuple(int(p) for p in parts[:3])
        
        remote_tuple = parse_version(remote_version)
        local_tuple = parse_version(local_version)
        
        return remote_tuple > local_tuple
    except (ValueError, AttributeError):
        return False


@admin_bp.route('/check_update')
def check_update():
    """Check if updates are available from GitHub for both branches
    (data-dir installs; a 2.x-layout install gets LEGACY_LAYOUT_MESSAGE)."""
    if paths.USES_DATA_DIR:
        try:
            return _dd_check_update()
        except Exception as e:
            logger.error(f"Update check error: {e}")
            return {"error": str(e)}, 500

    return jsonify({"error": LEGACY_LAYOUT_MESSAGE, "current_version": get_current_version(),
                    "layout": "legacy"}), 409


# The 2.x in-process updater's status file. A 2.x updater that installed this
# version left its status here; its page keeps polling /admin/update_status
# through the restart (create_app() removes the file once the new code runs).
STATUS_FILE = os.path.join(BASE_DIR, 'update_status.json')

def get_update_status():
    """Retrieve the current update status from the local status file."""
    if os.path.exists(STATUS_FILE):
        try:
            with open(STATUS_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading update status: {e}")
    return {"status": "idle"}

# =============================================================================
# 3.0 data-directory installs (install.sh layout, paths.USES_DATA_DIR)
#
# The web process runs as the unprivileged `rscp` user and can write only
# /var/lib/rscp, so it never touches code, venvs or systemd itself. Instead:
#
#   1. the Update / Roll Back buttons write update-request.json into the
#      data dir (atomically);
#   2. rscp-update.path notices and starts rscp-update.service (root), which
#      runs /opt/rscp/bin/rscp-update (install.sh): build the new release +
#      venv, back up the DB, switch /opt/rscp/current, restart, health-check
#      /healthz and roll back automatically if unhealthy;
#   3. the updater reports progress in update-status.json, which the admin
#      page polls through /admin/update_status.
#
# Legacy (2.x layout) installs: RSCP 3 does not update itself in place. The
# endpoints refuse with LEGACY_LAYOUT_MESSAGE, because code swapped into that
# layout would run without the systemd units (no scheduled jobs or backups).
# =============================================================================

UPDATE_REQUEST_NAME = 'update-request.json'
UPDATE_STATUS_NAME = 'update-status.json'
# If the request file is still there after this long, rscp-update.path is not
# running (the updater deletes the request as soon as it starts).
REQUEST_PICKUP_TIMEOUT = 120
# A run that has reported nothing for this long is treated as dead.
STALE_RUN_TIMEOUT = 45 * 60
ACTIVE_STATES = ('updating', 'restarting')


def _request_path():
    return os.path.join(paths.DATA_DIR, UPDATE_REQUEST_NAME)


def _dd_status_path():
    return os.path.join(paths.DATA_DIR, UPDATE_STATUS_NAME)


def _atomic_write_json(path, data):
    """Write JSON via temp file + rename, so readers (and the systemd path
    unit) only ever see a complete file."""
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.' + os.path.basename(path) + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)  # the root updater reads it; nothing secret inside
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _read_json(path):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def read_dd_status(now=None):
    """Current updater status for the UI, with two safety nets: a request
    nobody picked up, and a run that stopped reporting."""
    now = now or time.time()
    status = _read_json(_dd_status_path()) or {'status': 'idle'}
    status.setdefault('progress', '')
    status.setdefault('error', None)

    req = _request_path()
    if os.path.exists(req):
        try:
            age = now - os.path.getmtime(req)
        except OSError:
            age = 0
        if age > REQUEST_PICKUP_TIMEOUT:
            return dict(status, status='error', error=(
                'The updater service did not pick up the request. On the server, '
                'check: systemctl status rscp-update.path rscp-update.service'))
        return status

    if status.get('status') in ACTIVE_STATES:
        updated = status.get('updated_at') or 0
        if isinstance(updated, (int, float)) and now - updated > STALE_RUN_TIMEOUT:
            return dict(status, status='error', error=(
                'The update stopped reporting progress. Check: journalctl -u rscp-update'))
    return status


def write_update_request(action, channel=None, requested_by=None):
    """Queue an update/rollback for rscp-update.service. Returns the request."""
    req = {
        'id': uuid.uuid4().hex,
        'action': action,
        'channel': channel,
        'requested_by': requested_by,
        'requested_at': int(time.time()),
    }
    # Status first: the UI's first poll must not see a previous run's result.
    _atomic_write_json(_dd_status_path(), {
        'status': 'updating',
        'progress': 'Waiting for the updater service to start...',
        'error': None,
        'request_id': req['id'],
        'action': action,
        'updated_at': req['requested_at'],
    })
    _atomic_write_json(_request_path(), req)
    return req


def _current_username():
    try:
        from flask_login import current_user
        return current_user.username if current_user.is_authenticated else None
    except Exception:
        return None


def _dd_release_dirs():
    """(prefix, releases_root, active) for /opt/rscp/releases/<v>, or None
    when the running code is not inside a releases/ directory."""
    active = os.path.realpath(paths.APP_DIR)
    releases_root = os.path.dirname(active)
    if os.path.basename(releases_root) != 'releases':
        return None
    return os.path.dirname(releases_root), releases_root, active


def _dd_releases():
    """Installed releases: active first, then the rollback target, then the
    rest newest first. Each: {name, version, active, previous}."""
    found = _dd_release_dirs()
    if not found:
        return []
    prefix, releases_root, active = found
    prev_link = os.path.join(prefix, 'previous')
    previous = os.path.realpath(prev_link) if os.path.islink(prev_link) else None

    rels = []
    for name in os.listdir(releases_root):
        full = os.path.join(releases_root, name)
        if name.startswith('.') or not os.path.isdir(full):
            continue
        real = os.path.realpath(full)
        version = None
        try:
            with open(os.path.join(full, 'VERSION')) as f:
                version = f.read().strip()
        except OSError:
            pass
        rels.append({'name': name, 'version': version,
                     'active': real == active, 'previous': real == previous,
                     '_mtime': os.path.getmtime(full)})
    if not any(r['previous'] and not r['active'] for r in rels):
        # No valid previous link: the newest other release is the target,
        # matching install.sh --rollback.
        for r in rels:
            r['previous'] = False
        others = sorted((r for r in rels if not r['active']), key=lambda r: r['_mtime'], reverse=True)
        if others:
            others[0]['previous'] = True
    rels.sort(key=lambda r: (not r['active'], not r['previous'], -r['_mtime']))
    for r in rels:
        r.pop('_mtime')
    return rels


def latest_stable_tag():
    """Newest stable release on GitHub: the latest published release, else
    the highest vX.Y.Z tag. Returns e.g. '3.0.1', or None."""
    headers = {'Accept': 'application/vnd.github+json'}
    try:
        r = requests.get(f"{GITHUB_API_URL}/releases/latest", timeout=10, headers=headers)
        if r.status_code == 200:
            tag = (r.json() or {}).get('tag_name') or ''
            if re.fullmatch(r'v\d+\.\d+\.\d+', tag):
                return tag[1:]
        r = requests.get(f"{GITHUB_API_URL}/tags", params={'per_page': 100}, timeout=10, headers=headers)
        if r.status_code == 200:
            best = None
            for t in r.json() or []:
                m = re.fullmatch(r'v(\d+)\.(\d+)\.(\d+)', (t or {}).get('name', ''))
                if m:
                    key = tuple(int(x) for x in m.groups())
                    if best is None or key > best:
                        best = key
            if best:
                return '.'.join(str(x) for x in best)
    except Exception as e:
        logger.error(f"Error checking GitHub releases: {e}")
    return None


def _dd_check_update():
    """Same response shape as the legacy check. Stable = newest release tag
    (falling back to main's VERSION while no tags exist, as install.sh does);
    beta = the beta branch's VERSION."""
    current_version = get_current_version()
    stable_version = latest_stable_tag() or get_latest_version('main')
    beta_version = get_latest_version('beta')
    result = {"current_version": current_version, "branches": {}, "layout": "data-dir"}
    if stable_version:
        result["branches"]["stable"] = {
            "version": stable_version,
            "update_available": version_is_newer(stable_version, current_version),
            "display": "Stable",
            "warning": None,
        }
    if beta_version:
        result["branches"]["beta"] = {
            "version": beta_version,
            "update_available": True,
            "display": "Beta",
            "warning": BRANCHES['beta']['warning'],
        }
    result["latest_version"] = stable_version
    result["update_available"] = result["branches"].get("stable", {}).get("update_available", False)
    result["status"] = f"Current: {current_version}"
    return result


def _dd_perform_update():
    status = read_dd_status()
    if status.get('status') in ACTIVE_STATES:
        return jsonify({"error": "An update is already in progress."}), 400
    branch_key = request.form.get('branch', 'stable')
    if branch_key not in BRANCHES:
        return jsonify({"error": "Unknown update channel."}), 400
    try:
        req = write_update_request('upgrade', channel=branch_key, requested_by=_current_username())
    except OSError as e:
        logger.error(f"Could not write update request: {e}")
        return jsonify({"error": f"Could not queue the update: {e}"}), 500
    logger.info(f"Update to {branch_key} requested by {req['requested_by']} (request {req['id']})")
    return jsonify({"status": "success",
                    "message": "Update requested; the updater service is applying it."})


def _dd_rollback():
    status = read_dd_status()
    if status.get('status') in ACTIVE_STATES:
        return jsonify({"error": "An update is already in progress."}), 400
    target = next((r for r in _dd_releases() if r['previous'] and not r['active']), None)
    if not target:
        return jsonify({"error": "No previous release to roll back to."}), 400
    try:
        write_update_request('rollback', requested_by=_current_username())
    except OSError as e:
        return jsonify({"error": f"Could not queue the rollback: {e}"}), 500
    return jsonify({"status": "success", "message": f"Rollback to {target['name']} requested."})


def _dd_release_info():
    rels = _dd_releases()
    active = next((r['name'] for r in rels if r['active']), None)
    return jsonify({"layout": "release" if rels else "data-dir", "active": active,
                    "releases": rels})


@admin_bp.route('/update', methods=['POST'])
def perform_update():
    """Queue an update for rscp-update.service (data-dir installs only)."""
    if paths.USES_DATA_DIR:
        return _dd_perform_update()
    return jsonify({"error": LEGACY_LAYOUT_MESSAGE}), 409


@admin_bp.route('/update_status')
def update_status():
    """Get the current update progress status."""
    if paths.USES_DATA_DIR:
        return jsonify(read_dd_status())
    status = get_update_status()
    return jsonify(status)


@admin_bp.route('/rollback', methods=['POST'])
def rollback_update():
    """Roll back to the previous release (atomic-release layout only) and
    restart. Instant; note the shared DB is not reverted."""
    if paths.USES_DATA_DIR:
        return _dd_rollback()
    return jsonify({"error": LEGACY_LAYOUT_MESSAGE}), 409


@admin_bp.route('/release_info')
def release_info():
    """Report the current layout and available releases (for the admin UI)."""
    if paths.USES_DATA_DIR:
        return _dd_release_info()
    return jsonify({"layout": "legacy", "releases": []})
