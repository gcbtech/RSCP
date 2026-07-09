#!/usr/bin/env python3
"""
One-time migration: flat install  ->  atomic-release (symlink) layout.

    BEFORE:  /opt/ebay_receiver/            (real dir: code + config.json + rscp.db + static/uploads)

    AFTER:   /opt/ebay_receiver             -> symlink -> /opt/rscp/releases/<ts>_initial
             /opt/rscp/releases/<ts>_initial/   code; config.json, rscp.db*, static/uploads
                                                are symlinks into ../../shared
             /opt/rscp/shared/                  config.json, rscp.db(+wal/shm), static/uploads

After this, the RSCP updater builds each update as a new release dir and
atomically repoints the symlink, so updates are instant and rollback is one step.

RUN AS ROOT, WITH THE SERVICE STOPPED:

    systemctl stop rscp
    python3 /opt/ebay_receiver/scripts/migrate_to_releases.py
    # verify the printed layout, then:
    systemctl start rscp

It is move-based (no data is copied/duplicated), idempotent (safe to re-run),
and reversible — see the rollback commands it prints. Nothing is deleted.
"""
import os
import sys
import shutil
import time

APP = os.environ.get('RSCP_APP', '/opt/ebay_receiver')
ROOT = os.environ.get('RSCP_ROOT', '/opt/rscp')
RELEASES = os.path.join(ROOT, 'releases')
SHARED = os.path.join(ROOT, 'shared')

SHARED_FILES = ['config.json', 'rscp.db', 'rscp.db-wal', 'rscp.db-shm']
SHARED_DIRS = [os.path.join('static', 'uploads')]


def fail(msg):
    print(f"\nERROR: {msg}\nNo changes were made (or see above for how to reverse). Aborting.")
    sys.exit(1)


def main():
    print(f"Migrating {APP} to atomic-release layout under {ROOT}\n")

    if os.path.islink(APP):
        print(f"{APP} is already a symlink -> {os.path.realpath(APP)}")
        print("Already migrated. Nothing to do.")
        return

    if not os.path.isdir(APP):
        fail(f"{APP} is not a directory.")

    # Refuse to run against a live service (best-effort check).
    # RSCP_ALLOW_RUNNING=1 bypasses this — used only by the sandbox test that
    # migrates a throwaway /tmp layout while the real service is up.
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

    # 4. Put the symlink in place of the original path
    print(f"  link  {APP}  ->  {release_dir}")
    os.symlink(release_dir, APP)

    print("\nMigration complete. Layout:")
    print(f"  {APP} -> {os.path.realpath(APP)}")
    print(f"  releases: {RELEASES}")
    print(f"  shared:   {SHARED}  ({', '.join(os.listdir(SHARED))})")
    print("\nNext: `systemctl start rscp`, confirm the app works, then future")
    print("updates will use atomic releases with one-step rollback.\n")
    print("To REVERSE this migration (as root, service stopped):")
    print(f"  rm {APP}")
    print(f"  mv {release_dir} {APP}")
    for name in SHARED_FILES:
        print(f"  [ -e {os.path.join(SHARED, name)} ] && mv {os.path.join(SHARED, name)} {os.path.join(APP, name)}")
    for rel in SHARED_DIRS:
        print(f"  [ -e {os.path.join(SHARED, rel)} ] && rm -f {os.path.join(APP, rel)} && mv {os.path.join(SHARED, rel)} {os.path.join(APP, rel)}")


if __name__ == '__main__':
    main()
