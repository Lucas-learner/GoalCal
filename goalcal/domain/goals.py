from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..data.goal_templates import build_steps, milestones, pick_template
from ..models import Checkin, Goal, Step, User
from ..timeutil import now_utc


def ensure_user(session: Session, name: str = "demo", tz: str = "Asia/Shanghai") -> User:
    user = session.scalar(select(User).where(User.name == name))
    if user is None:
        user = User(name=name, tz=tz)
        session.add(user)
        session.flush()
    return user


def create_goal(
    session: Session,
    user_id: int,
    title: str,
    deadline: date | None = None,
    outcome: str = "",
) -> Goal:
    """建目标 + 一次性拆解成叶子任务（M1 用模板，M2 换成 LLM + RAG）。"""
    deadline = deadline or (date.today() + timedelta(days=30))
    key, step_defs = build_steps(title)

    goal = Goal(
        user_id=user_id,
        title=title.strip()[:200],
        outcome=outcome or title.strip(),
        deadline=deadline,
        template_key=key,
    )
    session.add(goal)
    session.flush()

    for d in step_defs:
        session.add(Step(goal_id=goal.id, **d))
    session.flush()
    session.refresh(goal)
    return goal


def active_goals(session: Session, user_id: int) -> list[Goal]:
    return list(
        session.scalars(
            select(Goal).where(Goal.user_id == user_id, Goal.status == "active").order_by(Goal.created_at)
        )
    )


def progress(goal: Goal) -> float:
    """按预估时长加权的完成度，避免“小任务刷进度”。"""
    total = sum(s.est_min for s in goal.steps) or 1
    done = sum(s.est_min for s in goal.steps if s.status == "done")
    return round(done / total, 6)


def next_step(goal: Goal) -> Step | None:
    for s in sorted(goal.steps, key=lambda x: x.seq):
        if s.status in {"todo", "scheduled"}:
            return s
    return None


def complete_step(
    session: Session,
    step: Step,
    minutes: int | None = None,
    verdict: str = "done",
    raw_text: str = "",
) -> None:
    step.status = "done" if verdict == "done" else "scheduled"
    step.completed_at = now_utc()
    step.minutes_spent = minutes if minutes is not None else step.est_min
    session.add(
        Checkin(
            step_id=step.id,
            raw_text=raw_text,
            verdict=verdict,
            minutes=step.minutes_spent,
        )
    )
    goal = step.goal
    if all(s.status == "done" for s in goal.steps):
        goal.status = "done"


def snapshot(goal: Goal) -> dict:
    steps = sorted(goal.steps, key=lambda x: x.seq)
    done = [s for s in steps if s.status == "done"]
    nxt = next_step(goal)
    return {
        "id": goal.id,
        "title": goal.title,
        "deadline": goal.deadline,
        "progress": progress(goal),
        "done": len(done),
        "total": len(steps),
        "milestones": milestones(goal.template_key),
        "next_step": nxt,
        "steps": steps,
        "days_left": (goal.deadline - date.today()).days if goal.deadline else None,
        "template_key": goal.template_key or pick_template(goal.title),
    }
