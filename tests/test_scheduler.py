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


def test_candidates_are_aligned_to_quarter_hour():
    now = datetime(2026, 10, 22, 9, 0, tzinfo=TZ)
    busy = [BusyBlock(datetime(2026, 10, 22, 10, 0, tzinfo=TZ),
                      datetime(2026, 10, 22, 11, 0, tzinfo=TZ))]
    got = propose(Requirement(est_min=30), now, busy, days=1, top_n=5)
    assert got
    for c in got:
        assert c.start.minute % 15 == 0
        assert c.end.minute % 15 == 0


def test_every_candidate_has_a_human_reason():
    got = propose(Requirement(est_min=45), WED, [], days=2, top_n=3)
    assert len(got) == 3
    assert all(c.reasons and all(isinstance(r, str) and r for r in c.reasons) for c in got)


def test_candidates_are_at_least_one_hour_apart():
    got = propose(Requirement(est_min=30), WED, [], days=2, top_n=3)
    for a, b in zip(got, got[1:]):
        assert abs((a.start - b.start).total_seconds()) >= 3600


# ------------------------------------------------------------ 时间窗口


def test_window_restricts_candidates_to_the_window():
    window = (
        datetime(2026, 10, 21, 14, 0, tzinfo=TZ),
        datetime(2026, 10, 21, 18, 0, tzinfo=TZ),
    )
    got = propose(Requirement(est_min=45), WED, [], window=window, top_n=6)
    assert got
    for c in got:
        assert window[0] <= c.start and c.end <= window[1]


def test_window_does_not_scan_outside_days():
    # 窗口在后天：今天/明天的空档再好也不能出现
    window = (
        datetime(2026, 10, 23, 8, 0, tzinfo=TZ),
        datetime(2026, 10, 23, 12, 0, tzinfo=TZ),
    )
    got = propose(Requirement(est_min=30), WED, [], window=window, top_n=6)
    assert got
    assert all(c.start.date() == date(2026, 10, 23) for c in got)


def test_window_can_cross_midnight():
    window = (
        datetime(2026, 10, 21, 20, 0, tzinfo=TZ),
        datetime(2026, 10, 22, 10, 0, tzinfo=TZ),
    )
    got = propose(Requirement(est_min=30), WED, [], window=window, top_n=6)
    assert got
    days = {c.start.date() for c in got}
    assert days <= {date(2026, 10, 21), date(2026, 10, 22)}
    for c in got:
        assert window[0] <= c.start and c.end <= window[1]


def test_window_without_gap_returns_empty():
    # 窗口被一个大会议占满（含两侧 buffer）→ 空，由上层决定提示或回退
    busy = [BusyBlock(datetime(2026, 10, 21, 13, 30, tzinfo=TZ),
                      datetime(2026, 10, 21, 18, 30, tzinfo=TZ))]
    window = (
        datetime(2026, 10, 21, 14, 0, tzinfo=TZ),
        datetime(2026, 10, 21, 18, 0, tzinfo=TZ),
    )
    assert propose(Requirement(est_min=45), WED, busy, window=window) == []


def test_past_window_returns_empty():
    window = (
        datetime(2026, 10, 21, 7, 0, tzinfo=TZ),
        datetime(2026, 10, 21, 8, 0, tzinfo=TZ),
    )
    assert propose(Requirement(est_min=45), WED, [], window=window) == []
