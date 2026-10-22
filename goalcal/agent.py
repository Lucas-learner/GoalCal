"""Agent 编排层：解析 → 执行 → 回复。

LLM 只出现在 `parse()` 里；下面所有写库动作都是确定性代码。
这是报告里“LLM 解析意图，代码执行日历操作”的那张图。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .domain import calendar_service as cal
from .domain import goals as goal_svc
from .domain import planner
from .domain.scheduler import Candidate, Preferences, Requirement, propose
from .llm.action import Action
from .llm.parser import parse
from .llm.plan import generate_plan
from .llm.rules import NEW_COMMAND_INTENTS, parse_with_rules
from .models import Goal, StateLog, Step
from .timeutil import as_utc_naive

HELP = (
    "I can:\n"
    "• add / move / delete events — “Add a meeting Wednesday 10am”\n"
    "• track a goal — “I want to publish my first short video in 30 days”; "
    "complex skills get a quick level + timeline check\n"
    "• delete a goal — “delete “track my goal””\n"
    "• report progress — “I finished the script today”\n"
    "• tell me your state — “I'm tired” / “busy” / “uninspired”\n"
    "• find time for a goal step — “帮我安排下一步”\n"
    "• see your week — “When am I free this week?”"
)

PARTIAL = re.compile(r"\b(half|partly|partially|some|started|almost)\b|一半|部分|开始", re.I)

HELP_MARKERS = re.compile(
    r"\b(help|what can you do|how do i|commands?|what should i say)\b|帮助|怎么用|能做什么|你会什么",
    re.I,
)
CLARIFY = (
    "I'm not sure what you want me to change yet. Try:\n"
    "• “Add a meeting tomorrow 10am”\n"
    "• “I finished the script today”\n"
    "• “I'm tired”\n"
    "Or pick a goal step on the right and press **Find time**."
)

# 删除/改期这类不好撤销的操作，置信度不够就先追问，不直接执行
RISKY_INTENTS = {"delete_event", "delete_goal", "move_event"}
CONFIRM_THRESHOLD = 0.5


def _confirm_text(action: Action) -> str:
    target = action.target or action.title or "那个"
    if action.intent == "delete_goal":
        return f"你是想删除目标「{target}」吗？我没太听清，确认之后再动手。"
    if action.intent == "delete_event":
        return f"你是想删除日程「{target}」吗？我没太听清，确认之后再动手。"
    return f"你是想改期「{target}」吗？我没太听清新时间，确认之后再动手。"


@dataclass
class Reply:
    text: str
    action: Action
    parser: str
    candidates: list[Candidate] = field(default_factory=list)
    goal_id: int | None = None
    step_id: int | None = None
    goal_brief: planner.GoalBrief | None = None


def handle(
    session: Session,
    user_id: int,
    text: str,
    now: datetime,
    tz: str = "Asia/Shanghai",
) -> Reply:
    action, parser = parse(text, now)
    handler = {
        "add_event": _add_event,
        "move_event": _move_event,
        "delete_event": _delete_event,
        "query_events": _query,
        "create_goal": _create_goal,
        "delete_goal": _delete_goal,
        "schedule_step": _schedule_step,
        "log_progress": _log_progress,
        "set_state": _set_state,
    }.get(action.intent)

    if handler is None:
        text = HELP if HELP_MARKERS.search(action.raw or "") else CLARIFY
        return Reply(text, action, parser)
    if action.intent in RISKY_INTENTS and action.confidence < CONFIRM_THRESHOLD:
        return Reply(_confirm_text(action), action, parser)
    return handler(session, user_id, action, now, tz, parser)


# ------------------------------------------------------------------ handlers


def _add_event(session, user_id, action, now, tz, parser) -> Reply:
    if action.start is None:
        return Reply(
            f'When should I put “{action.title}”? Try “tomorrow 10am” or “Friday afternoon”.',
            action, parser,
        )
    ev = cal.create_event(
        session, user_id, action.title or "Untitled", action.start, action.end, tz=tz
    )
    return Reply(
        f"Added “{ev.title}” on {action.start:%a %b %d, %H:%M}–{action.end:%H:%M}."
        " Anything else?",
        action, parser,
    )


def _has_explicit_time(text: str) -> bool:
    return bool(re.search(r"\b\d{1,2}(:\d{2})?\s*(am|pm|h|o'clock)\b|\d{1,2}:\d{2}|上午|下午|晚上", text, re.I))


def _move_event(session, user_id, action, now, tz, parser) -> Reply:
    target = action.target or action.title or ""
    ev = cal.find_event(session, user_id, target, now_utc=as_utc_naive(now, tz))
    if ev is None:
        return Reply(
            f"I couldn't find “{target}” on your calendar. What's the exact event name?",
            action, parser,
        )
    if action.start is None:
        return Reply(f"When should I move “{ev.title}” to?", action, parser)

    new_start = action.start
    if not _has_explicit_time(action.raw):
        # 只说了“移到周五” → 保留原来的时间点
        local = cal.as_local(ev.start_utc, tz)
        new_start = new_start.replace(hour=local.hour, minute=local.minute)

    cal.move_event(session, ev, new_start, tz)
    return Reply(
        f"Moved “{ev.title}” to {new_start:%a %b %d, %H:%M}.", action, parser
    )


def _delete_event(session, user_id, action, now, tz, parser) -> Reply:
    target = action.target or action.title or ""
    ev = cal.find_event(session, user_id, target, now_utc=as_utc_naive(now, tz))
    if ev is None:
        return Reply(f"I couldn't find “{target}”. Nothing was deleted.", action, parser)
    title = ev.title
    cal.delete_event(session, ev)
    return Reply(f"Deleted “{title}”.", action, parser)


def _query(session, user_id, action, now, tz, parser) -> Reply:
    # 以用户当前（可被时间机器快进的）时刻为基准，而不是墙钟时间
    start_utc = as_utc_naive(now, tz) - timedelta(days=1)
    events = cal.events_between(session, user_id, start_utc, start_utc + timedelta(days=8))
    if not events:
        return Reply(
            "Your week is empty. Want to add something, or should I find time for a goal step?",
            action, parser,
        )
    lines = [f"• {cal.as_local(e.start_utc, tz):%a %b %d %H:%M} — {e.title}" for e in events[:12]]
    return Reply("Here's what's coming up:\n" + "\n".join(lines), action, parser)


def _pick_step(goal, prefer_low_energy: bool = False, target: str | None = None) -> Step | None:
    steps = sorted(goal.steps, key=lambda s: s.seq)
    pending = [s for s in steps if s.status == "todo"]
    if target:
        needle = target.lower().replace(" ", "")
        for s in pending:
            title = s.title.lower().replace(" ", "")
            if needle in title or title in needle:
                return s
    if prefer_low_energy:
        for s in pending:
            if s.energy <= 1:
                return s
    return pending[0] if pending else None


def looks_like_new_command(text: str, now: datetime) -> bool:
    """等待回答（目标澄清/签到）时，判断这句是不是明确的新指令。

    状态/进度类短回答（"太累了"、"done"）不算新指令，应按当前上下文解释。
    """
    action = parse_with_rules(text, now)
    return action.intent in NEW_COMMAND_INTENTS


def _create_goal(session, user_id, action, now, tz, parser) -> Reply:
    brief = planner.assess(
        action.title or "New goal",
        action.deadline,
        action.raw,
        today=now.date(),
    )
    if not planner.ready(brief):
        return Reply(
            planner.clarification_text(brief),
            action,
            parser,
            goal_brief=brief,
        )
    return _create_goal_now(session, user_id, brief, action, now, tz, parser)


def continue_goal(
    session: Session,
    user_id: int,
    brief: planner.GoalBrief,
    text: str,
    now: datetime,
    tz: str = "Asia/Shanghai",
) -> Reply:
    """处理目标澄清的下一句；只追问一轮，之后按默认值给出可调整的计划。"""
    action = Action(intent="create_goal", title=brief.title, raw=text, confidence=0.8)
    if planner.cancellation_requested(text):
        return Reply(
            "Okay — no goal created. You can start again anytime.",
            Action(intent="chat", raw=text, confidence=0.4),
            "goal:clarify",
        )
    if HELP_MARKERS.search(text):
        return Reply(HELP, action, "goal:clarify", goal_brief=brief)

    updated = planner.apply_answer(brief, text, today=now.date())
    if not planner.ready(updated):
        updated = planner.fill_defaults(updated, today=now.date())
    return _create_goal_now(session, user_id, updated, action, now, tz, "goal:clarify")


def create_goal_from_brief(
    session: Session,
    user_id: int,
    brief: planner.GoalBrief,
    now: datetime,
    tz: str = "Asia/Shanghai",
) -> tuple[Goal, str]:
    """LLM 生成专属步骤；失败才用模板。返回 (goal, source)。"""
    deadline = brief.deadline or (now.date() + timedelta(days=90))
    plan = generate_plan(
        brief.title,
        deadline,
        today=now.date(),
        level=brief.level,
        target=brief.target,
        complexity=brief.complexity,
        raw=brief.raw,
        tz=tz,
    )
    if plan:
        goal = goal_svc.create_goal(
            session,
            user_id,
            brief.title,
            deadline=deadline,
            step_defs=plan,
            template_key="llm",
        )
        return goal, "llm"
    goal = goal_svc.create_goal(
        session,
        user_id,
        brief.title,
        deadline=deadline,
        complexity=brief.complexity,
    )
    return goal, "template"


def _create_goal_now(session, user_id, brief, action, now, tz, parser) -> Reply:
    deadline = brief.deadline or (now.date() + timedelta(days=90))
    goal, source = create_goal_from_brief(session, user_id, brief, now, tz)
    session.flush()

    steps = sorted(goal.steps, key=lambda s: s.seq)
    preview = "\n".join(
        f"  {s.seq}. {s.title} · {s.est_min}m" for s in steps[:5]
    )
    first = steps[0]
    candidates = _propose(session, user_id, first, now, tz, goal.deadline)

    label = planner.complexity_label(brief.complexity)
    body = (
        f"Goal set: **{goal.title}** — deadline {deadline:%b %d} "
        f"({(deadline - now.date()).days} days).\n"
        f"I treated it as a **{label}** goal and split it into {len(steps)} steps "
        f"across {len({s.milestone for s in steps})} milestones:\n{preview}\n  …\n"
    )
    if source == "llm":
        body += "These steps were generated for this specific goal.\n"
    else:
        body += (
            "AI planning was unavailable, so I used the closest built-in plan; "
            "you can edit or delete any step.\n"
        )
    if brief.level or brief.target:
        body += (
            f"Plan assumptions: level **{brief.level or '未指定'}**, "
            f"target **{brief.target or '完成目标'}**.\n"
        )
    if brief.assumptions:
        body += "Assumptions: " + "；".join(brief.assumptions) + ".\n"
    if candidates:
        body += "\nHere are 3 slots for the first step. Pick one and I'll put it on your calendar."
    return Reply(
        body,
        action,
        parser,
        candidates=candidates,
        goal_id=goal.id,
        step_id=first.id,
    )


def propose_goal_plan(
    session: Session,
    goal: Goal,
    now: datetime,
    tz: str = "Asia/Shanghai",
    *,
    instruction: str = "",
) -> list[dict] | None:
    """为已有目标生成一份“待预览”的计划，不写库。"""
    brief = planner.assess(
        goal.title,
        goal.deadline,
        goal.title,
        today=now.date(),
    )
    brief = planner.fill_defaults(brief, today=now.date())
    deadline = brief.deadline or (now.date() + timedelta(days=90))
    current = [
        f"{step.seq}. {step.title} ({step.est_min}m, {step.status})"
        for step in sorted(goal.steps, key=lambda item: item.seq)
    ]
    return generate_plan(
        goal.title,
        deadline,
        today=now.date(),
        level=brief.level,
        target=brief.target,
        complexity=brief.complexity,
        raw=goal.title,
        tz=tz,
        instruction=instruction,
        current_steps=current,
    )


def regenerate_goal_plan(
    session: Session,
    goal: Goal,
    now: datetime,
    tz: str = "Asia/Shanghai",
    *,
    instruction: str = "",
) -> int | None:
    """给已有目标重新生成 AI 计划；失败返回 None，不改动原步骤。"""
    plan = propose_goal_plan(session, goal, now, tz, instruction=instruction)
    if not plan:
        return None
    goal_svc.replace_steps(session, goal, plan, template_key="llm")
    return len(plan)


def _delete_goal(session, user_id, action, now, tz, parser) -> Reply:
    target = action.target or action.title or ""
    goals = goal_svc.active_goals(session, user_id)
    if not goals:
        return Reply("You don't have an active goal to delete.", action, parser)
    goal = goal_svc.find_goal(session, user_id, target)
    if goal is None:
        if not target:
            names = "、".join(f"“{g.title}”" for g in goals[:3])
            return Reply(f"Which goal should I delete? You have {names}.", action, parser)
        return Reply(
            f"I couldn't find a goal named “{target}”. Nothing was deleted.",
            action, parser,
        )

    title = goal.title
    removed = goal_svc.archive_goal(session, goal)
    extra = f" Removed {removed} calendar block(s)." if removed else ""
    return Reply(f"Deleted goal “{title}”.{extra}", action, parser)


def _schedule_step(session, user_id, action, now, tz, parser) -> Reply:
    goals = goal_svc.active_goals(session, user_id)
    if not goals:
        return Reply(
            "You don't have an active goal yet. Tell me one and I'll break it down.",
            action, parser,
        )
    goal = goals[0]
    step = _pick_step(goal, target=action.progress_target or action.title)
    if step is None:
        return Reply(f"“{goal.title}” is already complete. 🎉", action, parser)

    candidates = _propose(session, user_id, step, now, tz, goal.deadline)
    if not candidates:
        return Reply(
            f"I couldn't find a free slot for “{step.title}” in the next 5 days. "
            "Try a shorter step or move the deadline.",
            action, parser,
        )
    return Reply(
        f"Here are 3 slots for “{step.title}” ({step.est_min}m). Pick one and I'll book it.",
        action, parser, candidates=candidates, goal_id=goal.id, step_id=step.id,
    )


def _log_progress(session, user_id, action, now, tz, parser) -> Reply:
    goals = goal_svc.active_goals(session, user_id)
    if not goals:
        return Reply("You don't have an active goal yet. Tell me one and I'll break it down.", action, parser)
    goal = goals[0]
    step = _pick_step(goal, target=action.progress_target or action.title)
    if step is None:
        return Reply(f"“{goal.title}” is already complete. 🎉", action, parser)

    partial = action.checkin_outcome == "partial" or bool(PARTIAL.search(action.raw or ""))
    goal_svc.complete_step(
        session, step, verdict="partial" if partial else "done", raw_text=action.raw,
        now=as_utc_naive(now, tz),
    )
    if not partial:
        # 步骤已完成，取消还挂在日历上的未来目标块，避免“做完了还留着块”
        cal.cancel_future_blocks(session, step.id, as_utc_naive(now, tz))
    pct = int(goal_svc.progress(goal) * 100)
    nxt = _pick_step(goal)
    if partial:
        text = f"Logged partial progress on “{step.title}”. Goal is at {pct}%."
        candidates: list[Candidate] = []
    else:
        text = f"Nice — “{step.title}” done. Goal is now at **{pct}%**."
        candidates = _propose(session, user_id, nxt, now, tz, goal.deadline) if nxt else []
        if nxt and candidates:
            text += f"\nNext: “{nxt.title}” ({nxt.est_min}m). Here are 3 slots."
    return Reply(
        text, action, parser, candidates=candidates, goal_id=goal.id,
        step_id=(nxt.id if nxt else None),
    )


def _set_state(session, user_id, action, now, tz, parser) -> Reply:
    energy = {"tired": "low", "busy": "low", "uninspired": "low", "fresh": "high"}.get(
        action.state or "fresh", "mid"
    )
    session.add(StateLog(user_id=user_id, label=action.state or "", energy=energy, note=action.raw))
    session.flush()

    if action.state == "busy":
        return Reply("Got it — I'll keep this week light and only suggest short blocks.", action, parser)

    goal = next(iter(goal_svc.active_goals(session, user_id)), None)
    if goal is None:
        return Reply("Noted. Tell me a goal and I'll keep it in mind when scheduling.", action, parser)

    step = _pick_step(goal, prefer_low_energy=action.state in {"tired", "uninspired"})
    if step is None:
        return Reply("Noted.", action, parser)
    candidates = _propose(session, user_id, step, now, tz, goal.deadline)
    note = {
        "tired": "Let's not force a deep block. I picked a lighter step:",
        "uninspired": "When you're stuck, starting is the hard part. A small step:",
        "fresh": "Good — let's use it. Next step:",
    }.get(action.state or "", "Next step:")
    return Reply(
        f"Noted you're {action.state}. {note} “{step.title}” ({step.est_min}m).",
        action, parser, candidates=candidates, goal_id=goal.id, step_id=step.id,
    )


# ------------------------------------------------------------------ proposals


def _propose(session, user_id, step: Step | None, now, tz, deadline=None, days: int = 5):
    if step is None:
        return []
    start_utc = as_utc_naive(now, tz) - timedelta(hours=1)
    end_utc = as_utc_naive(now, tz) + timedelta(days=days + 1)
    busy = cal.busy_blocks(session, user_id, start_utc, end_utc, tz)
    return propose(
        Requirement(est_min=step.est_min, energy=step.energy, kind=step.kind),
        now=now,
        busy=busy,
        prefs=Preferences(),
        days=days,
        top_n=3,
        deadline=deadline,
    )


def propose_for_step(
    session: Session, user_id: int, step_id: int, now: datetime, tz: str, days: int = 5
) -> tuple[list[Candidate], Step | None]:
    """给 UI 的“找时间”按钮用：为指定步骤生成候选。"""
    step = session.get(Step, step_id)
    if step is None or step.status == "done":
        return [], step
    return _propose(session, user_id, step, now, tz, step.goal.deadline, days), step


def accept(
    session: Session, user_id: int, step_id: int, candidate: Candidate, tz: str
) -> str:
    step = session.get(Step, step_id)
    if step is None:
        return "That step no longer exists."
    if step.status == "done":
        return f"“{step.title}” is already done — nothing to book."
    # 一个步骤只保留一个目标块：重新选时段 = 改期，而不是叠加
    cal.cancel_blocks(session, step.id)
    ev = cal.create_event(
        session,
        user_id,
        f"{step.goal.title}: {step.title}",
        candidate.start,
        candidate.end,
        kind="block",
        source="proposal",
        step_id=step.id,
        tz=tz,
    )
    session.flush()
    return f"Booked “{step.title}” on {candidate.start:%a %b %d, %H:%M}–{candidate.end:%H:%M}."
