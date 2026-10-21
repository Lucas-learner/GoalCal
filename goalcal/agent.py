"""Agent 编排层：解析 → 执行 → 回复。

LLM 只出现在 `parse()` 里；下面所有写库动作都是确定性代码。
这是报告里“LLM 解析意图，代码执行日历操作”的那张图。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from .domain import calendar_service as cal
from .domain import goals as goal_svc
from .domain.scheduler import Candidate, Preferences, Requirement, propose
from .llm.action import Action
from .llm.parser import parse
from .models import StateLog, Step
from .timeutil import day_bounds, now_utc

HELP = (
    "I can:\n"
    "• add / move / delete events — “Add a meeting Wednesday 10am”\n"
    "• track a goal — “I want to publish my first short video in 30 days”\n"
    "• report progress — “I finished the script today”\n"
    "• tell me your state — “I'm tired” / “busy” / “uninspired”\n"
    "• find time — “When am I free this week?”"
)

PARTIAL = re.compile(r"\b(half|partly|partially|some|started|almost)\b|一半|部分|开始", re.I)


@dataclass
class Reply:
    text: str
    action: Action
    parser: str
    candidates: list[Candidate] = field(default_factory=list)
    goal_id: int | None = None
    step_id: int | None = None


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
        "log_progress": _log_progress,
        "set_state": _set_state,
    }.get(action.intent)

    if handler is None:
        return Reply(HELP, action, parser)
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
    ev = cal.find_event(session, user_id, target)
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
    ev = cal.find_event(session, user_id, target)
    if ev is None:
        return Reply(f"I couldn't find “{target}”. Nothing was deleted.", action, parser)
    title = ev.title
    session.delete(ev)
    return Reply(f"Deleted “{title}”.", action, parser)


def _query(session, user_id, action, now, tz, parser) -> Reply:
    start_utc = now_utc() - timedelta(days=1)
    events = cal.events_between(session, user_id, start_utc, start_utc + timedelta(days=8))
    if not events:
        return Reply(
            "Your week is empty. Want to add something, or should I find time for a goal step?",
            action, parser,
        )
    lines = [f"• {cal.as_local(e.start_utc, tz):%a %b %d %H:%M} — {e.title}" for e in events[:12]]
    return Reply("Here's what's coming up:\n" + "\n".join(lines), action, parser)


def _pick_step(goal, prefer_low_energy: bool = False) -> Step | None:
    steps = sorted(goal.steps, key=lambda s: s.seq)
    pending = [s for s in steps if s.status in {"todo", "scheduled"}]
    if prefer_low_energy:
        for s in pending:
            if s.energy <= 1:
                return s
    return pending[0] if pending else None


def _create_goal(session, user_id, action, now, tz, parser) -> Reply:
    deadline = action.deadline or (now.date() + timedelta(days=30))
    goal = goal_svc.create_goal(session, user_id, action.title or "New goal", deadline=deadline)
    session.flush()

    steps = sorted(goal.steps, key=lambda s: s.seq)
    preview = "\n".join(
        f"  {s.seq}. {s.title} · {s.est_min}m" for s in steps[:4]
    )
    first = steps[0]
    candidates = _propose(session, user_id, first, now, tz, goal.deadline)

    body = (
        f"Goal set: **{goal.title}** — deadline {deadline:%b %d} "
        f"({(deadline - now.date()).days} days).\n"
        f"I split it into {len(steps)} steps across "
        f"{len({s.milestone for s in steps})} milestones:\n{preview}\n  …\n"
    )
    if candidates:
        body += "\nHere are 3 slots for the first step. Pick one and I'll put it on your calendar."
    return Reply(body, action, parser, candidates=candidates, goal_id=goal.id, step_id=first.id)


def _log_progress(session, user_id, action, now, tz, parser) -> Reply:
    goals = goal_svc.active_goals(session, user_id)
    if not goals:
        return Reply("You don't have an active goal yet. Tell me one and I'll break it down.", action, parser)
    goal = goals[0]
    step = _pick_step(goal)
    if step is None:
        return Reply(f"“{goal.title}” is already complete. 🎉", action, parser)

    partial = bool(PARTIAL.search(action.raw or ""))
    goal_svc.complete_step(
        session, step, verdict="partial" if partial else "done", raw_text=action.raw
    )
    pct = int(goal_svc.progress(goal) * 100)
    if partial:
        text = f"Logged partial progress on “{step.title}”. Goal is at {pct}%."
        candidates: list[Candidate] = []
    else:
        text = f"Nice — “{step.title}” done. Goal is now at **{pct}%**."
        nxt = _pick_step(goal)
        candidates = _propose(session, user_id, nxt, now, tz, goal.deadline) if nxt else []
        if nxt and candidates:
            text += f"\nNext: “{nxt.title}” ({nxt.est_min}m). Here are 3 slots."
    return Reply(
        text, action, parser, candidates=candidates, goal_id=goal.id,
        step_id=(_pick_step(goal).id if _pick_step(goal) else None),
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
    start_utc = now.replace(tzinfo=None) - timedelta(hours=1)
    end_utc = now.replace(tzinfo=None) + timedelta(days=days + 1)
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


def accept(
    session: Session, user_id: int, step_id: int, candidate: Candidate, tz: str
) -> str:
    step = session.get(Step, step_id)
    if step is None:
        return "That step no longer exists."
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
    step.status = "scheduled"
    session.flush()
    return f"Booked “{step.title}” on {candidate.start:%a %b %d, %H:%M}–{candidate.end:%H:%M}."
