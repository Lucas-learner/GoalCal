from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from goalcal.llm import parser as parser_mod
from goalcal.llm.action import Action

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=TZ)


def test_without_llm_uses_rules():
    action, used = parser_mod.parse("Add a meeting tomorrow 10am", NOW, use_llm=False)
    assert used == "rules:v1"
    assert action.intent == "add_event"
    assert action.end is not None


def test_llm_exception_falls_back_to_rules(monkeypatch):
    monkeypatch.setattr(
        parser_mod, "settings", SimpleNamespace(llm_ready=True, llm_api_key="test")
    )

    def boom(text, now):
        raise RuntimeError("network down")

    monkeypatch.setattr(parser_mod, "_parse_with_llm", boom)
    action, used = parser_mod.parse("I'm tired", NOW, use_llm=True)
    assert used == "rules:v1"
    assert action.intent == "set_state"
    assert action.state == "tired"


def test_invalid_llm_output_falls_back(monkeypatch):
    monkeypatch.setattr(
        parser_mod, "settings", SimpleNamespace(llm_ready=True, llm_api_key="test")
    )
    monkeypatch.setattr(parser_mod, "_parse_with_llm", lambda text, now: None)
    action, used = parser_mod.parse("Add a meeting tomorrow 10am", NOW, use_llm=True)
    assert used == "rules:v1"
    assert action.intent == "add_event"


def test_valid_llm_action_is_normalized(monkeypatch):
    monkeypatch.setattr(
        parser_mod, "settings", SimpleNamespace(llm_ready=True, llm_api_key="test")
    )
    monkeypatch.setattr(
        parser_mod,
        "_parse_with_llm",
        lambda text, now: Action(
            intent="add_event",
            title="review",
            start=datetime(2026, 10, 22, 15, 0, tzinfo=TZ),
            duration_min=30,
            confidence=0.9,
        ),
    )
    action, used = parser_mod.parse("review tomorrow 3pm", NOW, use_llm=True)
    assert used == f"llm:{parser_mod.PROMPT_VERSION}"
    assert action.end == datetime(2026, 10, 22, 15, 30, tzinfo=TZ)


def test_llm_start_is_overwritten_by_natdate(monkeypatch):
    """日期由规则算（铁律 2）：LLM 给的 start 和 natdate 不一致时，以 natdate 为准。"""
    monkeypatch.setattr(
        parser_mod, "settings", SimpleNamespace(llm_ready=True, llm_api_key="test")
    )
    monkeypatch.setattr(
        parser_mod,
        "_parse_with_llm",
        lambda text, now: Action(
            intent="add_event",
            title="开会",
            when_text="明天10点",
            start=datetime(2026, 11, 5, 10, 0, tzinfo=TZ),  # LLM 照抄/算错的日期
            confidence=0.9,
        ),
    )
    action, _ = parser_mod.parse("明天10点开会", NOW, use_llm=True)
    assert action.start == datetime(2026, 10, 22, 10, 0, tzinfo=TZ)
    assert action.confidence == 0.9


def test_llm_date_kept_with_lower_confidence_when_natdate_fails(monkeypatch):
    monkeypatch.setattr(
        parser_mod, "settings", SimpleNamespace(llm_ready=True, llm_api_key="test")
    )
    llm_start = datetime(2026, 10, 30, 9, 0, tzinfo=TZ)
    monkeypatch.setattr(
        parser_mod,
        "_parse_with_llm",
        lambda text, now: Action(
            intent="add_event",
            title="review",
            when_text="after my class",
            start=llm_start,
            confidence=0.9,
        ),
    )
    action, _ = parser_mod.parse("add a review after my class", NOW, use_llm=True)
    assert action.start == llm_start
    assert action.confidence == 0.9 - parser_mod.LLM_DATE_PENALTY


def test_llm_goal_deadline_comes_from_natdate(monkeypatch):
    monkeypatch.setattr(
        parser_mod, "settings", SimpleNamespace(llm_ready=True, llm_api_key="test")
    )
    monkeypatch.setattr(
        parser_mod,
        "_parse_with_llm",
        lambda text, now: Action(
            intent="create_goal",
            title="Publish my first short video",
            when_text="in 30 days",
            deadline=datetime(2026, 12, 25).date(),
            confidence=0.9,
        ),
    )
    action, _ = parser_mod.parse("I want to publish my first short video in 30 days", NOW, use_llm=True)
    assert action.deadline.isoformat() == "2026-11-20"


# ------------------------------------------------------------ 上下文优先解析


def test_checkin_context_interprets_short_answers():
    ctx = {"expecting": "checkin"}
    action, used = parser_mod.parse("一半", NOW, use_llm=False, context=ctx)
    assert used == "rules:checkin"
    assert action.intent == "log_progress"
    assert action.checkin_outcome == "partial"

    action, _ = parser_mod.parse("太累了", NOW, use_llm=False, context=ctx)
    assert action.intent == "set_state" and action.state == "tired"

    action, _ = parser_mod.parse("没做，太忙了", NOW, use_llm=False, context=ctx)
    assert action.intent == "set_state" and action.checkin_outcome == "none"


def test_checkin_context_passes_new_commands_through():
    ctx = {"expecting": "checkin"}
    action, used = parser_mod.parse("帮我安排明天10点开会", NOW, use_llm=False, context=ctx)
    assert used == "rules:v1"
    assert action.intent == "add_event"


def test_llm_delete_goal_misread_is_guarded(monkeypatch):
    monkeypatch.setattr(
        parser_mod, "settings", SimpleNamespace(llm_ready=True, llm_api_key="test")
    )
    monkeypatch.setattr(
        parser_mod,
        "_parse_with_llm",
        lambda text, now: Action(intent="create_goal", title=text, confidence=0.8),
    )
    action, used = parser_mod.parse('delete "track my goal"', NOW, use_llm=True)
    assert used == "rules:guard"
    assert action.intent == "delete_goal"
    assert action.target == "track my goal"


def test_normalized_fills_missing_end():
    action = Action(
        intent="add_event",
        title="x",
        start=datetime(2026, 10, 22, 10, 0),
    ).normalized(NOW)
    assert action.start.tzinfo is not None
    assert action.duration_min == 60
    assert action.end.hour == 11
