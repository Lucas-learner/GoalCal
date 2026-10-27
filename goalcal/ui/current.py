"""共享只读当前状态视图：Chat 首页展示真实的 Goals/Calendar 数据。

只查询、不写库；租户与用户时区由调用方（runtime）提供。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..domain import calendar_service as cal
from ..domain import goals as goal_svc
from ..timeutil import as_local, as_utc_naive
from . import runtime


def current_status(session, user_id: int, now: datetime, tz: str) -> dict:
    """第一个 active goal 的进度与下一步、以及下一个真实日程；无数据字段为 None。"""
    goals = goal_svc.active_goals(session, user_id)
    goal = goals[0] if goals else None
    step = goal_svc.next_step(goal) if goal is not None else None
    start_utc = as_utc_naive(now, tz)
    upcoming = cal.events_between(session, user_id, start_utc, start_utc + timedelta(days=30))
    next_event = upcoming[0] if upcoming else None
    return {
        "goal_id": goal.id if goal is not None else None,
        "goal_title": goal.title if goal is not None else None,
        "progress_pct": round(goal_svc.progress(goal) * 100) if goal is not None else None,
        "days_left": (goal.deadline - now.date()).days if goal is not None and goal.deadline else None,
        "next_step_id": step.id if step is not None else None,
        "next_step_title": step.title if step is not None else None,
        "next_step_minutes": step.est_min if step is not None else None,
        "next_event_title": next_event.title if next_event is not None else None,
        "next_event_start": as_local(next_event.start_utc, tz) if next_event is not None else None,
    }


def has_data(status: dict) -> bool:
    return status["goal_title"] is not None or status["next_event_title"] is not None


def load() -> dict:
    with runtime.session_scope("current_status.load") as session:
        return current_status(session, runtime.user_id(), runtime.now(), runtime.timezone())
