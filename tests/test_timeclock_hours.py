"""Overtime is per workweek, breaks are paid by default, PTO never counts.

Entries are stored in UTC; workweeks are local (America/Denver) calendar weeks.
"""
import datetime

import pytest

from app.services import timeclock_hours as tc
from app.services.timeclock_hours import summarize

ANCHOR = datetime.date(2026, 10, 4)  # a Sunday: workweeks run Sun-Sat


@pytest.fixture(autouse=True)
def denver(monkeypatch):
    zoneinfo = pytest.importorskip('zoneinfo')
    try:
        tz = zoneinfo.ZoneInfo('America/Denver')
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip('tz database unavailable (pip install tzdata)')
    monkeypatch.setattr(tc, 'LOCAL_TZ', tz)
    return tz


def _shift(day, start_h, hours, kind='shift', month=10):
    """An entry starting at local `start_h` on that day, lasting `hours` of real time."""
    start = tc.local_to_utc(datetime.datetime(2026, month, day, start_h))
    end = start + datetime.timedelta(hours=hours)
    return {'type': kind, 'clock_in': start.strftime(tc.FMT), 'clock_out': end.strftime(tc.FMT)}


def test_biweekly_30_plus_30_has_no_overtime():
    entries = [_shift(d, 9, 6) for d in (5, 6, 7, 8, 9)] + [_shift(d, 9, 6) for d in (12, 13, 14, 15, 16)]
    t = summarize(entries, ANCHOR, 40)
    assert (t['regular'], t['overtime']) == (60.0, 0.0)


def test_overtime_counted_in_the_heavy_week_only():
    week1 = [_shift(d, 8, 10) for d in (5, 6, 7, 8, 9)]   # 50h
    week2 = [_shift(d, 9, 6) for d in (12, 13, 14, 15, 16)]  # 30h
    t = summarize(week1 + week2, ANCHOR, 40)
    assert (t['regular'], t['overtime']) == (70.0, 10.0)


def test_shift_crossing_the_week_boundary_is_split():
    # 38h earlier in the week, then Sat 20:00 -> Sun 04:00 local (4h each side)
    entries = [_shift(5, 6, 19), _shift(6, 6, 19), _shift(10, 20, 8)]
    t = summarize(entries, ANCHOR, 40)
    assert t['overtime'] == 2.0  # 38 + 4 = 42 in week one; 4 in week two


def test_week_boundary_is_local_midnight_not_utc_midnight():
    # Sat 17:00-23:00 local is Sat 23:00 -> Sun 05:00 UTC: all of it is week one.
    entries = [_shift(5, 6, 19), _shift(6, 6, 19), _shift(10, 17, 6)]
    t = summarize(entries, ANCHOR, 40)
    assert t['overtime'] == 4.0  # 38 + 6 = 44 in week one


def test_breaks_paid_by_default_and_optional():
    entries = [_shift(5, 9, 4), _shift(5, 13, 0.5, 'break'), _shift(5, 13, 4)]
    assert summarize(entries, ANCHOR, 40)['regular'] == 8.5
    assert summarize(entries, ANCHOR, 40, paid_breaks=False)['regular'] == 8.0


def test_pto_is_separate_from_overtime():
    entries = [_shift(d, 8, 10) for d in (5, 6, 7, 8)] + [_shift(9, 8, 8, 'pto')]
    t = summarize(entries, ANCHOR, 40)
    assert (t['overtime'], t['pto'], t['total']) == (0.0, 8.0, 48.0)


def test_fall_back_shift_counts_its_real_duration():
    # Sun Nov 1 2026, clocks go 02:00 MDT -> 01:00 MST. 00:00 -> 06:00 on the
    # wall is 7 real hours.
    entry = {'type': 'shift',
             'clock_in': tc.local_to_utc_str('2026-11-01 00:00:00'),
             'clock_out': tc.local_to_utc_str('2026-11-01 06:00:00')}
    assert entry['clock_in'] == '2026-11-01 06:00:00'   # MDT, UTC-6
    assert entry['clock_out'] == '2026-11-01 13:00:00'  # MST, UTC-7
    assert summarize([entry], ANCHOR, 40)['regular'] == 7.0


def test_fall_back_shift_split_at_the_local_week_boundary():
    # Sat Oct 31 22:00 MDT -> Sun Nov 1 06:00 MST: 2h in week one, 7h in week two.
    start = tc.local_to_utc(datetime.datetime(2026, 10, 31, 22))
    end = tc.local_to_utc(datetime.datetime(2026, 11, 1, 6))
    weeks = tc.hours_by_workweek(start, end, ANCHOR)
    assert weeks == {datetime.date(2026, 10, 25): 2.0, datetime.date(2026, 11, 1): 7.0}


def test_spring_forward_shift_skips_the_missing_hour():
    # Sun Mar 8 2026, 02:00 MST -> 03:00 MDT. 00:00 -> 06:00 on the wall is 5h.
    entry = {'type': 'shift',
             'clock_in': tc.local_to_utc_str('2026-03-08 00:00:00'),
             'clock_out': tc.local_to_utc_str('2026-03-08 06:00:00')}
    assert summarize([entry], datetime.date(2026, 3, 1), 40)['regular'] == 5.0


def test_open_entry_runs_until_now():
    entry = {'type': 'shift', 'clock_in': '2026-10-05 15:00:00', 'clock_out': None}
    now = datetime.datetime(2026, 10, 5, 18, 30, tzinfo=datetime.timezone.utc)
    assert summarize([entry], ANCHOR, 40, now=now)['regular'] == 3.5
