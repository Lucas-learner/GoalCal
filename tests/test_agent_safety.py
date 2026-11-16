from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from goalcal import agent
from goalcal.domain import calendar_service as cal
from goalcal.domain import goals as goal_svc
from goalcal.llm.action import Action
from goalcal.models import Checkin, Event

TZ = "Asia/Shanghai"
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=ZoneInfo(TZ))


def _goal(session, user, goal_title, *step_titles):
    defs = [
        {
            "seq": index,
            "milestone": "M",
            "title": title,
            "outcome": "",
            "est_min": 30,
            "energy": 1,
            "kind": "deep",
        }
        for index, title in enumerate(step_titles, start=1)
    ]
    return goal_svc.create_goal(
        session,
        user.id,
        goal_title,
        deadline=NOW.date() + timedelta(days=30),
        step_defs=defs,
        template_key="llm",
    )


def _parsed(monkeypatch, action):
    monkeypatch.setattr(agent, "parse", lambda *args, **kwargs: (action, "test"))


def test_explicit_progress_target_is_resolved_across_active_goals(session, user, monkeypatch):
    first = _goal(session, user, "First", "Write the script")
    second = _goal(session, user, "Second", "Record the demo")
    _parsed(monkeypatch, Action(
        intent="log_progress", progress_target="Record the demo", raw="done", confidence=0.9,
    ))

    reply = agent.handle(session, user.id, "done", NOW, TZ)

    assert first.steps[0].status == "todo"
    assert second.steps[0].status == "done"
    assert reply.goal_id == second.id


def test_explicit_progress_mismatch_does_not_complete_first_step(session, user, monkeypatch):
    first = _goal(session, user, "First", "Write the script")
    second = _goal(session, user, "Second", "Record the demo")
    _parsed(monkeypatch, Action(
        intent="log_progress", progress_target="missing step", raw="done", confidence=0.9,
    ))

    reply = agent.handle(session, user.id, "done", NOW, TZ)

    assert "couldn't find" in reply.text
    assert first.steps[0].status == second.steps[0].status == "todo"
    assert session.scalars(select(Checkin)).all() == []


def test_ambiguous_progress_target_requires_clarification(session, user, monkeypatch):
    first = _goal(session, user, "First", "Write the script")
    second = _goal(session, user, "Second", "Write the script")
    _parsed(monkeypatch, Action(
        intent="log_progress", progress_target="Write the script", raw="done", confidence=0.9,
    ))

    reply = agent.handle(session, user.id, "done", NOW, TZ)

    assert "more than one" in reply.text
    assert first.steps[0].status == second.steps[0].status == "todo"
    assert session.scalars(select(Checkin)).all() == []


def test_schedule_target_mismatch_does_not_fall_back(session, user, monkeypatch):
    goal = _goal(session, user, "First", "Write the script")
    _parsed(monkeypatch, Action(
        intent="schedule_step", progress_target="missing step", raw="schedule", confidence=0.9,
    ))

    reply = agent.handle(session, user.id, "schedule", NOW, TZ)

    assert "couldn't find" in reply.text
    assert reply.step_id is None and reply.candidates == []
    assert goal.steps[0].status == "todo"
    assert session.scalars(select(Event)).all() == []


def test_untargeted_progress_uses_next_available_step_across_goals(session, user, monkeypatch):
    empty = _goal(session, user, "Empty")
    available = _goal(session, user, "Available", "Next concrete step")
    _parsed(monkeypatch, Action(intent="log_progress", raw="done", confidence=0.9))

    agent.handle(session, user.id, "done", NOW, TZ)

    assert available.steps[0].status == "done"
    assert empty.status == "active"


def test_low_confidence_delete_can_be_cancelled_without_db_then_confirmed(session, user, monkeypatch):
    ev = cal.create_event(
        session, user.id, "Dentist", NOW + timedelta(days=1), NOW + timedelta(days=1, hours=1), tz=TZ,
    )
    _parsed(monkeypatch, Action(
        intent="delete_event", target="Dentist", raw="maybe delete", confidence=0.4,
    ))
    pending = agent.handle(session, user.id, "maybe delete", NOW, TZ).pending_action
    assert pending is not None

    cancelled = agent.confirm_action(None, user.id, pending, False, NOW, TZ)
    assert "nothing was changed" in cancelled.text
    assert cancelled.pending_action is None
    assert session.get(Event, ev.id) is not None

    confirmed = agent.confirm_action(session, user.id, pending, True, NOW, TZ)
    assert "Deleted" in confirmed.text
    assert confirmed.pending_action is None
    assert session.get(Event, ev.id) is None


