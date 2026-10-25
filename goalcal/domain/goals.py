from __future__ import annotations

import difflib
import json
from datetime import date, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..data.goal_templates import first_batch, full_steps, milestones, pick_template
from ..models import Checkin, Event, Goal, Step, User, Interaction
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
    outline: list[str] | None = None,
    now: datetime | None = None,
) -> Goal:
    """建目标（滚动拆解：阶段大纲 + 第一批具体步骤）。

    step_defs 不为空时使用调用方给的步骤（例如 LLM 拆解的首批步骤），
    outline 传对应的阶段大纲；否则按模板取大纲，第一批只落前 2–3 步，
    后续步骤由 refine.refine_upcoming 在做完前补出来。with_template=False 建空目标。
    """
    today = now.date() if now is not None else date.today()
    deadline = deadline or (today + timedelta(days=30))
    if step_defs is not None:
        key = template_key or "llm"
    elif with_template:
        key = pick_template(title)
        outline = milestones(key)
        step_defs = first_batch(full_steps(key))
    else:
        key, step_defs = "custom", []

    goal = Goal(
        user_id=user_id,
        title=title.strip()[:200],
        outcome=outcome or title.strip(),
        deadline=deadline,
        template_key=key,
        outline=json.dumps(outline, ensure_ascii=False) if outline else "",
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


def extend_deadline(session: Session, goal: Goal, days: int, today: date | None = None) -> date:
    """截止日往后延，返回新截止日。"""
    base = goal.deadline or (today or date.today())
    goal.deadline = base + timedelta(days=days)
    session.flush()
    return goal.deadline


def pause_goal(session: Session, goal: Goal, now: datetime | None = None) -> int:
    """暂停目标：不再参与排期和过期块提醒；未来目标块撤掉，恢复时重新排。"""
    from . import calendar_service as cal

    reference = now or now_utc()
    removed = sum(cal.cancel_future_blocks(session, step.id, reference) for step in goal.steps)
    goal.status = "paused"
    session.flush()
    return removed


def resume_goal(session: Session, goal: Goal) -> None:
    goal.status = "active"
    session.flush()


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
    session.execute(update(Interaction).where(Interaction.step_id == step.id).values(step_id=None))
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
        session.execute(update(Interaction).where(Interaction.step_id == step.id).values(step_id=None))
        session.delete(step)
    session.flush()
    session.refresh(goal)

    for d in step_defs:
        session.add(Step(goal_id=goal.id, **d))
    goal.template_key = template_key
    goal.status = "active"
    session.flush()
    session.refresh(goal)


def outline_phases(goal: Goal) -> list[str]:
    """读 Goal.outline（JSON 数组）；没有或解析失败返回 []。"""
    if not goal.outline:
        return []
    try:
        parsed = json.loads(goal.outline)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(p) for p in parsed if str(p).strip()]


def phases(goal: Goal) -> list[str]:
    """阶段顺序：outline 优先；旧数据没有 outline 时按步骤 milestone 顺序推导。"""
    names = outline_phases(goal)
    if names:
        return names
    derived = list(
        dict.fromkeys(
            s.milestone for s in sorted(goal.steps, key=lambda x: x.seq) if s.milestone
        )
    )
    return derived or milestones(goal.template_key or pick_template(goal.title))


def phase_status(goal: Goal) -> list[dict]:
    """每个阶段的步骤统计与状态：done（无待办）/ current（最早有待办）/ todo。"""
    steps = sorted(goal.steps, key=lambda x: x.seq)
    out = []
    current_assigned = False
    for name in phases(goal):
        pst = [s for s in steps if s.milestone == name]
        done = sum(1 for s in pst if s.status == "done")
        todo = sum(1 for s in pst if s.status == "todo")
        if todo and not current_assigned:
            state, current_assigned = "current", True
        elif todo:
            state = "todo"
        elif pst:
            state = "done"
        else:
            state = "todo"
        out.append({"name": name, "done": done, "todo": todo, "state": state})
    return out


def progress(goal: Goal) -> float:
    """按阶段：已完成阶段数 + 当前阶段完成比例，除以总阶段数。

    滚动拆解下总步骤数未知（后续阶段还没拆），不能再按预估时长加权。
    没有完成步骤的阶段记 0；dropped 步骤不计入。
    """
    if goal.status == "done":
        return 1.0
    stats = phase_status(goal)
    if not stats:
        return 0.0
    done_phases = sum(1 for p in stats if p["state"] == "done")
    fraction = 0.0
    for p in stats:
        if p["state"] == "current":
            total = p["done"] + p["todo"]
            fraction = p["done"] / total if total else 0.0
            break
    return round((done_phases + fraction) / len(stats), 6)


def next_step(goal: Goal) -> Step | None:
    for s in sorted(goal.steps, key=lambda x: x.seq):
        if s.status == "todo":
            return s
    return None


def step_display_status(step: Step, has_future_block: bool = False) -> str:
    """展示用状态：scheduled 不落库，由"是否还有未来目标块"推导。"""
    if step.status == "todo" and has_future_block:
        return "scheduled"
    return step.status


def complete_step(
    session: Session,
    step: Step,
    minutes: int | None = None,
    verdict: str = "done",
    raw_text: str = "",
    now: datetime | None = None,
) -> Checkin:
    """记录一次打卡，返回写入的 Checkin。now 为 UTC naive，为 None 时取真实当前时间。

    done     → 步骤完成，按预估时长计入进度。
    partial  → 只记录投入时间，步骤仍是 todo（不伪造 scheduled 状态）。
    not_done → 保留原状态，只留一条打卡记录，供 72h 恢复闭环使用。
    """
    if verdict == "done":
        logged = minutes if minutes is not None else step.est_min
        step.status = "done"
        step.completed_at = now or now_utc()
        step.minutes_spent = logged
    else:
        logged = minutes if minutes is not None else 0
        step.minutes_spent += logged
        if verdict == "partial":
            step.completed_at = None

    checkin = Checkin(
        step_id=step.id,
        raw_text=raw_text,
        verdict=verdict,
        minutes=logged,
    )
    session.add(checkin)
    session.flush()
    if _all_phases_done(step.goal):
        step.goal.status = "done"
        session.flush()
    return checkin


def _all_phases_done(goal: Goal) -> bool:
    """全部步骤完成、且大纲里的每个阶段都已有步骤落库，目标才算完成。

    滚动拆解下前一批步骤做完时后续阶段还没拆出来，不能提前判完成。
    """
    steps = goal.steps
    if not steps or any(s.status == "todo" for s in steps):
        return False
    outline = outline_phases(goal)
    if not outline:
        return True
    instantiated = {s.milestone for s in steps}
    return all(name in instantiated for name in outline)


def snapshot(goal: Goal, now: datetime | None = None) -> dict:
    steps = sorted(goal.steps, key=lambda x: x.seq)
    done = [s for s in steps if s.status == "done"]
    nxt = next_step(goal)
    today = now.date() if now is not None else date.today()
    phase_stats = phase_status(goal)
    current = next((p["name"] for p in phase_stats if p["state"] == "current"), None)
    return {
        "id": goal.id,
        "title": goal.title,
        "deadline": goal.deadline,
        "progress": progress(goal),
        "done": len(done),
        "total": len(steps),
        "milestones": [p["name"] for p in phase_stats],
        "phases": phase_stats,
        "current_phase": current,
        "next_step": nxt,
        "steps": steps,
        "days_left": (goal.deadline - today).days if goal.deadline else None,
        "template_key": goal.template_key or pick_template(goal.title),
    }
