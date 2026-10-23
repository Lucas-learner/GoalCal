"""对话签到流程（agent 层）：主动提问 → 结果 → 原因 → 恢复提议 → RescheduleLog。"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from goalcal import agent
from goalcal.domain import calendar_service as cal
from goalcal.domain import goals as goal_svc
from goalcal.models import Checkin, Event, RescheduleLog, Step

TZ = "Asia/Shanghai"
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=ZoneInfo(TZ))  # Wednesday


def _goal_with_steps(session, user, titles):
    defs = [
        {
            "seq": i + 1,
            "milestone": "M",
            "title": t,
            "outcome": "",
            "est_min": 30,
            "energy": 1,
            "kind": "deep",
        }
        for i, t in enumerate(titles)
    ]
    return goal_svc.create_goal(
        session, user.id, "My project",
        deadline=NOW.date() + timedelta(days=30),
        step_defs=defs, template_key="llm",
    )


def _overdue_block(session, user, step, start):
    return cal.create_event(
        session, user.id, f"{step.goal.title}: {step.title}",
        start, start + timedelta(minutes=step.est_min),
        kind="block", source="proposal", step_id=step.id, tz=TZ,
    )


def _one_overdue_block(session, user):
    goal = _goal_with_steps(session, user, ["写脚本", "画分镜"])
    step = sorted(goal.steps, key=lambda s: s.seq)[0]
    ev = _overdue_block(session, user, step, NOW - timedelta(hours=16))  # 昨天 17:00
    return goal, step, ev


def test_begin_checkin_asks_about_the_earliest_block(session, user):
    _, _, ev = _one_overdue_block(session, user)

    ctx, question = agent.begin_checkin(session, user.id, NOW, TZ)

    assert ctx is not None and ctx.stage == "outcome"
    assert ctx.event_ids == [ev.id]
    assert "yesterday 17:00" in question
    assert "写脚本" in question and "How did" in question


def test_begin_checkin_is_quiet_without_overdue_blocks(session, user):
    goal = _goal_with_steps(session, user, ["写脚本"])
    step = sorted(goal.steps, key=lambda s: s.seq)[0]
    _overdue_block(session, user, step, NOW + timedelta(hours=3))  # 未来的块

    ctx, question = agent.begin_checkin(session, user.id, NOW, TZ)
    assert ctx is None and question == ""


def test_quick_reply_done_resolves_and_celebrates(session, user):
    goal, step, ev = _one_overdue_block(session, user)
    ctx, _ = agent.begin_checkin(session, user.id, NOW, TZ)

    reply = agent.continue_checkin(session, user.id, ctx, "Done", NOW, TZ)

    assert reply is not None and reply.checkin_context is None
    assert "marked done" in reply.text
    assert session.get(Event, ev.id).status == "done"
    assert session.get(Step, step.id).status == "done"
    checkin = session.scalar(select(Checkin).where(Checkin.event_id == ev.id))
    assert checkin.verdict == "done"
    # 祝贺后给出下一步候选
    assert 1 <= len(reply.candidates) <= 6
    assert reply.step_id != step.id


def test_partial_quick_reply_asks_reason_then_offers_recovery(session, user):
    _, step, ev = _one_overdue_block(session, user)
    ctx, _ = agent.begin_checkin(session, user.id, NOW, TZ)

    reply = agent.continue_checkin(session, user.id, ctx, "Partly", NOW, TZ)
    assert reply.checkin_context is not None
    assert reply.checkin_context.stage == "reason"
    assert "What got in the way" in reply.text
    assert session.get(Event, ev.id).status == "partial"
    assert session.get(Step, step.id).status == "todo"

    reply = agent.continue_checkin(
        session, user.id, reply.checkin_context, "Too tired", NOW, TZ
    )
    assert reply.checkin_context is not None
    assert reply.checkin_context.stage == "recovery"
    assert 1 <= len(reply.candidates) <= 6
    checkin = session.scalar(select(Checkin).where(Checkin.event_id == ev.id))
    assert checkin.verdict == "partial" and checkin.reason == "tired"


def test_free_text_parses_outcome_and_reason_in_one_turn(session, user):
    _, _, ev = _one_overdue_block(session, user)
    ctx, _ = agent.begin_checkin(session, user.id, NOW, TZ)

    reply = agent.continue_checkin(session, user.id, ctx, "写了一半，太累了", NOW, TZ)

    assert reply.checkin_context is not None
    assert reply.checkin_context.stage == "recovery"
    assert reply.candidates
    checkin = session.scalar(select(Checkin).where(Checkin.event_id == ev.id))
    assert checkin.verdict == "partial" and checkin.reason == "tired"


def test_free_text_missed_with_busy_reason(session, user):
    _, _, ev = _one_overdue_block(session, user)
    ctx, _ = agent.begin_checkin(session, user.id, NOW, TZ)

    reply = agent.continue_checkin(session, user.id, ctx, "没做，太忙了", NOW, TZ)

    assert session.get(Event, ev.id).status == "missed"
    checkin = session.scalar(select(Checkin).where(Checkin.event_id == ev.id))
    assert checkin.verdict == "not_done" and checkin.reason == "no_time"
    assert reply.candidates


def test_new_command_interrupts_and_keeps_context(session, user):
    _, _, _ = _one_overdue_block(session, user)
    ctx, _ = agent.begin_checkin(session, user.id, NOW, TZ)

    reply = agent.continue_checkin(
        session, user.id, ctx, "Add a meeting tomorrow 10am", NOW, TZ
    )

    assert reply is None  # 调用方转交 handle，签到上下文原样保留
    assert ctx.stage == "outcome"
    assert agent.checkin_prompt(session, ctx, NOW, TZ)


def test_unrecognised_answer_reasks_the_question(session, user):
    _, _, ev = _one_overdue_block(session, user)
    ctx, _ = agent.begin_checkin(session, user.id, NOW, TZ)

    reply = agent.continue_checkin(session, user.id, ctx, "hmm", NOW, TZ)

    assert reply.checkin_context is not None
    assert reply.checkin_context.stage == "outcome"
    assert "didn't quite catch" in reply.text
    assert session.get(Event, ev.id).status == "planned"


def test_accept_recovery_books_new_block_and_logs(session, user):
    _, step, ev = _one_overdue_block(session, user)
    ctx, _ = agent.begin_checkin(session, user.id, NOW, TZ)
    reply = agent.continue_checkin(session, user.id, ctx, "没做，太忙了", NOW, TZ)
    ctx = reply.checkin_context
    chosen = reply.candidates[0]

    done = agent.accept_recovery(session, user.id, ctx, chosen, NOW, TZ)

    assert done.checkin_context is None  # 队列里只有这一个块
    old = session.get(Event, ev.id)
    assert old.status == "missed"  # 旧块保留，供恢复率统计
    new = session.scalar(select(Event).where(Event.id != ev.id))
    assert new.kind == "block" and new.step_id == step.id
    assert new.start_utc < new.end_utc
    log = session.scalar(select(RescheduleLog))
    assert log.event_id == ev.id
    assert log.new_event_id == new.id
    assert log.reason == "no_time"
    assert log.auto is False


def test_decline_recovery_keeps_step_todo(session, user):
    _, step, ev = _one_overdue_block(session, user)
    ctx, _ = agent.begin_checkin(session, user.id, NOW, TZ)
    reply = agent.continue_checkin(session, user.id, ctx, "Didn't do it", NOW, TZ)
    reply = agent.continue_checkin(
        session, user.id, reply.checkin_context, "No time", NOW, TZ
    )
    ctx = reply.checkin_context
    assert ctx.stage == "recovery"

    done = agent.decline_recovery(session, user.id, ctx, NOW, TZ)

    assert done.checkin_context is None
    assert session.get(Step, step.id).status == "todo"
    assert session.scalars(select(RescheduleLog)).all() == []
    assert len(session.scalars(select(Event)).all()) == 1  # 只有原来的旧块


def test_unwilling_reason_is_recorded(session, user):
    _, _, ev = _one_overdue_block(session, user)
    ctx, _ = agent.begin_checkin(session, user.id, NOW, TZ)
    reply = agent.continue_checkin(session, user.id, ctx, "没做", NOW, TZ)

    reply = agent.continue_checkin(
        session, user.id, reply.checkin_context, "不想做了", NOW, TZ
    )

    checkin = session.scalar(select(Checkin).where(Checkin.event_id == ev.id))
    assert checkin.reason == "unwilling"


# ------------------------------------------------------------ 多过期块合并


def _many_overdue_blocks(session, user, count):
    goal = _goal_with_steps(session, user, [f"步骤{i + 1}" for i in range(count)])
    events = [
        _overdue_block(session, user, step, NOW - timedelta(hours=10 + i * 2))
        for i, step in enumerate(sorted(goal.steps, key=lambda s: s.seq))
    ]
    return goal, events


def test_many_overdue_blocks_merge_into_one_question(session, user):
    _, events = _many_overdue_blocks(session, user, 4)

    ctx, question = agent.begin_checkin(session, user.id, NOW, TZ)

    assert ctx is not None and ctx.stage == "batch" and ctx.merged
    assert "You missed 4 blocks" in question
    assert agent.checkin_quick_options(ctx) == ["Reschedule all", "Skip them"]


def test_batch_reschedule_marks_all_missed_then_recovers_one_by_one(session, user):
    _, events = _many_overdue_blocks(session, user, 4)
    ctx, _ = agent.begin_checkin(session, user.id, NOW, TZ)

    reply = agent.continue_checkin(session, user.id, ctx, "Reschedule all", NOW, TZ)

    for ev in events:
        assert session.get(Event, ev.id).status == "missed"
    assert reply.checkin_context is not None
    assert reply.checkin_context.stage == "recovery"
    assert reply.candidates

    ctx = reply.checkin_context
    accepted = agent.accept_recovery(session, user.id, ctx, reply.candidates[0], NOW, TZ)
    earliest = min(events, key=lambda e: e.start_utc)
    log = session.scalar(select(RescheduleLog))
    assert log is not None and log.event_id == earliest.id
    # 接受一个后进入下一个块的恢复提议
    assert accepted.checkin_context is not None
    assert accepted.checkin_context.stage == "recovery"


def test_batch_decline_marks_all_missed_without_rescheduling(session, user):
    _, events = _many_overdue_blocks(session, user, 4)
    ctx, _ = agent.begin_checkin(session, user.id, NOW, TZ)

    reply = agent.continue_checkin(session, user.id, ctx, "Skip them", NOW, TZ)

    assert reply.checkin_context is None
    for ev in events:
        assert session.get(Event, ev.id).status == "missed"
    assert session.scalars(select(RescheduleLog)).all() == []


def test_two_blocks_are_asked_one_at_a_time(session, user):
    _, events = _many_overdue_blocks(session, user, 2)
    earliest, later = sorted(events, key=lambda e: e.start_utc)
    ctx, question = agent.begin_checkin(session, user.id, NOW, TZ)

    assert ctx is not None and ctx.stage == "outcome" and not ctx.merged
    assert ctx.event_ids == [earliest.id, later.id]
    reply = agent.continue_checkin(session, user.id, ctx, "Done", NOW, TZ)

    # 第一个块做完后，接着问第二个块
    assert reply.checkin_context is not None
    assert reply.checkin_context.stage == "outcome"
    assert reply.checkin_context.event_ids == [later.id]
    assert "How did" in reply.text
