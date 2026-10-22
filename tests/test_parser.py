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
    assert used == "llm:parse-v1"
    assert action.end == datetime(2026, 10, 22, 15, 30, tzinfo=TZ)


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
