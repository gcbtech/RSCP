#!/usr/bin/env python3
"""
Reverse the atomic-release layout back to a plain flat install.

Handles both the one-level layout (<APP> -> releases/<x>) and the two-level
layout (<APP> -> <DATA>/current -> releases/<x>). Move-based, idempotent.

    AFTER:  <APP>/   (real dir again: code + config.json + rscp.db + static/uploads)

RUN AS ROOT, WITH THE SERVICE STOPPED:

    systemctl stop rscp
    python3 <APP>/scripts/unmigrate_releases.py
    systemctl start rscp        # (or re-run migrate_to_releases.py first)
"""
import os
import sys
import shutil

APP = os.environ.get('RSCP_APP') or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = APP.rstrip('/')

SHARED_FILES = ['config.json', 'rscp.db', 'rscp.db-wal', 'rscp.db-shm']
SHARED_DIRS = [os.path.join('static', 'uploads')]


def main():
    print(f"App dir: {APP}")

    if not os.path.islink(APP):
        print(f"{APP} is not a symlink — already a flat install. Nothing to do.")
        return

    active = os.path.realpath(APP)             # .../releases/<ts>
    releases_root = os.path.dirname(active)    # .../releases
    data_root = os.path.dirname(releases_root) # <DATA> (or /opt/rscp for old layout)
    shared = os.path.join(data_root, 'shared')
    current = os.path.join(data_root, 'current')

    if os.path.basename(releases_root) != 'releases' or not os.path.isdir(active):
        print(f"ERROR: {APP} -> {active} does not look like a release layout. Aborting.")
        sys.exit(1)

    # 1. Drop the symlink(s) that make <APP> point at the release
    print(f"  remove symlink {APP}")
    os.remove(APP)
    if os.path.islink(current):
        print(f"  remove symlink {current}")
        os.remove(current)

    # 2. Move the active release back to the flat app path
    print(f"  move {active} -> {APP}")
    shutil.move(active, APP)

    # 3. Replace the in-release shared symlinks with the real shared files
    for name in SHARED_FILES:
        link = os.path.join(APP, name)
        src = os.path.join(shared, name)
        if os.path.islink(link):
            os.remove(link)
        if os.path.exists(src):
            print(f"  restore {name}")
            shutil.move(src, link)
    for rel in SHARED_DIRS:
        link = os.path.join(APP, rel)
        src = os.path.join(shared, rel)
        if os.path.islink(link):
            os.remove(link)
        elif os.path.isdir(link) and not os.listdir(link):
            os.rmdir(link)
        if os.path.exists(src):
            print(f"  restore {rel}")
            os.makedirs(os.path.dirname(link), exist_ok=True)
            shutil.move(src, link)

    # 4. Best-effort cleanup of the now-empty data root
    try:
        if os.path.isdir(shared) and not os.listdir(shared):
            os.rmdir(shared)
        if os.path.isdir(releases_root) and not os.listdir(releases_root):
            os.rmdir(releases_root)
        if os.path.isdir(data_root) and not os.listdir(data_root):
            os.rmdir(data_root)
    except Exception:
        pass

    print(f"\nReversed. {APP} is a plain directory again.")
    print("Re-run migrate_to_releases.py to (re)build the atomic-release layout,")
    print("or just `systemctl start rscp` to run flat.")


if __name__ == '__main__':
    main()
