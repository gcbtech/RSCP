"""
System Migrations
=================

One-time, idempotent *system-level* steps that must run after an update —
the operational counterpart to ensure_db_ready()'s database migrations.
Examples: rewriting the systemd unit when the process model changes,
backfilling derived files (thumbnails), cleaning up obsolete artifacts.

How it works:
- Each migration has a stable id and an idempotent apply() function.
- Applied ids are recorded in the `system_migrations` table, so each step
  runs exactly once per install (including prod boxes updated via the
  in-app updater — no manual SSH steps).
- run_system_migrations() is called at startup by the ONE worker that owns
  background tasks (wsgi.py's flock guard), so multi-worker deployments
  don't race.
- A migration that cannot apply on this host (wrong OS, not root, file
  missing) records itself as skipped-but-done where that is permanent, or
  leaves itself unrecorded to retry next boot where the condition is
  transient. Each migration documents its choice.

Adding a migration: append (id, function) to MIGRATIONS. Never reorder or
remove entries; ids are history.
"""
import logging
import os
import subprocess
import threading
from datetime import datetime

from app.services.db import get_db_connection

logger = logging.getLogger(__name__)

UNIT_PATH = '/etc/systemd/system/rscp.service'


def _ensure_table(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS system_migrations (
            id TEXT PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            note TEXT
        )
    ''')
    conn.commit()


def _is_applied(conn, mig_id):
    return conn.execute(
        'SELECT 1 FROM system_migrations WHERE id = ?', (mig_id,)
    ).fetchone() is not None


def _record(conn, mig_id, note=''):
    conn.execute(
        'INSERT OR IGNORE INTO system_migrations (id, note) VALUES (?, ?)',
        (mig_id, note)
    )
    conn.commit()


def _log(msg):
    """Log AND print: module-level logging happens before app handlers are
    configured, so print(flush=True) is what reliably reaches journald via
    gunicorn's captured stdout."""
    logger.info(msg)
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# 001 — switch the systemd unit from eventlet -w 1 to the repo gunicorn config
# ---------------------------------------------------------------------------

def migrate_001_gthread_systemd_unit():
    """Point the unit's ExecStart at gunicorn.conf.py (gthread multi-worker).

    Replaces hand-edited ExecStart lines (e.g. the old
    `--worker-class eventlet -w 1 ...`) with `-c gunicorn.conf.py wsgi:app`
    so the repo config is authoritative. Preserves whatever gunicorn binary
    path the unit already uses.

    The unit hardens itself with ProtectSystem=full, which mounts /etc
    READ-ONLY inside the service — we cannot edit our own unit file
    directly. Instead we hand the edit to systemd-run: a transient unit
    started by PID 1 runs OUTSIDE our sandbox (writable /etc), performs
    backup + sed + daemon-reload, then restarts this service into the new
    worker model. That restart also proves the edit worked.

    Returns (done, note). Records as done on non-systemd hosts (permanent
    condition for that install style — e.g. Windows dev, bare flask).
    """
    if os.name != 'posix':
        return True, 'skipped: not posix'
    if not os.path.exists(UNIT_PATH):
        return True, 'skipped: no systemd unit at ' + UNIT_PATH
    if hasattr(os, 'geteuid') and os.geteuid() != 0:
        # Retry next boot is harmless if install style ever changes.
        return False, 'not root; cannot edit unit'

    # /etc is readable (just not writable) under ProtectSystem=full.
    with open(UNIT_PATH, 'r') as f:
        content = f.read()

    exec_line = None
    for line in content.splitlines():
        if line.strip().startswith('ExecStart=') and 'wsgi:app' in line:
            exec_line = line
            break
    if exec_line is None:
        return True, 'skipped: no gunicorn wsgi:app ExecStart found'
    if '-c gunicorn.conf.py' in exec_line:
        return True, 'already using gunicorn.conf.py'

    # Keep the existing gunicorn binary path (handles /usr/local/bin vs
    # /usr/bin vs venv installs).
    old_cmd = exec_line.split('=', 1)[1].strip()
    gunicorn_bin = old_cmd.split()[0] if old_cmd else '/usr/local/bin/gunicorn'
    if os.path.basename(gunicorn_bin) != 'gunicorn':
        gunicorn_bin = '/usr/local/bin/gunicorn'
    new_line = f'ExecStart={gunicorn_bin} -c gunicorn.conf.py wsgi:app'

    ts = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = f'{UNIT_PATH}.bak-{ts}'

    # Script lives in the app dir (shared, writable filesystem — the
    # transient unit has its own /tmp, so PrivateTmp paths won't line up).
    app_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    script_path = os.path.join(app_root, '.sysmigrate_001.sh')
    script = f"""#!/bin/sh
set -e
cp -p {UNIT_PATH} {backup}
sed -i 's|^ExecStart=.*wsgi:app.*$|{new_line}|' {UNIT_PATH}
systemctl daemon-reload
rm -f {script_path}
sleep 2
systemctl restart rscp
"""
    with open(script_path, 'w') as f:
        f.write(script)
    os.chmod(script_path, 0o755)

    try:
        subprocess.run(
            ['systemd-run', '--collect', f'--unit=rscp-sysmigrate-{ts}',
             '/bin/sh', script_path],
            timeout=15, check=True, capture_output=True)
    except Exception as e:
        try:
            os.remove(script_path)
        except OSError:
            pass
        return False, f'systemd-run failed: {e}'

    _log(f"[SysMigrate 001] Unit rewrite handed to systemd-run "
         f"(backup: {backup}); service will restart into gthread workers")
    return True, f'ExecStart rewritten via systemd-run (was: {old_cmd}); backup {backup}'


# ---------------------------------------------------------------------------
# 002 — backfill inventory list thumbnails for images uploaded before 2.6.6
# ---------------------------------------------------------------------------

def migrate_002_backfill_thumbnails():
    """Generate missing list thumbnails in the background.

    Thumbnails are created at upload time as of 2.6.6; this covers images
    from before. Runs in a daemon thread (can take minutes on large image
    sets) and records completion from the thread — if the box restarts
    mid-run, it simply resumes next boot (generation skips fresh thumbs).

    Returns (False, note) immediately: the thread records the migration
    itself on successful completion.
    """
    def _go():
        try:
            import json
            from app.routes.inventory.items import generate_thumbnail
            conn = get_db_connection()
            rows = conn.execute(
                "SELECT image_url, additional_images FROM inventory_items "
                "WHERE image_url IS NOT NULL OR additional_images IS NOT NULL"
            ).fetchall()

            urls = []
            for r in rows:
                if r['image_url']:
                    urls.append(r['image_url'])
                if r['additional_images']:
                    try:
                        urls.extend(json.loads(r['additional_images']) or [])
                    except (json.JSONDecodeError, TypeError):
                        pass

            done = 0
            for url in urls:
                if generate_thumbnail(url):
                    done += 1

            _record(conn, '002_backfill_thumbnails',
                    f'{done} thumbnails ensured from {len(urls)} references')
            conn.close()
            logger.info(f"[SysMigrate 002] Thumbnail backfill complete: "
                        f"{done}/{len(urls)}")
        except Exception as e:
            logger.error(f"[SysMigrate 002] Backfill failed (will retry next "
                         f"boot): {e}")

    threading.Thread(target=_go, daemon=True, name="SysMigrateThumbs").start()
    return False, 'backfill running in background; records itself on completion'


# Ordered, append-only.
MIGRATIONS = [
    ('001_gthread_systemd_unit', migrate_001_gthread_systemd_unit),
    ('002_backfill_thumbnails', migrate_002_backfill_thumbnails),
]


def run_system_migrations():
    """Run pending system migrations. Call from ONE process only
    (wsgi.py invokes this in the worker holding the background-task lock)."""
    try:
        conn = get_db_connection()
        _ensure_table(conn)
        for mig_id, fn in MIGRATIONS:
            if _is_applied(conn, mig_id):
                continue
            try:
                done, note = fn()
            except Exception as e:
                _log(f"[SysMigrate {mig_id}] FAILED: {e} (will retry next boot)")
                continue
            if done:
                _record(conn, mig_id, note)
                _log(f"[SysMigrate {mig_id}] applied: {note}")
            else:
                _log(f"[SysMigrate {mig_id}] pending: {note}")
        conn.close()
    except Exception as e:
        logger.error(f"[SysMigrate] runner error: {e}")
