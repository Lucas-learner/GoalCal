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
from .domain import recovery
from .domain.scheduler import Candidate, Preferences, Requirement, propose
from .llm.action import Action
from .llm.parser import parse
from .llm.plan import generate_plan
from .llm.rules import NEW_COMMAND_INTENTS, parse_with_rules
from .models import Event, Goal, RescheduleLog, StateLog, Step
from .timeutil import as_local, as_utc_naive

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
    checkin_context: "CheckinContext | None" = None


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


# ------------------------------------------------------------------ check-in


@dataclass
class CheckinContext:
    """签到流程的对话状态：event_ids 是待处理队列，第一个是当前在问的块。

    stage: batch（合并提问）→ outcome（问结果）→ reason（问原因）→ recovery（给候选时段）
    """

    stage: str
    event_ids: list[int] = field(default_factory=list)
    merged: bool = False
    outcome: str | None = None
    reason: str | None = None
    step_id: int | None = None
    goal_id: int | None = None


_REASON_FROM_STATE = {"tired": "tired", "busy": "no_time", "uninspired": "stuck"}
_UNWILLING = re.compile(r"不想做|不想继续|没动力|坚持不下去")
_DECLINE = re.compile(r"算了|不用|先不|不要|不需要|no thanks|never ?mind", re.I)
_BATCH_NO = re.compile(r"不|算了|^no\b", re.I)
_BATCH_YES = re.compile(r"重新安排|重排|^好|好的|嗯|可以|来吧|要", re.I)


def _friendly_when(start_local: datetime, now: datetime) -> str:
    days = (now.date() - start_local.date()).days
    day = {0: "今天", 1: "昨天", 2: "前天"}.get(days) or f"{start_local.month}月{start_local.day}日"
    return f"{day} {start_local:%H:%M}"


def _block_title(session: Session, event: Event) -> str:
    if event.step_id is not None:
        step = session.get(Step, event.step_id)
        if step is not None:
            return step.title
    return event.title


def _checkin_question(session: Session, event: Event, now: datetime, tz: str) -> str:
    start = as_local(event.start_utc, tz)
    return f"{_friendly_when(start, now)} 的「{_block_title(session, event)}」怎么样了？"


def _reason_from(action: Action, text: str) -> str | None:
    if _UNWILLING.search(text):
        return "unwilling"
    if action.state in _REASON_FROM_STATE:
        return _REASON_FROM_STATE[action.state]
    return None


def begin_checkin(
    session: Session, user_id: int, now: datetime, tz: str = "Asia/Shanghai"
) -> tuple[CheckinContext | None, str]:
    """有过期块就开启签到流程，返回 (上下文, 主动提问)；没有则 (None, "")。"""
    blocks = recovery.unresolved_blocks(session, user_id, as_utc_naive(now, tz))
    if not blocks:
        return None, ""
    ids = [b.id for b in blocks]
    if len(blocks) > recovery.MERGE_THRESHOLD:
        ctx = CheckinContext(stage="batch", event_ids=ids, merged=True)
        return ctx, f"你有 {len(blocks)} 个块错过了，要我一起重新安排吗？"
    ctx = CheckinContext(stage="outcome", event_ids=ids)
    return ctx, _checkin_question(session, blocks[0], now, tz)


def checkin_quick_options(ctx: CheckinContext) -> list[str]:
    if ctx.stage == "batch":
        return ["一起重新安排", "先不安排了"]
    if ctx.stage == "outcome":
        return ["做完了", "做了一部分", "没做"]
    if ctx.stage == "reason":
        return ["太累了", "没时间", "卡住了", "不想做了"]
    return []


