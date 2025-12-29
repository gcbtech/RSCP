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
from flask import redirect, url_for, flash

from app.routes.admin import admin_bp, require_admin
from app.services.auth import BASE_DIR

logger = logging.getLogger(__name__)

# GitHub Configuration
GITHUB_REPO = "gcbtech/RSCP"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}"
GITHUB_ZIP_URL = f"https://github.com/{GITHUB_REPO}/archive/refs/heads/main.zip"

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


def get_latest_version():
    """Check GitHub for latest version."""
    try:
        url = f"https://raw.githubusercontent.com/{GITHUB_REPO}/main/VERSION"
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return response.text.strip()
    except Exception as e:
        logger.error(f"Error checking latest version: {e}")
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
    """Check if updates are available from GitHub."""
    error = require_admin()
    if error:
        return {"error": "Admin required"}, 403
    
    try:
        current_version = get_current_version()
        latest_version = get_latest_version()
        
        if not latest_version:
            return {"error": "Could not check for updates. Please try again later."}, 500
        
        update_available = version_is_newer(latest_version, current_version)
        
        return {
            "current_version": current_version,
            "latest_version": latest_version,
            "update_available": update_available,
            "status": f"Current: {current_version}, Latest: {latest_version}"
        }
    except Exception as e:
        logger.error(f"Update check error: {e}")
        return {"error": str(e)}, 500


@admin_bp.route('/update', methods=['POST'])
def perform_update():
    """Download and apply latest updates from GitHub."""
    error = require_admin()
    if error:
        flash("Admin access required")
        return redirect(url_for('admin.admin_panel'))
    
    try:
        old_version = get_current_version()
        
        # Download ZIP from GitHub
        logger.info("Downloading update from GitHub...")
        response = requests.get(GITHUB_ZIP_URL, timeout=60, stream=True)
        if response.status_code != 200:
            flash(f"Failed to download update: HTTP {response.status_code}")
            return redirect(url_for('admin.admin_panel'))
        
        # Save to temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.zip') as tmp_file:
            for chunk in response.iter_content(chunk_size=8192):
                tmp_file.write(chunk)
            tmp_zip_path = tmp_file.name
        
        try:
            # Extract to temp directory
            with tempfile.TemporaryDirectory() as tmp_dir:
                with zipfile.ZipFile(tmp_zip_path, 'r') as zip_ref:
                    zip_ref.extractall(tmp_dir)
                
                # Find the extracted folder (GitHub adds -main suffix)
                extracted_dirs = [d for d in os.listdir(tmp_dir) if os.path.isdir(os.path.join(tmp_dir, d))]
                if not extracted_dirs:
                    flash("Update failed: Invalid archive structure")
                    return redirect(url_for('admin.admin_panel'))
                
                source_dir = os.path.join(tmp_dir, extracted_dirs[0])
                
                # Copy files, skipping protected ones
                files_updated = 0
                for root, dirs, files in os.walk(source_dir):
                    # Skip protected directories
                    dirs[:] = [d for d in dirs if d not in PROTECTED_DIRS]
                    
                    rel_path = os.path.relpath(root, source_dir)
                    dest_path = os.path.join(BASE_DIR, rel_path) if rel_path != '.' else BASE_DIR
                    
                    # Create directory if needed
                    if not os.path.exists(dest_path):
                        os.makedirs(dest_path)
                    
                    for file in files:
                        # Skip protected files
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
            # Clean up temp zip file
            if os.path.exists(tmp_zip_path):
                os.unlink(tmp_zip_path)
        
        # Install any new dependencies
        venv_pip = os.path.join(BASE_DIR, 'venv', 'bin', 'pip')
        if os.path.exists(venv_pip):
            subprocess.run(
                [venv_pip, 'install', '-r', 'requirements.txt', '-q'],
                cwd=BASE_DIR,
                capture_output=True,
                timeout=120
            )
        
        new_version = get_current_version()
        
        flash(f"Update successful! {old_version} → {new_version}. Please restart the service for changes to take effect.")
        logger.info(f"RSCP updated from {old_version} to {new_version}")
        
    except requests.RequestException as e:
        logger.error(f"Download error: {e}")
        flash(f"Failed to download update: {str(e)}")
    except Exception as e:
        logger.error(f"Update error: {e}")
        flash(f"Update failed: {str(e)}")
    
    return redirect(url_for('admin.admin_panel'))
