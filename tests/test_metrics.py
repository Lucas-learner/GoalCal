"""阶段 3：埋点写入与三个指标（建议接受率 / 72h 恢复率 / 推进率）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from goalcal import agent, metrics
from goalcal.domain import calendar_service as cal
from goalcal.domain import goals as goal_svc
from goalcal.domain import recovery
from goalcal.models import Event, Interaction, RescheduleLog
from goalcal.timeutil import as_local, as_utc_naive

TZ = "Asia/Shanghai"
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=ZoneInfo(TZ))  # Wednesday


def _interactions(session, kind):
    return list(
        session.scalars(
            select(Interaction).where(Interaction.kind == kind).order_by(Interaction.id)
        )
    )


def _payload(row):
    return json.loads(row.payload)


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


def _resolved_block(session, user, status, start, minutes=60, end=None):
    ev = cal.create_event(
        session, user.id, "block", start, end or start + timedelta(minutes=minutes),
        kind="block", tz=TZ,
    )
    ev.status = status
    ev.resolved_at = as_utc_naive(NOW, TZ)
    session.flush()
    return ev


def _reschedule(session, old_ev, new_start):
    """new_start 传 timedelta 时按 原块 end + 偏移 计算（aware，避免被当成UTC）。"""
    if isinstance(new_start, timedelta):
        new_start = as_local(old_ev.end_utc, TZ) + new_start
    new_ev = cal.create_event(
        session, old_ev.user_id, old_ev.title, new_start,
        new_start + (old_ev.end_utc - old_ev.start_utc),
        kind="block", source="proposal", step_id=old_ev.step_id, tz=TZ,
    )
    session.add(
        RescheduleLog(event_id=old_ev.id, new_event_id=new_ev.id, reason="tired", auto=False)
    )
    session.flush()
    return new_ev


# ------------------------------------------------------------ 埋点写入


def test_record_writes_ts_sim_and_wall(session, user):
    metrics.record(session, user.id, metrics.PARSE, NOW, payload={"intent": "chat"})

    row = session.scalar(select(Interaction))
    assert row.user_id == user.id
    assert row.ts_sim == as_utc_naive(NOW, TZ)  # 时间机器口径，UTC naive
    assert row.ts_wall is not None
    assert _payload(row) == {"intent": "chat"}


def test_handle_records_parse_event(session, user):
    agent.handle(session, user.id, "Add a meeting tomorrow 10am", NOW, TZ)

    rows = _interactions(session, metrics.PARSE)
    assert len(rows) == 1
    assert _payload(rows[0]) == {"parser": "rules:v1", "intent": "add_event"}


def test_proposal_shown_and_accepted(session, user):
    reply = agent.handle(
        session, user.id, "I want to publish my first short video in 30 days", NOW, TZ
    )
    reply = agent.confirm_goal_draft(session, user.id, reply.goal_draft, True, NOW, TZ)

    shown = _interactions(session, metrics.PROPOSAL_SHOWN)
    assert len(shown) == 1
    assert _payload(shown[0]) == {"count": len(reply.candidates), "recovery": False}
    assert shown[0].step_id == reply.step_id and shown[0].goal_id == reply.goal_id

    agent.accept(session, user.id, reply.step_id, reply.candidates[0], NOW, TZ)

    accepted = _interactions(session, metrics.PROPOSAL_ACCEPTED)
    assert len(accepted) == 1
    assert _payload(accepted[0]) == {"recovery": False}
    assert metrics.acceptance_rate(session, user.id, NOW) == 1.0


def test_block_outcome_recorded_and_reason_backfilled(session, user):
    goal = _goal_with_steps(session, user, [("写脚本", 60, 1)])
    step = sorted(goal.steps, key=lambda s: s.seq)[0]
    ev = _overdue_block(session, user, step)

    recovery.resolve_block(session, ev, "missed", as_utc_naive(NOW, TZ))

    rows = _interactions(session, metrics.BLOCK_OUTCOME)
    assert len(rows) == 1
    assert rows[0].event_id == ev.id and rows[0].step_id == step.id
    assert _payload(rows[0]) == {"outcome": "missed"}

    recovery.attach_reason(session, ev.id, "tired")
    assert _payload(rows[0]) == {"outcome": "missed", "reason": "tired"}


def test_recovery_action_records_scheme_and_recovery_proposal(session, user):
    goal = _goal_with_steps(session, user, [("写脚本", 60, 1)])
    step = sorted(goal.steps, key=lambda s: s.seq)[0]
    _overdue_block(session, user, step)

    ctx, _ = agent.begin_checkin(session, user.id, NOW, TZ)
    reply = agent.continue_checkin(session, user.id, ctx, "没做", NOW, TZ)
    reply = agent.continue_checkin(
        session, user.id, reply.checkin_context, "没时间", NOW, TZ
    )

    assert reply.checkin_context.stage == "recovery"
    actions = _interactions(session, metrics.RECOVERY_ACTION)
    assert len(actions) == 1
    assert _payload(actions[0]) == {"reason": "no_time", "scheme": "starter_15"}
    shown = _interactions(session, metrics.PROPOSAL_SHOWN)
    assert len(shown) == 1 and _payload(shown[0])["recovery"] is True


def test_unwilling_choice_records_recovery_action(session, user):
    goal = _goal_with_steps(session, user, [("写脚本", 60, 1)])
    step = sorted(goal.steps, key=lambda s: s.seq)[0]
    _overdue_block(session, user, step)

    ctx, _ = agent.begin_checkin(session, user.id, NOW, TZ)
    reply = agent.continue_checkin(session, user.id, ctx, "没做", NOW, TZ)
    reply = agent.continue_checkin(
        session, user.id, reply.checkin_context, "不想做了", NOW, TZ
    )
    assert reply.checkin_context.stage == "goal_options"
    agent.continue_checkin(session, user.id, reply.checkin_context, "延后 7 天", NOW, TZ)

    actions = _interactions(session, metrics.RECOVERY_ACTION)
    assert len(actions) == 1
    assert _payload(actions[0]) == {"reason": "unwilling", "choice": "extend_7"}
    assert actions[0].goal_id == goal.id


def test_plan_refined_recorded_when_stuck(session, user):
    goal = _goal_with_steps(session, user, [("写脚本", 60, 2)])
    step = sorted(goal.steps, key=lambda s: s.seq)[0]
    _overdue_block(session, user, step)

    ctx, _ = agent.begin_checkin(session, user.id, NOW, TZ)
    reply = agent.continue_checkin(session, user.id, ctx, "没做", NOW, TZ)
    agent.continue_checkin(session, user.id, reply.checkin_context, "卡住了", NOW, TZ)

    rows = _interactions(session, metrics.PLAN_REFINED)
    assert len(rows) == 1
    assert rows[0].goal_id == goal.id and rows[0].step_id == step.id
    assert _payload(rows[0])["created"] >= 1
    actions = _interactions(session, metrics.RECOVERY_ACTION)
    assert _payload(actions[0]) == {"reason": "stuck", "scheme": "refine"}


# ------------------------------------------------------------ 建议接受率


def test_acceptance_rate_none_without_proposals(session, user):
    assert metrics.acceptance_rate(session, user.id, NOW) is None


def test_acceptance_rate_counts_only_window(session, user):
    # 8 天前（窗口外）的展示不计入分母
    metrics.record(session, user.id, metrics.PROPOSAL_SHOWN, NOW - timedelta(days=8))
    metrics.record(session, user.id, metrics.PROPOSAL_SHOWN, NOW)
    metrics.record(session, user.id, metrics.PROPOSAL_ACCEPTED, NOW)

    assert metrics.acceptance_rate(session, user.id, NOW) == 1.0
    assert metrics.acceptance_rate(session, user.id, NOW, days=10) == 0.5


# ------------------------------------------------------------ 72h 恢复率


def test_recovery_rate_none_without_missed_blocks(session, user):
    assert metrics.recovery_rate_72h(session, user.id, NOW) is None


def test_recovery_rate_72h_boundary_counts(session, user):
    old = _resolved_block(session, user, "missed", NOW - timedelta(hours=20))
    # 新块 start - 原块 end 正好 72h：算恢复
    _reschedule(session, old, timedelta(hours=72))

    assert metrics.recovery_rate_72h(session, user.id, NOW) == 1.0


def test_recovery_rate_over_72h_does_not_count(session, user):
    old = _resolved_block(session, user, "missed", NOW - timedelta(hours=20))
    _reschedule(session, old, timedelta(hours=72, minutes=1))
    no_log = _resolved_block(session, user, "partial", NOW - timedelta(hours=10))
    assert no_log.status == "partial"

    assert metrics.recovery_rate_72h(session, user.id, NOW) == 0.0


def test_recovery_rate_uses_resolved_at_window(session, user):
    old = _resolved_block(session, user, "missed", NOW - timedelta(hours=20))
    _reschedule(session, old, NOW + timedelta(hours=2))

    assert metrics.recovery_rate_72h(session, user.id, NOW) == 1.0
    # 快进到 8 天后：签到时刻落在窗口外，分母为 0
    assert metrics.recovery_rate_72h(session, user.id, NOW + timedelta(days=8)) is None


# ------------------------------------------------------------ 推进率


def test_progress_rate_done_over_all_resolved(session, user):
    _resolved_block(session, user, "done", NOW - timedelta(hours=3), minutes=60)
    _resolved_block(session, user, "partial", NOW - timedelta(hours=2), minutes=30)
    _resolved_block(session, user, "missed", NOW - timedelta(hours=1), minutes=30)

    assert metrics.progress_rate(session, user.id, NOW) == 0.5


def test_progress_rate_none_without_resolved_blocks(session, user):
    _resolved_block(session, user, "planned", NOW - timedelta(hours=1))
    assert metrics.progress_rate(session, user.id, NOW) is None


# ------------------------------------------------------------ 端到端：demo 脚本


def test_end_to_end_fast_forward_recovery_rate_is_100(session, user):
    reply = agent.handle(
        session, user.id, "I want to publish my first short video in 30 days", NOW, TZ
    )
    reply = agent.confirm_goal_draft(session, user.id, reply.goal_draft, True, NOW, TZ)
    agent.accept(session, user.id, reply.step_id, reply.candidates[0], NOW, TZ)

    later = NOW + timedelta(days=2)  # 时间机器快进 2 天
    ctx, question = agent.begin_checkin(session, user.id, later, TZ)
    assert ctx is not None and question

    r1 = agent.continue_checkin(session, user.id, ctx, "没做", later, TZ)
    r2 = agent.continue_checkin(session, user.id, r1.checkin_context, "太累了", later, TZ)
    assert r2.checkin_context.stage == "recovery" and r2.candidates

    done = agent.continue_checkin(session, user.id, r2.checkin_context, "第一个", later, TZ)
    assert done.checkin_context is None

    assert metrics.recovery_rate_72h(session, user.id, later) == 1.0
    accepted = [
        r for r in _interactions(session, metrics.PROPOSAL_ACCEPTED)
        if _payload(r)["recovery"]
    ]
    assert len(accepted) == 1
    outcomes = [_payload(r)["outcome"] for r in _interactions(session, metrics.BLOCK_OUTCOME)]
    assert "missed" in outcomes
