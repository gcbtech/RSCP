# RSCP - Receive, Scan, Check, Process

A self-hosted package and shipment management system for tracking incoming packages, deliveries, returns, and inventory.

![Version](https://img.shields.io/badge/version-2.6.0-blue)
![Python](https://img.shields.io/badge/python-3.11+-green)
![Flask](https://img.shields.io/badge/flask-2.3+-orange)

## Features

### 📦 Receiving & Packages
- **Package Scanning** - Barcode/tracking number scanning with status tracking
- **Dashboard** - Live stats for expected, received, past-due, and returned items
- **Return Management** - Track returns and refunds with reason logging
- **Email Ingestion** - Auto-import tracking from Amazon shipping emails
- **Webhook Alerts** - Priority package notifications to Slack/Discord

### 📋 Inventory Management
- **Comprehensive Tracking** - Track items, quantities, costs, and locations
- **Stock Alerts** - Low stock notifications via webhook
- **Visual Grid** - Overview of all inventory with filtering and sorting
- **Quick Actions** - Rapid stock adjustments and label printing
- **History Log** - Detailed audit trail of all inventory movements

### 💳 Point of Sale (POS)
- **Fast Checkout** - Barcode scanning, quick-add, and manual entry
- **Payment Processing** - Support for Cash, Card, and Split payments
- **Cash Management** - Cash drawer tracking, float in/out, and end-of-day reconciliation
- **Discounts & Taxes** - Configurable tax rates and manager-approved discounts
- **Receipts** - Professional digital and printable receipts (thermal printer optimized)

### 📊 Analytics & Reporting
- **Sales Reports** - Daily/Monthly sales breakdown with profit margin analysis
- **Inventory Reports** - Valuation, turnover rate, and low stock reports
- **End of Day** - Automated EOD email summaries with Z-Report data
- **Visual Charts** - Interactive graphs for sales trends and category performance

### 🛠️ System
- **Multi-User** - Role-based authentication (Admin/Manager/Staff)
- **Dark Mode** - Eye-friendly interface for low-light environments
- **Mobile-Ready** - Responsive design for tablets and mobile scanners
- **Secure** - CSRF protection, rate limiting, and secure session handling

## 📘 Documentation
**New to RSCP?** Check out the [**Detailed User Guide & Workflow**](USER_GUIDE.md) for a step-by-step tutorial on how to run your business with RSCP.

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

- **2.6.0** - Significant scanning speed optimization (<30ms DB lookups) and clean single-beep client auditory feedback integration for Receiving and POS checkout views.
- **2.1.18** - Full POS system implementation, robust Inventory tracking, advanced Analytics/Reporting, and receipt printer support.
- **1.16.5** - Security audit fixes, performance optimizations, admin panel redesign, automated tests
- **1.16.2** - Inventory module, ASIN tracking, audit sessions
- **1.15.0** - First public release with security hardening
