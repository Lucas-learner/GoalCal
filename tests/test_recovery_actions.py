"""阶段 2：四种原因各自的恢复动作、文字选候选、72h 兜底、暂停目标的隔离。"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from goalcal import agent
from goalcal.domain import calendar_service as cal
from goalcal.domain import goals as goal_svc
from goalcal.domain import recovery
from goalcal.models import Event, Goal, RescheduleLog, Step
from goalcal.timeutil import as_utc_naive

TZ = "Asia/Shanghai"
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=ZoneInfo(TZ))  # Wednesday


def _goal_with_steps(session, user, defs):
    step_defs = [
        {
            "seq": i + 1,
            "milestone": "M",
            "title": title,
            "outcome": "",
            "est_min": est,
            "energy": energy,
            "kind": "deep",
        }
        for i, (title, est, energy) in enumerate(defs)
    ]
    return goal_svc.create_goal(
        session, user.id, "My project",
        deadline=NOW.date() + timedelta(days=30),
        step_defs=step_defs, template_key="llm",
    )


def _overdue_block(session, user, step, start=None):
    start = start or NOW - timedelta(hours=16)  # 昨天 17:00
    return cal.create_event(
        session, user.id, f"{step.goal.title}: {step.title}",
        start, start + timedelta(minutes=step.est_min),
        kind="block", source="proposal", step_id=step.id, tz=TZ,
    )


def _reach_recovery(session, user, step, reason_text, ev=None):
    ev = ev or _overdue_block(session, user, step)
    ctx, _ = agent.begin_checkin(session, user.id, NOW, TZ)
    reply = agent.continue_checkin(session, user.id, ctx, "Didn't do it", NOW, TZ)
    reply = agent.continue_checkin(
        session, user.id, reply.checkin_context, reason_text, NOW, TZ
    )
    return ev, reply


# ------------------------------------------------------------ tired


def test_tired_swaps_to_a_low_energy_step(session, user):
    goal = _goal_with_steps(session, user, [("写脚本", 60, 2), ("整理素材", 20, 0)])
    heavy, light = sorted(goal.steps, key=lambda s: s.seq)

    _, reply = _reach_recovery(session, user, heavy, "Too tired")

    assert reply.checkin_context.stage == "recovery"
    assert reply.step_id == light.id
    assert reply.candidates
    assert "lighter step" in reply.text and "整理素材" in reply.text


def test_tired_without_low_energy_step_halves_the_block(session, user):
    goal = _goal_with_steps(session, user, [("写脚本", 60, 2), ("画分镜", 45, 2)])
    step = sorted(goal.steps, key=lambda s: s.seq)[0]

    _, reply = _reach_recovery(session, user, step, "Too tired")

    assert reply.checkin_context.stage == "recovery"
    assert reply.step_id == step.id  # 还是当前步骤
    assert reply.candidates
    assert all(c.minutes == 30 for c in reply.candidates)  # 60 减半
    assert "half length" in reply.text


# ------------------------------------------------------------ no_time


def test_no_time_offers_a_15_minute_starter_block(session, user):
    goal = _goal_with_steps(session, user, [("写脚本", 60, 1)])
    step = sorted(goal.steps, key=lambda s: s.seq)[0]

    _, reply = _reach_recovery(session, user, step, "No time")

    assert reply.step_id == step.id
    assert reply.candidates
    assert all(c.minutes == 15 for c in reply.candidates)
    assert "15-minute" in reply.text


# ------------------------------------------------------------ stuck


def test_stuck_refines_step_and_offers_the_5_minute_starter(session, user):
    goal = _goal_with_steps(session, user, [("写脚本", 60, 2), ("画分镜", 45, 1)])
    step = sorted(goal.steps, key=lambda s: s.seq)[0]
    before = [s.title for s in goal.steps]

    _, reply = _reach_recovery(session, user, step, "Stuck")

    starter = session.get(Step, reply.step_id)
    assert starter is not None and starter.id != step.id
    assert "5 分钟" in starter.title and starter.est_min == 5
    assert starter.seq < session.get(Step, step.id).seq  # 插在原步骤前面
    steps = session.scalars(select(Step).where(Step.goal_id == goal.id)).all()
    assert len(steps) == len(before) + 2  # 规则降级：5 分钟启动 + 先完成一半
    assert session.get(Step, step.id).status == "todo"  # 原步骤保留
    assert reply.candidates and all(c.minutes == 5 for c in reply.candidates)
    assert "broke it down" in reply.text


# ------------------------------------------------------------ unwilling


def _reach_goal_options(session, user):
    goal = _goal_with_steps(session, user, [("写脚本", 60, 1)])
    step = sorted(goal.steps, key=lambda s: s.seq)[0]
    ev, reply = _reach_recovery(session, user, step, "Not feeling it")
    return goal, step, ev, reply


def test_unwilling_offers_goal_options_instead_of_slots(session, user):
    _, _, _, reply = _reach_goal_options(session, user)

    ctx = reply.checkin_context
    assert ctx is not None and ctx.stage == "goal_options"
    assert not reply.candidates  # 不出时段卡片
    assert agent.checkin_quick_options(ctx) == [
        "Push 7 days", "Push 14 days", "Pause this goal", "Archive this goal"
    ]
    assert "No pressure" in reply.text


def test_extend_deadline_by_7_days(session, user):
    goal, _, _, reply = _reach_goal_options(session, user)
    old = goal.deadline

    done = agent.continue_checkin(
        session, user.id, reply.checkin_context, "Push 7 days", NOW, TZ
    )

    assert done.checkin_context is None
    assert goal.deadline == old + timedelta(days=7)
    assert goal.status == "active"
    assert f"{goal.deadline:%b %d}" in done.text


def test_extend_deadline_by_14_days(session, user):
    goal, _, _, reply = _reach_goal_options(session, user)
    old = goal.deadline

    done = agent.continue_checkin(
        session, user.id, reply.checkin_context, "Push 14 days", NOW, TZ
    )

    assert done.checkin_context is None
    assert goal.deadline == old + timedelta(days=14)
    assert goal.status == "active"


def test_pause_goal_hides_it_from_scheduling_and_reminders(session, user):
    goal, step, ev, reply = _reach_goal_options(session, user)
    future = cal.create_event(
        session, user.id, f"{goal.title}: {step.title}",
        NOW + timedelta(days=1), NOW + timedelta(days=1, minutes=30),
        kind="block", source="proposal", step_id=step.id, tz=TZ,
    )

    done = agent.continue_checkin(
        session, user.id, reply.checkin_context, "Pause this goal", NOW, TZ
    )

    assert done.checkin_context is None
    assert goal.status == "paused"
    assert session.get(Event, future.id) is None  # 未来块撤掉
    assert goal_svc.active_goals(session, user.id) == []
    assert agent._pick_step(goal) is None
    # 暂停目标的过期块不再进入签到提醒
    assert recovery.unresolved_blocks(session, user.id, as_utc_naive(NOW, TZ)) == []


def test_archive_goal_from_goal_options(session, user):
    goal, _, _, reply = _reach_goal_options(session, user)

    done = agent.continue_checkin(
        session, user.id, reply.checkin_context, "Archive this goal", NOW, TZ
    )

    assert done.checkin_context is None
    assert goal.status == "archived"
    assert goal_svc.active_goals(session, user.id) == []


def test_goal_options_keep_context_on_unrecognised_answer(session, user):
    goal, _, _, reply = _reach_goal_options(session, user)

    again = agent.continue_checkin(
        session, user.id, reply.checkin_context, "hmm", NOW, TZ
    )

    assert again.checkin_context is not None
    assert again.checkin_context.stage == "goal_options"
    assert goal.status == "active"


# ------------------------------------------------------------ 文字选候选


def test_text_ordinal_accepts_the_matching_candidate(session, user):
    goal = _goal_with_steps(session, user, [("写脚本", 60, 1)])
    step = sorted(goal.steps, key=lambda s: s.seq)[0]
    ev, reply = _reach_recovery(session, user, step, "No time")
    ctx = reply.checkin_context
    chosen = reply.candidates[0]

    done = agent.continue_checkin(session, user.id, ctx, "the first one", NOW, TZ)

    assert done is not None and done.checkin_context is None
    new = session.scalar(select(Event).where(Event.id != ev.id))
    assert new is not None and new.kind == "block" and new.step_id == step.id
    assert (new.end_utc - new.start_utc) == timedelta(minutes=15)
    log = session.scalar(select(RescheduleLog))
    assert log.event_id == ev.id and log.new_event_id == new.id
    assert log.reason == "no_time"


def test_text_digit_selects_the_second_candidate(session, user):
    goal = _goal_with_steps(session, user, [("写脚本", 60, 1)])
    step = sorted(goal.steps, key=lambda s: s.seq)[0]
    ev, reply = _reach_recovery(session, user, step, "No time")
    assert len(reply.candidates) >= 2
    second = reply.candidates[1]

    done = agent.continue_checkin(session, user.id, reply.checkin_context, "2", NOW, TZ)

    assert done is not None and done.checkin_context is None
    new = session.scalar(select(Event).where(Event.id != ev.id))
    assert new.start_utc == as_utc_naive(second.start, TZ)


def test_ordinal_out_of_range_reasks(session, user):
    goal = _goal_with_steps(session, user, [("写脚本", 60, 1)])
    step = sorted(goal.steps, key=lambda s: s.seq)[0]
    _, reply = _reach_recovery(session, user, step, "No time")
    ctx = reply.checkin_context
    ctx.candidates = ctx.candidates[:1]  # 只剩一个候选

    again = agent.continue_checkin(session, user.id, ctx, "the third one", NOW, TZ)

    assert again.checkin_context is not None
    assert again.checkin_context.stage == "recovery"
    assert session.scalars(select(RescheduleLog)).all() == []


# ------------------------------------------------------------ 72h 兜底


def test_candidates_fall_back_to_day_4_to_7_when_72h_is_full(session, user):
    goal = _goal_with_steps(session, user, [("写脚本", 60, 1)])
    step = sorted(goal.steps, key=lambda s: s.seq)[0]
    for d in range(3):  # 未来 72 小时排满 external 日程
        day = NOW + timedelta(days=d)
        start = day.replace(hour=8, minute=0)
        cal.create_event(
            session, user.id, "busy", start, start + timedelta(hours=14), tz=TZ
        )

    _, reply = _reach_recovery(session, user, step, "No time")

    assert reply.candidates
    assert all(
        c.start.date() >= NOW.date() + timedelta(days=3) for c in reply.candidates
    )