def checkin_prompt(session: Session, ctx: CheckinContext, now: datetime, tz: str) -> str:
    """当前阶段该问的话，用于新指令打断后把签到问题重新提回来。"""
    ev = session.get(Event, ctx.event_ids[0]) if ctx.event_ids else None
    if ctx.stage == "batch":
        return f"你有 {len(ctx.event_ids)} 个块错过了，要我一起重新安排吗？"
    if ctx.stage == "reason":
        return "接着刚才的：那个块是什么原因呢？"
    if ctx.stage == "recovery":
        return "刚才那几个时间有合适的吗？不想安排说「算了」就行。"
    if ev is not None:
        return _checkin_question(session, ev, now, tz)
    return ""


def suggest_recovery(
    session: Session,
    user_id: int,
    event: Event,
    reason: str | None,
    now: datetime,
    tz: str = "Asia/Shanghai",
    days: int = 3,
) -> tuple[list[Candidate], Step | None]:
    """按原因生成恢复方案：阶段 1 是通用实现（72 小时内找候选时段）。

    阶段 2 在这里按 tired / no_time / stuck / unwilling 分四种动作。
    """
    step = session.get(Step, event.step_id) if event.step_id is not None else None
    if step is None or step.status != "todo":
        return [], step
    return _propose(session, user_id, step, now, tz, step.goal.deadline, days=days), step


def _prepare_current(
    session: Session, user_id: int, ctx: CheckinContext, now: datetime, tz: str
) -> Reply | None:
    """为队列第一个块准备该问的话/该给的候选；队列空返回 None。"""
    chat_action = Action(intent="chat", raw="", confidence=0.5)
    while ctx.event_ids:
        ev = session.get(Event, ctx.event_ids[0])
        if ev is None:
            ctx.event_ids.pop(0)
            continue
        if not ctx.merged:
            ctx.stage, ctx.outcome, ctx.reason = "outcome", None, None
            ctx.step_id = ev.step_id
            return Reply(
                _checkin_question(session, ev, now, tz), chat_action, "checkin",
                checkin_context=ctx,
            )
        candidates, step = suggest_recovery(session, user_id, ev, ctx.reason, now, tz)
        if not candidates:
            ctx.event_ids.pop(0)
            continue
        ctx.stage = "recovery"
        ctx.step_id = step.id
        ctx.goal_id = step.goal_id
        return Reply(
            f"再给「{step.title}」找个新时间：",
            Action(intent="schedule_step", raw="", confidence=0.6), "checkin",
            candidates=candidates, goal_id=step.goal_id, step_id=step.id,
            checkin_context=ctx,
        )
    return None


def _advance(
    session: Session, user_id: int, ctx: CheckinContext, now: datetime, tz: str
) -> Reply | None:
    """当前块处理完，进入队列里的下一个块。"""
    if ctx.event_ids:
        ctx.event_ids.pop(0)
    return _prepare_current(session, user_id, ctx, now, tz)


def _offer_recovery(
    session: Session,
    user_id: int,
    ctx: CheckinContext,
    ev: Event,
    now: datetime,
    tz: str,
    parser: str,
    intro: str = "",
) -> Reply:
    candidates, step = suggest_recovery(session, user_id, ev, ctx.reason, now, tz)
    if candidates:
        ctx.stage = "recovery"
        ctx.step_id = step.id
        ctx.goal_id = step.goal_id
        return Reply(
            intro + "我在未来 3 天给你留了几个时间，挑一个重新开始？",
            Action(intent="schedule_step", raw="", confidence=0.6), parser,
            candidates=candidates, goal_id=step.goal_id, step_id=step.id,
            checkin_context=ctx,
        )
    text = intro + "未来 3 天实在没有空档，步骤先留在清单里；想安排的时候跟我说「安排下一步」。"
    nxt = _advance(session, user_id, ctx, now, tz)
    if nxt is None:
        return Reply(text, Action(intent="chat", raw="", confidence=0.5), parser,
                     checkin_context=None)
    nxt.text = text + "\n" + nxt.text
    return nxt


