"""
Email alerts for problems nobody is looking at: the `monitor` job
(python -m app.jobs monitor, every 2 minutes from rscp-monitor.timer).

What it watches:

* Scheduled jobs and the nightly backup (rows of app.jobs.overview()): one
  email when a run fails, a reminder at most every JOB_REPEAT_AFTER while it
  keeps failing, and a recovery email at the next success. Also one email
  when a job has not succeeded for longer than STALE_AFTER (a timer that
  stopped, a backup that no longer runs), and one when it succeeds again.
* Customer displays (pos_peripherals with role 'display'): one email when a
  paired display has not polled for ALERT_DISPLAY_OFFLINE_MINUTES while the
  store is open (STORE_*), and one when it is back. Never repeated: a display
  left off over a weekend is reported once, the next time the store is open.
  Handheld scanners are shown in the register's device list but not alerted
  on (they are switched off and carried around all day).

Each open problem is a row in monitor_alerts (schema migration 14); a
problem without a row is new. One check sends at most one email (all of its
events together), and its rows are written only after that email went out,
so a failed send is simply retried by the next check (and the job shows as
failed in Admin > Logs > Scheduled jobs).

Right after a gap in checks (first run, server switched off) new "quiet" and
"stale" problems are not reported for one check: everything looked quiet
because RSCP itself was down, and displays and timers catch up within
seconds once it is back.

Settings (Admin > Settings > Notifications > Email alerts): ALERT_JOB_FAILURES,
ALERT_DISPLAY_OFFLINE, ALERT_DISPLAY_OFFLINE_MINUTES and ALERT_EMAIL_RECIPIENTS
(blank: the POS report recipients). Mail goes out through the POS SMTP
settings (pos_email.send_email); until those are filled in the check does
nothing at all.
"""
import datetime
import logging
import sqlite3
from html import escape

logger = logging.getLogger(__name__)

UTC = datetime.timezone.utc

# Store hours: alerts about quiet displays are only raised while the store is
# open. Tuesday to Saturday (Monday = 0), 10:00-18:00 store time.
STORE_TZ_NAME = 'America/Denver'
STORE_DAYS = (1, 2, 3, 4, 5)
STORE_OPEN = datetime.time(10, 0)
STORE_CLOSE = datetime.time(18, 0)
STORE_HOURS_TEXT = 'Tuesday-Saturday 10:00-18:00 (America/Denver)'

# A job that keeps failing is mailed again at most this often.
JOB_REPEAT_AFTER = datetime.timedelta(hours=6)

# A job that has not succeeded for this long is reported as stale. Only jobs
# with a fixed cadence: system-migrations runs once per service start.
STALE_AFTER = {
    'package-maintenance': datetime.timedelta(hours=3),   # every 15 minutes
    'eod-email': datetime.timedelta(hours=3),             # checks every 5 minutes
    'backup': datetime.timedelta(hours=36),               # daily (= backup.STALE_AFTER)
}

# Not alerted on: the monitor's own failures are mostly failed sends, which
# another email cannot report. They show in Admin > Logs like any job's.
SKIP_JOBS = frozenset({'monitor'})

# Checks closer together than this count as "monitoring was running".
WARM_GAP = datetime.timedelta(minutes=10)
LAST_CHECK_MARKER = 'monitor:last-check'

JOB_KINDS = ('job_failed', 'job_stale')
DISPLAY_KIND = 'display_offline'


class AlertSendError(RuntimeError):
    """The alert email could not be sent (nothing was recorded)."""


# --- time --------------------------------------------------------------------

def _utcnow():
    return datetime.datetime.now(UTC)


def _store_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(STORE_TZ_NAME)
    except Exception:  # no tz database: the server's own zone (also the store's)
        return None


def store_time(now):
    """Aware UTC datetime -> aware store-local datetime."""
    tz = _store_tz()
    return now.astimezone(tz) if tz is not None else now.astimezone()


def store_open_for(now):
    """How long the store has been open at `now` (a timedelta), or None
    while it is closed."""
    local = store_time(now)
    if local.weekday() not in STORE_DAYS or not (STORE_OPEN <= local.time() < STORE_CLOSE):
        return None
    opened = local.replace(hour=STORE_OPEN.hour, minute=STORE_OPEN.minute, second=0, microsecond=0)
    return local - opened


def _server_local(now):
    """job_status and the backup status file keep the server's naive local time."""
    return now.astimezone().replace(tzinfo=None)


