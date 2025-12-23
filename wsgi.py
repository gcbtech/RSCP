"""
WSGI Entry Point for Production Deployment
Use with Gunicorn: gunicorn -w 4 -b 0.0.0.0:5000 wsgi:app
"""
from app import create_app
from app.services.background_tasks import start_background_tasks

# Create the Flask application
app = create_app()

# Start background tasks (manifest sync every 5 min, email ingest every 15 min)
start_background_tasks()

# Gunicorn will import 'app' from this module
