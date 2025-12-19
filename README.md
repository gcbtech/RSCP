# RSCP - Receive, Scan, Check, Process

A self-hosted package and shipment management system for tracking incoming packages, deliveries, returns, and more.

![Version](https://img.shields.io/github/v/tag/gcbtech/RSCP?label=version&color=blue)
![Python](https://img.shields.io/badge/python-3.11+-green)
![Flask](https://img.shields.io/badge/flask-2.3+-orange)

## Features

- 📦 **Package Scanning** - Barcode/tracking number scanning with detailed status updates and history
- 📊 **Dashboard** - Live stats for expected, received, past-due, and returned items
- 🔄 **Return Management** - Track returns and refunds with reason logging
- 👥 **Multi-User** - PIN-based authentication for warehouse staff
- 📧 **Email Ingestion** - Auto-import tracking from Amazon shipping emails
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
│   ├── routes/         # Flask blueprints
│   ├── services/       # Business logic
│   └── utils/          # Helper functions
├── templates/          # Jinja2 templates
├── static/             # CSS, JS, images
├── tests/              # Test suite
├── app.py              # Application entry point
├── wsgi.py             # WSGI entry point
└── requirements.txt    # Python dependencies
```

## Security

- CSRF protection on all POST requests
- Rate limiting on login
- HSTS header (when behind HTTPS)
- Parameterized SQL queries
- Session-based authentication
- 32MB upload limit

## License

MIT License - See LICENSE file for details.

## Version History

- **1.15.0** - First public release with security hardening
