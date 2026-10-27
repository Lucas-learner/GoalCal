"""共享只读当前状态视图：Chat 首页展示真实的 Goals/Calendar 数据。

只查询、不写库；租户与用户时区由调用方（runtime）提供。
首页按目标优先级呈现一个未排期步骤（pinned → deadline → created_at），多目标管理在 Goals 页。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from ..domain import calendar_service as cal
from ..domain import goals as goal_svc
from ..models import Event
from ..timeutil import as_local, as_utc_naive
from . import runtime


def current_status(session, user_id: int, now: datetime, tz: str) -> dict:
    """优先目标的下一未排期步骤、以及下一个真实日程；无数据字段为 None。"""
    goals = goal_svc.prioritized_goals(session, user_id)
    start_utc = as_utc_naive(now, tz)
    step_ids = [step.id for goal in goals for step in goal.steps]
    scheduled = set(session.scalars(
        select(Event.step_id).where(
            Event.user_id == user_id,
            Event.step_id.in_(step_ids or [-1]),
            Event.kind == "block",
            Event.start_utc >= start_utc,
        )
    ))
    goal = goals[0] if goals else None
    step = None
    for candidate_goal in goals:
        step = next(
            (item for item in sorted(candidate_goal.steps, key=lambda item: item.seq)
             if item.status == "todo" and item.id not in scheduled),
            None,
        )
        if step is not None:
            goal = candidate_goal
            break
    upcoming = cal.events_between(session, user_id, start_utc, start_utc + timedelta(days=30))
    next_event = upcoming[0] if upcoming else None
    return {
        "goal_title": goal.title if goal is not None else None,
        "next_step_id": step.id if step is not None else None,
        "next_step_minutes": step.est_min if step is not None else None,
        "next_event_title": next_event.title if next_event is not None else None,
        "next_event_start": as_local(next_event.start_utc, tz) if next_event is not None else None,
    }


def load() -> dict:
    with runtime.session_scope("current_status.load") as session:
        return current_status(session, runtime.user_id(), runtime.now(), runtime.timezone())
