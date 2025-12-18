"""
WSGI Entry Point for Production Deployment
Use with Gunicorn: gunicorn -w 4 -b 0.0.0.0:5000 wsgi:app
"""
from app import create_app
from app.services.background_tasks import start_email_thread

# Create the Flask application
app = create_app()

# Start background tasks (email ingest, etc.)
start_email_thread()

# Gunicorn will import 'app' from this module
