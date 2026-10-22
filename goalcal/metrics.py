"""指标埋点与三个核心指标（阶段 3）。

时间口径：埋点的 ts_sim 和指标窗口一律用注入的模拟时间（时间机器口径），
存库前统一转成 UTC naive，和项目其他表一致；ts_wall 由模型默认值写真实时间。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Event, Interaction, RescheduleLog
from .timeutil import UTC

PROPOSAL_SHOWN = "proposal_shown"
PROPOSAL_ACCEPTED = "proposal_accepted"
BLOCK_OUTCOME = "block_outcome"
RECOVERY_ACTION = "recovery_action"
PLAN_REFINED = "plan_refined"
PARSE = "parse"

RECOVERY_LIMIT = timedelta(hours=72)


def _utc(dt: datetime) -> datetime:
    """aware → UTC naive；naive 一律视为 UTC（domain 层约定）。"""
    if dt.tzinfo is not None:
        return dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def record(
    session: Session,
    user_id: int,
    kind: str,
    now: datetime,
    *,
    goal_id: int | None = None,
    step_id: int | None = None,
    event_id: int | None = None,
    payload: dict | None = None,
) -> Interaction:
    """写一条埋点；ts_sim 用注入的 now，ts_wall 用真实时间。"""
    row = Interaction(
        user_id=user_id,
        ts_sim=_utc(now),
        kind=kind,
        goal_id=goal_id,
        step_id=step_id,
        event_id=event_id,
        payload=json.dumps(payload or {}, ensure_ascii=False),
    )
    session.add(row)
    session.flush()
    return row


def update_payload(session: Session, kind: str, event_id: int, **fields) -> None:
    """给同 kind + event 最新一条埋点补充 payload 字段（如签到后补问来的原因）。"""
    row = session.scalar(
        select(Interaction)
        .where(Interaction.kind == kind, Interaction.event_id == event_id)
        .order_by(Interaction.id.desc())
    )
    if row is not None:
        payload = json.loads(row.payload or "{}")
        payload.update(fields)
        row.payload = json.dumps(payload, ensure_ascii=False)
        session.flush()


def _window(now: datetime, days: int) -> tuple[datetime, datetime]:
    end = _utc(now)
    return end - timedelta(days=days), end


def _count(session: Session, user_id: int, kind: str, start: datetime, end: datetime) -> int:
    return session.scalar(
        select(func.count())
        .select_from(Interaction)
        .where(
            Interaction.user_id == user_id,
            Interaction.kind == kind,
            Interaction.ts_sim >= start,
            Interaction.ts_sim <= end,
        )
    ) or 0


def acceptance_rate(session: Session, user_id: int, now: datetime, days: int = 7) -> float | None:
    """建议接受率 = proposal_accepted / proposal_shown（窗口内 ts_sim 口径）。

    分母为 0 返回 None。
    """
    start, end = _window(now, days)
    shown = _count(session, user_id, PROPOSAL_SHOWN, start, end)
    if shown == 0:
        return None
    return _count(session, user_id, PROPOSAL_ACCEPTED, start, end) / shown


def recovery_rate_72h(session: Session, user_id: int, now: datetime, days: int = 7) -> float | None:
    """72h 恢复率 = 窗口内签到为 missed/partial 的块中，72 小时内有新块接上的比例。

    口径：分母取 Event.resolved_at（签到时刻，注入的模拟时间）落在窗口内的
    missed/partial 块；分子要求 RescheduleLog 有 new_event_id，且
    新块 start - 原块 end ≤ 72h。选 RescheduleLog 而不是 interactions 关联，
    因为它是接受恢复候选时确定性写库的结构化记录，不依赖埋点的先后顺序。
    分母为 0 返回 None。
    """
    start, end = _window(now, days)
    blocks = list(
        session.scalars(
            select(Event).where(
                Event.user_id == user_id,
                Event.kind == "block",
                Event.status.in_(("missed", "partial")),
                Event.resolved_at >= start,
                Event.resolved_at <= end,
            )
        )
    )
    if not blocks:
        return None
    recovered = 0
    for ev in blocks:
        log = session.scalar(
            select(RescheduleLog)
            .where(
                RescheduleLog.event_id == ev.id,
                RescheduleLog.new_event_id.is_not(None),
            )
            .order_by(RescheduleLog.id)
        )
        if log is None:
            continue
        new_ev = session.get(Event, log.new_event_id)
        if new_ev is not None and new_ev.start_utc - ev.end_utc <= RECOVERY_LIMIT:
            recovered += 1
    return recovered / len(blocks)


def _minutes(ev: Event) -> int:
    return int((ev.end_utc - ev.start_utc).total_seconds() // 60)


def progress_rate(session: Session, user_id: int, now: datetime, days: int = 7) -> float | None:
    """推进率 ≈ done 块时长 / (done + partial + missed) 块总时长（窗口内）。

    理想口径是“done 时长 / 空闲可排时长”，但历史空闲无法重建
    （scheduler.free_intervals 只能按当前日程算未来空档），故用已排块的
    结果结构近似：分母里 missed/partial 越多说明排了没做的越多。
    窗口按 Event.resolved_at（签到时刻，模拟时间）取。分母为 0 返回 None。
    """
    start, end = _window(now, days)
    blocks = list(
        session.scalars(
            select(Event).where(
                Event.user_id == user_id,
                Event.kind == "block",
                Event.status.in_(("done", "partial", "missed")),
                Event.resolved_at >= start,
                Event.resolved_at <= end,
            )
        )
    )
    total = sum(_minutes(ev) for ev in blocks)
    if total == 0:
        return None
    done = sum(_minutes(ev) for ev in blocks if ev.status == "done")
    return done / total
