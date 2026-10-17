"""
Development server: python app.py

Uses the same application object as production (wsgi.py), so there is one
code path. Production runs gunicorn: gunicorn -c gunicorn.conf.py wsgi:app
"""
import os

from wsgi import app

if __name__ == '__main__':
    host, _, port = (os.environ.get('RSCP_BIND', '').strip() or '0.0.0.0:5000').rpartition(':')
    app.run(host=host.strip('[]') or '0.0.0.0', port=int(port), debug=False)
