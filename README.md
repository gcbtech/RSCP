# RSCP - Receive, Scan, Check, Process

A self-hosted package and shipment management system for tracking incoming packages, deliveries, returns, and inventory.

![Version](https://img.shields.io/badge/version-1.16.5-blue)
![Python](https://img.shields.io/badge/python-3.11+-green)
![Flask](https://img.shields.io/badge/flask-2.3+-orange)

## Features

- 📦 **Package Scanning** - Barcode/tracking number scanning with status tracking and history
- 📊 **Dashboard** - Live stats for expected, received, past-due, and returned items
- 🔄 **Return Management** - Track returns and refunds with reason logging
- 📦 **Inventory Module** - Track items, quantities, locations, and stock levels
- 👥 **Multi-User** - Role-based authentication (Admin/Staff)
- 📧 **Email Ingestion** - Auto-import tracking from Amazon shipping emails
- 🔔 **Webhook Alerts** - Priority package notifications to Slack/Discord
- 🌙 **Dark Mode** - Eye-friendly scanning in low-light environments
- 📱 **Mobile-Ready** - Responsive design works on phones/tablets

## Quick Start

### One-Line Install (Debian/Ubuntu)

```bash
curl -sSL https://raw.githubusercontent.com/gcbtech/RSCP/main/install.sh | sudo bash
```

This script automatically:
- Installs Python 3, pip, and dependencies
- Creates a dedicated `rscp` user
- Sets up a Python virtual environment
- Configures systemd service
- Provides next-step instructions

Visit `http://IP_ADDRESS:5000` and complete the setup wizard.

### Manual Installation

**Prerequisites:** Python 3.11+, pip

```bash
# Clone the repository
git clone https://github.com/gcbtech/RSCP.git
cd RSCP

# Install dependencies
pip install -r requirements.txt

# Run the application
python app.py
```

Visit `http://IP_ADDRESS:5000` and complete the setup wizard.

### Production Deployment (Gunicorn)

```bash
gunicorn -c gunicorn.conf.py wsgi:app
```

See `rscp.service.example` for systemd service configuration.

## Configuration

### Environment Variables (Recommended for Production)

| Variable | Description |
|----------|-------------|
| `FLASK_ENV` | Set to `production` for secure cookies |
| `RSCP_SECRET_KEY` | Flask session secret key |
| `RSCP_WEBHOOK_URL` | Webhook URL for priority alerts |
| `RSCP_IMAP_SERVER` | IMAP server for email ingestion |
| `RSCP_EMAIL_USER` | Email username |
| `RSCP_EMAIL_PASS` | Email password |

### First Run
On first run, the setup wizard creates:
- `config.json` - Application configuration
- `rscp.db` - SQLite database

## Project Structure

```
rscp/
├── app/
│   ├── routes/         # Flask blueprints (main, admin, inventory, auth)
│   ├── services/       # Business logic (db, auth, data_manager, background_tasks)
│   └── utils/          # Helper functions
├── templates/          # Jinja2 templates
├── static/             # CSS, JS, images
├── tests/              # Pytest test suite
├── app.py              # Application entry point
├── wsgi.py             # WSGI entry point (production)
└── requirements.txt    # Python dependencies
```

## Testing

Run the test suite with pytest:

```bash
python -m pytest tests/ -v
```

## Security

- ✅ CSRF protection on all POST requests
- ✅ Rate limiting on login (5 attempts/minute)
- ✅ Content Security Policy headers
- ✅ HSTS header (when behind HTTPS)
- ✅ Parameterized SQL queries (injection-safe)
- ✅ Flask-Login session authentication
- ✅ Password complexity requirements
- ✅ Path traversal protection on backups
- ✅ Secure cookie settings in production
- ✅ 32MB upload limit

## Performance

- SQLite WAL mode for concurrent reads
- Config caching with 60-second TTL
- Background manifest sync (every 5 minutes)
- Request-scoped database connections
- Optimized GROUP BY queries

## License

MIT License - See LICENSE file for details.

## Version History

- **1.16.5** - Security audit fixes, performance optimizations, admin panel redesign, automated tests
- **1.16.2** - Inventory module, ASIN tracking, audit sessions
- **1.15.0** - First public release with security hardening