def _parse_local(value):
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def _parse_utc(value):
    """SQLite CURRENT_TIMESTAMP text, or our own ISO strings -> aware UTC."""
    if not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(value).strip().replace(' ', 'T').rstrip('Z'))
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _iso(dt):
    return dt.astimezone(UTC).isoformat(timespec='seconds')


def _store_text(dt, now):
    """'14:05' today, else 'Sat 2026-10-17 14:05' (store time)."""
    local = store_time(dt)
    return local.strftime('%H:%M' if local.date() == store_time(now).date() else '%a %Y-%m-%d %H:%M')


# --- settings / email --------------------------------------------------------

def alert_recipients():
    from app.services import settings as st
    return (st.get('ALERT_EMAIL_RECIPIENTS') or '').strip() or (st.get('POS_EMAIL_RECIPIENTS') or '').strip()


def email_ready():
    """True when an alert email could be sent (SMTP host and recipients set)."""
    from app.services import settings as st
    return bool((st.get('POS_EMAIL_HOST') or '').strip() and alert_recipients())


class Event:
    def __init__(self, action, kind, subject, title, headline, lines=(), since=None):
        self.action = action        # 'alert' | 'remind' | 'recover'
        self.kind = kind
        self.subject = subject      # job name or peripheral id
        self.title = title
        self.headline = headline
        self.lines = list(lines)
        self.since = since

    @property
    def key(self):
        return f'{self.kind}:{self.subject}'

    def __repr__(self):
        return f'<Event {self.action} {self.key}: {self.headline}>'


def compose(events):
    """(subject, text, html) for one email about `events`."""
    from app.services import settings as st
    org = (st.get('ORG_NAME') or st.get('RECEIPT_STORE_NAME') or '').strip()
    prefix = f'[{org} RSCP]' if org else '[RSCP]'
    if len(events) == 1:
        subject = f'{prefix} {events[0].headline}'
    else:
        subject = f'{prefix} {len(events)} alerts: ' + '; '.join(e.headline for e in events)
        if len(subject) > 150:
            subject = subject[:147] + '...'
    footer = ('Sent by RSCP. Job details: Admin > Logs > Scheduled jobs. '
              'Alert settings: Admin > Settings > Notifications.')
    text = '\n\n'.join('\n'.join([e.headline] + [f'  {line}' for line in e.lines]) for e in events)
    text += f'\n\n-- \n{footer}\n'
    colors = {'alert': '#dc3545', 'remind': '#fd7e14', 'recover': '#198754'}
    items = ''.join(
        f"<li style='margin-bottom: 12px;'><strong style='color: {colors[e.action]};'>{escape(e.headline)}</strong>"
        + ''.join(f'<br>{escape(line)}' for line in e.lines) + '</li>'
        for e in events)
    html = (f"<html><body style=\"font-family: 'Segoe UI', sans-serif; color: #333; line-height: 1.5;\">"
            f"<ul style='padding-left: 18px;'>{items}</ul>"
            f"<p style='font-size: 0.8em; color: #999;'>{escape(footer)}</p></body></html>")
    return subject, text, html


def send(events):
    """Email `events` in one message. Raises AlertSendError."""
    from app.services import pos_email
    subject, text, html = compose(events)
    try:
        pos_email.send_email(subject, html, alert_recipients(), text=text)
    except Exception as e:
        raise AlertSendError(f'Could not send the alert email ({subject}): {e}') from e
    logger.info(f"[Monitor] Sent: {subject}")


# --- storage -----------------------------------------------------------------

def _connect(db_path):
    from app import jobs
    from app.services.migration import MONITOR_TABLES_SQL
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    jobs.create_tables(conn)            # job_markers (the last-check marker)
    for sql in MONITOR_TABLES_SQL:
        conn.execute(sql)
    conn.commit()
    return conn


def open_alerts(conn):
    return {r['key']: dict(r) for r in conn.execute('SELECT * FROM monitor_alerts')}


def _was_running(conn, now):
    row = conn.execute('SELECT value FROM job_markers WHERE name = ?', (LAST_CHECK_MARKER,)).fetchone()
    last = _parse_utc(row['value']) if row else None
    return last is not None and now - last <= WARM_GAP


