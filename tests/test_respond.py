import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import select

from goalcal import agent
from goalcal.domain import goals as goal_svc
from goalcal.llm import respond
from goalcal.models import Event, Goal, Step

TZ = "Asia/Shanghai"
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=ZoneInfo(TZ))  # Wednesday


class _FakeTextClient:
    def __init__(self, reply="Hi there — I'm listening."):
        self.reply = reply
        self.calls = []

    def complete_text(self, system, user, temperature=0.3):
        self.calls.append((system, user))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _enable(monkeypatch, client):
    monkeypatch.setattr(respond, "settings", SimpleNamespace(llm_ready=True))
    monkeypatch.setattr(respond, "LLMClient", lambda: client)


def test_chat_reply_context_reads_only_the_last_six_messages(session, user, monkeypatch):
    from goalcal import conversation

    messages = [
        {"role": "user" if i % 2 else "assistant", "content": f"turn-{i}"} for i in range(10)
    ]
    conversation.save_state(session, user.id, messages, {})
    client = _FakeTextClient()
    _enable(monkeypatch, client)

    respond.chat_reply(session, user.id, "hello", NOW, TZ)

    recent = json.loads(client.calls[0][1])["context"]["recent_conversation"]
    assert recent == [
        f"{'user' if i % 2 else 'assistant'}: turn-{i}" for i in range(4, 10)
    ]


def test_chat_reply_returns_fake_llm_text(session, user, monkeypatch):
    client = _FakeTextClient("听起来今天有点满，需要我帮你留点空吗？")
    _enable(monkeypatch, client)

    reply = respond.chat_reply(session, user.id, "今天有点焦虑", NOW, TZ)

    assert reply == "听起来今天有点满，需要我帮你留点空吗？"
    assert len(client.calls) == 1


def test_chat_reply_context_carries_goals_events_and_history(session, user, monkeypatch):
    goal = goal_svc.create_goal(
        session, user.id, "Publish my first short video", deadline=date(2026, 11, 20)
    )
    step = goal_svc.next_step(goal)
    start = NOW + timedelta(days=1)
    from goalcal.domain import calendar_service as cal

    cal.create_event(session, user.id, "Team sync", start, start + timedelta(hours=1), tz=TZ)
    from goalcal import conversation

    conversation.save_state(session, user.id, [{"role": "user", "content": "earlier note"}], {})
    client = _FakeTextClient()
    _enable(monkeypatch, client)

    respond.chat_reply(session, user.id, "hello", NOW, TZ)

    payload = json.loads(client.calls[0][1])
    assert payload["message"] == "hello"
    ctx = payload["context"]
    assert ctx["now"].startswith("2026-10-21")
    assert ctx["goals"][0]["title"] == "Publish my first short video"
    assert ctx["goals"][0]["next_step"] == step.title
    assert ctx["upcoming_events"][0]["title"] == "Team sync"
    assert ctx["recent_conversation"][-1] == "user: earlier note"


def test_chat_reply_returns_none_on_llm_failure(session, user, monkeypatch):
    _enable(monkeypatch, _FakeTextClient(RuntimeError("boom")))
    assert respond.chat_reply(session, user.id, "hello", NOW, TZ) is None


def test_chat_reply_returns_none_when_llm_disabled(session, user):
    assert respond.chat_reply(session, user.id, "hello", NOW, TZ) is None


def test_chat_reply_empty_text_falls_back(session, user, monkeypatch):
    _enable(monkeypatch, _FakeTextClient("   "))
    reply = agent.handle(session, user.id, "hello", NOW, TZ)
    assert reply.text == agent.CLARIFY


def test_agent_chat_turn_uses_llm_and_writes_nothing(session, user, monkeypatch):
    client = _FakeTextClient("Got it — no rush.")
    _enable(monkeypatch, client)

    reply = agent.handle(session, user.id, "just thinking out loud", NOW, TZ)

    assert reply.text == "Got it — no rush."
    assert reply.action.intent == "chat"
    assert session.scalars(select(Goal)).all() == []
    assert session.scalars(select(Step)).all() == []
    assert session.scalars(select(Event)).all() == []


def test_agent_chat_turn_falls_back_without_llm(session, user):
    goal = goal_svc.create_goal(session, user.id, "Learn guitar")
    step = goal_svc.next_step(goal)

    reply = agent.handle(session, user.id, "just thinking out loud", NOW, TZ)

    assert reply.action.intent == "chat"
    assert goal.title in reply.text and step.title in reply.text
    assert "find time" in reply.text
    assert session.scalars(select(Event)).all() == []


def test_chat_fallback_matches_chinese_without_forcing_an_action(session, user):
    reply = agent.handle(session, user.id, "随便聊聊", NOW, TZ)
    assert reply.text == "我在听。你可以继续说说，或告诉我现在最想推进的一件事。"


def test_chat_fallback_handles_gratitude_without_goal_pitch(session, user):
    goal_svc.create_goal(session, user.id, "Learn guitar")
    assert agent.handle(session, user.id, "谢谢！", NOW, TZ).text == "不客气。"


def test_repeated_chat_turns_stay_responsive(session, user, monkeypatch):
    client = _FakeTextClient("I'm here.")
    _enable(monkeypatch, client)

    for text in ["hello", "hmm", "thanks!", "okay", "nice weather today"]:
        reply = agent.handle(session, user.id, text, NOW, TZ)
        assert reply.text == "I'm here."
        assert reply.action.intent == "chat"
    assert session.scalars(select(Event)).all() == []
