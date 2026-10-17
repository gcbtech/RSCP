"""Overtime is per workweek, breaks are paid by default, PTO never counts."""
import datetime

from app.services.timeclock_hours import summarize

ANCHOR = datetime.date(2026, 10, 4)  # a Sunday: workweeks run Sun-Sat


def _shift(day, start_h, hours, kind='shift'):
    start = datetime.datetime(2026, 10, day, start_h)
    end = start + datetime.timedelta(hours=hours)
    return {'type': kind, 'clock_in': start.strftime('%Y-%m-%d %H:%M:%S'),
            'clock_out': end.strftime('%Y-%m-%d %H:%M:%S')}


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
    # 38h earlier in the week, then Sat 20:00 -> Sun 04:00 (4h each side)
    entries = [_shift(5, 6, 19), _shift(6, 6, 19), _shift(10, 20, 8)]
    t = summarize(entries, ANCHOR, 40)
    assert t['overtime'] == 2.0  # 38 + 4 = 42 in week one; 4 in week two


def test_breaks_paid_by_default_and_optional():
    entries = [_shift(5, 9, 4), _shift(5, 13, 0.5, 'break'), _shift(5, 13, 4)]
    assert summarize(entries, ANCHOR, 40)['regular'] == 8.5
    assert summarize(entries, ANCHOR, 40, paid_breaks=False)['regular'] == 8.0


def test_pto_is_separate_from_overtime():
    entries = [_shift(d, 8, 10) for d in (5, 6, 7, 8)] + [_shift(9, 8, 8, 'pto')]
    t = summarize(entries, ANCHOR, 40)
    assert (t['overtime'], t['pto'], t['total']) == (0.0, 8.0, 48.0)
