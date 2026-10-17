"""
Where RSCP keeps things on disk. Every module gets file locations from here.

Two layouts are supported:

* Data directory (3.0+, set by the installer): the systemd unit sets
  RSCP_DATA_DIR (default /var/lib/rscp). Code is read-only and replaceable;
  all state lives under the data dir:

      $RSCP_DATA_DIR/rscp.db, config.json
      $RSCP_DATA_DIR/uploads/     (served at /static/uploads/...)
      $RSCP_DATA_DIR/backups/
      $RSCP_DATA_DIR/tmp/

* Legacy (no RSCP_DATA_DIR): state sits next to the code, as in 2.x
  (rscp.db, config.json and static/uploads inside the app directory, which
  in the release layout are symlinks into <data>/shared).

Uploaded files are always addressed by URL as /static/uploads/<rel>, so
stored image URLs stay valid when an install moves to a data directory.
"""
import os

APP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

_ENV_DATA_DIR = os.environ.get('RSCP_DATA_DIR', '').strip()
USES_DATA_DIR = bool(_ENV_DATA_DIR)
DATA_DIR = os.path.abspath(_ENV_DATA_DIR) if USES_DATA_DIR else APP_DIR

DB_PATH = os.path.join(DATA_DIR, 'rscp.db')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
UPLOADS_DIR = os.path.join(DATA_DIR, 'uploads') if USES_DATA_DIR else os.path.join(APP_DIR, 'static', 'uploads')
TEMP_DIR = os.path.join(DATA_DIR, 'tmp') if USES_DATA_DIR else os.path.join(APP_DIR, 'temp')
VERSION_FILE = os.path.join(APP_DIR, 'VERSION')

UPLOADS_URL_PREFIX = '/static/uploads/'


def backups_dir():
    """Directory for database backups (kept beside the real database file,
    so in the legacy release layout it lands in the shared dir)."""
    if USES_DATA_DIR:
        return os.path.join(DATA_DIR, 'backups')
    return os.path.join(os.path.dirname(os.path.realpath(DB_PATH)), 'backups')


def upload_path(*parts):
    """Filesystem path of an uploaded file, e.g. upload_path('inventory', 'a.jpg')."""
    return os.path.join(UPLOADS_DIR, *parts)


def upload_url(rel):
    """Public URL of an uploaded file, e.g. upload_url('inventory/a.jpg')."""
    return UPLOADS_URL_PREFIX + rel.lstrip('/')


def upload_url_to_path(url):
    """Map '/static/uploads/<rel>' to its file, or None for any other URL.

    Refuses anything that would resolve outside UPLOADS_DIR.
    """
    if not url or not url.startswith(UPLOADS_URL_PREFIX):
        return None
    rel = url[len(UPLOADS_URL_PREFIX):].split('?', 1)[0]
    path = os.path.realpath(os.path.join(UPLOADS_DIR, rel))
    root = os.path.realpath(UPLOADS_DIR)
    if path != root and not path.startswith(root + os.sep):
        return None
    return path
