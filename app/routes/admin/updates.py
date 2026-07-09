"""
Admin Update System Routes
Handles version checking and GitHub-based updates.
"""
import os
import sys
import logging
import shutil
import tempfile
import tarfile
import sqlite3
import zipfile
import subprocess
import requests
import json
import threading
import signal
import time
from flask import redirect, url_for, flash, request, jsonify

from app.routes.admin import admin_bp, require_admin
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

def get_github_zip_url(branch='main'):
    """Get the GitHub ZIP download URL for a specific branch."""
    return f"https://github.com/{GITHUB_REPO}/archive/refs/heads/{branch}.zip"

def get_version_url(branch='main'):
    """Get the raw VERSION file URL for a specific branch."""
    return f"https://raw.githubusercontent.com/{GITHUB_REPO}/{branch}/VERSION"

# Files that should never be overwritten during updates
PROTECTED_FILES = [
    'config.json',
    'rscp.db', 
    'manifest.csv',
    'app.log',
]

# Directories that should never be overwritten
PROTECTED_DIRS = [
    'venv',
    '__pycache__',
    '.pytest_cache',
]

# Entries the extracted archive MUST contain to be considered a valid RSCP
# payload. If any are missing we abort before touching the live install.
REQUIRED_PAYLOAD_ENTRIES = ['wsgi.py', 'requirements.txt', 'VERSION', 'app']

# Excluded from the pre-update code backup snapshot: bulky, regenerable, or
# user-data that the update never overwrites anyway.
BACKUP_EXCLUDE_DIRS = {
    'venv', '__pycache__', '.pytest_cache', '.git', 'backups',
    'temp', 'ignore', 'node_modules',
}

# How many timestamped backups to retain (older ones are pruned).
MAX_BACKUPS = 5

# Orphan cleanup (Tier 2): after an update, delete code files that no longer
# exist in the new version. Deliberately conservative — restricted to the
# app-owned pure-code directories and to known code extensions, so user data,
# config, uploads, and unrecognised files can never be removed.
ORPHAN_SCAN_DIRS = ('app', 'templates')
ORPHAN_EXTENSIONS = {'.py', '.html', '.js', '.css', '.map'}

# Tier 3 (atomic releases). When BASE_DIR is a symlink to a release dir, the
# updater builds a new release and atomically repoints the symlink instead of
# copying over the live tree. These shared items live once in <root>/shared and
# are symlinked into every release so they survive swaps and rollbacks.
SHARED_LINK_FILES = ('config.json', 'rscp.db', 'rscp.db-wal', 'rscp.db-shm')
SHARED_LINK_DIRS = (os.path.join('static', 'uploads'),)
MAX_RELEASES = 5


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
    """Check if updates are available from GitHub for both branches."""
    error = require_admin()
    if error:
        return {"error": "Admin required"}, 403
    
    try:
        current_version = get_current_version()
        
        # Check both stable and beta branches
        stable_version = get_latest_version('main')
        beta_version = get_latest_version('beta')
        
        result = {
            "current_version": current_version,
            "branches": {}
        }
        
        if stable_version:
            result["branches"]["stable"] = {
                "version": stable_version,
                "update_available": version_is_newer(stable_version, current_version),
                "display": "Stable",
                "warning": None
            }
        
        if beta_version:
            result["branches"]["beta"] = {
                "version": beta_version,
                "update_available": True,  # Always show beta as available for testing
                "display": "Beta",
                "warning": "⚠️ Beta versions may contain bugs and unstable features."
            }
        
        # For backwards compatibility
        result["latest_version"] = stable_version
        result["update_available"] = result["branches"].get("stable", {}).get("update_available", False)
        result["status"] = f"Current: {current_version}"
        
        return result
    except Exception as e:
        logger.error(f"Update check error: {e}")
        return {"error": str(e)}, 500


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

