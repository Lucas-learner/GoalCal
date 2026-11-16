from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from goalcal.llm.natdate import extract_time, extract_window, find_dates

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=TZ)  # Wednesday


def _first(text):
    found = find_dates(text, NOW)
    assert found, f"no date found in {text!r}"
    return found[0][1]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("tomorrow", datetime(2026, 10, 22, 9, 0, tzinfo=TZ)),
        ("今天", datetime(2026, 10, 21, 9, 0, tzinfo=TZ)),
        ("day after tomorrow", datetime(2026, 10, 23, 9, 0, tzinfo=TZ)),
        ("后天", datetime(2026, 10, 23, 9, 0, tzinfo=TZ)),
        ("in 3 days", datetime(2026, 10, 24, 9, 0, tzinfo=TZ)),
        ("3 天后", datetime(2026, 10, 24, 9, 0, tzinfo=TZ)),
        ("next week", datetime(2026, 10, 28, 9, 0, tzinfo=TZ)),
        ("Friday", datetime(2026, 10, 23, 9, 0, tzinfo=TZ)),
        ("Wednesday", datetime(2026, 10, 21, 9, 0, tzinfo=TZ)),
        ("this Friday", datetime(2026, 10, 23, 9, 0, tzinfo=TZ)),
        ("coming Friday", datetime(2026, 10, 23, 9, 0, tzinfo=TZ)),
        ("next Monday", datetime(2026, 10, 26, 9, 0, tzinfo=TZ)),
        ("next Friday", datetime(2026, 10, 30, 9, 0, tzinfo=TZ)),
        ("周五", datetime(2026, 10, 23, 9, 0, tzinfo=TZ)),
        ("下周五", datetime(2026, 10, 30, 9, 0, tzinfo=TZ)),
        ("下下周五", datetime(2026, 11, 6, 9, 0, tzinfo=TZ)),
        ("这周五", datetime(2026, 10, 23, 9, 0, tzinfo=TZ)),
        ("下周三", datetime(2026, 10, 28, 9, 0, tzinfo=TZ)),
        ("下下周三", datetime(2026, 11, 4, 9, 0, tzinfo=TZ)),
        ("下下周", datetime(2026, 11, 4, 9, 0, tzinfo=TZ)),
        ("October 28th", datetime(2026, 10, 28, 9, 0, tzinfo=TZ)),
        ("10/28", datetime(2026, 10, 28, 9, 0, tzinfo=TZ)),
        ("10月29日", datetime(2026, 10, 29, 9, 0, tzinfo=TZ)),
        ("12月3号", datetime(2026, 12, 3, 9, 0, tzinfo=TZ)),
    ],
)
def test_relative_dates(text, expected):
    assert _first(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("明天10点开会", datetime(2026, 10, 22, 10, 0, tzinfo=TZ)),
        ("明天上午开会", datetime(2026, 10, 22, 9, 0, tzinfo=TZ)),
        ("后天下午3点", datetime(2026, 10, 23, 15, 0, tzinfo=TZ)),
        ("大后天晚上8点", datetime(2026, 10, 24, 20, 0, tzinfo=TZ)),
        ("今晚9点", datetime(2026, 10, 21, 21, 0, tzinfo=TZ)),
    ],
)
def test_chinese_date_glued_to_time(text, expected):
    # 回归（P0-1）：汉字和数字都算 \w，\b明天\b 在 "明天10点" 里匹配不上
    assert _first(text) == expected


def test_chinese_month_day_beats_next_year():
    # 已过的月日滚到明年
    found = find_dates("3月5日", NOW)
    assert found[0][1].date().isoformat() == "2027-03-05"


def test_next_week_does_not_eat_next_monday():
    # "下周一" 要按星期一算（10-26），不能被 "下周"（10-28）抢掉
    found = find_dates("把周五的会挪到下周一", NOW)
    assert [d.date().isoformat() for _, d in found] == ["2026-10-23", "2026-10-26"]


