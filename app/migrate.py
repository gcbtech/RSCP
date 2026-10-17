"""
Bring the database schema up to date, then exit.

    python -m app.migrate            # migrate the configured database
    python -m app.migrate --check    # exit 1 if migrations are pending
    python -m app.migrate --db PATH  # a specific database file

rscp.service runs this as ExecStartPre, so migrations happen exactly once,
before any gunicorn worker starts; a failure stops the start (and makes the
updater roll back) instead of serving a half-migrated database. Workers still
call run_migrations() at boot, which is a cheap no-op once this has run, so
legacy installs without the ExecStartPre keep working.
"""
import argparse
import logging
import os
import sys


def _schema_version(db_path):
    from app.services.migration import _user_version
    if not os.path.exists(db_path):
        return 0
    return _user_version(db_path)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m app.migrate', description=__doc__.strip().splitlines()[0])
    parser.add_argument('--db', help='database file (default: the configured RSCP database)')
    parser.add_argument('--check', action='store_true', help='only report; exit 1 if migrations are pending')
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format='[migrate] %(message)s', stream=sys.stdout)

    from app import paths
    from app.services.migration import LATEST_SCHEMA_VERSION, run_migrations

    db_path = os.path.abspath(args.db or paths.DB_PATH)
    before = _schema_version(db_path)

    if args.check:
        pending = before < LATEST_SCHEMA_VERSION
        print(f"[migrate] {db_path}: schema v{before}, latest v{LATEST_SCHEMA_VERSION}"
              f"{' (migrations pending)' if pending else ' (up to date)'}")
        return 1 if pending else 0

    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.isdir(db_dir):
        print(f"[migrate] data directory {db_dir} does not exist", file=sys.stderr)
        return 2

    try:
        run_migrations(db_path)
    except Exception as e:  # noqa: BLE001 - report and fail the unit start
        logging.getLogger(__name__).exception(f"Schema migration failed: {e}")
        return 1

    after = _schema_version(db_path)
    if after == before:
        print(f"[migrate] {db_path}: schema v{after} (up to date)")
    else:
        print(f"[migrate] {db_path}: schema v{before} -> v{after}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
