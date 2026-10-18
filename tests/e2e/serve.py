"""
Serve the real RSCP app for the browser tests: python tests/e2e/serve.py PORT

Started by tests/e2e/conftest.py in its own process with RSCP_DATA_DIR
pointing at a throwaway data directory, so app.paths, the database and
config resolve exactly as in production (create_app() with no test config:
real CSRF, real login, real CSP headers). Werkzeug's threaded server stands
in for gunicorn, which does not run on Windows.

Background jobs and system migrations (wsgi.py) are deliberately not
started: they are not page behaviour and must never touch the host.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))


def main():
    port = int(sys.argv[1])
    if not os.environ.get('RSCP_DATA_DIR'):
        sys.exit('RSCP_DATA_DIR must point at a throwaway data directory')

    from werkzeug.serving import make_server
    from app import create_app

    app = create_app()
    server = make_server('127.0.0.1', port, app, threaded=True)
    print(f'RSCP e2e server listening on http://127.0.0.1:{port}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
