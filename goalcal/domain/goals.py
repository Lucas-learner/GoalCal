from __future__ import annotations

import difflib
from datetime import date, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..data.goal_templates import build_steps, milestones, pick_template
from ..models import Checkin, Event, Goal, Step, User
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
    with_template: bool = True,
    complexity: str = "full",
    step_defs: list[dict] | None = None,
    template_key: str | None = None,
) -> Goal:
    """建目标。

    step_defs 不为空时使用调用方给的步骤（例如 LLM 拆解）；
    否则按复杂度从模板库取步骤。with_template=False 建空目标。
    """
    deadline = deadline or (date.today() + timedelta(days=30))
    if step_defs is not None:
        key = template_key or "llm"
    elif with_template:
        key, step_defs = build_steps(title, complexity)
    else:
        key, step_defs = "custom", []

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


def find_goal(session: Session, user_id: int, hint: str) -> Goal | None:
    """按标题找目标；没有标题时，只有在唯一目标的情况下才自动选中。"""
    goals = active_goals(session, user_id)
    if not goals:
        return None
    hint_l = (hint or "").lower().strip().strip("\"'“”")
    if not hint_l:
        return goals[0] if len(goals) == 1 else None

    for goal in goals:
        if goal.title.lower() == hint_l:
            return goal
    for goal in goals:
        if hint_l in goal.title.lower() or goal.title.lower() in hint_l:
            return goal

    by_title = {goal.title.lower(): goal for goal in goals}
    match = difflib.get_close_matches(hint_l, list(by_title), n=1, cutoff=0.5)
    return by_title[match[0]] if match else None


def archive_goal(session: Session, goal: Goal) -> int:
    """软删目标，并撤掉它挂在日历上的未来目标块。"""
    from . import calendar_service as cal

    removed = sum(cal.cancel_blocks(session, step.id) for step in goal.steps)
    goal.status = "archived"
    session.flush()
    return removed


def add_step(
    session: Session,
    goal: Goal,
    title: str,
    est_min: int = 45,
    energy: int = 1,
    kind: str = "deep",
    milestone: str = "Custom",
) -> Step:
    seq = max((step.seq for step in goal.steps), default=0) + 1
    step = Step(
        goal_id=goal.id,
        seq=seq,
        milestone=milestone,
        title=title.strip()[:200],
        est_min=max(5, min(int(est_min), 240)),
        energy=max(0, min(int(energy), 2)),
        kind=kind,
    )
    session.add(step)
    session.flush()
    return step


def update_step(
    session: Session,
    step: Step,
    *,
    title: str | None = None,
    milestone: str | None = None,
    outcome: str | None = None,
    est_min: int | None = None,
    energy: int | None = None,
    kind: str | None = None,
) -> Step:
    """手动修正计划：标题、阶段、完成标志、时长、精力、类型。

    计划是用户的工作文档，不只是 AI 一次性生成的结果；修改后同步未来目标块标题。
    """
    if title is not None:
        cleaned = title.strip()[:200]
        if cleaned:
            step.title = cleaned
    if milestone is not None:
        step.milestone = (milestone.strip() or "Custom")[:120]
    if outcome is not None:
        step.outcome = outcome.strip()[:500]
    if est_min is not None:
        step.est_min = max(5, min(int(est_min), 240))
    if energy is not None:
        step.energy = max(0, min(int(energy), 2))
    if kind is not None:
        step.kind = kind
    session.flush()

    if title is not None:
        session.execute(
            update(Event)
            .where(Event.step_id == step.id, Event.kind == "block")
            .values(title=f"{step.goal.title}: {step.title}"[:200])
        )
        session.flush()
    return step


def delete_step(session: Session, step: Step) -> None:
    """删除一个步骤，连同它的打卡记录和未来目标块。"""
    from . import calendar_service as cal

    cal.cancel_blocks(session, step.id)
    # 非 block 的关联事件保留，但解除外键，避免删步骤时变孤儿
    session.execute(update(Event).where(Event.step_id == step.id).values(step_id=None))
    session.execute(delete(Checkin).where(Checkin.step_id == step.id))
    session.delete(step)
    session.flush()


def replace_steps(
    session: Session, goal: Goal, step_defs: list[dict], template_key: str = "llm"
) -> None:
    """用一份新计划替换目标的全部步骤（用于 AI 重新拆解）。"""
    from . import calendar_service as cal

    for step in list(goal.steps):
        cal.cancel_blocks(session, step.id)
        session.execute(update(Event).where(Event.step_id == step.id).values(step_id=None))
        session.execute(delete(Checkin).where(Checkin.step_id == step.id))
        session.delete(step)
    session.flush()
    session.refresh(goal)

    for d in step_defs:
        session.add(Step(goal_id=goal.id, **d))
    goal.template_key = template_key
    goal.status = "active"
    session.flush()
    session.refresh(goal)


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
    """记录一次打卡。

    done     → 步骤完成，按预估时长计入进度。
    partial  → 只记录投入时间，步骤仍是 todo（不伪造 scheduled 状态）。
    not_done → 保留原状态，只留一条打卡记录，供 72h 恢复闭环使用。
    """
    if verdict == "done":
        logged = minutes if minutes is not None else step.est_min
        step.status = "done"
        step.completed_at = now_utc()
        step.minutes_spent = logged
    else:
        logged = minutes if minutes is not None else 0
        step.minutes_spent += logged
        if verdict == "partial":
            step.completed_at = None

    session.add(
        Checkin(
            step_id=step.id,
            raw_text=raw_text,
            verdict=verdict,
            minutes=logged,
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
        "milestones": list(dict.fromkeys(s.milestone for s in steps))
        or milestones(goal.template_key),
        "next_step": nxt,
        "steps": steps,
        "days_left": (goal.deadline - date.today()).days if goal.deadline else None,
        "template_key": goal.template_key or pick_template(goal.title),
    }