def test_explicit_time_applies_to_last_date():
    found = find_dates("Monday and Friday at 3pm", NOW)
    assert [d for _, d in found] == [
        datetime(2026, 10, 26, 9, 0, tzinfo=TZ),
        datetime(2026, 10, 23, 15, 0, tzinfo=TZ),
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("at 10am", (10, 0)),
        ("10:30", (10, 30)),
        ("3pm", (15, 0)),
        ("下午3点", (15, 0)),
        ("下午3点半", (15, 30)),
        ("10点45分", (10, 45)),
        ("晚上8点", (20, 0)),
        ("noon", (12, 0)),
        ("morning", (9, 0)),
        ("没有时间", None),
    ],
)
def test_extract_time(text, expected):
    assert extract_time(text) == expected


def test_day_after_tomorrow_wins_over_tomorrow():
    # "day after tomorrow" 里也包含 "tomorrow"，不能被拆成两个日期
    found = find_dates("day after tomorrow", NOW)
    assert len(found) == 1
    assert found[0][1].date().isoformat() == "2026-10-23"


def test_results_are_sorted_by_position():
    found = find_dates("Friday then Monday", NOW)
    assert [d.date().isoformat() for _, d in found] == ["2026-10-23", "2026-10-26"]


# ------------------------------------------------------------ 时间窗口


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("今天下午", (datetime(2026, 10, 21, 12, 0, tzinfo=TZ), datetime(2026, 10, 21, 18, 0, tzinfo=TZ))),
        ("明天上午", (datetime(2026, 10, 22, 8, 0, tzinfo=TZ), datetime(2026, 10, 22, 12, 0, tzinfo=TZ))),
        ("后天晚上", (datetime(2026, 10, 23, 18, 0, tzinfo=TZ), datetime(2026, 10, 23, 23, 0, tzinfo=TZ))),
        ("今晚", (datetime(2026, 10, 21, 18, 0, tzinfo=TZ), datetime(2026, 10, 21, 23, 0, tzinfo=TZ))),
        ("明晚", (datetime(2026, 10, 22, 18, 0, tzinfo=TZ), datetime(2026, 10, 22, 23, 0, tzinfo=TZ))),
        ("this afternoon", (datetime(2026, 10, 21, 12, 0, tzinfo=TZ), datetime(2026, 10, 21, 18, 0, tzinfo=TZ))),
        ("tomorrow morning", (datetime(2026, 10, 22, 8, 0, tzinfo=TZ), datetime(2026, 10, 22, 12, 0, tzinfo=TZ))),
        ("tonight", (datetime(2026, 10, 21, 18, 0, tzinfo=TZ), datetime(2026, 10, 21, 23, 0, tzinfo=TZ))),
        ("tomorrow night", (datetime(2026, 10, 22, 18, 0, tzinfo=TZ), datetime(2026, 10, 22, 23, 0, tzinfo=TZ))),
    ],
)
def test_extract_window(text, expected):
    assert extract_window(text, NOW) == expected


@pytest.mark.parametrize(
    "text",
    [
        "下午3点",  # 明确时间点不是窗口
        "今晚9点",
        "tonight at 9",
        "tomorrow morning at 9",
        "写脚本",
        "明天",
    ],
)
def test_extract_window_rejects_explicit_times_and_non_windows(text):
    assert extract_window(text, NOW) is None


# ------------------------------------------------------------ 边界与周期回归：这周 / 下周 / 下下周


def test_reproduce_next_next_week_saturday_boundary():
    """回归用例：复现附件边界解析复测问题。
    当前时刻：2026-10-31 10:00:00（周六）
    输入：下下周三下午三点开组会
    预期：2026-11-11 15:00
    """
    sat_now = datetime(2026, 10, 31, 10, 0, tzinfo=TZ)
    found = find_dates("下下周三下午三点开组会", sat_now)
    assert len(found) == 1
    token, dt = found[0]
    assert token == "下下周三"
    assert dt == datetime(2026, 11, 11, 15, 0, tzinfo=TZ)


