from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from goalcal import agent
from goalcal.domain import calendar_service as cal
from goalcal.domain import goals as goal_svc
from goalcal.domain import recovery
from goalcal.models import Checkin, Event, Step

TZ = "Asia/Shanghai"
NOW_LOCAL = datetime(2026, 10, 22, 12, 0, tzinfo=ZoneInfo(TZ))
NOW = datetime(2026, 10, 22, 4, 0)  # NOW_LOCAL 对应的 UTC naive


def _event(session, user, start_local, minutes=60, kind="block", status="planned"):
    ev = cal.create_event(
        session, user.id, "Write the script",
        start_local, start_local + timedelta(minutes=minutes),
        kind=kind, tz=TZ,
    )
    ev.status = status
    session.flush()
    return ev


def _block_with_step(session, user, start_local, minutes=45):
    goal = goal_svc.create_goal(session, user.id, "Learn guitar")
    step = sorted(goal.steps, key=lambda s: s.seq)[0]
    ev = _event(session, user, start_local, minutes)
    ev.step_id = step.id
    session.flush()
    return goal, step, ev


# ------------------------------------------------------------ unresolved_blocks


def test_future_blocks_are_not_unresolved(session, user):
    _event(session, user, NOW_LOCAL + timedelta(hours=2))
    _event(session, user, NOW_LOCAL + timedelta(days=1))
    assert recovery.unresolved_blocks(session, user.id, NOW) == []


def test_non_block_events_are_not_unresolved(session, user):
    _event(session, user, NOW_LOCAL - timedelta(hours=3), kind="external")
    _event(session, user, NOW_LOCAL - timedelta(hours=5), kind="buffer")
    assert recovery.unresolved_blocks(session, user.id, NOW) == []


def test_resolved_blocks_are_not_unresolved(session, user):
    for status in ("done", "partial", "missed", "skipped"):
        _event(session, user, NOW_LOCAL - timedelta(hours=3), status=status)
    assert recovery.unresolved_blocks(session, user.id, NOW) == []


def test_moved_past_block_still_begins_checkin(session, user):
    _, step, ev = _block_with_step(session, user, NOW_LOCAL - timedelta(hours=3))
    ev.status = "moved"
    session.flush()

    assert recovery.unresolved_blocks(session, user.id, NOW) == [ev]
    ctx, prompt = agent.begin_checkin(session, user.id, NOW_LOCAL, TZ)
    assert ctx is not None and ctx.event_ids == [ev.id]
    assert step.title in prompt

    reply = agent.continue_checkin(session, user.id, ctx, "done", NOW_LOCAL, TZ)
    assert reply is not None and reply.checkin_context is None
    assert ev.status == "done"
    assert step.status == "done"
    checkin = session.scalar(select(Checkin).where(Checkin.event_id == ev.id))
    assert checkin is not None and checkin.verdict == "done"


def test_unresolved_blocks_are_ordered_earliest_first(session, user):
    late = _event(session, user, NOW_LOCAL - timedelta(hours=2))
    early = _event(session, user, NOW_LOCAL - timedelta(days=2))
    mid = _event(session, user, NOW_LOCAL - timedelta(hours=20))
    blocks = recovery.unresolved_blocks(session, user.id, NOW)
    assert [b.id for b in blocks] == [early.id, mid.id, late.id]


def test_block_ending_exactly_now_is_not_unresolved(session, user):
    ev = _event(session, user, NOW_LOCAL - timedelta(hours=1))
    assert recovery.unresolved_blocks(session, user.id, ev.end_utc) == []


# ------------------------------------------------------------ resolve_block


def test_resolve_done_marks_event_step_and_checkin(session, user):
    goal, step, ev = _block_with_step(session, user, NOW_LOCAL - timedelta(hours=3))

    checkin = recovery.resolve_block(session, ev, "done", NOW)

    assert ev.status == "done"
    assert ev.resolved_at == NOW
    assert step.status == "done"
    assert step.completed_at == NOW
    assert step.minutes_spent == step.est_min
    assert checkin.event_id == ev.id
    assert checkin.step_id == step.id
    assert checkin.verdict == "done"
    assert checkin.minutes == step.est_min
    assert checkin.ts == NOW


def test_resolve_partial_keeps_step_todo(session, user):
    goal, step, ev = _block_with_step(session, user, NOW_LOCAL - timedelta(hours=3))

    checkin = recovery.resolve_block(session, ev, "partial", NOW, minutes=20)

    assert ev.status == "partial"
    assert ev.resolved_at == NOW
    assert step.status == "todo"
    assert step.completed_at is None
    assert step.minutes_spent == 20
    assert goal_svc.progress(goal) == 0
    assert checkin.verdict == "partial"
    assert checkin.minutes == 20
    assert checkin.event_id == ev.id


def test_resolve_missed_only_leaves_a_checkin(session, user):
    goal, step, ev = _block_with_step(session, user, NOW_LOCAL - timedelta(hours=3))

    checkin = recovery.resolve_block(session, ev, "missed", NOW)

    assert ev.status == "missed"
    assert step.status == "todo"
    assert step.minutes_spent == 0
    assert checkin.verdict == "not_done"
    assert checkin.minutes == 0
    assert checkin.event_id == ev.id


def test_resolve_rejects_unknown_outcome(session, user):
    _, _, ev = _block_with_step(session, user, NOW_LOCAL - timedelta(hours=3))
    with pytest.raises(ValueError):
        recovery.resolve_block(session, ev, "maybe", NOW)


def test_resolve_block_without_step_only_updates_the_event(session, user):
    ev = _event(session, user, NOW_LOCAL - timedelta(hours=3))
    checkin = recovery.resolve_block(session, ev, "missed", NOW)
    assert checkin is None
    assert ev.status == "missed"
    assert session.scalars(select(Checkin)).all() == []


def test_attach_reason_writes_back_to_the_blocks_checkin(session, user):
    _, _, ev = _block_with_step(session, user, NOW_LOCAL - timedelta(hours=3))
    recovery.resolve_block(session, ev, "missed", NOW)

    checkin = recovery.attach_reason(session, ev.id, "no_time")

    assert checkin is not None and checkin.reason == "no_time"
    stored = session.scalar(select(Checkin).where(Checkin.event_id == ev.id))
    assert stored.reason == "no_time"
