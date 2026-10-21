from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from goalcal.domain.scheduler import (
    BusyBlock,
    Preferences,
    Requirement,
    free_intervals,
    propose,
)

TZ = ZoneInfo("Asia/Shanghai")
WED = datetime(2026, 10, 21, 9, 0, tzinfo=TZ)  # Wednesday


def test_free_intervals_subtract_busy_with_buffer():
    busy = [BusyBlock(datetime(2026, 10, 22, 10, 0, tzinfo=TZ),
                      datetime(2026, 10, 22, 11, 0, tzinfo=TZ))]
    gaps = free_intervals(
        busy,
        datetime(2026, 10, 22, 8, 0, tzinfo=TZ),
        datetime(2026, 10, 22, 12, 0, tzinfo=TZ),
        buffer_minutes=10,
        min_minutes=20,
    )
    assert [g.minutes for g in gaps] == [110, 50]


def test_candidates_never_overlap_busy():
    busy = [BusyBlock(datetime(2026, 10, 22, 9, 0, tzinfo=TZ),
                      datetime(2026, 10, 22, 12, 0, tzinfo=TZ))]
    got = propose(Requirement(est_min=45, energy=2), WED, busy, days=2)
    assert got
    for c in got:
        assert not (c.start < busy[0].end and c.end > busy[0].start)


def test_candidates_are_in_the_future():
    now = datetime(2026, 10, 22, 15, 30, tzinfo=TZ)
    got = propose(Requirement(est_min=30), now, [], days=1)
    assert got
    assert all(c.start >= now for c in got)


def test_deep_work_prefers_morning():
    now = datetime(2026, 10, 22, 0, 1, tzinfo=TZ)
    got = propose(Requirement(est_min=60, energy=2, kind="deep"), now, [], days=1)
    assert got
    assert 8 <= got[0].start.hour <= 11


def test_short_gaps_are_skipped():
    # 每 40 分钟一个会议，中间只剩不到 20 分钟
    busy = [
        BusyBlock(datetime(2026, 10, 22, 9, 0, tzinfo=TZ),
                  datetime(2026, 10, 22, 9, 40, tzinfo=TZ)),
        BusyBlock(datetime(2026, 10, 22, 10, 0, tzinfo=TZ),
                  datetime(2026, 10, 22, 10, 40, tzinfo=TZ)),
        BusyBlock(datetime(2026, 10, 22, 11, 0, tzinfo=TZ),
                  datetime(2026, 10, 22, 11, 40, tzinfo=TZ)),
    ]
    got = propose(Requirement(est_min=45, min_chunk=45), WED, busy, days=2, top_n=5)
    for c in got:
        for b in busy:
            assert not (c.start < b.end and c.end > b.start)


def test_daily_cap_discourages_overload():
    prefs = Preferences(max_goal_minutes_per_day=60)
    busy = [BusyBlock(datetime(2026, 10, 21, 9, 0, tzinfo=TZ),
                      datetime(2026, 10, 21, 11, 0, tzinfo=TZ), kind="block")]
    now = datetime(2026, 10, 21, 7, 0, tzinfo=TZ)
    got = propose(Requirement(est_min=45), now, busy, prefs=prefs, days=3, top_n=3)
    # 第一天已排 120 分钟目标块，超过上限 → 应该排到后面几天
    assert got[0].start.date() > date(2026, 10, 21)
