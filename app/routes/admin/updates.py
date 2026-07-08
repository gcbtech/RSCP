"""
Admin Update System Routes
Handles version checking and GitHub-based updates.
"""
import os
import logging
import shutil
import tempfile
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

def run_update_in_background(branch_name, branch_info):
    """Thread target that downloads the update, copies files, updates deps, and restarts."""
    set_update_status("updating", "Starting update process...")
    old_version = get_current_version()
    
    try:
        # 1. Download
        set_update_status("updating", f"Downloading update ZIP from GitHub ({branch_info['display']})...")
        zip_url = get_github_zip_url(branch_name)
        logger.info(f"Downloading update from GitHub ({branch_name} branch)...")
        response = requests.get(zip_url, timeout=60, stream=True)
        if response.status_code != 200:
            raise Exception(f"Failed to download update: HTTP {response.status_code}")
        
        # Save ZIP
        with tempfile.NamedTemporaryFile(delete=False, suffix='.zip') as tmp_file:
            for chunk in response.iter_content(chunk_size=8192):
                tmp_file.write(chunk)
            tmp_zip_path = tmp_file.name
            
        try:
            # 2. Extract
            set_update_status("updating", "Extracting update archive...")
            with tempfile.TemporaryDirectory() as tmp_dir:
                with zipfile.ZipFile(tmp_zip_path, 'r') as zip_ref:
                    zip_ref.extractall(tmp_dir)
                
                extracted_dirs = [d for d in os.listdir(tmp_dir) if os.path.isdir(os.path.join(tmp_dir, d))]
                if not extracted_dirs:
                    raise Exception("Update failed: Invalid archive structure")
                
                source_dir = os.path.join(tmp_dir, extracted_dirs[0])
                
                # 3. Copy files
                set_update_status("updating", "Copying updated files to application...")
                files_updated = 0
                for root, dirs, files in os.walk(source_dir):
                    dirs[:] = [d for d in dirs if d not in PROTECTED_DIRS]
                    rel_path = os.path.relpath(root, source_dir)
                    dest_path = os.path.join(BASE_DIR, rel_path) if rel_path != '.' else BASE_DIR
                    
                    if not os.path.exists(dest_path):
                        os.makedirs(dest_path)
                    
                    for file in files:
                        if file in PROTECTED_FILES:
                            continue
                        
                        src_file = os.path.join(root, file)
                        dst_file = os.path.join(dest_path, file)
                        
                        try:
                            shutil.copy2(src_file, dst_file)
                            files_updated += 1
                        except Exception as e:
                            logger.warning(f"Could not update {file}: {e}")
                            
                logger.info(f"Updated {files_updated} files")
                
        finally:
            if os.path.exists(tmp_zip_path):
                os.unlink(tmp_zip_path)
                
        # 4. Install requirements
        set_update_status("updating", "Checking and installing dependencies...")
        venv_pip = os.path.join(BASE_DIR, 'venv', 'bin', 'pip')
        if os.path.exists(venv_pip):
            subprocess.run(
                [venv_pip, 'install', '-r', 'requirements.txt', '-q'],
                cwd=BASE_DIR,
                capture_output=True,
                timeout=120
            )
            
        new_version = get_current_version()
        logger.info(f"RSCP updated from {old_version} to {new_version}")
        
        # 5. Success -> Restarting status
        set_update_status("restarting", f"Update successful ({old_version} → {new_version})! Restarting services...", version=new_version)
        
        # Sleep for a bit to allow the frontend to fetch the restarting status
        time.sleep(2)
        
        # 6. Trigger Restart
        # First, try cleanly restarting via systemd
        logger.info("Initiating RSCP service restart...")
        try:
            # Run asynchronously so that we don't block or hang
            subprocess.Popen(['sudo', 'systemctl', 'restart', 'rscp'])
            time.sleep(1)
        except Exception as e:
            logger.warning(f"Failed to run sudo systemctl restart rscp: {e}")
            
        # Fallback 1: Terminate the Gunicorn parent process
        try:
            ppid = os.getppid()
            if ppid > 1:
                logger.info(f"Sending SIGTERM to parent Gunicorn process {ppid}")
                os.kill(ppid, signal.SIGTERM)
                time.sleep(1)
        except Exception as e:
            logger.warning(f"Failed to kill Gunicorn parent: {e}")
            
        # Fallback 2: Terminate our own process
        logger.info(f"Sending SIGTERM to current process {os.getpid()}")
        os.kill(os.getpid(), signal.SIGTERM)
        
    except Exception as e:
        logger.error(f"Update background error: {e}")
        set_update_status("error", error=str(e))


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
