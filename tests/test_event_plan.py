"""上下文排期 → 集中预览 → 确认写入：提取、预览、确认/取消、去重与回滚。"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from goalcal import agent, conversation
from goalcal.domain import calendar_service as cal
from goalcal.llm import event_plan
from goalcal.llm.action import Action
from goalcal.models import Event

TZ = "Asia/Shanghai"
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=ZoneInfo(TZ))  # Wednesday
TOMORROW = NOW.date() + timedelta(days=1)

PROPOSAL = "根据截图建议：明天下午 15:30-16:15 写脚本，明天晚上 20:00-21:00 剪辑。"


class _FakeClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls = []

    def complete_json(self, system, user, temperature=0.0):
        self.calls.append((system, user))
        if self.error is not None:
            raise self.error
        return self.payload


def _enable(monkeypatch, client):
    monkeypatch.setattr(event_plan, "settings", SimpleNamespace(llm_enabled=True))
    monkeypatch.setattr(event_plan, "plan_client", lambda: client)


def _payload():
    return {
        "events": [
            {"title": "写脚本", "when_text": "明天下午 15:30-16:15", "confidence": 0.9},
            {"title": "剪辑", "when_text": "明天晚上 20:00-21:00", "confidence": 0.85},
        ]
    }


def _draft_actions():
    start = datetime.combine(TOMORROW, datetime.min.time(), tzinfo=ZoneInfo(TZ)).replace(hour=15, minute=30)
    second = start.replace(hour=20, minute=0)
    return [
        Action(intent="add_event", title="写脚本", start=start, end=start + timedelta(minutes=45), confidence=0.9),
        Action(intent="add_event", title="剪辑", start=second, end=second + timedelta(hours=1), confidence=0.85),
    ]


def _seed_proposal(session, user_id):
    conversation.save_state(
        session, user_id,
        [
            {"role": "user", "content": "帮我看看这张截图怎么安排"},
            {"role": "assistant", "content": PROPOSAL},
        ],
        {},
    )


def _titles(session, user_id):
    return sorted(
        e.title for e in session.scalars(select(Event).where(Event.user_id == user_id))
    )


def test_extract_events_parses_two_screenshot_blocks(session, user, monkeypatch):
    _seed_proposal(session, user.id)
    _enable(monkeypatch, _FakeClient(_payload()))

    actions = event_plan.extract_events(session, user.id, "帮我安排进日历", NOW, TZ)

    assert [a.title for a in actions] == ["写脚本", "剪辑"]
    assert actions[0].start == datetime(2026, 10, 22, 15, 30, tzinfo=ZoneInfo(TZ))
    assert actions[0].end == datetime(2026, 10, 22, 16, 15, tzinfo=ZoneInfo(TZ))
    assert actions[1].start == datetime(2026, 10, 22, 20, 0, tzinfo=ZoneInfo(TZ))
    assert actions[1].end == datetime(2026, 10, 22, 21, 0, tzinfo=ZoneInfo(TZ))


def test_out_of_range_clock_times_are_dropped(session, user, monkeypatch):
    """原文里出现 "25:30"/"29点" 这类越界钟点：丢弃该条，不能抛 ValueError 击穿整轮。"""
    _seed_proposal(session, user.id)
    _enable(monkeypatch, _FakeClient({
        "events": [
            {"title": "坏时间", "when_text": "明天 25:30-26:00", "confidence": 0.9},
            {"title": "写脚本", "when_text": "明天下午 15:30-16:15", "confidence": 0.9},
        ]
    }))

    actions = event_plan.extract_events(session, user.id, "帮我安排进日历", NOW, TZ)

    assert [a.title for a in actions] == ["写脚本"]


def test_preview_writes_nothing_to_calendar(session, user, monkeypatch):
    _seed_proposal(session, user.id)
    _enable(monkeypatch, _FakeClient(_payload()))

    reply = agent.handle(session, user.id, "帮我安排进日历", NOW, TZ)

    assert reply.event_draft is not None
    assert [a.title for a in reply.event_draft.events] == ["写脚本", "剪辑"]
    assert "preview" in reply.text
    assert _titles(session, user.id) == []


def test_confirm_writes_both_events(session, user):
    draft = agent.EventDraft(events=_draft_actions())

    reply = agent.confirm_event_draft(session, user.id, draft, True, NOW, TZ)

    assert "Added 2 event(s)" in reply.text
    assert _titles(session, user.id) == ["写脚本", "剪辑"]


def test_cancel_writes_nothing(session, user):
    draft = agent.EventDraft(events=_draft_actions())

    reply = agent.confirm_event_draft(session, user.id, draft, False, NOW, TZ)

    assert "nothing was added" in reply.text
    assert _titles(session, user.id) == []


def test_repeated_confirm_does_not_duplicate(session, user):
    draft = agent.EventDraft(events=_draft_actions())
    agent.confirm_event_draft(session, user.id, draft, True, NOW, TZ)

    reply = agent.confirm_event_draft(session, user.id, draft, True, NOW, TZ)

    assert "already on your calendar" in reply.text
    assert _titles(session, user.id) == ["写脚本", "剪辑"]


def test_existing_same_title_and_start_is_skipped(session, user):
    first = _draft_actions()[0]
    cal.create_event(session, user.id, "写脚本", first.start, first.end, tz=TZ)

    reply = agent.confirm_event_draft(
        session, user.id, agent.EventDraft(events=_draft_actions()), True, NOW, TZ
    )

    assert "Added 1 event(s)" in reply.text
    assert _titles(session, user.id) == ["写脚本", "剪辑"]


def test_preview_drops_blocks_already_on_calendar(session, user, monkeypatch):
    first = _draft_actions()[0]
    cal.create_event(session, user.id, "写脚本", first.start, first.end, tz=TZ)
    _seed_proposal(session, user.id)
    _enable(monkeypatch, _FakeClient(_payload()))

    reply = agent.handle(session, user.id, "帮我安排进日历", NOW, TZ)

    assert [a.title for a in reply.event_draft.events] == ["剪辑"]


def test_preview_all_existing_shows_no_draft(session, user, monkeypatch):
    for action in _draft_actions():
        cal.create_event(session, user.id, action.title, action.start, action.end, tz=TZ)
    _seed_proposal(session, user.id)
    _enable(monkeypatch, _FakeClient(_payload()))

    reply = agent.handle(session, user.id, "帮我安排进日历", NOW, TZ)

    assert reply.event_draft is None
    assert "already on your calendar" in reply.text
    assert _titles(session, user.id) == ["写脚本", "剪辑"]


def test_invalid_structure_falls_back_to_clarification(session, user, monkeypatch):
    _seed_proposal(session, user.id)
    _enable(monkeypatch, _FakeClient({"events": [{"title": ""}]}))

    reply = agent.handle(session, user.id, "帮我安排进日历", NOW, TZ)

    assert reply.event_draft is None
    assert "安排在几点" in reply.text
    assert _titles(session, user.id) == []


def test_llm_failure_falls_back_to_clarification(session, user, monkeypatch):
    _seed_proposal(session, user.id)
    _enable(monkeypatch, _FakeClient(error=RuntimeError("boom")))

    reply = agent.handle(session, user.id, "帮我安排进日历", NOW, TZ)

    assert reply.event_draft is None
    assert "安排在几点" in reply.text


def test_low_confidence_and_unresolvable_items_are_dropped(session, user, monkeypatch):
    _seed_proposal(session, user.id)
    payload = {
        "events": [
            {"title": "太低置信", "when_text": "明天下午 15:30-16:15", "confidence": 0.3},
            {"title": "没有时间", "when_text": None, "confidence": 0.9},
            {"title": "写脚本", "when_text": "明天下午 15:30-16:15", "confidence": 0.9},
        ]
    }
    _enable(monkeypatch, _FakeClient(payload))

    actions = event_plan.extract_events(session, user.id, "帮我安排进日历", NOW, TZ)

    assert [a.title for a in actions] == ["写脚本"]


def test_extract_returns_none_when_llm_disabled(session, user):
    _seed_proposal(session, user.id)
    assert event_plan.extract_events(session, user.id, "帮我安排进日历", NOW, TZ) is None


def test_event_draft_survives_conversation_round_trip(session, user):
    draft = agent.EventDraft(events=_draft_actions())
    conversation.save_state(
        session, user.id, [{"role": "assistant", "content": "preview"}], {"event_draft": draft}
    )

    state = conversation.load_task_state(session, user.id)

    restored = state["event_draft"]
    assert restored == draft
    assert isinstance(restored, agent.EventDraft)
    assert all(isinstance(a, Action) for a in restored.events)
    assert restored.events[0].start == draft.events[0].start
    # 恢复后的草稿仍然可以直接确认写入
    reply = agent.confirm_event_draft(session, user.id, restored, True, NOW, TZ)
    assert "Added 2 event(s)" in reply.text


def test_confirm_failure_rolls_back_partial_writes(session, user, monkeypatch):
    real_create = cal.create_event

    def flaky(session_, user_id, title, start, end, **kwargs):
        if title == "剪辑":
            raise RuntimeError("db exploded")
        return real_create(session_, user_id, title, start, end, **kwargs)

    monkeypatch.setattr(agent.cal, "create_event", flaky)
    draft = agent.EventDraft(events=_draft_actions())

    with pytest.raises(RuntimeError):
        agent.confirm_event_draft(session, user.id, draft, True, NOW, TZ)
    session.rollback()  # 与 runtime.session_scope 的异常回滚同口径

    assert _titles(session, user.id) == []
