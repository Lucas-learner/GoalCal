from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .domain import calendar_service as cal
from .domain import goals as goal_svc
from .domain import refine
from .models import User
from .timeutil import local_now


def seed_if_empty(session: Session, tz: str = "Asia/Shanghai") -> User:
    user = session.scalar(select(User).limit(1))
    if user is not None:
        return user

    user = goal_svc.ensure_user(session, "demo", tz)
    now = local_now(tz)
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)

    ext = [
        ("Team standup", 1, 10, 30),
        ("Class: GenAI", 1, 14, 90),
        ("Gym", 2, 19, 60),
        ("Client call", 3, 10, 60),
        ("Dentist", 4, 16, 45),
        ("Team standup", 5, 10, 30),
        ("Dinner with friends", 5, 19, 120),
    ]
    for title, day_offset, hour, minutes in ext:
        start = base + timedelta(days=day_offset, hours=hour)
        cal.create_event(
            session, user.id, title, start, start + timedelta(minutes=minutes),
            kind="external", source="seed", tz=tz,
        )

    goal = goal_svc.create_goal(
        session,
        user.id,
        "Publish my first short video",
        deadline=(base + timedelta(days=30)).date(),
    )
    # 让它像一个用了一周的目标：第一步已完成，下一批步骤已滚出来
    first = sorted(goal.steps, key=lambda s: s.seq)[0]
    goal_svc.complete_step(session, first, raw_text="seeded progress")
    refine.refine_upcoming(session, goal)

    session.flush()
    return user
