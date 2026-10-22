from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from goalcal.llm.natdate import extract_time, find_dates

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
    ],
)
def test_relative_dates(text, expected):
    assert _first(text) == expected


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
