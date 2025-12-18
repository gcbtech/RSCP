from app import create_app
from app.services.background_tasks import start_email_thread

app = create_app()

# Start Background Tasks
start_email_thread()

if __name__ == '__main__':
    # Development server only - use wsgi.py + Gunicorn for production
    app.run(host='0.0.0.0', port=5000, debug=False)
