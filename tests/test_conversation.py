from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from goalcal import conversation
from goalcal.agent import CheckinContext, PendingAction
from goalcal.domain.planner import GoalBrief
from goalcal.domain.scheduler import Candidate
from goalcal.llm.action import Action


def test_conversation_messages_and_typed_state_round_trip(session, user):
    messages = [
        {"role": "assistant", "content": "What is your goal?"},
        {"role": "user", "content": "Learn piano"},
    ]
    start = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)
    state = {
        "goal_brief": GoalBrief(title="Learn piano", raw="Learn piano", deadline=date(2026, 12, 1)),
        "pending_action": PendingAction(
            action=Action(intent="delete_goal", target="Old goal", confidence=0.2), parser="test"
        ),
        "pending": {
            "step_id": 42,
            "origin": "chat",
            "candidates": [Candidate(start=start, end=start + timedelta(minutes=30), score=0.8, reasons=["quiet"])],
        },
        "checkin_context": CheckinContext(stage="outcome", event_ids=[7]),
        "replan_draft": {"goal_id": 3, "plan": [{"title": "Next"}]},
    }
    conversation.save_state(session, user.id, messages, state)
    loaded_messages, loaded_state = conversation.load_state(session, user.id)
    assert loaded_messages == messages
    assert loaded_state["goal_brief"] == state["goal_brief"]
    assert loaded_state["pending_action"] == state["pending_action"]
    assert loaded_state["pending"]["candidates"] == state["pending"]["candidates"]
    assert loaded_state["checkin_context"] == state["checkin_context"]
    assert loaded_state["replan_draft"] == state["replan_draft"]

    messages.append({"role": "assistant", "content": "I can help."})
    conversation.save_state(session, user.id, messages, {})
    loaded_messages, loaded_state = conversation.load_state(session, user.id)
    assert loaded_messages == messages
    assert loaded_state == {}


def test_batch_pending_round_trip_can_still_book(session, user):
    from sqlalchemy import select
    from goalcal import agent
    from goalcal.domain import goals
    from goalcal.models import Event

    goal = goals.create_goal(session, user.id, "Batch project", with_template=False)
    step = goals.add_step(session, goal, "First batch step", est_min=30)
    start = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)
    batch = [{"step_id": step.id, "title": step.title, "start": start, "end": start + timedelta(minutes=30)}]
    conversation.save_state(session, user.id, [], {"pending": {"batch": batch, "origin": "goal"}})

    _, state = conversation.load_state(session, user.id)
    restored = state["pending"]["batch"]
    assert isinstance(restored[0]["start"], datetime)
    assert isinstance(restored[0]["end"], datetime)
    assert agent.accept_batch(session, user.id, restored, start, "Asia/Shanghai").startswith("Booked 1")
    assert session.scalar(select(Event).where(Event.step_id == step.id)) is not None


def test_message_client_ids_make_repeated_saves_idempotent(session, user):
    from goalcal.models import Message

    conversation.save_state(session, user.id, [{"role": "user", "content": "once"}], {})
    messages, _ = conversation.load_state(session, user.id)
    conversation.save_state(session, user.id, messages, {})
    messages.append({"role": "assistant", "content": "twice"})
    conversation.save_state(session, user.id, messages, {})
    conversation.save_state(session, user.id, messages, {})

    restored, _ = conversation.load_state(session, user.id)
    assert [(item["role"], item["content"]) for item in restored] == [("user", "once"), ("assistant", "twice")]
    assert len({item["client_id"] for item in restored}) == 2


def test_corrupt_typed_state_falls_back_without_losing_messages(session, user):
    from goalcal.models import Conversation

    conversation.save_state(session, user.id, [{"role": "user", "content": "keep me"}], {})
    row = session.get(Conversation, 1)
    row.state_json = '{"goal_brief":{"__type__":"GoalBrief","value":3}}'
    session.flush()

    messages, state = conversation.load_state(session, user.id)

    assert [(item["role"], item["content"]) for item in messages] == [("user", "keep me")]
    assert messages[0]["client_id"]
    assert state == {}


def test_conversation_is_scoped_to_each_user(session, user):
    from goalcal.domain.goals import ensure_user

    other = ensure_user(session, "other")
    conversation.save_state(session, user.id, [{"role": "user", "content": "private"}], {"pending": None})
    assert conversation.load_state(session, other.id) == ([], {})


def _seed_messages(session, user_id, count, start=0):
    messages = [
        {"role": "user" if i % 2 else "assistant", "content": f"msg-{i:02d}"}
        for i in range(start, start + count)
    ]
    conversation.save_state(session, user_id, messages, {})
    return messages


def test_load_page_returns_latest_page_with_cursor_and_has_more(session, user):
    _seed_messages(session, user.id, 35)

    page = conversation.load_page(session, user.id)
    assert [m["content"] for m in page["messages"]] == [f"msg-{i:02d}" for i in range(5, 35)]
    assert page["has_more"] is True
    assert page["has_user_history"] is True
    assert all(m["id"] and m["client_id"] for m in page["messages"])

    earlier = conversation.load_page(session, user.id, before_id=page["messages"][0]["id"])
    assert [m["content"] for m in earlier["messages"]] == [f"msg-{i:02d}" for i in range(5)]
    assert earlier["has_more"] is False

    messages, _ = conversation.load_state(session, user.id)
    assert len(messages) == 35


def test_load_page_reports_user_history_beyond_the_current_page(session, user):
    messages = [{"role": "user", "content": "old question"}] + [
        {"role": "assistant", "content": f"reply-{i:02d}"} for i in range(30)
    ]
    conversation.save_state(session, user.id, messages, {})

    page = conversation.load_page(session, user.id)
    assert all(m["role"] == "assistant" for m in page["messages"])
    assert page["has_user_history"] is True

    from goalcal.domain.goals import ensure_user

    fresh = ensure_user(session, "fresh")
    conversation.save_state(session, fresh.id, [{"role": "assistant", "content": "welcome"}], {})
    fresh_page = conversation.load_page(session, fresh.id)
    assert fresh_page["has_user_history"] is False
    assert fresh_page["has_more"] is False

    empty = ensure_user(session, "empty")
    assert conversation.load_page(session, empty.id) == {
        "messages": [], "has_more": False, "has_user_history": False,
    }


def test_append_after_partial_page_load_loses_no_messages(session, user):
    _seed_messages(session, user.id, 35)
    page = conversation.load_page(session, user.id)
    assert len(page["messages"]) == 30

    page["messages"].append({"role": "user", "content": "appended after paged load"})
    conversation.save_state(session, user.id, page["messages"], {})
    conversation.save_state(session, user.id, page["messages"], {})  # 重复保存幂等

    messages, _ = conversation.load_state(session, user.id)
    contents = [m["content"] for m in messages]
    assert contents == [f"msg-{i:02d}" for i in range(35)] + ["appended after paged load"]


def test_load_recent_returns_only_the_latest_messages_in_order(session, user):
    _seed_messages(session, user.id, 10)
    recent = conversation.load_recent(session, user.id, limit=6)
    assert [m["content"] for m in recent] == [f"msg-{i:02d}" for i in range(4, 10)]


def test_load_task_state_restores_pending_without_messages(session, user):
    from goalcal.agent import CheckinContext

    _seed_messages(session, user.id, 40)
    conversation.save_state(
        session, user.id, [],
        {"checkin_context": CheckinContext(stage="outcome", event_ids=[3])},
    )
    state = conversation.load_task_state(session, user.id)
    assert state["checkin_context"] == CheckinContext(stage="outcome", event_ids=[3])
