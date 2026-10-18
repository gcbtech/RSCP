"""
WSGI Entry Point for Production Deployment
Use with Gunicorn: gunicorn -c gunicorn.conf.py wsgi:app
"""
import logging

from app import create_app
from app import jobs as _jobs

logger = logging.getLogger(__name__)

# Create the Flask application
app = create_app()

# Periodic jobs (package status upkeep, POS EOD email, nightly backup) and
# post-update system migrations run from systemd, never inside a web worker:
# rscp-*.timer -> rscp-job@<name>.service / rscp-backup.service, and
# `python -m app.jobs system-migrations` as rscp.service's ExecStartPre
# (app/jobs.py, deploy/). If those units are missing, say so loudly; the same
# text is shown on Admin > Logs > Scheduled jobs.
_schedule_warning = _jobs.scheduling_warning()
if _schedule_warning:
    logger.warning(f"[Jobs] {_schedule_warning}")

# Gunicorn will import 'app' from this module