def continue_checkin(
    session: Session,
    user_id: int,
    ctx: CheckinContext,
    text: str,
    now: datetime,
    tz: str = "Asia/Shanghai",
) -> Reply | None:
    """处理签到流程里的下一句；返回 None 表示这是明确的新指令，交给 handle 并保留 ctx。"""
    if looks_like_new_command(text, now):
        return None
    action, parser = parse(text, now, context={"expecting": "checkin"})
    handler = {
        "batch": _checkin_batch,
        "outcome": _checkin_outcome,
        "reason": _checkin_reason,
        "recovery": _checkin_recovery,
    }[ctx.stage]
    return handler(session, user_id, ctx, action, text.strip(), now, tz, parser)


def _checkin_batch(session, user_id, ctx, action, text, now, tz, parser) -> Reply:
    now_utc = as_utc_naive(now, tz)

    def resolve_all(outcome: str, reason: str | None) -> None:
        for event_id in ctx.event_ids:
            ev = session.get(Event, event_id)
            if ev is None or ev.status != "planned":
                continue
            recovery.resolve_block(session, ev, outcome, now_utc)
            if reason is not None:
                recovery.attach_reason(session, ev.id, reason)
        ctx.reason = reason

    if _BATCH_NO.search(text):
        resolve_all("missed", None)
        return Reply(
            "好，都先记为错过了。想重新安排的时候随时跟我说。",
            action, parser, checkin_context=None,
        )
    outcome = action.checkin_outcome
    if outcome is not None or _BATCH_YES.search(text):
        outcome = "missed" if outcome in (None, "none") else outcome
        resolve_all(outcome, _reason_from(action, text))
        label = {"done": "完成", "partial": "部分完成", "missed": "错过"}[outcome]
        nxt = _prepare_current(session, user_id, ctx, now, tz)
        if nxt is None:
            return Reply(f"都记为{label}了。", action, parser, checkin_context=None)
        nxt.text = f"好，都记为{label}了。我们一块一块来：\n" + nxt.text
        return nxt
    return Reply(
        "要我一起重新安排吗？说「重新安排」或者「先不了」就行。",
        action, parser, checkin_context=ctx,
    )


def _checkin_outcome(session, user_id, ctx, action, text, now, tz, parser) -> Reply:
    ev = session.get(Event, ctx.event_ids[0]) if ctx.event_ids else None
    if ev is None:
        nxt = _advance(session, user_id, ctx, now, tz)
        return nxt or Reply("好，都处理完了。", action, parser, checkin_context=None)

    outcome = action.checkin_outcome
    reason = _reason_from(action, text)
    if outcome is None:
        if action.intent == "set_state":
            # "太累了" 这种回答：按没做 + 原因理解
            outcome = "missed"
        else:
            return Reply(
                f"没太听懂——{_checkin_question(session, ev, now, tz)}",
                action, parser, checkin_context=ctx,
            )
    if outcome == "none":
        outcome = "missed"

    now_utc = as_utc_naive(now, tz)
    recovery.resolve_block(session, ev, outcome, now_utc)
    ctx.outcome = outcome
    ctx.step_id = ev.step_id

    if outcome == "done":
        step = session.get(Step, ev.step_id) if ev.step_id is not None else None
        if ev.step_id is not None:
            cal.cancel_future_blocks(session, ev.step_id, now_utc)
        goal = step.goal if step is not None else None
        pct = int(goal_svc.progress(goal) * 100) if goal is not None else 0
        text_out = f"太好了，「{_block_title(session, ev)}」记为完成！目标进度 {pct}%。"
        cands: list[Candidate] = []
        nxt_step = goal_svc.next_step(goal) if goal is not None and goal.status == "active" else None
        if nxt_step is not None:
            cands = _propose(session, user_id, nxt_step, now, tz, goal.deadline)
            if cands:
                text_out += f"\n下一步是「{nxt_step.title}」（{nxt_step.est_min} 分钟），这几个时间合适吗？"
        nxt = _advance(session, user_id, ctx, now, tz)
        if nxt is None:
            return Reply(
                text_out, action, parser, candidates=cands,
                goal_id=goal.id if goal is not None else None,
                step_id=nxt_step.id if nxt_step is not None else None,
                checkin_context=None,
            )
        nxt.text = text_out + "\n\n" + nxt.text
        if cands and not nxt.candidates:
            nxt.candidates = cands
            nxt.goal_id = goal.id
            nxt.step_id = nxt_step.id
        return nxt

    if reason is not None:
        ctx.reason = reason
        recovery.attach_reason(session, ev.id, reason)
        return _offer_recovery(session, user_id, ctx, ev, now, tz, parser)
    ctx.stage = "reason"
    gentle = "做了一部分也是进展。" if outcome == "partial" else "没关系，这很正常。"
    return Reply(f"{gentle}是什么原因呢？", action, parser, checkin_context=ctx)


