from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from goalcal.llm.rules import parse_with_rules

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=TZ)  # Wednesday


def test_goal_creation_with_relative_deadline():
    a = parse_with_rules("I want to publish my first short video in 30 days", NOW)
    assert a.intent == "create_goal"
    assert a.deadline.isoformat() == "2026-11-20"
    assert "short video" in (a.title or "")


def test_add_event_with_weekday_and_time():
    a = parse_with_rules("Add a meeting Wednesday 10am", NOW)
    assert a.intent == "add_event"
    assert a.start.weekday() == 2
    assert a.start.hour == 10
    assert "meeting" in (a.title or "").lower()


def test_add_event_with_duration():
    a = parse_with_rules("Block 90 minutes for writing tomorrow", NOW)
    assert a.intent == "add_event"
    assert a.duration_min == 90


def test_move_event_keeps_target():
    a = parse_with_rules("Move tomorrow's workout to Friday", NOW)
    assert a.intent == "move_event"
    assert "workout" in (a.target or "")
    assert a.start.weekday() == 4


def test_delete_event():
    a = parse_with_rules("Cancel the dentist appointment", NOW)
    assert a.intent == "delete_event"


def test_state_detection():
    assert parse_with_rules("I'm exhausted today", NOW).intent == "set_state"
    assert parse_with_rules("I'm tired", NOW).state == "tired"
    assert parse_with_rules("我有点累", NOW).state == "tired"


def test_progress_report():
    a = parse_with_rules("I finished the script today", NOW)
    assert a.intent == "log_progress"


def test_query():
    a = parse_with_rules("What do I have tomorrow?", NOW)
    assert a.intent == "query_events"


def test_unknown_falls_back_to_chat():
    assert parse_with_rules("hello there", NOW).intent == "chat"