def _apply(conn, events, drop, now):
    for e in events:
        if e.action == 'alert':
            conn.execute('INSERT OR REPLACE INTO monitor_alerts (key, kind, subject, title, since, last_sent, detail) '
                         'VALUES (?, ?, ?, ?, ?, ?, ?)',
                         (e.key, e.kind, e.subject, e.title, e.since, _iso(now), '\n'.join(e.lines)))
        elif e.action == 'remind':
            conn.execute('UPDATE monitor_alerts SET last_sent = ?, detail = ? WHERE key = ?',
                         (_iso(now), '\n'.join(e.lines), e.key))
        else:
            conn.execute('DELETE FROM monitor_alerts WHERE key = ?', (e.key,))
    for key in drop:
        conn.execute('DELETE FROM monitor_alerts WHERE key = ?', (key,))
    conn.execute('INSERT INTO job_markers (name, value, updated_at) VALUES (?, ?, ?) '
                 'ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at',
                 (LAST_CHECK_MARKER, _iso(now), _iso(now)))


# --- the checks --------------------------------------------------------------

def job_events(rows, alerts, now, warm, backup_enabled=True):
    """Events for job status rows (as app.jobs.overview() returns them).
    Returns (events, keys of open alerts to drop silently)."""
    events, drop = [], []
    local_now = _server_local(now)
    for row in rows:
        name, title = row['name'], row['title']
        if name in SKIP_JOBS:
            continue
        failed_key, stale_key = f'job_failed:{name}', f'job_stale:{name}'
        failed = row.get('result') == 'failed'
        error = (row.get('last_error') or 'no error message').strip()
        if len(error) > 500:
            error = error[:497] + '...'
        when = (row.get('last_failure') or row.get('last_finished') or '').replace('T', ' ')

        if failed:
            open_ = alerts.get(failed_key)
            if open_ is None:
                lines = [f'Failed at {when}.' if when else 'The last run failed.', f'Error: {error}']
                if (row.get('failures') or 0) > 1:
                    lines.append(f"{row['failures']} failed runs in a row.")
                events.append(Event('alert', 'job_failed', name, title, f'{title} failed', lines, since=when))
            else:
                last_sent = _parse_utc(open_.get('last_sent'))
                if last_sent is None or now - last_sent >= JOB_REPEAT_AFTER:
                    hours = int(JOB_REPEAT_AFTER.total_seconds() // 3600)
                    events.append(Event('remind', 'job_failed', name, title, f'{title} is still failing', [
                        f"Failing since {open_.get('since') or 'earlier'}; last failure {when}.",
                        f'Error: {error}',
                        f'(A reminder; sent at most every {hours} hours while it keeps failing.)'],
                        since=open_.get('since')))
        elif row.get('result') == 'ok' and failed_key in alerts:
            ok_at = (row.get('last_success') or '').replace('T', ' ')
            events.append(Event('recover', 'job_failed', name, title, f'{title} is working again', [
                f'Succeeded at {ok_at}.' if ok_at else 'The last run succeeded.',
                f"It had been failing since {alerts[failed_key].get('since') or 'earlier'}."]))

        limit = STALE_AFTER.get(name)
        if limit is None or (name == 'backup' and not backup_enabled):
            if stale_key in alerts:
                drop.append(stale_key)
            continue
        last_ok = _parse_local(row.get('last_success'))
        stale = last_ok is not None and local_now - last_ok > limit
        last_ok_text = last_ok.strftime('%Y-%m-%d %H:%M') if last_ok else ''
        if stale and not failed and stale_key not in alerts and failed_key not in alerts and warm:
            hours = int(limit.total_seconds() // 3600)
            events.append(Event('alert', 'job_stale', name, title, f'{title} has not run for {hours}+ hours', [
                f'Last success: {last_ok_text}, more than {hours} hours ago.',
                f"Its schedule: {row.get('schedule') or 'unknown'}. Check the timers with "
                "systemctl list-timers 'rscp-*'."], since=last_ok_text))
        elif not stale and stale_key in alerts:
            events.append(Event('recover', 'job_stale', name, title, f'{title} is running again', [
                f'Succeeded at {last_ok_text}.' if last_ok_text else 'It ran again.']))
    # Jobs that no longer exist (renamed or removed in an update)
    names = {row['name'] for row in rows}
    drop += [k for k, a in alerts.items() if a['kind'] in JOB_KINDS and a['subject'] not in names]
    return events, drop


def display_rows(conn):
    return [dict(r) for r in conn.execute(
        '''SELECT p.peripheral_id, p.friendly_name, p.last_seen, p.paired_at,
                  r.friendly_name AS register_name
           FROM pos_peripherals p
           LEFT JOIN pos_registers r ON r.register_id = p.register_id
           WHERE p.role = 'display'
           ORDER BY p.paired_at''')]


def display_events(displays, alerts, now, minutes, warm):
    """Events for paired customer displays. Returns (events, keys to drop):
    open alerts of displays that are no longer paired are dropped silently."""
    limit = datetime.timedelta(minutes=minutes)
    open_for = store_open_for(now)
    may_alert = warm and open_for is not None and open_for >= limit
    events, present = [], set()
    for d in displays:
        pid = d['peripheral_id']
        key = f'{DISPLAY_KIND}:{pid}'
        present.add(key)
        name = d['friendly_name'] or 'Customer Display'
        title = f"{name} ({d['register_name']})" if d.get('register_name') else name
        # Never polled since pairing: count from the pairing.
        seen = _parse_utc(d.get('last_seen')) or _parse_utc(d.get('paired_at'))
        quiet = seen is None or now - seen > limit
        seen_text = _store_text(seen, now) if seen else 'never'
        if quiet and key not in alerts and may_alert:
            events.append(Event('alert', DISPLAY_KIND, pid, title, f'{title} is offline', [
                f'Last seen {seen_text} (store time), more than {minutes} minutes ago, while the store is open.',
                'Check that the display is switched on, on the network and showing the POS page; '
                'it reconnects by itself once it can reach RSCP.'], since=seen_text))
        elif not quiet and key in alerts:
            events.append(Event('recover', DISPLAY_KIND, pid, title, f'{title} is back online', [
                f"Checking in again (offline since {alerts[key].get('since') or 'earlier'})."]))
    drop = [k for k, a in alerts.items() if a['kind'] == DISPLAY_KIND and k not in present]
    return events, drop


def run_checks(db_path=None, now=None):
    """One monitor pass. Returns a one-line summary; raises AlertSendError
    when the email could not be sent (then nothing is recorded)."""
    from app import jobs
    from app.services import settings as st
    db_path = jobs._db_path(db_path)
    now = now or _utcnow()
    if not email_ready():
        return 'alert email not set up (POS SMTP host and recipients): nothing checked'

    conn = _connect(db_path)
    try:
        alerts = open_alerts(conn)
        warm = _was_running(conn, now)
        events, drop = [], []

        if st.get('ALERT_JOB_FAILURES'):
            _runner, rows = jobs.overview(db_path)
            ev, dr = job_events(rows, alerts, now, warm, backup_enabled=bool(st.get('BACKUP_ENABLED')))
            events += ev
            drop += dr
        else:
            drop += [k for k, a in alerts.items() if a['kind'] in JOB_KINDS]

        if st.get('ALERT_DISPLAY_OFFLINE') and st.get('POS_ENABLED'):
            ev, dr = display_events(display_rows(conn), alerts, now,
                                    st.get('ALERT_DISPLAY_OFFLINE_MINUTES'), warm)
            events += ev
            drop += dr
        else:
            drop += [k for k, a in alerts.items() if a['kind'] == DISPLAY_KIND]

        if events:
            send(events)
        _apply(conn, events, drop, now)
        conn.commit()
        still_open = len(open_alerts(conn))
    finally:
        conn.close()

    parts = []
    if events:
        parts.append(f"emailed {len(events)}: " + '; '.join(e.headline for e in events))
    parts.append(f'{still_open} open alert(s)' if still_open else 'no open alerts')
    if not warm:
        parts.append('first check after a gap: new quiet/stale problems wait for the next check')
    return '; '.join(parts)


# --- admin panel -------------------------------------------------------------

def admin_summary(db_path=None):
    """What Admin > Settings > Notifications shows about email alerts."""
    from app import jobs
    from app.services import settings as st
    summary = {
        'email_ready': False, 'recipients': '', 'open': [], 'store_hours': STORE_HOURS_TEXT,
        'settings': st.get_many(['ALERT_EMAIL_RECIPIENTS', 'ALERT_JOB_FAILURES', 'ALERT_DISPLAY_OFFLINE',
                                 'ALERT_DISPLAY_OFFLINE_MINUTES']),
    }
    try:
        summary['email_ready'] = email_ready()
        summary['recipients'] = alert_recipients()
        conn = sqlite3.connect(jobs._db_path(db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            summary['open'] = [dict(r) for r in conn.execute(
                'SELECT kind, title, since FROM monitor_alerts ORDER BY kind, title')]
        finally:
            conn.close()
    except sqlite3.Error as e:  # not migrated yet
        logger.debug(f"[Monitor] alert summary unavailable: {e}")
    return summary
