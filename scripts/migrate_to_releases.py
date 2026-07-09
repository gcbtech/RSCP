#!/usr/bin/env python3
"""
One-time migration: flat install  ->  atomic-release (symlink) layout.

Works for ANY app directory name — the old install may be /opt/ebay_receiver
(the original name, never renamed on production) or /opt/rscp (new installs).
The app dir is auto-detected from this script's location; the release data
root is a NON-colliding sibling, <APP>_data, so it never clashes with an
/opt/rscp app dir.

    BEFORE:  <APP>/                      (real dir: code + config.json + rscp.db + static/uploads)

    AFTER:   <APP>            -> <APP>_data/current      (fixed outer symlink)
             <APP>_data/current -> releases/<ts>_initial (symlink the updater repoints)
             <APP>_data/releases/<ts>_initial/           code; config.json, rscp.db*,
                                                          static/uploads are symlinks
                                                          into ../../shared
             <APP>_data/shared/  config.json, rscp.db(+wal/shm), static/uploads

The updater detects this layout structurally (no hardcoded paths) and, on each
update, builds a new release and atomically repoints <APP>_data/current.

RUN AS ROOT, WITH THE SERVICE STOPPED:

    systemctl stop rscp
    python3 <APP>/scripts/migrate_to_releases.py
    # verify the printed layout, then:
    systemctl start rscp

Move-based (no data duplication), idempotent, reversible (prints the exact
reversal commands). Nothing is deleted.
"""
import os
import sys
import shutil
import time

# App dir: auto-detected from this file's location (<APP>/scripts/this.py),
# overridable via RSCP_APP for testing.
APP = os.environ.get('RSCP_APP') or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = APP.rstrip('/')
# Release data root: a non-colliding sibling. Overridable via RSCP_DATA.
DATA = (os.environ.get('RSCP_DATA') or (APP + '_data')).rstrip('/')

RELEASES = os.path.join(DATA, 'releases')
SHARED = os.path.join(DATA, 'shared')
CURRENT = os.path.join(DATA, 'current')

SHARED_FILES = ['config.json', 'rscp.db', 'rscp.db-wal', 'rscp.db-shm']
SHARED_DIRS = [os.path.join('static', 'uploads')]


def fail(msg):
    print(f"\nERROR: {msg}\nAborting; see above for how to reverse any partial change.")
    sys.exit(1)


def main():
    print(f"App dir : {APP}")
    print(f"Data dir: {DATA}\n")

    if os.path.islink(APP):
        print(f"{APP} is already a symlink -> {os.path.realpath(APP)}")
        print("Already migrated. Nothing to do.")
        return
    if not os.path.isdir(APP):
        fail(f"{APP} is not a directory. Set RSCP_APP if the app lives elsewhere.")

    # Refuse to run against a live service (best-effort). RSCP_ALLOW_RUNNING=1
    # bypasses this — used only by the sandbox test on a throwaway /tmp layout.
    if os.environ.get('RSCP_ALLOW_RUNNING') == '1':
        print("(RSCP_ALLOW_RUNNING=1: skipping the running-service check)")
    elif os.path.exists('/proc'):
        for pid in os.listdir('/proc'):
            if not pid.isdigit():
                continue
            try:
                with open(f'/proc/{pid}/cmdline', 'rb') as f:
                    cmd = f.read().decode('utf-8', 'replace')
                if 'gunicorn' in cmd and 'wsgi:app' in cmd:
                    fail("gunicorn (rscp) still appears to be running. Run "
                         "`systemctl stop rscp` first, then re-run this script.")
            except Exception:
                pass

    real_app = os.path.realpath(APP)
    ts = time.strftime('%Y%m%d-%H%M%S')
    release_dir = os.path.join(RELEASES, f'{ts}_initial')

    os.makedirs(RELEASES, exist_ok=True)
    os.makedirs(SHARED, exist_ok=True)

    # 1. Move the shared state OUT of the install into shared/ (move = the one
    #    authoritative copy; no duplication, no stale snapshot).
    for name in SHARED_FILES:
        src = os.path.join(real_app, name)
        dst = os.path.join(SHARED, name)
        if os.path.exists(src) and not os.path.exists(dst):
            print(f"  move  {name}  ->  shared/")
            shutil.move(src, dst)
    for rel in SHARED_DIRS:
        src = os.path.join(real_app, rel)
        dst = os.path.join(SHARED, rel)
        if os.path.exists(src) and not os.path.exists(dst):
            print(f"  move  {rel}  ->  shared/")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)

    # 2. Move the now-code-only install into releases/<ts>_initial
    print(f"  move  {APP}  ->  {release_dir}")
    shutil.move(real_app, release_dir)

    # 3. Symlink the shared state back into the release
    for name in SHARED_FILES:
        target = os.path.join(SHARED, name)
        if os.path.exists(target):
            link = os.path.join(release_dir, name)
            if os.path.lexists(link):
                os.remove(link)
            os.symlink(target, link)
    for rel in SHARED_DIRS:
        target = os.path.join(SHARED, rel)
        if os.path.exists(target):
            link = os.path.join(release_dir, rel)
            os.makedirs(os.path.dirname(link), exist_ok=True)
            if os.path.lexists(link):
                if os.path.islink(link):
                    os.remove(link)
                else:
                    shutil.rmtree(link)
            os.symlink(target, link)

    # 4. Two-level symlink: <DATA>/current -> release, then <APP> -> current.
    #    The updater only ever repoints <DATA>/current; <APP> stays fixed.
    if os.path.lexists(CURRENT):
        os.remove(CURRENT)
    os.symlink(release_dir, CURRENT)
    os.symlink(CURRENT, APP)

    print("\nMigration complete. Layout:")
    print(f"  {APP} -> {os.readlink(APP)} -> {os.path.realpath(APP)}")
    print(f"  releases: {RELEASES}")
    print(f"  shared:   {SHARED}  ({', '.join(sorted(os.listdir(SHARED)))})")
    print("\nNext: `systemctl start rscp`, confirm the app works. Future updates")
    print("will build new releases and repoint current atomically; rollback is")
    print("one step (POST /admin/rollback).\n")
    print("To REVERSE this migration (as root, service stopped):")
    print(f"  rm {APP} {CURRENT}")
    print(f"  mv {release_dir} {APP}")
    for name in SHARED_FILES:
        print(f"  [ -e {os.path.join(SHARED, name)} ] && mv {os.path.join(SHARED, name)} {os.path.join(APP, name)}")
    for rel in SHARED_DIRS:
        print(f"  [ -e {os.path.join(SHARED, rel)} ] && rm -f {os.path.join(APP, rel)} && mv {os.path.join(SHARED, rel)} {os.path.join(APP, rel)}")


if __name__ == '__main__':
    main()