def test_confirmed_move_refinds_current_target(session, user, monkeypatch):
    old = cal.create_event(
        session, user.id, "Team meeting", NOW + timedelta(days=1), NOW + timedelta(days=1, hours=1), tz=TZ,
    )
    start = NOW + timedelta(days=2, hours=6)
    _parsed(monkeypatch, Action(
        intent="move_event", target="Team meeting", start=start,
        end=start + timedelta(hours=1), raw="move to 3pm", confidence=0.4,
    ))
    pending = agent.handle(session, user.id, "move", NOW, TZ).pending_action
    cal.delete_event(session, old)
    replacement = cal.create_event(
        session, user.id, "Team meeting", NOW + timedelta(days=3), NOW + timedelta(days=3, hours=1), tz=TZ,
    )

    reply = agent.confirm_action(session, user.id, pending, True, NOW, TZ)

    assert "Moved" in reply.text
    assert len(session.scalars(select(Event)).all()) == 1
    assert replacement.start_utc == datetime(2026, 10, 23, 7, 0)


def test_low_confidence_delete_goal_executes_only_after_approval(session, user, monkeypatch):
    goal = _goal(session, user, "Learn guitar", "Practice")
    _parsed(monkeypatch, Action(
        intent="delete_goal", target="Learn guitar", raw="maybe delete", confidence=0.4,
    ))
    reply = agent.handle(session, user.id, "maybe delete", NOW, TZ)

    assert reply.pending_action is not None and goal.status == "active"
    agent.confirm_action(session, user.id, reply.pending_action, False, NOW, TZ)
    assert goal.status == "active"
    agent.confirm_action(session, user.id, reply.pending_action, True, NOW, TZ)
    assert goal.status == "archived"


def test_ambiguous_delete_goal_does_not_archive_either(session, user, monkeypatch):
    first = _goal(session, user, "Guitar basics", "Practice")
    second = _goal(session, user, "Guitar songs", "Practice")
    _parsed(monkeypatch, Action(
        intent="delete_goal", target="Guitar", raw="delete", confidence=0.9,
    ))

    reply = agent.handle(session, user.id, "delete", NOW, TZ)

    assert "Which goal" in reply.text
    assert first.status == second.status == "active"


def test_targeted_progress_exits_checkin_instead_of_completing_old_step(session, user):
    old_goal = _goal(session, user, "Old goal", "Old step")
    new_goal = _goal(session, user, "New goal", "New step")
    old_step = old_goal.steps[0]
    old_event = cal.create_event(
        session, user.id, "Old goal: Old step", NOW - timedelta(hours=2),
        NOW - timedelta(hours=1), kind="block", step_id=old_step.id, tz=TZ,
    )
    ctx, _ = agent.begin_checkin(session, user.id, NOW, TZ)

    follow_up = agent.continue_checkin(session, user.id, ctx, "I finished New step", NOW, TZ)
    assert follow_up is None
    reply = agent.handle(session, user.id, "I finished New step", NOW, TZ)

    assert old_step.status == "todo"
    assert session.get(Event, old_event.id).status == "planned"
    assert new_goal.steps[0].status == "done"
    assert reply.goal_id == new_goal.id


def test_similar_but_different_goal_name_is_not_deleted(session, user):
    goal = _goal(session, user, "Learn guitar", "Practice")

    reply = agent.handle(session, user.id, 'delete goal "Learn piano"', NOW, TZ)

    assert "Nothing was deleted" in reply.text
    assert goal.status == "active"


def test_similar_but_different_event_name_is_not_deleted(session, user):
    event = cal.create_event(
        session, user.id, "Team meeting", NOW + timedelta(days=1),
        NOW + timedelta(days=1, hours=1), tz=TZ,
    )

    reply = agent.handle(session, user.id, 'delete "Team lunch"', NOW, TZ)

    assert "Nothing was deleted" in reply.text
    assert session.get(Event, event.id) is not None
