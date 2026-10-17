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

## Installation

RSCP runs on one Debian 12+ or Ubuntu 24.04+ machine or LXC container
(Python 3.11+, systemd). One script installs, upgrades and migrates it.

### Install

```bash
curl -sSL https://raw.githubusercontent.com/gcbtech/RSCP/main/install.sh | sudo bash
```

Then open `http://<server-ip>:5000` and complete the setup wizard.

Options go after `bash -s --`, e.g. the beta channel without prompts:

```bash
curl -sSL https://raw.githubusercontent.com/gcbtech/RSCP/main/install.sh | sudo bash -s -- --channel beta --noninteractive
```

| Option | Meaning |
|--------|---------|
| `--channel stable\|beta` | stable = newest GitHub release tag (default; the `main` branch while no tags exist), beta = `beta` branch |
| `--version vX.Y.Z` | install a specific release tag |
| `--branch NAME` | install the tip of a branch |
| `--source DIR` | install from a local source tree (no download) |
| `--bind HOST:PORT` | listen address, stored in `/etc/default/rscp` (default `0.0.0.0:5000`) |
| `--noninteractive` | never prompt (automatic when there is no terminal) |
| `--migrate-from DIR` | one-time move of a 2.x install (see below) |
| `--rollback` | switch back to the previous release |

The script only installs the packages it needs (`python3`, `python3-venv`,
`curl`, `ca-certificates`); it never runs a full `apt-get upgrade`. Running
it again is safe: an existing install is upgraded.

### What gets installed where

| Path | Contents |
|------|----------|
| `/opt/rscp/releases/<version>/` | the code and its own virtualenv (`venv/`), owned by root, read-only to the app |
| `/opt/rscp/current` | symlink to the active release (switched atomically); `/opt/rscp/previous` is the rollback target |
| `/var/lib/rscp/` | **all state**: `rscp.db`, `config.json` (mode 600), `uploads/`, `backups/` |
| `/etc/default/rscp` | optional environment, e.g. `RSCP_BIND=127.0.0.1:5000` |
| `rscp.service` | gunicorn as the unprivileged `rscp` user with `RSCP_DATA_DIR=/var/lib/rscp`; runs `python -m app.migrate` (schema migrations) before starting |
| `rscp-update.path` / `rscp-update.service` | applies update and rollback requests from the web UI |
| `/opt/rscp/bin/rscp-update` | installed copy of `install.sh`, used by the updater |

The newest 5 releases are kept. Logs go to the journal only:

```bash
systemctl status rscp
journalctl -u rscp -f            # the app
journalctl -u rscp-update        # updates and rollbacks
```

The unit templates live in [`deploy/`](deploy/). Don't edit
`/etc/systemd/system/rscp.service` (upgrades overwrite it); use
`/etc/default/rscp` or `systemctl edit rscp` instead.

### Upgrading

**From the web UI:** Admin > System Updates > Update. The web process has no
privileges. It writes `/var/lib/rscp/update-request.json`, and
`rscp-update.path` starts the root-owned updater, which:

1. downloads the release (curl, no git needed) and builds a new release dir
   with a fresh venv from `requirements.lock`, while the old version keeps serving;
2. backs up the database with SQLite's online backup API to
   `/var/lib/rscp/backups/pre-update-<old version>-<time>.db` (newest 5 kept);
3. switches `/opt/rscp/current`, restarts `rscp` (schema migrations run first)
   and checks `http://127.0.0.1:<port>/healthz`;
4. if the new version is not healthy within 2 minutes, it switches back and
   restarts the old release. The database backup is kept but **not**
   restored automatically.

Progress appears in the update dialog (from `/var/lib/rscp/update-status.json`).
**Roll Back** in the same card switches to the previous release the same
way. Only the code goes back; the database stays as it is.

**From a shell:** run the installer again, or use the installed copy:

```bash
sudo /opt/rscp/bin/rscp-update --upgrade --channel beta
sudo /opt/rscp/bin/rscp-update --rollback
```

### Migrating a 2.x install (one time)

2.x installs keep code and data together (e.g. `/opt/ebay_receiver`, often a
symlink into `/opt/ebay_receiver_data/releases/...`) and run as root. 3.0 still
boots in that layout, so the old in-app updater can install it. Moving to the
new layout takes one run of the installer as root, on the server's console:

```bash
# 0. Safety net: snapshot the container first (on the Proxmox host),
#    e.g.  pct snapshot <ctid> pre-rscp3

# 1. Look at what will be migrated
readlink -f /opt/ebay_receiver
ls -l /opt/ebay_receiver/rscp.db /opt/ebay_receiver/config.json /opt/ebay_receiver/static/uploads
systemctl cat rscp | head -20

# 2. Fetch the 3.0 installer from the branch you deploy (beta shown), check it, run it
curl -fsSL -o /root/rscp-install.sh https://raw.githubusercontent.com/gcbtech/RSCP/beta/install.sh
bash -n /root/rscp-install.sh
bash /root/rscp-install.sh --channel beta --migrate-from /opt/ebay_receiver --noninteractive

# 3. Verify
curl -s http://127.0.0.1:5000/healthz
systemctl status rscp rscp-update.path --no-pager
journalctl -u rscp -n 50 --no-pager
```

What the migration does, in order:

1. Checks the old install (database, config, free space) and refuses to run if
   `/var/lib/rscp` already holds a different database.
2. Builds the new release and venv **while the old service keeps running**.
3. Saves the old unit file (and any drop-ins) to `/opt/rscp/legacy/` and writes
   `/opt/rscp/legacy/restore-legacy.sh`.
4. Stops the old service, then copies into `/var/lib/rscp` (owned by `rscp`):
   `rscp.db` via the SQLite backup API (never a raw copy of a live WAL database)
   with an integrity check, `config.json`, `manifest.csv` if present, and
   `static/uploads/` (symlinks into the shared dir are resolved). If a copy
   fails, the old service is started again.
5. Installs the new units. The old unit has the same name (`rscp`), so its file
   is replaced and its drop-ins are moved away. A legacy unit with another
   name (`--legacy-unit NAME`) is disabled instead.
6. Starts `rscp` on the new layout and checks `/healthz`. If it is not healthy,
   the old unit is restored and started again.

The old directories are **never modified or deleted**, so going back is
`/opt/rscp/legacy/restore-legacy.sh`. Anything entered after the migration
exists only in `/var/lib/rscp`. Re-running the same command later is
harmless: the data copy is skipped and the install is just upgraded. Once
you're happy, remove the old directories yourself. Old 2.x DB backups in
`<old data>/shared/backups` are not copied.

Downtime is only the data copy plus one restart.

### Cloudflare Tunnel / reverse proxy

RSCP listens on port 5000 (all interfaces by default, so POS terminals on the
LAN can connect). To publish it with `cloudflared` on the same machine, point
the tunnel at the local port:

```yaml
# /etc/cloudflared/config.yml
tunnel: <tunnel-id>
credentials-file: /etc/cloudflared/<tunnel-id>.json
ingress:
  - hostname: rscp.example.com
    service: http://127.0.0.1:5000
  - service: http_status:404
```

(or set the same public hostname -> `http://127.0.0.1:5000` in the Zero Trust
dashboard for a token-based tunnel).

**Trusted proxies:** RSCP only believes `X-Forwarded-For` / `-Proto` from
addresses listed under **Admin > Settings > Reverse Proxy / Tunnel**
(`TRUSTED_PROXIES`). The default is loopback (`127.0.0.1`, `::1`), which covers
cloudflared on the same host. **If cloudflared or your proxy runs on another
machine, add that machine's IP there**. Otherwise every request appears to
come from the proxy, which breaks per-client login throttling and audit IPs.

If the server should only be reachable through the tunnel, set
`RSCP_BIND=127.0.0.1:5000` in `/etc/default/rscp` and `systemctl restart rscp`.
When all access is over HTTPS, you can also enable `SECURE_COOKIES` in
`config.json`.

### Running from source (development)

**Prerequisites:** Python 3.11+

```bash
git clone https://github.com/gcbtech/RSCP.git
cd RSCP
pip install -r requirements.txt
python app.py          # dev server on 0.0.0.0:5000 (RSCP_BIND to change)
```

Without `RSCP_DATA_DIR`, state is kept next to the code (the 2.x layout). Set
`RSCP_DATA_DIR=/some/dir` to use the 3.0 layout. Production uses the same app
object via `gunicorn -c gunicorn.conf.py wsgi:app`.

Admin tools in an installed system run as the service user:

```bash
cd /opt/rscp/current && sudo -u rscp RSCP_DATA_DIR=/var/lib/rscp venv/bin/python reset_password.py <user> <new-password>
```

## Configuration

### Environment Variables (Recommended for Production)

| Variable | Description |
|----------|-------------|
| `RSCP_DATA_DIR` | Directory for all state (DB, config, uploads, backups). Set by the installer to `/var/lib/rscp`; unset = legacy 2.x layout next to the code |
| `RSCP_BIND` | gunicorn listen address (default `0.0.0.0:5000`) |
| `RSCP_WORKERS` | override the gunicorn worker count |
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
├── app.py              # Development server (same app as wsgi.py)
├── wsgi.py             # WSGI entry point (production)
├── install.sh          # installer / upgrader / 2.x migration
├── deploy/             # systemd unit templates
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
