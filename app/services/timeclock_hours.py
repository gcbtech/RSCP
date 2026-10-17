"""
Worked-hours and overtime arithmetic for the timeclock.

One implementation shared by the timeclock home page and the payroll report,
so they cannot disagree.

Rules:
- Overtime is computed per WORKWEEK, not per pay period (FLSA: hours over the
  threshold in a fixed 7-day workweek). With biweekly pay, 30 + 30 hours is
  0 overtime and 50 + 30 is 10, never "60 - 40 = 20".
- Workweeks are LOCAL calendar weeks starting on the weekday of the configured
  pay-period start date.
- A shift that crosses the workweek boundary is split at that local midnight.
- Hours are real elapsed time: a shift across the November fall-back counts
  the repeated hour, one across the March spring-forward does not count the
  skipped hour.
- Breaks count as worked time when TIMECLOCK_PAID_BREAKS is true (default).
- PTO is reported separately and never counts toward overtime.

Storage: timeclock times (time_entries.clock_in/clock_out,
scheduled_shifts.start_time/end_time) are naive UTC strings
('YYYY-MM-DD HH:MM:SS'), like SQLite CURRENT_TIMESTAMP. Everything people see
or type is business-local time; convert at the edges with the helpers below.
"""
import datetime

FMT = '%Y-%m-%d %H:%M:%S'
WEEK = datetime.timedelta(days=7)
UTC = datetime.timezone.utc

# Business timezone. None = the server's system timezone (which is the
# business timezone, America/Denver). Tests set a zoneinfo.ZoneInfo here so
# results do not depend on the machine running them.
LOCAL_TZ = None


# --- Conversions (business-local <-> stored UTC) -----------------------------

def _parse(value):
    """'YYYY-MM-DD HH:MM[:SS]' (space or 'T') or a datetime -> naive datetime."""
    if isinstance(value, datetime.datetime):
        return value
    text = str(value).strip().replace('T', ' ')[:19]
    fmt = FMT if text.count(':') == 2 else '%Y-%m-%d %H:%M'
    return datetime.datetime.strptime(text, fmt)


def as_utc(value):
    """Stored UTC value (string or naive datetime) -> aware UTC datetime."""
    dt = _parse(value)
    if dt.tzinfo is not None:
        return dt.astimezone(UTC)
    return dt.replace(tzinfo=UTC)


def utc_now():
    return datetime.datetime.now(UTC)


def utc_now_str():
    return utc_now().strftime(FMT)


def to_local(value):
    """Stored UTC value -> naive business-local datetime (None if empty/bad)."""
    if not value:
        return None
    try:
        aware = as_utc(value)
    except (TypeError, ValueError):
        return None
    return aware.astimezone(LOCAL_TZ).replace(tzinfo=None)


def local_now():
    return to_local(utc_now())


def local_today():
    return local_now().date()


def local_to_utc(local):
    """Business-local wall time (naive datetime or string) -> aware UTC.

    Uses the offset in force on that date (DST-correct), like
    app.utils.helpers.local_to_utc_str.
    """
    dt = _parse(local)
    if LOCAL_TZ is not None:
        return dt.replace(tzinfo=LOCAL_TZ).astimezone(UTC)
    return dt.astimezone(UTC)  # naive -> interpreted in the system timezone


def local_to_utc_str(local):
    """Business-local wall time -> stored UTC string (None for empty input)."""
    if not local:
        return None
    return local_to_utc(local).strftime(FMT)


def local_dates_to_utc_range(start_date, end_date=None):
    """Local calendar dates (inclusive) -> (utc_start, utc_end) strings.

    Same rule as app.utils.helpers.local_date_to_utc_range: 00:00:00 of the
    first day to 23:59:59 of the last, each converted with its own offset.
    Use as `col >= ? AND col <= ?`.
    """
    end_date = end_date or start_date
    if isinstance(start_date, str):
        start_date = datetime.datetime.strptime(start_date[:10], '%Y-%m-%d').date()
    if isinstance(end_date, str):
        end_date = datetime.datetime.strptime(end_date[:10], '%Y-%m-%d').date()
    start = datetime.datetime.combine(start_date, datetime.time(0, 0, 0))
    end = datetime.datetime.combine(end_date, datetime.time(23, 59, 59))
    return local_to_utc_str(start), local_to_utc_str(end)


# --- Hours -------------------------------------------------------------------

def _week_start_date(local_day, anchor):
    """Local date starting the workweek that contains `local_day`."""
    weeks = (local_day - anchor).days // 7
    return anchor + datetime.timedelta(days=weeks * 7)


def hours_by_workweek(start, end, anchor):
    """{workweek_start_date: hours} for one UTC interval, split at local week boundaries."""
    start, end = as_utc(start), as_utc(end)
    out = {}
    cursor = start
    while cursor < end:
        ws = _week_start_date(to_local(cursor).date(), anchor)
        next_week = local_to_utc(datetime.datetime.combine(ws + WEEK, datetime.time()))
        boundary = min(next_week, end)
        out[ws] = out.get(ws, 0.0) + (boundary - cursor).total_seconds() / 3600
        cursor = boundary
    return out


def _end_or_now(entry, now):
    return as_utc(entry['clock_out']) if entry['clock_out'] else (as_utc(now) if now else utc_now())


def entry_hours(entry, now=None):
    """Hours of one time entry (open entries run until `now`, UTC)."""
    start = as_utc(entry['clock_in'])
    end = _end_or_now(entry, now)
    return max(0.0, (end - start).total_seconds() / 3600)


def summarize(entries, anchor, weekly_threshold=40.0, paid_breaks=True, now=None):
    """Totals for one employee's entries (clock_in/clock_out stored UTC).

    Returns dict(regular, overtime, pto, total) in hours (rounded to 2dp).
    """
    now = as_utc(now) if now else utc_now()
    weekly = {}
    pto = 0.0
    for e in entries:
        kind = e['type'] or 'shift'
        if kind == 'pto':
            pto += entry_hours(e, now)
            continue
        if kind == 'break' and not paid_breaks:
            continue
        start = as_utc(e['clock_in'])
        end = _end_or_now(e, now)
        if end <= start:
            continue
        for week, hours in hours_by_workweek(start, end, anchor).items():
            weekly[week] = weekly.get(week, 0.0) + hours

    overtime = sum(max(0.0, h - weekly_threshold) for h in weekly.values())
    worked = sum(weekly.values())
    return {
        'regular': round(worked - overtime, 2),
        'overtime': round(overtime, 2),
        'pto': round(pto, 2),
        'total': round(worked + pto, 2),
    }
