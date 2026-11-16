"""过期块识别与签到落库（纯业务逻辑，不依赖 UI）。

时间口径和 calendar_service 一致：now 一律是 UTC naive，和 Event.end_utc 同基准。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import metrics
from ..models import Checkin, Event, Goal, Step
from . import goals as goal_svc

# 过期块超过这个数量就合并提问，避免一条一条追问堆积愧疚
MERGE_THRESHOLD = 3

OUTCOMES = ("done", "partial", "missed")
_VERDICT = {"done": "done", "partial": "partial", "missed": "not_done"}


def unresolved_blocks(session: Session, user_id: int, now: datetime) -> list[Event]:
    """已经结束但还没签到的目标块，从最早的开始排。暂停/归档目标的块不再提醒。"""
    return list(
        session.scalars(
            select(Event)
            .outerjoin(Step, Event.step_id == Step.id)
            .outerjoin(Goal, Step.goal_id == Goal.id)
            .where(
                Event.user_id == user_id,
                Event.kind == "block",
                Event.status.in_(("planned", "moved")),
                Event.end_utc < now,
                or_(Goal.id.is_(None), Goal.status == "active"),
            )
            .order_by(Event.start_utc)
        )
    )


def resolve_block(
    session: Session,
    event: Event,
    outcome: str,
    now: datetime,
    minutes: int | None = None,
) -> Checkin | None:
    """写入块的结果和 Checkin；块没有挂步骤时只更新块本身（Checkin.step_id 非空约束）。

    done    → 步骤标 done（complete_step 口径）；
    partial → 步骤保持 todo，只记录投入时间；
    missed  → 步骤保持 todo，只留打卡记录。
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome: {outcome}")
    event.status = outcome
    event.resolved_at = now

    step = session.get(Step, event.step_id) if event.step_id is not None else None
    metrics.record(
        session, event.user_id, metrics.BLOCK_OUTCOME, now,
        goal_id=step.goal_id if step is not None else None,
        step_id=event.step_id, event_id=event.id,
        payload={"outcome": outcome},
    )
    if step is None:
        session.flush()
        return None
    checkin = goal_svc.complete_step(
        session, step, minutes=minutes, verdict=_VERDICT[outcome], now=now
    )
    checkin.event_id = event.id
    checkin.ts = now
    session.flush()
    return checkin


def attach_reason(session: Session, event_id: int, reason: str) -> Checkin | None:
    """把补问来的原因写回这个块的 Checkin。"""
    checkin = session.scalar(
        select(Checkin)
        .where(Checkin.event_id == event_id)
        .order_by(Checkin.id.desc())
    )
    if checkin is not None:
        checkin.reason = reason
        session.flush()
    metrics.update_payload(session, metrics.BLOCK_OUTCOME, event_id, reason=reason)
    return checkin