def set_update_status(status, progress="", error=None, version=None):
    """Write the current update status to the status file."""
    try:
        data = {
            "status": status,
            "progress": progress,
            "error": error,
            "version": version
        }
        with open(STATUS_FILE, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        logger.error(f"Error writing update status: {e}")

# =============================================================================
# Update helpers (each does one job; the orchestrator wires them together so a
# failure aborts BEFORE the service is restarted into a half-applied state).
# =============================================================================

def _validate_payload(source_dir):
    """Confirm the extracted archive looks like a real RSCP install.

    Returns (ok, new_version, missing_entries). Guards against a corrupt or
    truncated download being copied over the live tree.
    """
    missing = [e for e in REQUIRED_PAYLOAD_ENTRIES
               if not os.path.exists(os.path.join(source_dir, e))]
    new_version = None
    vf = os.path.join(source_dir, 'VERSION')
    if os.path.exists(vf):
        try:
            with open(vf) as f:
                new_version = f.read().strip()
        except Exception:
            pass
    return (len(missing) == 0), new_version, missing


def _should_exclude_from_backup(name):
    """True if a tar member (path relative to BASE_DIR) should be skipped."""
    name = name.lstrip('./')
    if not name:
        return False
    parts = name.split('/')
    # Skip if ANY path component is an excluded dir (catches nested
    # __pycache__ like app/__pycache__, not just top-level).
    if any(p in BACKUP_EXCLUDE_DIRS for p in parts):
        return True
    if name.startswith('static/uploads'):
        return True  # user uploads: large, and never overwritten by updates
    base = parts[-1]
    if base.endswith(('.db', '.db-wal', '.db-shm')) or base.endswith('.log') or '.log.' in base:
        return True
    return False


def _create_backup(old_version):
    """Snapshot the DB (consistent) and current code BEFORE any change.

    Returns the backup directory. Raises on failure — if we can't build a
    safety net we must not proceed with the update.
    """
    ts = time.strftime('%Y%m%d-%H%M%S')
    backup_root = os.path.join(BASE_DIR, 'backups')
    os.makedirs(backup_root, exist_ok=True)
    backup_dir = os.path.join(backup_root, f'{ts}_v{old_version or "unknown"}')
    os.makedirs(backup_dir, exist_ok=True)

    # 1. Database — the one-way migration on restart makes this the critical
    #    asset. Use the SQLite online-backup API for a consistent copy even
    #    while the app is live in WAL mode (a raw file copy can tear).
    db_path = os.path.join(BASE_DIR, 'rscp.db')
    if os.path.exists(db_path):
        src = sqlite3.connect(db_path)
        try:
            dst = sqlite3.connect(os.path.join(backup_dir, 'rscp.db'))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()

    # 2. Code snapshot (small once bulky/user dirs are excluded), so rollback
    #    doesn't depend on git or network. Skipped in release layout — there
    #    the previous release dir already IS the code backup.
    if not _is_release_layout():
        with tarfile.open(os.path.join(backup_dir, 'code.tar.gz'), 'w:gz') as tar:
            tar.add(BASE_DIR, arcname='.',
                    filter=lambda ti: None if _should_exclude_from_backup(ti.name) else ti)

    # 3. Manifest for humans and for rollback tooling later.
    try:
        with open(os.path.join(backup_dir, 'manifest.json'), 'w') as f:
            json.dump({'old_version': old_version,
                       'created_at': ts,
                       'base_dir': BASE_DIR}, f, indent=2)
    except Exception:
        pass

    return backup_dir


def _restore_code(backup_dir):
    """Best-effort restore of the code snapshot (used if a copy fails partway).
    The DB is protected during copy and the migration hasn't run yet, so only
    code needs restoring. Returns True on success."""
    tar_path = os.path.join(backup_dir, 'code.tar.gz')
    if not os.path.exists(tar_path):
        return False
    with tarfile.open(tar_path, 'r:gz') as tar:
        tar.extractall(BASE_DIR)
    return True


def _apply_update(source_dir):
    """Copy new files over the live tree. Returns (files_updated, failures)
    where failures is a list of (relpath, error). Protected files/dirs are
    left untouched. Unlike the old code, failures are collected — not
    swallowed — so the orchestrator can abort before restarting."""
    files_updated = 0
    failures = []
    for root, dirs, files in os.walk(source_dir):
        dirs[:] = [d for d in dirs if d not in PROTECTED_DIRS]
        rel_path = os.path.relpath(root, source_dir)
        dest_path = os.path.join(BASE_DIR, rel_path) if rel_path != '.' else BASE_DIR
        try:
            os.makedirs(dest_path, exist_ok=True)
        except Exception as e:
            failures.append((rel_path, f'mkdir failed: {e}'))
            continue
        for file in files:
            if file in PROTECTED_FILES:
                continue
            try:
                shutil.copy2(os.path.join(root, file), os.path.join(dest_path, file))
                files_updated += 1
            except Exception as e:
                failures.append((os.path.join(rel_path, file), str(e)))
    return files_updated, failures


def _remove_orphans(source_dir):
    """Delete code files that no longer exist in the new version.

    SAFETY (this is the only step that deletes from the live tree):
      - only within ORPHAN_SCAN_DIRS (app/, templates/) — pure code, no
        user data
      - only files with a known code extension (ORPHAN_EXTENSIONS)
      - only when the scan dir exists in BOTH old and new (so a malformed
        payload that omits a whole dir can never wipe it)
      - never PROTECTED_FILES / PROTECTED_DIRS
      - the pre-update backup already captured these files, so a wrong
        delete is recoverable

    Best-effort: a failure here is harmless (a dead file lingers) and never
    aborts the update. Returns the list of removed relpaths.
    """
    removed = []
    for scan_dir in ORPHAN_SCAN_DIRS:
        live_root = os.path.join(BASE_DIR, scan_dir)
        new_root = os.path.join(source_dir, scan_dir)
        if not os.path.isdir(live_root) or not os.path.isdir(new_root):
            continue
        # Bottom-up so we can prune dirs that become empty.
        for root, dirs, files in os.walk(live_root, topdown=False):
            rel_parts = os.path.relpath(root, BASE_DIR).split(os.sep)
            if any(p in PROTECTED_DIRS for p in rel_parts):
                continue
            for f in files:
                if f in PROTECTED_FILES:
                    continue
                if os.path.splitext(f)[1].lower() not in ORPHAN_EXTENSIONS:
                    continue
                live_path = os.path.join(root, f)
                rel_within = os.path.relpath(live_path, live_root)
                if not os.path.exists(os.path.join(new_root, rel_within)):
                    try:
                        os.remove(live_path)
                        removed.append(os.path.join(scan_dir, rel_within))
                    except Exception as e:
                        logger.warning(f"Could not remove orphan {live_path}: {e}")
            # Prune a now-empty directory that the new version doesn't have.
            try:
                rel_dir = os.path.relpath(root, live_root)
                if (root != live_root and not os.listdir(root)
                        and not os.path.isdir(os.path.join(new_root, rel_dir))):
                    os.rmdir(root)
            except Exception:
                pass
    return removed


def _purge_pycache():
    """Remove stale __pycache__ dirs so old .pyc can't shadow modules that were
    deleted or changed in the update. Skips the venv."""
    targets = []
    for root, dirs, files in os.walk(BASE_DIR):
        if 'venv' in root.split(os.sep):
            dirs[:] = []
            continue
        if '__pycache__' in dirs:
            targets.append(os.path.join(root, '__pycache__'))
    removed = 0
    for t in targets:
        try:
            shutil.rmtree(t)
            removed += 1
        except Exception as e:
            logger.warning(f"Could not remove {t}: {e}")
    return removed


def _install_dependencies(base_dir=None):
    """Install requirements. Returns (ok, message).

    Uses the venv pip when present, otherwise the running interpreter — so
    system-Python deployments (no venv) actually get their deps installed
    instead of silently skipping the step. The return code IS checked.

    On Debian/PEP 668 "externally-managed" system Pythons (our LXC), a plain
    system pip install is refused; we retry with --break-system-packages,
    which is what an admin does by hand there. A genuinely bad/unresolvable
    requirement still fails (and aborts the update before restart).
    """
    base = base_dir or BASE_DIR
    req = os.path.join(base, 'requirements.txt')
    if not os.path.exists(req):
        return True, 'no requirements.txt'
    venv_pip = os.path.join(base, 'venv', 'bin', 'pip')
    if os.path.exists(venv_pip):
        base_cmd = [venv_pip, 'install', '-r', 'requirements.txt', '-q']
    else:
        base_cmd = [sys.executable, '-m', 'pip', 'install', '-r', 'requirements.txt', '-q']

    def _run(cmd):
        return subprocess.run(cmd, cwd=base, capture_output=True, timeout=300, text=True)

    try:
        result = _run(base_cmd)
        if result.returncode == 0:
            return True, 'ok'
        combined = (result.stderr or '') + (result.stdout or '')
        if 'externally-managed-environment' in combined or 'externally managed' in combined:
            # PEP 668 refusal on a no-venv system Python — retry allowing it.
            result2 = _run(base_cmd + ['--break-system-packages'])
            if result2.returncode == 0:
                return True, 'ok (system site-packages)'
            return False, ((result2.stderr or '') + (result2.stdout or '') or 'pip failed')[-600:]
        return False, (combined or 'pip returned non-zero')[-600:]
    except Exception as e:
        return False, str(e)


def _prune_backups(keep=MAX_BACKUPS):
    """Keep only the most recent `keep` backups to bound disk usage."""
    backup_root = os.path.join(BASE_DIR, 'backups')
    if not os.path.isdir(backup_root):
        return
    entries = [os.path.join(backup_root, d) for d in os.listdir(backup_root)
               if os.path.isdir(os.path.join(backup_root, d))]
    entries.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    for old in entries[keep:]:
        try:
            shutil.rmtree(old)
        except Exception as e:
            logger.warning(f"Could not prune backup {old}: {e}")


def _restart_service():
    """Restart the service. systemctl if privileged (dev/prod LXC has no sudo),
    otherwise fall back to signalling gunicorn so systemd's Restart=always
    respawns us on the new code."""
    logger.info("Initiating RSCP service restart...")
    try:
        subprocess.Popen(['sudo', 'systemctl', 'restart', 'rscp'])
        time.sleep(1)
    except Exception as e:
        logger.warning(f"systemctl restart unavailable: {e}")
    try:
        ppid = os.getppid()
        if ppid > 1:
            os.kill(ppid, signal.SIGTERM)
            time.sleep(1)
    except Exception as e:
        logger.warning(f"Could not signal gunicorn parent: {e}")
    os.kill(os.getpid(), signal.SIGTERM)


# =============================================================================
# Tier 3 — atomic release helpers (active only when BASE_DIR is a symlink)
# =============================================================================

def _is_release_layout():
    """True when the app dir is a symlink to a release (post-migration)."""
    try:
        return os.path.islink(BASE_DIR)
    except Exception:
        return False


def _release_paths():
    """Return (releases_root, shared_dir) derived from the active release."""
    active = os.path.realpath(BASE_DIR)
    releases_root = os.path.dirname(active)
    rscp_root = os.path.dirname(releases_root)
    return releases_root, os.path.join(rscp_root, 'shared')


def _link_shared_into(release_dir, shared_dir):
    """Point config/db/uploads inside a freshly built release at the single
    shared copy, so state survives release swaps and rollbacks."""
    def _clear(path):
        if os.path.islink(path):
            os.remove(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)

    for name in SHARED_LINK_FILES:
        target = os.path.join(shared_dir, name)
        link = os.path.join(release_dir, name)
        _clear(link)
        if os.path.exists(target):
            os.symlink(target, link)

    for rel in SHARED_LINK_DIRS:
        target = os.path.join(shared_dir, rel)
        link = os.path.join(release_dir, rel)
        _clear(link)
        os.makedirs(os.path.dirname(link), exist_ok=True)
        if os.path.exists(target):
            os.symlink(target, link)


def _atomic_point(target, link_path):
    """Atomically make link_path a symlink to target, replacing any existing
    symlink. os.replace() over a symlink is an atomic rename — no window where
    the app path is missing."""
    tmp = link_path + '.swap'
    if os.path.islink(tmp) or os.path.exists(tmp):
        os.remove(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, link_path)


def _prune_releases(keep=MAX_RELEASES):
    """Keep the newest `keep` releases plus the active one; delete the rest."""
    releases_root, _ = _release_paths()
    active = os.path.realpath(BASE_DIR)
    if not os.path.isdir(releases_root):
        return
    dirs = [os.path.join(releases_root, d) for d in os.listdir(releases_root)
            if os.path.isdir(os.path.join(releases_root, d))]
    dirs.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    keep_set = set(dirs[:keep])
    for d in dirs:
        if d in keep_set or os.path.realpath(d) == active:
            continue
        try:
            shutil.rmtree(d)
        except Exception as e:
            logger.warning(f"Could not prune release {d}: {e}")


def rollback_to_previous():
    """Repoint the app symlink to the previous release. Returns (ok, message).
    Instant for code; note the shared DB is NOT reverted (restore a DB backup
    too if a destructive migration ran)."""
    if not _is_release_layout():
        return False, "Not in release layout; rollback is unavailable."
    releases_root, _ = _release_paths()
    active = os.path.realpath(BASE_DIR)
    others = [os.path.join(releases_root, d) for d in os.listdir(releases_root)
              if os.path.isdir(os.path.join(releases_root, d))
              and os.path.realpath(os.path.join(releases_root, d)) != active]
    if not others:
        return False, "No previous release to roll back to."
    others.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    prev = others[0]
    _atomic_point(prev, BASE_DIR)
    logger.info(f"Rolled back active release to {prev}")
    return True, f"Rolled back to {os.path.basename(prev)}."


def _apply_flat_update(source_dir, old_version, new_version, backup_dir):
    """Flat layout (pre-migration): copy new files over the live tree, remove
    orphans, purge bytecode, install deps. Raises before restart on failure;
    a partial copy is auto-restored from the backup."""
    # Apply files, collecting any failures
    set_update_status("updating", f"Installing files (v{old_version} → v{new_version})...")
    files_updated, failures = _apply_update(source_dir)
    if failures:
        detail = '; '.join(f"{p}: {e}" for p, e in failures[:5])
        restored = False
        try:
            restored = _restore_code(backup_dir)
        except Exception as re:
            logger.error(f"Auto-restore failed: {re}")
        raise Exception(
            f"{len(failures)} file(s) failed to copy ({detail}). "
            f"{'Previous version auto-restored; ' if restored else 'AUTO-RESTORE FAILED — '}"
            f"service NOT restarted. Backup: {backup_dir}")
    logger.info(f"Updated {files_updated} files")

    # Remove obsolete files, clear bytecode
    set_update_status("updating", "Removing obsolete files...")
    orphans = _remove_orphans(source_dir)
    if orphans:
        logger.info(f"Removed {len(orphans)} obsolete file(s): {', '.join(orphans[:10])}")
    set_update_status("updating", "Clearing cached bytecode...")
    _purge_pycache()

    # Dependencies — abort before restart if they fail
    set_update_status("updating", "Checking dependencies...")
    dep_ok, dep_msg = _install_dependencies()
    if not dep_ok:
        raise Exception(
            f"Dependency install failed: {dep_msg} — service NOT restarted "
            f"to avoid a broken boot. Backup: {backup_dir}")


def _apply_release_update(source_dir, old_version, new_version, backup_dir):
    """Release layout: build a brand-new release dir, link shared state in,
    install deps, then ATOMICALLY repoint the app symlink. Nothing live is
    touched until the final atomic swap, so any earlier failure leaves the
    current release active and just discards the half-built one."""
    releases_root, shared_dir = _release_paths()
    ts = time.strftime('%Y%m%d-%H%M%S')
    new_release = os.path.join(releases_root, f'{ts}_v{new_version or "unknown"}')

    set_update_status("updating", f"Building release {os.path.basename(new_release)}...")
    shutil.copytree(source_dir, new_release)

    # Point config/db/uploads at the shared copies
    _link_shared_into(new_release, shared_dir)

    # Dependencies installed against the NEW release; on failure discard it
    set_update_status("updating", "Checking dependencies...")
    dep_ok, dep_msg = _install_dependencies(base_dir=new_release)
    if not dep_ok:
        shutil.rmtree(new_release, ignore_errors=True)
        raise Exception(
            f"Dependency install failed: {dep_msg} — new release discarded, "
            f"current release still active (nothing changed). Backup: {backup_dir}")

    # Atomic activation — the single moment the live app changes
    set_update_status("updating", "Activating new release...")
    _atomic_point(new_release, BASE_DIR)
    logger.info(f"Activated release {new_release}")
    _prune_releases(MAX_RELEASES)


def run_update_in_background(branch_name, branch_info):
    """Download, validate, back up, apply, and restart — in that order, so the
    service is only ever restarted after a fully successful, validated update.
    Any failure aborts before the restart and (for a partial copy) restores the
    previous code from the just-made backup."""
    set_update_status("updating", "Starting update process...")
    old_version = get_current_version()
    backup_dir = None

    try:
        # 1. Download
        set_update_status("updating", f"Downloading update from GitHub ({branch_info['display']})...")
        zip_url = get_github_zip_url(branch_name)
        logger.info(f"Downloading update from GitHub ({branch_name} branch)...")
        response = requests.get(zip_url, timeout=60, stream=True)
        if response.status_code != 200:
            raise Exception(f"Failed to download update: HTTP {response.status_code}")

        with tempfile.NamedTemporaryFile(delete=False, suffix='.zip') as tmp_file:
            for chunk in response.iter_content(chunk_size=8192):
                tmp_file.write(chunk)
            tmp_zip_path = tmp_file.name

        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                # 2. Extract (into temp — nothing live touched yet)
                set_update_status("updating", "Extracting update archive...")
                with zipfile.ZipFile(tmp_zip_path, 'r') as zip_ref:
                    zip_ref.extractall(tmp_dir)
                extracted_dirs = [d for d in os.listdir(tmp_dir)
                                  if os.path.isdir(os.path.join(tmp_dir, d))]
                if not extracted_dirs:
                    raise Exception("Update failed: invalid archive structure")
                source_dir = os.path.join(tmp_dir, extracted_dirs[0])

                # 3. Validate the payload BEFORE touching the live install
                ok, new_version, missing = _validate_payload(source_dir)
                if not ok:
                    raise Exception(
                        f"Downloaded update looks incomplete (missing: {', '.join(missing)}). "
                        f"Aborted — your installation was not modified.")

                # 4. Back up (the safety net). DB always; code tar only in the
                #    flat layout. If this fails we stop.
                set_update_status("updating", "Backing up database and current version...")
                backup_dir = _create_backup(old_version)
                logger.info(f"Pre-update backup created at {backup_dir}")

                # 5. Apply — atomic release swap if migrated, else in-place copy
                if _is_release_layout():
                    _apply_release_update(source_dir, old_version, new_version, backup_dir)
                else:
                    _apply_flat_update(source_dir, old_version, new_version, backup_dir)
        finally:
            if os.path.exists(tmp_zip_path):
                os.unlink(tmp_zip_path)

        # 9. Prune old backups
        _prune_backups(MAX_BACKUPS)

        new_version = get_current_version()
        logger.info(f"RSCP updated from {old_version} to {new_version}")

        # 10. Restart — reached ONLY after a fully validated, successful update
        set_update_status("restarting",
                          f"Update successful ({old_version} → {new_version})! Restarting services...",
                          version=new_version)
        time.sleep(2)
        _restart_service()

    except Exception as e:
        logger.error(f"Update background error: {e}")
        msg = str(e)
        if backup_dir and 'Backup:' not in msg:
            msg += f" | Backup available at: {backup_dir}"
        set_update_status("error", error=msg)


@admin_bp.route('/update', methods=['POST'])
def perform_update():
    """Download and apply updates from GitHub asynchronously."""
    error = require_admin()
    if error:
        return jsonify({"error": "Admin required"}), 403
    
    # Check if an update is already in progress
    status = get_update_status()
    if status.get("status") == "updating":
        return jsonify({"error": "An update is already in progress."}), 400
        
    # Get branch from request (defaults to stable)
    branch_key = request.form.get('branch', 'stable')
    branch_info = BRANCHES.get(branch_key, BRANCHES['stable'])
    branch_name = branch_info['name']
    
    # Spawn background worker thread
    thread = threading.Thread(
        target=run_update_in_background,
        args=(branch_name, branch_info),
        daemon=True
    )
    thread.start()
    
    return jsonify({"status": "success", "message": "Update initiated in the background."})


@admin_bp.route('/update_status')
def update_status():
    """Get the current update progress status."""
    error = require_admin()
    if error:
        return jsonify({"error": "Admin required"}), 403

    status = get_update_status()
    return jsonify(status)


@admin_bp.route('/rollback', methods=['POST'])
def rollback_update():
    """Roll back to the previous release (atomic-release layout only) and
    restart. Instant; note the shared DB is not reverted."""
    error = require_admin()
    if error:
        return jsonify({"error": "Admin required"}), 403

    if not _is_release_layout():
        return jsonify({"error": "Rollback requires the atomic-release layout."}), 400

    ok, message = rollback_to_previous()
    if not ok:
        return jsonify({"error": message}), 400

    def _rollback_and_restart():
        set_update_status("restarting", f"{message} Restarting services...")
        time.sleep(2)
        _restart_service()
    threading.Thread(target=_rollback_and_restart, daemon=True).start()
    return jsonify({"status": "success", "message": message})


@admin_bp.route('/release_info')
def release_info():
    """Report the current layout and available releases (for the admin UI)."""
    error = require_admin()
    if error:
        return jsonify({"error": "Admin required"}), 403

    if not _is_release_layout():
        return jsonify({"layout": "flat", "releases": []})
    try:
        releases_root, _ = _release_paths()
        active = os.path.realpath(BASE_DIR)
        rels = []
        for d in sorted(os.listdir(releases_root), reverse=True):
            full = os.path.join(releases_root, d)
            if os.path.isdir(full):
                rels.append({"name": d, "active": os.path.realpath(full) == active})
        return jsonify({"layout": "release", "active": os.path.basename(active), "releases": rels})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