def test_chinese_week_distinctions_cross_month():
    """周六跨月边界测试：2026-10-31（周六），验证这周/下周/下下周及跨月到11月。"""
    sat_now = datetime(2026, 10, 31, 10, 0, tzinfo=TZ)
    cases = [
        ("这周日", datetime(2026, 11, 1, 9, 0, tzinfo=TZ)),
        ("下周一", datetime(2026, 11, 2, 9, 0, tzinfo=TZ)),
        ("下周三", datetime(2026, 11, 4, 9, 0, tzinfo=TZ)),
        ("下周日", datetime(2026, 11, 8, 9, 0, tzinfo=TZ)),
        ("下下周一", datetime(2026, 11, 9, 9, 0, tzinfo=TZ)),
        ("下下周三", datetime(2026, 11, 11, 9, 0, tzinfo=TZ)),
        ("下下周日", datetime(2026, 11, 15, 9, 0, tzinfo=TZ)),
    ]
    for text, expected in cases:
        assert find_dates(text, sat_now)[0][1] == expected, f"failed on {text}"


def test_chinese_week_distinctions_on_sunday():
    """周日基准测试：2026-10-25（周日），验证周日当天的这周/下周/下下周。"""
    sun_now = datetime(2026, 10, 25, 10, 0, tzinfo=TZ)
    cases = [
        ("这周日", datetime(2026, 10, 25, 9, 0, tzinfo=TZ)),
        ("下周日", datetime(2026, 11, 1, 9, 0, tzinfo=TZ)),
        ("下下周日", datetime(2026, 11, 8, 9, 0, tzinfo=TZ)),
        ("周一", datetime(2026, 10, 26, 9, 0, tzinfo=TZ)),
        ("下周一", datetime(2026, 10, 26, 9, 0, tzinfo=TZ)),
        ("下下周一", datetime(2026, 11, 2, 9, 0, tzinfo=TZ)),
        ("下周三", datetime(2026, 10, 28, 9, 0, tzinfo=TZ)),
        ("下下周三", datetime(2026, 11, 4, 9, 0, tzinfo=TZ)),
    ]
    for text, expected in cases:
        assert find_dates(text, sun_now)[0][1] == expected, f"failed on {text}"


def test_chinese_week_distinctions_cross_year():
    """跨年基准测试：2026-12-30（周三），验证这周/下周/下下周跨入2027年。"""
    year_end_now = datetime(2026, 12, 30, 10, 0, tzinfo=TZ)
    cases = [
        ("这周五", datetime(2027, 1, 1, 9, 0, tzinfo=TZ)),
        ("下周五", datetime(2027, 1, 8, 9, 0, tzinfo=TZ)),
        ("下下周五", datetime(2027, 1, 15, 9, 0, tzinfo=TZ)),
        ("下周一", datetime(2027, 1, 4, 9, 0, tzinfo=TZ)),
        ("下周三", datetime(2027, 1, 6, 9, 0, tzinfo=TZ)),
        ("下下周三", datetime(2027, 1, 13, 9, 0, tzinfo=TZ)),
    ]
    for text, expected in cases:
        assert find_dates(text, year_end_now)[0][1] == expected, f"failed on {text}"


def test_chinese_week_variants():
    """验证'个'字修饰词及'星期/礼拜'同义词。"""
    cases = [
        ("下个周三", datetime(2026, 10, 28, 9, 0, tzinfo=TZ)),
        ("下个星期三", datetime(2026, 10, 28, 9, 0, tzinfo=TZ)),
        ("下下个星期三", datetime(2026, 11, 4, 9, 0, tzinfo=TZ)),
        ("下下礼拜五", datetime(2026, 11, 6, 9, 0, tzinfo=TZ)),
    ]
    for text, expected in cases:
        assert find_dates(text, NOW)[0][1] == expected, f"failed on {text}"

