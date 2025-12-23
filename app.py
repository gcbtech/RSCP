from app import create_app
from app.services.background_tasks import start_background_tasks

app = create_app()

# Start Background Tasks (manifest sync every 5 min, email ingest every 15 min)
start_background_tasks()

if __name__ == '__main__':
    # Development server only - use wsgi.py + Gunicorn for production
    app.run(host='0.0.0.0', port=5000, debug=False)
