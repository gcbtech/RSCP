# RSCP - Receive, Scan, Check, Process

A self-hosted business management system for small shops. It covers package receiving, inventory with full stock history, and point of sale with customer displays and handheld scanners. It also has sales reports and an employee timeclock. It runs on Python/Flask with SQLite and installs with a single script.

[![Tests](https://github.com/gcbtech/RSCP/actions/workflows/tests.yml/badge.svg)](https://github.com/gcbtech/RSCP/actions/workflows/tests.yml)
[![Stable](https://img.shields.io/github/v/release/gcbtech/RSCP?label=stable)](https://github.com/gcbtech/RSCP/releases/latest)
[![Beta](https://img.shields.io/badge/dynamic/regex?url=https%3A%2F%2Fraw.githubusercontent.com%2Fgcbtech%2FRSCP%2Fbeta%2FVERSION&search=%5Cd%2B%5C.%5Cd%2B%5C.%5Cd%2B&label=beta&color=orange)](https://github.com/gcbtech/RSCP/tree/beta)
![Python](https://img.shields.io/badge/python-3.11+-green)
![Flask](https://img.shields.io/badge/flask-3.1+-orange)

## Features

### 📦 Receiving & Packages
- **Package Scanning** - Barcode/tracking number scanning with status tracking
- **Dashboard** - Live stats for expected, received, past-due, and returned items
- **Return Management** - Track returns and refunds with reason logging
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

### ⏱️ Timeclock
- **Clock In/Out** - Staff clock in and out, with breaks
- **Schedules & PTO** - Shifts, recurring schedules, PTO requests and balances
- **Timesheets** - Manager review and edits, with pay-period exports

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
| `rscp-backup.timer` / `rscp-backup.service` | nightly backups to `/var/lib/rscp/backups/nightly` (see [Backups](#backups)) |
| `rscp-package-maintenance.timer`, `rscp-eod-email.timer`, `rscp-monitor.timer`, `rscp-job@.service` | package status upkeep, the end-of-day email and email alerts (see [Scheduled jobs](#scheduled-jobs)) |
| `/opt/rscp/bin/rscp-update` | installed copy of `install.sh`, used by the updater |

The newest 5 releases are kept. Logs go to the journal only:

```bash
systemctl status rscp
journalctl -u rscp -f            # the app
journalctl -u rscp-update        # updates and rollbacks
journalctl -u rscp-backup        # nightly backups
journalctl -u 'rscp-job@*'       # scheduled jobs (package upkeep, EOD email)
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
symlink into `/opt/ebay_receiver_data/releases/...`) and run as root. If a 2.x
in-app update pulls 3.x into that layout, the app still boots, but no scheduled
jobs or nightly backups run and 3.x's own in-app updater refuses to run there,
so migrate right away. Moving to the new layout takes one run of the installer
as root, on the server's console:

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

### Sign-in changes in 3.0

RSCP 3.0 has two ways in: **username + password** at `/login`, and
**Cloudflare Access** (see below; with Google as the Access login method,
that is how staff "sign in with Google"). Removed:

- RSCP's own **"Sign in with Google" (SSO / OIDC)**, with its settings
  (client ID and secret, discovery URL, allowed domains, auto-provisioning)
  and the `RSCP_SSO_*` environment variables. Schema migration 22 deletes the
  stored SSO settings. If SSO was configured, every administrator gets a
  one-time notification (the bell) pointing here.
- The **POS PIN/badge sign-in page** (`/pos/login`). POS staff use `/login`;
  `/pos/logout` still works (it is the normal logout).
- **PIN and badge manager approvals.** Voids, discounts over the limit and
  refund access now take the manager's **username and password**. PINs and
  badge IDs already stored are kept for a future PIN feature, but nothing
  uses them and the admin page no longer sets them.

If staff used Google sign-in, after the upgrade:

1. In **Admin > Settings > Cloudflare Access Sign-in**, enter the team domain
   and AUD tag, tick the switch and save (details below). If the SSO allowed
   domains were set and Access had no domain list of its own, migration 22
   copied them into *Allowed email domains*; check that it is what you want.
2. In **Admin > Settings > User Management**, each user shows their sign-in
   email ("No sign-in email" when none). For each person who signs in through
   Access, press **Edit** and set *Sign-in email* to the Google address they
   use in Access. Emails that SSO already matched on are kept. Each admin
   sets their own (an admin can't change another admin's).
3. Accounts that SSO created automatically have no password. Set one under
   **Edit > New Password** if the person should also be able to sign in
   without Cloudflare Access (e.g. on the LAN).
4. Open Admin through the tunnel and press **Test this browser's Access
   token** to check the mapping. Remove any `RSCP_SSO_*` lines from
   `/etc/default/rscp`; they are ignored now.

### Scheduled jobs

Periodic work runs as short systemd jobs, not inside the web service, so a
worker restart never stops it and each run's result is visible. Every job is
`python -m app.jobs <name>` as the `rscp` user, with the same sandbox as
`rscp.service`:

| Timer | Job | When |
|-------|-----|------|
| `rscp-package-maintenance.timer` | `rscp-job@package-maintenance` | every 15 minutes: package statuses follow the calendar (on time, expected, past due), `AUTO_TRIM` |
| `rscp-eod-email.timer` | `rscp-job@eod-email` | checks every 5 minutes; sends the POS end-of-day email once a day, at the first check at or after the time set in POS settings |
| `rscp-monitor.timer` | `rscp-job@monitor` | every 2 minutes: email alerts (below) |
| `rscp-backup.timer` | `rscp-backup` | see [Backups](#backups) |
| (none) | `system-migrations` | `rscp.service` runs it before starting, after `app.migrate`: post-update system steps, once per start |

The timers check often instead of firing at the configured time, so times
changed in the web UI apply without a root step. A job that is still running
when its timer fires again is skipped (each job has its own lock). The
end-of-day email is marked as sent in the database before and after sending,
so it never goes out twice for one day: a failed send is retried at the next
check, and an attempt that was cut off mid-send is reported, not repeated.
**Admin > Logs > Scheduled jobs** shows each job's last run, result and last
error.

**Email alerts** (the `monitor` job; Admin > Settings > Notifications) use the
POS SMTP settings and stay silent until those are filled in. They mail once
when a job or the nightly backup fails (a reminder at most every 6 hours while
it keeps failing) and once when it works again; once when a job has not
succeeded for far too long (backup: 36 hours); and once when a paired customer
display has not checked in for 10 minutes (configurable) while the store is
open (Tuesday-Saturday 10:00-18:00 Denver time), plus a "back online" mail.
Recipients default to the POS report recipients. The register's
**Pair Devices** menu shows when each display and scanner last checked in.

```bash
systemctl list-timers 'rscp-*'                    # next and last runs
journalctl -u rscp-job@eod-email -n 50            # one job's log ('rscp-job@*' for all)
systemctl status rscp-job@package-maintenance     # last run's exit status
sudo systemctl start rscp-job@package-maintenance # run one now
sudo -u rscp RSCP_DATA_DIR=/var/lib/rscp /opt/rscp/current/venv/bin/python -m app.jobs --list
```

Nothing inside the web service schedules these jobs. If the units are missing
(an install still in the 2.x layout, or an update run by an older updater that
did not install them), no scheduled job or nightly backup runs: the service
logs a warning at startup and **Admin > Logs > Scheduled jobs** shows it. Put
the units back with `sudo /opt/rscp/bin/rscp-update --install-units /opt/rscp/current`
(or move a 2.x install, see below).

### Backups

RSCP backs itself up once a day (**Admin > Settings > Automatic Backups**).
Each backup is two files in `/var/lib/rscp/backups/nightly/`:

| File | Contents |
|------|----------|
| `rscp-YYYYmmdd-HHMMSS.db` | the database, copied with SQLite's online backup API and integrity-checked |
| `uploads-YYYYmmdd-HHMMSS.tar.gz` | `uploads/` (item and package photos) plus `config.json` |

`config.json` holds `SECRET_KEY`, which encrypts the stored SMTP password and
webhook URLs. Treat the tarballs (and the off-box folder) as secrets.

Files are written under a temporary name and renamed when complete. After
each successful backup, old ones are pruned: the newest backup of each of the
last **14 days** and of each of the last **8 weeks** is kept (both numbers are
settings). Other files in the folder are left alone, including the updater's
`pre-update-*.db` copies.

**Schedule.** `rscp-backup.timer` starts `rscp-backup.service` every 15
minutes. The service runs `python -m app.backup --scheduled`, which backs up
only when no backup has succeeded since the last **backup time** (setting,
default 02:30). The timer checks often instead of firing at a fixed time, so a
time changed in the web UI applies at once, with no root step to rewrite the
unit. A backup missed while the server was off runs at the next check. A failed
run is retried after an hour. The card shows the last run, its sizes and any
error, and **Run backup now** starts one right away. Admins see a banner when
backups are on and none has succeeded for 36 hours.

```bash
systemctl list-timers rscp-backup        # next check
journalctl -u rscp-backup                # backup log
sudo -u rscp RSCP_DATA_DIR=/var/lib/rscp /opt/rscp/current/venv/bin/python -m app.backup   # back up now
sudo -u rscp RSCP_DATA_DIR=/var/lib/rscp /opt/rscp/current/venv/bin/python -m app.backup --status
```

Without the timer no scheduled backups run; see [Scheduled jobs](#scheduled-jobs).

**Off-box copy (recommended).** Local backups don't survive a dead disk. Mount
a NAS share on the server, give the `rscp` user write access to a folder on
it, enter that folder under **Off-box folder**, then run the updater once so
the backup service may write there:

```bash
# e.g. SMB, in /etc/fstab (credentials file mode 600):
# //nas/backups  /mnt/nas  cifs  credentials=/etc/rscp-nas.cred,uid=rscp,gid=rscp,file_mode=0640,dir_mode=0750,_netdev,nofail  0 0
sudo mkdir -p /mnt/nas && sudo mount /mnt/nas && sudo -u rscp mkdir -p /mnt/nas/rscp-backups
# set Admin > Settings > Automatic Backups > Off-box folder = /mnt/nas/rscp-backups, then:
sudo /opt/rscp/bin/rscp-update --backup-paths
```

Why the extra step: `rscp-backup.service` runs with `ProtectSystem=strict`, so
it can only write to `/var/lib/rscp`. `--backup-paths` (also run on every
install and update) reads the setting from the database and writes the drop-in
`/etc/systemd/system/rscp-backup.service.d/offsite.conf` with
`ReadWritePaths=-<folder>`. Only the backup service gets this; the web service
never writes to the share. For the same reason, after **Run backup now** the
copy is made by the next timer check (within 15 minutes). The path must be
plain (letters, digits, `._-@+/`, no spaces) and not under a system directory.

After every local backup the new files are copied there and pruned with the
same retention. A failed copy (share not mounted, full, read-only) is shown on
the card and retried hourly. It never fails or deletes local backups. RSCP
refuses to copy when the folder is missing or sits on the same disk as the
local backups, which is what an unmounted mount point looks like, so it can't
quietly fill the local disk.

**Restore.**

- *Database, same server:* **Automatic Backups > Restore a nightly backup**,
  then choose the backup and confirm. It uses the same safe path as the ZIP
  restore: the file is checked first (integrity, RSCP tables, at least one
  user), the current database is saved to
  `backups/pre-restore-<time>.db`, the backup is copied into the live
  database through SQLite (safe while workers are running), and migrations
  bring an older backup up to the current schema. Everyone is logged out.
- *From the NAS or another machine:* upload the `rscp-*.db` file under
  **Backup & Restore > Restore** (it accepts the export `.zip` or a nightly
  `.db`), or copy it into `/var/lib/rscp/backups/nightly/` (owner `rscp`)
  and it appears in the list.
- *Uploads and config.json* are restored by hand:

  ```bash
  sudo systemctl stop rscp
  sudo -u rscp tar -xzf uploads-YYYYmmdd-HHMMSS.tar.gz -C /var/lib/rscp uploads   # photos
  # config.json only when rebuilding a server (keeps encrypted settings readable):
  sudo -u rscp tar -xzf uploads-YYYYmmdd-HHMMSS.tar.gz -C /var/lib/rscp config.json
  sudo systemctl start rscp
  ```
- *New server after a disk failure:* install RSCP and finish the setup
  wizard, then restore `config.json` and `uploads/` as above (stop, extract,
  start). Log in again with the wizard's admin and restore the `.db` from the
  web UI.

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
When all access is over HTTPS, you can also turn on "HTTPS-only cookies"
(Admin > Settings > Reverse Proxy / Tunnel; restart afterwards).

### Cloudflare Access sign-in (optional)

If the tunnel's hostname is protected by a Cloudflare Access application,
RSCP can sign staff in from their Access login, so they don't log in twice.
It is off by default. Cloudflare adds a signed token (`Cf-Access-Jwt-Assertion`)
to every request it proxies. RSCP checks its RS256 signature against your
team's public keys (only RS256 is accepted), plus its audience, issuer and
expiry (exp/nbf/iat, 60 s clock skew), then signs in the
RSCP account whose **email** matches the token's email. Anyone can fake the
header on the LAN, but not the signature.

1. **Team domain:** Zero Trust dashboard > Settings > *Team name and domain*,
   e.g. `yourteam.cloudflareaccess.com`.
2. **AUD tag:** Zero Trust > Access > Applications > your RSCP application >
   Configure > *Basic information* > "Application Audience (AUD) Tag" (a
   64-character string).
3. In RSCP, open **Admin > Settings > Cloudflare Access Sign-in**, enter both,
   tick the switch and save. *Allowed email domains* is optional (when blank,
   your Access policy decides who gets through).
4. **Set each staff member's Workspace email on their RSCP account** (Admin >
   Settings > User Management > Edit > *Sign-in email*; the user list shows
   who has one). Matching uses that email only, never the username, and no
   accounts are created automatically. An admin can't set another admin's
   email; each admin sets their own. If someone has no linked account, they
   get the normal login page with an explanation.
5. Open Admin through the tunnel and press **Test this browser's Access
   token**. It shows whether the request carried a valid token and which RSCP
   user it maps to (the Test button works before you turn the switch on).

**Logout:** the Access token is sent with every request, so without a pause
the next page load would sign the user straight back in. After **Log out**,
RSCP sets a `rscp_cf_logged_out` cookie that turns off automatic sign-in for
that browser until the user signs in again (password or the login page's
**Sign in with Cloudflare Access** button) or 12 hours pass. Logging out of
RSCP doesn't end the Cloudflare session. To end that too (e.g. on a shared
PC), visit `https://<your RSCP hostname>/cdn-cgi/access/logout`.

The keys are fetched from `https://<team domain>/cdn-cgi/access/certs` (5 s
timeout) and cached for an hour. A token signed with a new key ID triggers a
refetch, at most every 30 seconds. Failures are logged with a reason, never
the token. If RSCP can't verify a token, the user just sees the normal login
page.

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

### First Run
On first run, the setup wizard creates:
- `config.json` - Application configuration
- `rscp.db` - SQLite database

## Project Structure

```
rscp/
├── app/
│   ├── routes/         # Flask blueprints (main, admin, inventory, auth)
│   ├── services/       # Business logic (db, auth, data_manager, throttle, ...)
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
pip install -r requirements-dev.txt
python -m pytest -q -p no:cacheprovider tests/
```

Tests never touch real data: `tests/conftest.py` points `RSCP_DATA_DIR` at a
temporary folder and builds each app on a fresh database.

GitHub Actions (`.github/workflows/tests.yml`) runs on every push and pull
request: the suite on Python 3.11 and 3.12 (Ubuntu, UTC), plus a lint job
(`bash -n install.sh`, `shellcheck install.sh`, `python -m compileall app`).
Merge and release only from a green run.

## Releases

### Channels

| Channel | What it installs | Who reads it |
|---------|------------------|--------------|
| **stable** | the newest GitHub Release (`releases/latest`), else the highest `vX.Y.Z` tag | `install.sh` default, the updater's *Stable* button |
| **beta** | the tip of the `beta` branch | `install.sh --channel beta`, the updater's *Beta* button |

Both the installer and **Admin > System Updates** download a GitHub tarball
(`archive/refs/tags/vX.Y.Z.tar.gz` or `archive/refs/heads/beta.tar.gz`), so a
release only exists once its tag is pushed. Pin a host to one release with
`sudo /opt/rscp/bin/rscp-update --upgrade --version vX.Y.Z` (or
`install.sh --version vX.Y.Z` for a fresh install).

Until the first tag exists, stable falls back to the `main` branch, and the
install one-liner always fetches `install.sh` from `main`, so keep `main` on
the latest release.

### Branch flow

`dev` -> `beta` -> `main`, fast-forward only. Work lands on `dev` (directly or
by merging feature branches); `beta` is moved up to a tested `dev` commit;
`main` is moved up to the commit a release tag points at. No automation
commits to any of these branches, so `git push origin dev:beta` and the
`main` update below never need a merge or a force.

### Cutting a release

1. On `dev`, bump `VERSION` (e.g. `3.0.0`) in its own commit and push. Wait
   for the Tests run to pass.
2. Promote to beta and let beta hosts run it:
   `git push origin dev:beta`
3. When beta is good, tag that exact commit, push the tag, and move `main`:

   ```bash
   git fetch origin
   git tag -a v3.0.0 -m "RSCP 3.0.0" origin/beta
   git push origin v3.0.0
   git push origin "v3.0.0^{commit}:refs/heads/main"
   ```

4. Publish a GitHub Release for the tag (this is what `releases/latest`
   returns, and what turns the stable update on for every host):

   ```bash
   gh release create v3.0.0 --verify-tag --title "RSCP 3.0.0" --notes-file NOTES.md
   ```

   Use `--prerelease` for a release candidate: pre-releases are never
   "latest". If you ever publish a fix for an older line after a newer
   release, pass `--latest=false` so stable does not go backwards.

Tags are permanent: never move or re-push a published tag. Ship a new patch
version instead.

### Repairing `main` (one time)

`main` is stale at 2.6.0 and carries one commit that is not on `dev`
(`0fd7446 Delete tests directory`), so it cannot be fast-forwarded. Its
parent is already on `dev`, so nothing is lost by realigning it. Either point
it at the current release on `dev` now, or do it as part of the first
release (step 3 above, with `--force-with-lease` added once):

```bash
git fetch origin
git push --force-with-lease=main:0fd74468 origin origin/dev:refs/heads/main
```

This rewrites `main`, so it needs the repo owner's approval and, if branch
protection is on, a temporary allowance for force-pushes. After that, `main`
only ever moves forward.

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
