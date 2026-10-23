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
        ("周五", datetime(2026, 10, 23, 9, 0, tzinfo=TZ)),
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
