"""
Worked-hours and overtime arithmetic for the timeclock.

One implementation shared by the timeclock home page and the payroll report,
so they cannot disagree.

Rules:
- Overtime is computed per WORKWEEK, not per pay period (FLSA: hours over the
  threshold in a fixed 7-day workweek). With biweekly pay, 30 + 30 hours is
  0 overtime and 50 + 30 is 10, never "60 - 40 = 20".
- Workweeks start on the weekday of the configured pay-period start date.
- A shift that crosses the workweek boundary is split at that midnight.
- Breaks count as worked time when TIMECLOCK_PAID_BREAKS is true (default).
- PTO is reported separately and never counts toward overtime.

Times are naive local timestamps ('YYYY-MM-DD HH:MM:SS'), as time_entries
stores them.
"""
import datetime

FMT = '%Y-%m-%d %H:%M:%S'
WEEK = datetime.timedelta(days=7)


def _parse(value):
    return datetime.datetime.strptime(str(value)[:19], FMT)


def _week_start(moment, anchor):
    """Midnight starting the workweek that contains `moment`."""
    anchor_dt = datetime.datetime.combine(anchor, datetime.time())
    weeks = (moment - anchor_dt) // WEEK
    return anchor_dt + weeks * WEEK


def hours_by_workweek(start, end, anchor):
    """{workweek_start_date: hours} for one interval, split at week boundaries."""
    out = {}
    cursor = start
    while cursor < end:
        ws = _week_start(cursor, anchor)
        boundary = min(ws + WEEK, end)
        out[ws.date()] = out.get(ws.date(), 0.0) + (boundary - cursor).total_seconds() / 3600
        cursor = boundary
    return out


def entry_hours(entry, now=None):
    """Hours of one time entry (open entries run until `now`)."""
    start = _parse(entry['clock_in'])
    end = _parse(entry['clock_out']) if entry['clock_out'] else (now or datetime.datetime.now())
    return max(0.0, (end - start).total_seconds() / 3600)


def summarize(entries, anchor, weekly_threshold=40.0, paid_breaks=True, now=None):
    """Totals for one employee's entries.

    Returns dict(regular, overtime, pto, total) in hours (rounded to 2dp).
    """
    now = now or datetime.datetime.now()
    weekly = {}
    pto = 0.0
    for e in entries:
        kind = e['type'] or 'shift'
        if kind == 'pto':
            pto += entry_hours(e, now)
            continue
        if kind == 'break' and not paid_breaks:
            continue
        start = _parse(e['clock_in'])
        end = _parse(e['clock_out']) if e['clock_out'] else now
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