def _checkin_reason(session, user_id, ctx, action, text, now, tz, parser) -> Reply:
    ev = session.get(Event, ctx.event_ids[0]) if ctx.event_ids else None
    if ev is None:
        nxt = _advance(session, user_id, ctx, now, tz)
        return nxt or Reply("好，都处理完了。", action, parser, checkin_context=None)
    reason = _reason_from(action, text)
    if reason is None:
        # 不想说或没听懂：不追问到底，按通用恢复处理
        return _offer_recovery(session, user_id, ctx, ev, now, tz, parser,
                               intro="好，那就不纠结原因了。")
    ctx.reason = reason
    recovery.attach_reason(session, ev.id, reason)
    return _offer_recovery(session, user_id, ctx, ev, now, tz, parser)


def _checkin_recovery(session, user_id, ctx, action, text, now, tz, parser) -> Reply:
    if _DECLINE.search(text):
        return decline_recovery(session, user_id, ctx, now, tz)
    return Reply(
        "在上面几个时间里挑一个就行；不想安排的话，说「算了」也可以。",
        action, parser, checkin_context=ctx,
    )


def accept_recovery(
    session: Session,
    user_id: int,
    ctx: CheckinContext,
    candidate: Candidate,
    now: datetime,
    tz: str = "Asia/Shanghai",
) -> Reply:
    """用户选定恢复候选：创建新块并写 RescheduleLog（旧块保留，供恢复率统计）。"""
    ev = session.get(Event, ctx.event_ids[0]) if ctx.event_ids else None
    step = session.get(Step, ctx.step_id) if ctx.step_id is not None else None
    if ev is None or step is None:
        return Reply("那个块已经不在了。", Action(intent="chat", raw="", confidence=0.5),
                     "checkin", checkin_context=None)
    new_ev = cal.create_event(
        session, user_id, f"{step.goal.title}: {step.title}",
        candidate.start, candidate.end,
        kind="block", source="proposal", step_id=step.id, tz=tz,
    )
    session.add(
        RescheduleLog(
            event_id=ev.id,
            new_event_id=new_ev.id,
            ts=as_utc_naive(now, tz),
            reason=ctx.reason or "unknown",
            moved_to_utc=new_ev.start_utc,
            auto=False,
        )
    )
    session.flush()
    text = f"好，「{step.title}」改到 {candidate.start:%a %b %d %H:%M}。"
    nxt = _advance(session, user_id, ctx, now, tz)
    if nxt is None:
        return Reply(text + " 慢慢来，一次一个块就好。",
                     Action(intent="chat", raw="", confidence=0.5), "checkin",
                     checkin_context=None)
    nxt.text = text + "\n" + nxt.text
    return nxt


def decline_recovery(
    session: Session,
    user_id: int,
    ctx: CheckinContext,
    now: datetime,
    tz: str = "Asia/Shanghai",
) -> Reply:
    """用户拒绝恢复候选：步骤保持 todo，进入下一个块。"""
    nxt = _advance(session, user_id, ctx, now, tz)
    text = "好，这个块先不安排了，步骤还留在清单里。"
    if nxt is None:
        return Reply(text, Action(intent="chat", raw="", confidence=0.5), "checkin",
                     checkin_context=None)
    nxt.text = text + "\n" + nxt.text
    return nxt
