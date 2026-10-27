"""Agent 编排层：解析 → 执行 → 回复。

LLM 出现在两处：`parse()` 里解析意图，`llm.respond` 里回复普通闲聊；
所有写库动作和写操作的结果文案都是确定性代码。
"""

from __future__ import annotations

import difflib
import re
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import memory, metrics
from .data.goal_templates import first_batch, full_steps, milestones, pick_template
from .domain import calendar_service as cal
from .domain import goals as goal_svc
from .domain import planner
from .domain import recovery
from .domain import refine
from .domain.scheduler import (
    Candidate,
    Requirement,
    _ceil_to,
    free_intervals,
    propose,
)
from .llm import event_plan, respond
from .llm.action import Action
from .llm.natdate import has_explicit_clock
from .llm.parser import parse
from .llm.plan import generate_outline_plan, generate_plan
from .llm.rules import CALENDAR_WRITE_MARKERS, NEW_COMMAND_INTENTS, parse_with_rules
from .models import Event, Goal, RescheduleLog, StateLog, Step
from .timeutil import as_local, as_utc_naive

HELP = (
    "I can add, move or delete events (“Add a meeting Wednesday 10am”), "
    "break a goal into steps and schedule them (“I want to publish my first short video in 30 days”), "
    "log progress (“I finished the script today”), "
    "and adapt to your state (“I'm tired”)."
)

PARTIAL = re.compile(r"\b(half|partly|partially|some|started|almost)\b|一半|部分|开始", re.I)

HELP_MARKERS = re.compile(
    r"\b(help|what can you do|how do i|commands?|what should i say)\b|帮助|怎么用|能做什么|你会什么",
    re.I,
)
CLARIFY = (
    "I'm not sure what you want to change yet. Try:\n"
    "• “Add a meeting tomorrow 10am”\n"
    "• “I finished the script today”\n"
    "• “Find time for my next step”"
)

# clarify 第三轮仍不明：停止追问，明确"我先不猜"，不循环
CLARIFY_STOP = (
    "I'm not following, so I'll stop guessing rather than keep asking. "
    "Whenever you're ready, tell me in a few words what you want — "
    "e.g. “Add a meeting tomorrow 10am” or “Find time for my next step”."
)


def clarify_options(session: Session, user_id: int) -> str:
    """clarify 第二轮：确定性的 2-3 个可执行选项；有进行中的目标时带上具体下一步。"""
    options = ["“Add a meeting tomorrow 10am” — put something on your calendar"]
    for goal in goal_svc.prioritized_goals(session, user_id):
        step = goal_svc.next_step(goal)
        if step is not None:
            options.append(
                f"“Find time for my next step” — schedule “{step.title}” for {goal.title}"
            )
            break
    options.append("“I finished the script today” — log progress on a step")
    return "Here's what I can do — pick one, or describe your own:\n" + "\n".join(
        f"• {option}" for option in options
    )

# 删除/改期这类不好撤销的操作，置信度不够就先追问，不直接执行
RISKY_INTENTS = {"delete_event", "delete_goal", "move_event"}
CONFIRM_THRESHOLD = 0.5

# 这些 handler 内部有 LLM 入口（event_plan 提取 / plan 拆解 / refine 细化），
# execute 会把本轮的 use_llm 预算传下去；False 时整轮走确定性路径。
_USE_LLM_HANDLERS = {"add_event", "create_goal", "refine_steps", "log_progress", "schedule_step"}


def _confirm_text(action: Action) -> str:
    target = action.target or action.title or "that"
    if action.intent == "delete_goal":
        return f'Did you mean to delete the goal "{target}"? I wasn\'t sure — please confirm before I touch anything.'
    if action.intent == "delete_event":
        return f'Did you mean to delete "{target}" from your calendar? I wasn\'t sure — please confirm first.'
    return f'Did you mean to move "{target}"? I didn\'t catch the new time — please confirm it.'


@dataclass
class PendingAction:
    action: Action
    parser: str


@dataclass
class GoalDraft:
    brief: planner.GoalBrief
    deadline: date
    outline: list[str]
    step_defs: list[dict]
    source: str
    draft_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class EventDraft:
    """待确认的日历预览：结构化 add_event Action 列表，确认前不写库。"""

    events: list[Action]
    draft_id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class Reply:
    text: str
    action: Action
    parser: str
    candidates: list[Candidate] = field(default_factory=list)
    batch: list[dict] | None = None
    goal_id: int | None = None
    step_id: int | None = None
    goal_brief: planner.GoalBrief | None = None
    goal_draft: GoalDraft | None = None
    event_draft: EventDraft | None = None
    checkin_context: "CheckinContext | None" = None
    pending_action: PendingAction | None = None


def handle(
    session: Session,
    user_id: int,
    text: str,
    now: datetime,
    tz: str = "Asia/Shanghai",
    *,
    use_llm: bool | None = None,
) -> Reply:
    action, parser = parse(text, now, use_llm=use_llm)
    # use_llm=False 时整轮不再调任何 LLM（解析与规划都走确定性路径）
    return execute(session, user_id, action, parser, now, tz, use_llm=use_llm is not False)


def execute(
    session: Session,
    user_id: int,
    action: Action,
    parser: str,
    now: datetime,
    tz: str = "Asia/Shanghai",
    *,
    chat_text: str | None = None,
    use_llm: bool = True,
) -> Reply:
    """执行一个已解析的 Action：白名单校验后的唯一执行入口。

    chat_text 是 L1 轮引擎已经生成的闲聊回复：给了就直接用，
    本轮不再调 respond.chat_reply（每轮最多一次模型调用）。
    """
    handler = {
        "add_event": _add_event,
        "move_event": _move_event,
        "delete_event": _delete_event,
        "query_events": _query,
        "create_goal": _create_goal,
        "delete_goal": _delete_goal,
        "resume_goal": _resume_goal,
        "schedule_step": _schedule_step,
        "schedule_all": _schedule_all,
        "refine_steps": _refine_steps,
        "log_progress": _log_progress,
        "set_state": _set_state,
    }.get(action.intent)
    if handler is None:
        if HELP_MARKERS.search(action.raw or ""):
            return Reply(HELP, action, parser)
        # 普通闲聊：先理解再建议（LLM），失败回退到简短的上下文相关话术
        if chat_text is not None:
            text = chat_text or respond.fallback(session, user_id, action.raw or "") or CLARIFY
        else:
            text = (
                (respond.chat_reply(session, user_id, action.raw or "", now, tz) if use_llm else None)
                or respond.fallback(session, user_id, action.raw or "")
                or CLARIFY
            )
        return Reply(text, action, parser)
    if action.intent in RISKY_INTENTS and action.confidence < CONFIRM_THRESHOLD:
        return Reply(
            _confirm_text(action), action, parser,
            pending_action=PendingAction(action=action, parser=parser),
        )
    if action.intent in _USE_LLM_HANDLERS:
        reply = handler(session, user_id, action, now, tz, parser, use_llm=use_llm)
    else:
        reply = handler(session, user_id, action, now, tz, parser)
    metrics.record(
        session, user_id, metrics.PARSE, now,
        payload={"parser": parser, "intent": action.intent},
    )
    return reply


def confirm_action(
    session: Session,
    user_id: int,
    pending_action: PendingAction,
    approved: bool,
    now: datetime,
    tz: str = "Asia/Shanghai",
) -> Reply:
    action = pending_action.action
    parser = pending_action.parser
    if not approved:
        return Reply("Okay — nothing was changed.", action, parser)
    handler = {
        "move_event": _move_event,
        "delete_event": _delete_event,
        "delete_goal": _delete_goal,
    }.get(action.intent)
    if handler is None:
        return Reply("That action is no longer available. Nothing was changed.", action, parser)
    reply = handler(session, user_id, action, now, tz, parser)
    metrics.record(
        session, user_id, metrics.PARSE, now,
        payload={"parser": parser, "intent": action.intent, "confirmed": True},
    )
    return reply


# ------------------------------------------------------------------ handlers


def _event_keys(session, user_id: int) -> set[tuple[str, datetime]]:
    """已有日程的 (标题小写, 开始时刻 UTC) 集合，用于预览与确认时去重。"""
    return {
        (ev.title.casefold(), ev.start_utc)
        for ev in session.scalars(select(Event).where(Event.user_id == user_id))
    }


def _add_event(session, user_id, action, now, tz, parser, *, use_llm: bool = True) -> Reply:
    chinese = bool(re.search(r"[\u4e00-\u9fff]", action.raw or ""))
    if parser.startswith("rules:") and not has_explicit_clock(action.raw):
        # The rules benchmark still recognizes broad periods as event intents,
        # but execution must not silently turn "tonight" into 09:00.
        action.start = action.end = None
    if action.start is not None and action.start <= now:
        return Reply(
            ("这个时间已经过去了。告诉我一个晚一点的时间，我再写进日历。" if chinese else
             "That time has already passed. Give me a later time and I'll add it."),
            action, parser,
        )
    if action.start is None:
        # 明确写日历但当前消息没时间：从上文提取助手刚提议的时间块
        # use_llm=False（L1 失败后的降级轮）跳过提取，直接追问时间
        if use_llm and CALENDAR_WRITE_MARKERS.search(action.raw or ""):
            actions = event_plan.extract_events(session, user_id, action.raw or "", now, tz)
            if actions:
                existing = _event_keys(session, user_id)
                fresh = [
                    a for a in actions
                    if ((a.title or "").casefold(), as_utc_naive(a.start, tz)) not in existing
                ]
                if not fresh:
                    return Reply(
                        "Those blocks are already on your calendar — nothing new to add.",
                        action, parser,
                    )
                draft = EventDraft(events=fresh)
                lines = [
                    f"• {a.title} — {a.start:%a %b %d %H:%M}–{a.end:%H:%M}" for a in draft.events
                ]
                return Reply(
                    f"Here's a calendar preview with {len(draft.events)} event(s):\n"
                    + "\n".join(lines)
                    + "\nNothing is saved yet — confirm below and I'll add them all at once.",
                    action, parser, event_draft=draft,
                )
        if action.title and action.title != "Untitled":
            return Reply(
                (f"想把「{action.title}」安排在几点？比如“明晚七点”。" if chinese else
                 f'When should I put “{action.title}”? Try “tomorrow 10am” or “Friday afternoon”.'),
                action, parser,
            )
        return Reply(
            ("想把什么事安排在几点？比如“明晚七点跑步”。" if chinese else
             "What should I add, and when? Try “Add a meeting tomorrow 10am”."),
            action, parser,
        )
    ev = cal.create_event(
        session, user_id, action.title or "Untitled", action.start, action.end, tz=tz
    )
    return Reply(
        (f"已把「{ev.title}」安排到 {action.start:%m月%d日 %H:%M}–{action.end:%H:%M}。"
         if chinese else
         f"Added “{ev.title}” on {action.start:%a %b %d, %H:%M}–{action.end:%H:%M}."
         " Anything else?"),
        action, parser,
    )


def confirm_event_draft(
    session: Session,
    user_id: int,
    draft: EventDraft,
    approved: bool,
    now: datetime,
    tz: str = "Asia/Shanghai",
) -> Reply:
    """确认/取消日历预览。确认时在同一事务内再次按标题+开始时间去重后逐个建块。

    取消不写库；任一 create_event 失败抛出让外层 session_scope 整体回滚。
    """
    action = Action(intent="add_event", confidence=1.0, raw="event draft confirmation")
    if not approved:
        return Reply("Okay — nothing was added to your calendar.", action, "event_draft:cancel")
    existing = _event_keys(session, user_id)
    created: list[Event] = []
    past_count = 0
    for item in draft.events:
        if item.start is None:
            continue
        item.normalized(now)
        if item.start <= now:
            past_count += 1
            continue
        key = ((item.title or "Untitled").casefold(), as_utc_naive(item.start, tz))
        if key in existing:
            continue
        ev = cal.create_event(
            session, user_id, item.title or "Untitled", item.start, item.end, tz=tz
        )
        existing.add(key)
        created.append(ev)
    session.flush()
    if not created:
        return Reply(
            ("Those proposed times have passed — nothing was added. Tell me a later time."
             if past_count else "Those blocks are already on your calendar — nothing new was added."),
            action, "event_draft:confirm",
        )
    lines = [
        f"• {ev.title} — {as_local(ev.start_utc, tz):%a %b %d %H:%M}–{as_local(ev.end_utc, tz):%H:%M}"
        for ev in created
    ]
    return Reply(
        f"Added {len(created)} event(s) to your calendar:\n" + "\n".join(lines),
        action, "event_draft:confirm",
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
    ev = cal.find_event(
        session, user_id, target, now_utc=as_utc_naive(now, tz), exact=True,
    )
    if ev is None:
        return Reply(f"I couldn't find “{target}”. Nothing was deleted.", action, parser)
    title = ev.title
    cal.delete_event(session, ev)
    return Reply(f"Deleted “{title}”.", action, parser)


def _query(session, user_id, action, now, tz, parser) -> Reply:
    # 以用户当前（可被时间机器快进的）时刻为基准，而不是墙钟时间
    start_utc = as_utc_naive(now, tz)
    events = cal.events_between(session, user_id, start_utc, start_utc + timedelta(days=8))
    if not events:
        return Reply(
            "Your week is empty. Want to add something, or should I find time for a goal step?",
            action, parser,
        )
    lines = ["Here's what's coming up:"]
    last_day = None
    for event in events[:12]:
        start = cal.as_local(event.start_utc, tz)
        if start.date() != last_day:
            last_day = start.date()
            name = {0: "Today", 1: "Tomorrow"}.get((last_day - now.date()).days)
            heading = f"{name} · {start:%a %b %d}" if name else f"{start:%a %b %d}"
            lines.extend(["", f"**{heading}**"])
        lines.append(f"- {start:%H:%M} — {event.title}")
    return Reply("\n".join(lines), action, parser)


def _step_title(value: str) -> str:
    return value.casefold().replace(" ", "")


def _matching_steps(goals: list[Goal], target: str) -> list[Step]:
    needle = _step_title(target)
    pending = [
        step
        for goal in goals
        for step in sorted(goal.steps, key=lambda item: item.seq)
        if step.status == "todo"
    ]
    exact = [step for step in pending if _step_title(step.title) == needle]
    if exact:
        return exact
    substring = [
        step for step in pending
        if needle in _step_title(step.title) or _step_title(step.title) in needle
    ]
    if substring:
        return substring
    return [
        step for step in pending
        if difflib.SequenceMatcher(None, needle, _step_title(step.title)).ratio() >= 0.75
    ]


def _resolve_step(goals: list[Goal], target: str | None) -> tuple[Goal | None, Step | None, str | None]:
    if target:
        matches = _matching_steps(goals, target)
        if not matches:
            return None, None, f'I couldn\'t find a pending step matching "{target}". Which step did you mean?'
        if len(matches) > 1:
            names = ", ".join(f'"{step.goal.title}: {step.title}"' for step in matches[:4])
            return None, None, f'"{target}" matches more than one step: {names}. Which one did you mean?'
        return matches[0].goal, matches[0], None
    # 无目标时按紧急度（置顶 > 最近截止日）选 goal，与 Chat 首页 featured goal 一致
    ordered = sorted(goals, key=goal_svc.priority_key)
    goal = next((g for g in ordered if _pick_step(g) is not None), ordered[0] if ordered else None)
    if goal is None:
        return None, None, None
    return goal, _pick_step(goal), None


def _pick_step(goal, prefer_low_energy: bool = False, target: str | None = None) -> Step | None:
    if goal.status != "active":
        return None
    steps = sorted(goal.steps, key=lambda s: s.seq)
    pending = [s for s in steps if s.status == "todo"]
    if target:
        matches = _matching_steps([goal], target)
        return matches[0] if len(matches) == 1 else None
    if prefer_low_energy:
        for s in pending:
            if s.energy <= 1:
                return s
    return pending[0] if pending else None


def next_unscheduled_step(
    session: Session, user_id: int, goal: Goal, now: datetime, tz: str,
) -> Step | None:
    scheduled = set(session.scalars(
        select(Event.step_id).where(
            Event.user_id == user_id,
            Event.step_id.in_([step.id for step in goal.steps] or [-1]),
            Event.kind == "block",
            Event.start_utc >= as_utc_naive(now, tz),
        )
    ))
    return next(
        (step for step in sorted(goal.steps, key=lambda item: item.seq)
         if step.status == "todo" and step.id not in scheduled),
        None,
    )


def looks_like_new_command(text: str, now: datetime) -> bool:
    """等待回答（目标澄清/签到）时，判断这句是不是明确的新指令。

    状态/进度类短回答（"太累了"、"done"）不算新指令，应按当前上下文解释。
    """
    action = parse_with_rules(text, now)
    target = (action.progress_target or "").casefold().strip(" .,!?'\"“”")
    targeted_progress = (
        action.intent == "log_progress"
        and target not in {"", "done", "finished", "completed", "it", "this", "that", "它", "这个", "那个"}
    )
    return action.intent in NEW_COMMAND_INTENTS or targeted_progress


def auto_refine_if_low(
    session: Session, user_id: int, goal: Goal, now: datetime, *, use_llm: bool = True
) -> str:
    """剩余 todo 步骤少于 2 个时，按阶段大纲自动补下一批；返回一句告知（没补则 ""）。"""
    if goal.status != "active":
        return ""
    pending = [s for s in goal.steps if s.status == "todo"]
    if len(pending) >= 2:
        return ""
    created = refine.refine_upcoming(session, goal, use_llm=use_llm)
    if not created:
        return ""
    metrics.record(
        session, user_id, metrics.PLAN_REFINED, now,
        goal_id=goal.id,
        payload={"created": len(created), "auto": True},
    )
    titles = ", ".join(f'"{s.title}"' for s in created)
    return f"You're almost out of steps, so I added {len(created)} more from the phase outline: {titles}."


def _create_goal(session, user_id, action, now, tz, parser, *, use_llm: bool = True) -> Reply:
    brief = planner.assess(
        action.title or "New goal",
        action.deadline,
        action.raw,
        today=now.date(),
    )
    if not planner.ready(brief):
        # 先确认期限，避免按默认 90 天生成计划后又只改日期。
        return Reply(planner.clarification_text(brief), action, parser, goal_brief=brief)
    return _preview_goal_now(session, user_id, brief, action, now, tz, parser,
                             use_llm=use_llm)


def continue_goal(
    session: Session,
    user_id: int,
    brief: planner.GoalBrief,
    text: str,
    now: datetime,
    tz: str = "Asia/Shanghai",
    *,
    use_llm: bool = True,
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
        return Reply(planner.clarification_text(updated), action, "goal:clarify",
                     goal_brief=updated)
    return _preview_goal_now(session, user_id, updated, action, now, tz, "goal:clarify",
                             use_llm=use_llm)


def prepare_goal_draft(
    session: Session,
    user_id: int,
    brief: planner.GoalBrief,
    now: datetime,
    tz: str = "Asia/Shanghai",
    *,
    use_llm: bool = True,
) -> GoalDraft:
    brief = planner.fill_defaults(brief, today=now.date())
    deadline = brief.deadline or (now.date() + timedelta(days=90))
    plan = generate_outline_plan(
        brief.title,
        deadline,
        today=now.date(),
        level=brief.level,
        target=brief.target,
        complexity=brief.complexity,
        raw=brief.raw,
        tz=tz,
        use_llm=use_llm,
    )
    if plan:
        outline, step_defs = plan
        return GoalDraft(brief, deadline, outline, step_defs, "llm")
    template_key = pick_template(brief.title)
    return GoalDraft(
        brief,
        deadline,
        milestones(template_key),
        first_batch(full_steps(template_key)),
        template_key,
    )


def create_goal_from_draft(session: Session, user_id: int, draft: GoalDraft) -> Goal:
    existing = session.scalar(select(Goal).where(Goal.user_id == user_id, Goal.draft_id == draft.draft_id))
    if existing is not None:
        return existing
    goal = goal_svc.create_goal(
        session, user_id, draft.brief.title, deadline=draft.deadline,
        step_defs=draft.step_defs, template_key=draft.source, outline=draft.outline,
    )
    goal.draft_id = draft.draft_id
    session.flush()
    return goal


def create_goal_from_brief(
    session: Session,
    user_id: int,
    brief: planner.GoalBrief,
    now: datetime,
    tz: str = "Asia/Shanghai",
) -> tuple[Goal, str]:
    draft = prepare_goal_draft(session, user_id, brief, now, tz)
    goal = create_goal_from_draft(session, user_id, draft)
    return goal, "llm" if draft.source == "llm" else "template"


_DATE_TEXT = re.compile(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}\s*月\s*\d{1,2}\s*[日号]")
# 抽掉时间表达后允许剩下的连接/虚词；还有其它内容残留即视为实质调整
_TIMELINE_FILLER = re.compile(
    r"\b(make|the|a|an|it|is|my|and|to|in|by|for|of|within|until|due|deadline|timeline|date)\b|"
    r"把|将|改成|改为|调整为?|定为?|定在|到|为|是|期限|截止",
    re.I,
)


def _timeline_only_edit(instruction: str) -> bool:
    """纯期限/日期修改判断：能解析出时间表达，且抽掉它和连接词后没有实质内容残留。

    克制的启发式（不是 NLP）：判断失败一律落到 L2/诚实拒绝，不会静默只改期限。
    """
    if (
        planner.parse_explicit_date(instruction) is None
        and planner.parse_duration_days(instruction) is None
    ):
        return False
    rest = _DATE_TEXT.sub("", instruction)
    rest = planner.DURATION.sub("", rest)
    rest = re.sub(
        r"\b(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten|twelve)\s+"
        r"(?:days?|weeks?|months?|years?)\b", "", rest, flags=re.I,
    )
    rest = _TIMELINE_FILLER.sub("", rest)
    return not re.sub(r"[\s，,。.、;；：:！!？?'\"'“”]+", "", rest)


def revise_goal_draft(
    draft: GoalDraft,
    instruction: str,
    now: datetime,
    tz: str = "Asia/Shanghai",
    *,
    use_llm: bool = True,
) -> GoalDraft | None:
    brief = planner.apply_answer(draft.brief, instruction, today=now.date())
    deadline = brief.deadline or draft.deadline
    if _timeline_only_edit(instruction):
        # 草稿期限变更后重建计划；沿用 draft_id，确认时不会重复建目标。
        if deadline == draft.deadline:
            return replace(draft, brief=brief)
        refreshed = prepare_goal_draft(None, None, brief, now, tz, use_llm=use_llm)
        return replace(refreshed, draft_id=draft.draft_id)
    plan = generate_plan(
        brief.title,
        deadline,
        today=now.date(),
        level=brief.level,
        target=brief.target,
        complexity=brief.complexity,
        raw=draft.brief.raw,
        tz=tz,
        instruction=instruction,
        current_steps=[item["title"] for item in draft.step_defs],
        use_llm=use_llm,
    )
    if not plan:
        return None
    outline = list(dict.fromkeys(item["milestone"] for item in plan if item.get("milestone")))
    return GoalDraft(brief, deadline, outline or draft.outline, plan[:3], "llm", draft.draft_id)


def confirm_goal_draft(
    session: Session,
    user_id: int,
    draft: GoalDraft,
    schedule_first: bool,
    now: datetime,
    tz: str = "Asia/Shanghai",
) -> Reply:
    goal = create_goal_from_draft(session, user_id, draft)
    session.flush()
    action = Action(intent="create_goal", title=goal.title, raw=draft.brief.raw, confidence=1.0)
    steps = sorted(goal.steps, key=lambda item: item.seq)
    candidates = []
    first = steps[0] if steps else None
    if schedule_first and first is not None:
        candidates = _propose(session, user_id, first, now, tz, goal.deadline)
    text = f"Goal set: **{goal.title}**."
    if schedule_first and first is not None:
        text += f" Choose a time for **{first.title}** below."
    else:
        text += " You can ask me to schedule the next step whenever you're ready."
    return Reply(
        text,
        action,
        "goal:confirm",
        candidates=candidates,
        goal_id=goal.id,
        step_id=first.id if first is not None else None,
    )


def _preview_goal_now(session, user_id, brief, action, now, tz, parser, *, use_llm: bool = True) -> Reply:
    draft = prepare_goal_draft(session, user_id, brief, now, tz, use_llm=use_llm)
    return Reply(
        f"Goal draft ready for **{draft.brief.title}**. Review it below, or tell me what to adjust.",
        action,
        parser,
        goal_draft=draft,
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
    goal = None
    if target:
        needle = target.casefold().strip().strip("\"'“”")
        exact = [goal for goal in goals if goal.title.casefold() == needle]
        matches = exact or [
            goal for goal in goals
            if needle in goal.title.casefold() or goal.title.casefold() in needle
        ]
        if len(matches) > 1:
            names = ", ".join(f"“{match.title}”" for match in matches[:4])
            return Reply(f"Which goal should I delete: {names}? Nothing was deleted.", action, parser)
        if matches:
            goal = matches[0]
    elif len(goals) == 1:
        goal = goals[0]
    if goal is None:
        if not target:
            names = ", ".join(f"“{g.title}”" for g in goals[:3])
            return Reply(f"Which goal should I delete? You have {names}.", action, parser)
        return Reply(
            f"I couldn't find a goal named “{target}”. Nothing was deleted.",
            action, parser,
        )

    title = goal.title
    removed = goal_svc.archive_goal(session, goal)
    extra = f" Removed {removed} calendar block(s)." if removed else ""
    return Reply(f"Deleted goal “{title}”.{extra}", action, parser)


def _resume_goal(session, user_id, action, now, tz, parser) -> Reply:
    """恢复暂停的目标；目标只能按标题从暂停列表里解析，模型不提供 ID。"""
    target = (action.target or action.title or "").strip().strip("\"'“”")
    paused = [
        goal for goal in session.scalars(
            select(Goal).where(Goal.user_id == user_id, Goal.status == "paused")
        )
    ]
    if not paused:
        return Reply("You don't have a paused goal to resume.", action, parser)
    goal = None
    if target:
        needle = target.casefold()
        matches = [
            goal for goal in paused
            if needle in goal.title.casefold() or goal.title.casefold() in needle
        ]
        if len(matches) > 1:
            names = ", ".join(f"“{match.title}”" for match in matches[:4])
            return Reply(f"Which paused goal should I resume: {names}?", action, parser)
        if matches:
            goal = matches[0]
    elif len(paused) == 1:
        goal = paused[0]
    if goal is None:
        names = ", ".join(f"“{g.title}”" for g in paused[:3])
        return Reply(f"Which goal do you want to resume? Paused: {names}.", action, parser)
    goal_svc.resume_goal(session, goal)
    step = _pick_step(goal)
    text = f"Welcome back — “{goal.title}” is active again."
    candidates = _propose(session, user_id, step, now, tz, goal.deadline) if step else []
    if step is not None and candidates:
        text += f" Next step: “{step.title}” ({step.est_min}m) — pick a time to restart?"
    elif step is not None:
        text += f' Next step: “{step.title}”. Say "find time" whenever you\'re ready.'
    return Reply(
        text, action, parser, candidates=candidates,
        goal_id=goal.id, step_id=step.id if step is not None else None,
    )


def _schedule_step(session, user_id, action, now, tz, parser, *, use_llm: bool = True) -> Reply:
    chinese = any("\u4e00" <= char <= "\u9fff" for char in action.raw)
    goals = goal_svc.active_goals(session, user_id)
    if not goals:
        return Reply(
            "You don't have an active goal yet. Tell me one and I'll break it down.",
            action, parser,
        )
    target = action.progress_target or action.title
    goal, step, error = _resolve_step(goals, target)
    if error is not None:
        return Reply(error, action, parser)
    if not target:
        for candidate_goal in sorted(goals, key=goal_svc.priority_key):
            unscheduled = next_unscheduled_step(session, user_id, candidate_goal, now, tz)
            if unscheduled is not None:
                goal, step = candidate_goal, unscheduled
                break
    note = ""
    if step is None:
        # 没有待办步骤不一定是目标完成：滚动拆解下可能只是下一批还没补
        note = auto_refine_if_low(session, user_id, goal, now, use_llm=use_llm)
        step = _pick_step(goal)
    if step is None:
        return Reply(f"“{goal.title}” is already complete. 🎉", action, parser)

    # 明确时间点（"安排在今天下午3点"）：直接建块跳过候选；低置信仍走候选
    if action.start is not None and action.confidence >= CONFIRM_THRESHOLD:
        candidate = Candidate(
            start=action.start,
            end=action.start + timedelta(minutes=step.est_min),
            score=1.0,
            reasons=["You picked this exact time."],
        )
        message = accept(session, user_id, step.id, candidate, now, tz, chinese=chinese)
        return Reply(message, action, parser, goal_id=goal.id, step_id=step.id)

    window = action.window
    candidates = _propose(session, user_id, step, now, tz, goal.deadline, window=window)
    if not candidates and window is not None:
        # 窗口内没有空档：说明一声，回退到默认的 5 天范围
        note += "That window has no free slot, so I looked a bit wider.\n"
        candidates = _propose(session, user_id, step, now, tz, goal.deadline)
    if not candidates:
        return Reply(
            (note + "\n" if note else "")
            + f"I couldn't find a free slot for “{step.title}” in the next 5 days. "
            "Try a shorter step or move the deadline.",
            action, parser,
        )
    candidate_text = (f"为「{step.title}」找到了 {len(candidates)} 个时段（每次 {step.est_min} 分钟）。"
                      "选一个我就写进日历。" if chinese else
                      f"Here are {len(candidates)} slots for “{step.title}” ({step.est_min}m). Pick one and I'll book it.")
    return Reply(
        (note + "\n" if note else "") + candidate_text,
        action, parser, candidates=candidates, goal_id=goal.id, step_id=step.id,
    )


def _pack_window(
    now: datetime, window: tuple[datetime, datetime] | None
) -> tuple[datetime, datetime]:
    """schedule_all 的装箱窗口：默认今天下午，下午已过则明天上午。

    显式窗口整体已过（"今天下午"但现在是晚上）时顺推一天，保留时段。
    """
    tz = now.tzinfo
    if window is None:
        start = datetime.combine(now.date(), time(hour=12), tzinfo=tz)
        end = datetime.combine(now.date(), time(hour=18), tzinfo=tz)
        if now >= end:
            start = datetime.combine(now.date() + timedelta(days=1), time(hour=8), tzinfo=tz)
            end = datetime.combine(now.date() + timedelta(days=1), time(hour=12), tzinfo=tz)
        return start, end
    if window[1] <= now:
        return window[0] + timedelta(days=1), window[1] + timedelta(days=1)
    return window


def _schedule_all(session, user_id, action, now, tz, parser) -> Reply:
    """把所有 active 目标的 todo 步骤按 seq 贪心装箱进窗口空档。

    规则：逐块 est_min、块间留 Preferences.buffer_minutes、当天目标块总时长
    （含已有块）不超过 Preferences.max_goal_minutes_per_day，超限即停；
    已经有未来块的步骤不重复装箱。
    """
    goals = goal_svc.active_goals(session, user_id)
    if not goals:
        return Reply(
            "You don't have an active goal yet. Tell me one and I'll break it down.",
            action, parser,
        )
    prefs = memory.get_scheduler_preferences(session, user_id)
    window = _pack_window(now, action.window)
    busy = cal.busy_blocks(
        session, user_id,
        as_utc_naive(window[0], tz) - timedelta(hours=1),
        as_utc_naive(window[1], tz),
        tz,
    )

    scheduled_ids = set(
        session.scalars(
            select(Event.step_id).where(
                Event.user_id == user_id,
                Event.kind == "block",
                Event.step_id.is_not(None),
                Event.start_utc >= as_utc_naive(now, tz),
            )
        )
    )
    todo: list[Step] = []
    already: list[str] = []
    for goal in goals:
        for step in sorted(goal.steps, key=lambda s: s.seq):
            if step.status != "todo":
                continue
            if step.id in scheduled_ids:
                already.append(step.title)
            else:
                todo.append(step)
    label = f"{window[0]:%a %b %d %H:%M}–{window[1]:%H:%M}"
    if not todo:
        detail = " — everything pending is already on the calendar." if already else "."
        return Reply(f"Nothing to pack{detail}", action, parser)

    start_floor = max(window[0], _ceil_to(now, prefs.granularity))
    gaps = free_intervals(
        busy, start_floor, window[1],
        buffer_minutes=prefs.buffer_minutes, min_minutes=15,
    )
    minutes_by_day: dict = {}
    for b in busy:
        if b.kind == "block":
            minutes_by_day[b.start.date()] = minutes_by_day.get(b.start.date(), 0) + b.minutes

    cursors = [g.start for g in gaps]
    planned: list[dict] = []
    unplaced: list[Step] = []
    idx = 0
    while idx < len(todo):
        step = todo[idx]
        placed = False
        hit_cap = False
        for i, gap in enumerate(gaps):
            start = cursors[i]
            end = start + timedelta(minutes=step.est_min)
            if end > gap.end:
                continue
            day_minutes = minutes_by_day.get(start.date(), 0)
            if day_minutes + step.est_min > prefs.max_goal_minutes_per_day:
                hit_cap = True
                break
            cursors[i] = end + timedelta(minutes=prefs.buffer_minutes)
            minutes_by_day[start.date()] = day_minutes + step.est_min
            planned.append(
                {"step_id": step.id, "title": step.title, "start": start, "end": end}
            )
            placed = True
            break
        if hit_cap:
            unplaced.extend(todo[idx:])
            break
        if not placed:
            unplaced.append(step)
        idx += 1

    if not planned:
        return Reply(
            f"Couldn't fit anything into {label} — try a wider window or shorter steps.",
            action, parser,
        )
    metrics.record(
        session, user_id, metrics.PROPOSAL_SHOWN, now,
        payload={"count": len(planned), "batch": True},
    )
    lines = [f"• {p['title']} — {p['start']:%H:%M}–{p['end']:%H:%M}" for p in planned]
    text = f"Packed {len(planned)} block(s) into {label}:\n" + "\n".join(lines)
    if unplaced:
        text += "\nDidn't fit: " + ", ".join(f"“{s.title}”" for s in unplaced) + "."
    if already:
        text += "\nAlready scheduled: " + ", ".join(f"“{t}”" for t in already) + "."
    text += "\nBook them all?"
    return Reply(text, action, parser, batch=planned)


def _refine_steps(session, user_id, action, now, tz, parser, *, use_llm: bool = True) -> Reply:
    """用户主动要求“下一步拆细一点”：把当前步骤拆成更小的动作（同卡住了的细化）。"""
    goals = goal_svc.active_goals(session, user_id)
    if not goals:
        return Reply(
            "You don't have an active goal yet. Tell me one and I'll break it down.",
            action, parser,
        )
    goal, step, error = _resolve_step(goals, action.progress_target or action.title)
    if error is not None:
        return Reply(error, action, parser)
    if step is None:
        return Reply(f'"{goal.title}" has no steps left to break down.', action, parser)

    created = refine.refine_next_steps(session, goal, step, use_llm=use_llm)
    metrics.record(
        session, user_id, metrics.PLAN_REFINED, now,
        goal_id=goal.id, step_id=step.id,
        payload={"created": len(created), "auto": False},
    )
    lines = "\n".join(f"• {s.title} ({s.est_min} min)" for s in created)
    text = (
        f'Done — I split "{step.title}" into {len(created)} smaller actions, '
        f"keeping the original step right after them:\n{lines}\n"
        "Start with the first 5-minute starter action."
    )
    candidates = _propose(session, user_id, created[0], now, tz, goal.deadline)
    if candidates:
        text += "\nI held a few slots for the starter action — pick one?"
    return Reply(
        text, action, parser, candidates=candidates,
        goal_id=goal.id, step_id=created[0].id,
    )


def _log_progress(session, user_id, action, now, tz, parser, *, use_llm: bool = True) -> Reply:
    goals = goal_svc.active_goals(session, user_id)
    if not goals:
        return Reply("You don't have an active goal yet. Tell me one and I'll break it down.", action, parser)
    goal, step, error = _resolve_step(goals, action.progress_target or action.title)
    if error is not None:
        return Reply(error, action, parser)
    if step is None:
        return Reply(f"“{goal.title}” is already complete. 🎉", action, parser)

    partial = action.checkin_outcome == "partial" or bool(PARTIAL.search(action.raw or ""))
    goal_svc.complete_step(
        session, step, verdict="partial" if partial else "done", raw_text=action.raw,
        now=as_utc_naive(now, tz),
    )
    if not partial:
        # 步骤已完成：已开始的块标 done 留作完成记录，未来块照常取消
        cal.resolve_due_blocks_done(session, step.id, as_utc_naive(now, tz))
        cal.cancel_future_blocks(session, step.id, as_utc_naive(now, tz))
    note = auto_refine_if_low(session, user_id, goal, now, use_llm=use_llm)
    pct = int(goal_svc.progress(goal) * 100)
    nxt = _pick_step(goal)
    if partial:
        text = f"Logged partial progress on “{step.title}”. Goal is at {pct}%."
        candidates: list[Candidate] = []
    else:
        text = f"Nice — “{step.title}” done. Goal is now at **{pct}%**."
        candidates = _propose(session, user_id, nxt, now, tz, goal.deadline) if nxt else []
        if nxt and candidates:
            text += f"\nNext: “{nxt.title}” ({nxt.est_min}m). Here are {len(candidates)} slots."
    if note:
        text += "\n" + note
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

    goals = goal_svc.active_goals(session, user_id)
    if not goals:
        return Reply("Noted. Tell me a goal and I'll keep it in mind when scheduling.", action, parser)

    target = action.progress_target or action.title
    goal, step, error = _resolve_step(goals, target)
    if error is not None:
        return Reply(error, action, parser)
    if not target:
        step = _pick_step(goal, prefer_low_energy=action.state in {"tired", "busy", "uninspired"})
    if step is None:
        return Reply("Noted.", action, parser)
    candidates = _propose(session, user_id, step, now, tz, goal.deadline)
    note = {
        "tired": "Let's not force a deep block. I picked a lighter step:",
        "uninspired": "When you're stuck, starting is the hard part. A small step:",
        "busy": "For the busy days, a short step that still moves things:",
        "fresh": "Good — let's use it. Next step:",
    }.get(action.state or "", "Next step:")
    return Reply(
        f"Noted you're {action.state}. {note} “{step.title}” ({step.est_min}m).",
        action, parser, candidates=candidates, goal_id=goal.id, step_id=step.id,
    )


# ------------------------------------------------------------------ proposals


def _propose(
    session, user_id, step: Step | None, now, tz, deadline=None, days: int = 5,
    est_min: int | None = None, is_recovery: bool = False,
    window: tuple[datetime, datetime] | None = None,
):
    if step is None:
        return []
    minutes = est_min or step.est_min
    start_utc = as_utc_naive(now, tz) - timedelta(hours=1)
    end_utc = as_utc_naive(now, tz) + timedelta(days=days + 1)
    if window is not None:
        start_utc = min(start_utc, as_utc_naive(window[0], tz))
        end_utc = max(end_utc, as_utc_naive(window[1], tz))
    busy = cal.busy_blocks(session, user_id, start_utc, end_utc, tz)
    candidates = propose(
        Requirement(
            est_min=minutes, energy=step.energy, kind=step.kind,
            min_chunk=min(20, minutes),
        ),
        now=now,
        busy=busy,
        prefs=memory.get_scheduler_preferences(session, user_id),
        days=days,
        top_n=6,
        deadline=deadline,
        window=window,
    )
    if candidates:
        metrics.record(
            session, user_id, metrics.PROPOSAL_SHOWN, now,
            goal_id=step.goal_id, step_id=step.id,
            payload={"count": len(candidates), "recovery": is_recovery},
        )
    return candidates


def propose_for_step(
    session: Session, user_id: int, step_id: int, now: datetime, tz: str, days: int = 5,
    window: tuple[datetime, datetime] | None = None,
) -> tuple[list[Candidate], Step | None]:
    """给 UI 的“找时间”按钮用：为指定步骤生成候选；给了 window 就只在该窗口内找。"""
    step = session.get(Step, step_id)
    if step is None or step.goal.user_id != user_id or step.status == "done":
        return [], step
    return _propose(session, user_id, step, now, tz, step.goal.deadline, days, window=window), step


def accept(
    session: Session, user_id: int, step_id: int, candidate: Candidate,
    now: datetime, tz: str, *, chinese: bool = False,
) -> str:
    step = session.get(Step, step_id)
    if step is None or step.goal.user_id != user_id:
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
    metrics.record(
        session, user_id, metrics.PROPOSAL_ACCEPTED, now,
        goal_id=step.goal_id, step_id=step.id, event_id=ev.id,
        payload={"recovery": False},
    )
    session.flush()
    if chinese:
        return (f"已将「{step.title}」安排到 {candidate.start:%m月%d日 %H:%M}–"
                f"{candidate.end:%H:%M}，并写入日历。")
    return f"Booked “{step.title}” on {candidate.start:%a %b %d, %H:%M}–{candidate.end:%H:%M}."


def accept_batch(
    session: Session, user_id: int, batch: list[dict], now: datetime, tz: str
) -> str:
    """批量接受：逐块建 Event（与 accept 同口径），proposal_accepted 只记一次。"""
    booked = 0
    for item in batch:
        step = session.get(Step, item["step_id"])
        if step is None or step.goal.user_id != user_id or step.status == "done":
            continue
        cal.cancel_blocks(session, step.id)
        cal.create_event(
            session,
            user_id,
            f"{step.goal.title}: {step.title}",
            item["start"],
            item["end"],
            kind="block",
            source="proposal",
            step_id=step.id,
            tz=tz,
        )
        booked += 1
    metrics.record(
        session, user_id, metrics.PROPOSAL_ACCEPTED, now,
        payload={"batch": booked},
    )
    session.flush()
    if not booked:
        return "Those steps are already done or gone — nothing was booked."
    return f"Booked {booked} block(s). Show up and start with the first one."


# ------------------------------------------------------------------ check-in


@dataclass
class CheckinContext:
    """签到流程的对话状态：event_ids 是待处理队列，第一个是当前在问的块。

    stage: batch（合并提问）→ outcome（问结果）→ reason（问原因）
           → recovery（给候选时段）/ goal_options（不想做了：延后/暂停/归档）
    """

    stage: str
    event_ids: list[int] = field(default_factory=list)
    merged: bool = False
    outcome: str | None = None
    reason: str | None = None
    step_id: int | None = None
    goal_id: int | None = None
    candidates: list[Candidate] = field(default_factory=list)


_REASON_FROM_STATE = {"tired": "tired", "busy": "no_time", "uninspired": "stuck"}
_UNWILLING = re.compile(
    r"不想做|不想继续|没动力|坚持不下去|"
    r"not feeling it|don'?t want to|do not want to|no motivation|not motivated|give up",
    re.I,
)
_DECLINE = re.compile(r"算了|不用|先不|不要|不需要|no thanks|never ?mind|\bskip\b", re.I)
_BATCH_NO = re.compile(r"不|算了|^no\b|^nope\b|\bskip\b|\bleave (them|it)\b", re.I)
_BATCH_YES = re.compile(
    r"重新安排|重排|^好|好的|嗯|可以|来吧|要|"
    r"\breschedule\b|\brearrange\b|\byes\b|\byeah\b|\byep\b|\bsure\b|\bok\b|\bokay\b|\bgo ahead\b",
    re.I,
)


def _friendly_when(start_local: datetime, now: datetime) -> str:
    days = (now.date() - start_local.date()).days
    day = {0: "today", 1: "yesterday", 2: "the day before"}.get(days) or f"{start_local:%b %d}"
    return f"{day} {start_local:%H:%M}"


def _block_title(session: Session, event: Event) -> str:
    if event.step_id is not None:
        step = session.get(Step, event.step_id)
        if step is not None:
            return step.title
    return event.title


def _checkin_question(session: Session, event: Event, now: datetime, tz: str) -> str:
    start = as_local(event.start_utc, tz)
    return (
        f'How did "{_block_title(session, event)}" go? '
        f"It was scheduled for {_friendly_when(start, now)}."
    )


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
        return ctx, f"You missed {len(blocks)} blocks — want me to reschedule them all together?"
    ctx = CheckinContext(stage="outcome", event_ids=ids)
    return ctx, _checkin_question(session, blocks[0], now, tz)


def checkin_quick_options(ctx: CheckinContext) -> list[str]:
    if ctx.stage == "batch":
        return ["Reschedule all", "Skip them"]
    if ctx.stage == "outcome":
        return ["Done", "Partly", "Didn't do it"]
    if ctx.stage == "reason":
        return ["Too tired", "No time", "Stuck", "Not feeling it"]
    if ctx.stage == "goal_options":
        return ["Push 7 days", "Push 14 days", "Pause this goal", "Archive this goal"]
    return []


def checkin_prompt(session: Session, ctx: CheckinContext, now: datetime, tz: str) -> str:
    """当前阶段该问的话，用于新指令打断后把签到问题重新提回来。"""
    ev = session.get(Event, ctx.event_ids[0]) if ctx.event_ids else None
    if ctx.stage == "batch":
        return f"You missed {len(ctx.event_ids)} blocks — want me to reschedule them all together?"
    if ctx.stage == "reason":
        return "Back to that block — what got in the way?"
    if ctx.stage == "recovery":
        return 'Did any of those times work? Say "skip it" if you\'d rather not reschedule.'
    if ctx.stage == "goal_options":
        return "Back to the options: push 7 days / push 14 days / pause / archive — what would you like to do?"
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
    *,
    use_llm: bool = True,
) -> tuple[list[Candidate], Step | None]:
    """按原因生成恢复方案；候选优先 72 小时内，找不到再放宽到 7 天。

    tired    → 换一个 energy ≤ 1 的步骤；没有低精力步骤就把当前步骤时长减半；
    no_time  → 同一步骤只排一个 15 分钟的“启动版”块；
    stuck    → 先把当前步骤细化出更小的下一步，再排第一个（5 分钟启动动作）；
    unwilling→ 不出时段候选，由对话层给延后/暂停/归档选项（见 _offer_recovery）。
    use_llm=False（L1 失败后的降级轮）时 stuck 细化走确定性规则，不调 LLM。
    """
    step = session.get(Step, event.step_id) if event.step_id is not None else None
    if step is None or step.status != "todo" or step.goal.status != "active":
        return [], step

    est_min: int | None = None
    scheme = "reschedule"
    if reason == "tired":
        lighter = _pick_step(step.goal, prefer_low_energy=True)
        if lighter is not None and lighter.energy <= 1:
            step = lighter
            scheme = "swap_light"
        else:
            est_min = max(15, step.est_min // 2)
            scheme = "halve"
    elif reason == "no_time":
        est_min = 15
        scheme = "starter_15"
    elif reason == "stuck":
        refined = refine.refine_next_steps(session, step.goal, step, use_llm=use_llm)
        if refined:
            metrics.record(
                session, user_id, metrics.PLAN_REFINED, now,
                goal_id=step.goal_id, step_id=step.id,
                payload={"created": len(refined)},
            )
            step = refined[0]
            scheme = "refine"

    candidates = _propose(session, user_id, step, now, tz, step.goal.deadline,
                          days=days, est_min=est_min, is_recovery=True)
    if not candidates and days < 7:
        candidates = _propose(session, user_id, step, now, tz, step.goal.deadline,
                              days=7, est_min=est_min, is_recovery=True)
    if candidates:
        metrics.record(
            session, user_id, metrics.RECOVERY_ACTION, now,
            goal_id=step.goal_id, step_id=step.id, event_id=event.id,
            payload={"reason": reason, "scheme": scheme},
        )
    return candidates, step


def _recovery_why(reason: str | None, step: Step, ev: Event, candidates: list[Candidate]) -> str:
    """一句话说明“为什么给你这个方案”，语气不责备。"""
    if reason == "tired":
        if step.id != ev.step_id:
            return f"Don't push through exhaustion — I swapped in a lighter step, \"{step.title}\". "
        if candidates and candidates[0].minutes < step.est_min:
            return f"Don't push through exhaustion — I cut \"{step.title}\" to half length; some progress beats none. "
        return "Don't push through exhaustion — this step is already light, so I picked a slot that works on low energy. "
    if reason == "no_time":
        return "No time is fine — let's just start with a 15-minute block; starting matters more than finishing. "
    if reason == "stuck":
        return "Being stuck usually means the step is too big, so I broke it down — start with the 5-minute starter action. "
    return ""


def _prepare_current(
    session: Session, user_id: int, ctx: CheckinContext, now: datetime, tz: str,
    *, use_llm: bool = True,
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
        candidates, step = suggest_recovery(session, user_id, ev, ctx.reason, now, tz,
                                            use_llm=use_llm)
        if not candidates:
            ctx.event_ids.pop(0)
            continue
        ctx.stage = "recovery"
        ctx.step_id = step.id
        ctx.goal_id = step.goal_id
        ctx.candidates = candidates
        return Reply(
            f"Let's find a new time for \"{step.title}\":",
            Action(intent="schedule_step", raw="", confidence=0.6), "checkin",
            candidates=candidates, goal_id=step.goal_id, step_id=step.id,
            checkin_context=ctx,
        )
    return None


def _advance(
    session: Session, user_id: int, ctx: CheckinContext, now: datetime, tz: str,
    *, use_llm: bool = True,
) -> Reply | None:
    """当前块处理完，进入队列里的下一个块。"""
    if ctx.event_ids:
        ctx.event_ids.pop(0)
    ctx.candidates = []
    return _prepare_current(session, user_id, ctx, now, tz, use_llm=use_llm)


def _offer_recovery(
    session: Session,
    user_id: int,
    ctx: CheckinContext,
    ev: Event,
    now: datetime,
    tz: str,
    parser: str,
    intro: str = "",
    *,
    use_llm: bool = True,
) -> Reply:
    step = session.get(Step, ev.step_id) if ev.step_id is not None else None
    if ctx.reason == "unwilling" and step is not None:
        ctx.stage = "goal_options"
        ctx.step_id = step.id
        ctx.goal_id = step.goal_id
        return Reply(
            intro
            + f"No pressure to keep going — forcing it makes quitting entirely more likely. "
            f"What should we do with \"{step.goal.title}\"?\n"
            "• Push 7 days / push 14 days: move the deadline out and loosen the pace\n"
            "• Pause this goal: tuck it away and resume whenever you like\n"
            "• Archive this goal: let it go for good — that's not failure",
            Action(intent="chat", raw="", confidence=0.5), parser,
            checkin_context=ctx,
        )

    candidates, step = suggest_recovery(session, user_id, ev, ctx.reason, now, tz,
                                        use_llm=use_llm)
    if candidates:
        ctx.stage = "recovery"
        ctx.step_id = step.id
        ctx.goal_id = step.goal_id
        ctx.candidates = candidates
        why = _recovery_why(ctx.reason, step, ev, candidates)
        return Reply(
            intro + why + "I held a few slots — pick one to restart?",
            Action(intent="schedule_step", raw="", confidence=0.6), parser,
            candidates=candidates, goal_id=step.goal_id, step_id=step.id,
            checkin_context=ctx,
        )
    text = (
        intro
        + "There's really no free slot in the next few days — the step stays on your list; "
        "tell me to \"find time for the next step\" whenever you want."
    )
    nxt = _advance(session, user_id, ctx, now, tz, use_llm=use_llm)
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
    *,
    use_llm: bool | None = None,
) -> Reply | None:
    """处理签到流程里的下一句；返回 None 表示这是明确的新指令，交给 handle 并保留 ctx。"""
    if looks_like_new_command(text, now) and not (
        ctx.stage == "goal_options" and _match_goal_option(text.strip()) is not None
    ) and not (
        ctx.stage == "batch" and (_BATCH_YES.search(text) or _BATCH_NO.search(text))
    ):
        return None
    action, parser = parse(text, now, use_llm=use_llm, context={"expecting": "checkin"})
    handler = {
        "batch": _checkin_batch,
        "outcome": _checkin_outcome,
        "reason": _checkin_reason,
        "recovery": _checkin_recovery,
        "goal_options": _checkin_goal_options,
    }[ctx.stage]
    # use_llm=False（L1 失败后的降级轮）沿 stage handler 传到底：
    # 恢复细化/自动续批都走确定性路径，不触达任何 LLM 入口
    return handler(session, user_id, ctx, action, text.strip(), now, tz, parser,
                   use_llm=use_llm is not False)


def _checkin_batch(session, user_id, ctx, action, text, now, tz, parser, *, use_llm: bool = True) -> Reply:
    now_utc = as_utc_naive(now, tz)

    def resolve_all(outcome: str, reason: str | None) -> None:
        for event_id in ctx.event_ids:
            ev = session.get(Event, event_id)
            if ev is None or ev.status not in ("planned", "moved"):
                continue
            recovery.resolve_block(session, ev, outcome, now_utc)
            if reason is not None:
                recovery.attach_reason(session, ev.id, reason)
        ctx.reason = reason

    if _BATCH_NO.search(text):
        resolve_all("missed", None)
        return Reply(
            "OK — I've marked them all as missed. Tell me whenever you want to reschedule.",
            action, parser, checkin_context=None,
        )
    outcome = action.checkin_outcome
    if outcome is not None or _BATCH_YES.search(text):
        outcome = "missed" if outcome in (None, "none") else outcome
        resolve_all(outcome, _reason_from(action, text))
        label = {"done": "done", "partial": "partially done", "missed": "missed"}[outcome]
        nxt = _prepare_current(session, user_id, ctx, now, tz, use_llm=use_llm)
        if nxt is None:
            return Reply(f"All marked as {label}.", action, parser, checkin_context=None)
        nxt.text = f"OK, all marked as {label}. Let's go block by block:\n" + nxt.text
        return nxt
    return Reply(
        'Want me to reschedule them all? Say "reschedule all" or "skip them".',
        action, parser, checkin_context=ctx,
    )


def _checkin_outcome(session, user_id, ctx, action, text, now, tz, parser, *, use_llm: bool = True) -> Reply:
    ev = session.get(Event, ctx.event_ids[0]) if ctx.event_ids else None
    if ev is None:
        nxt = _advance(session, user_id, ctx, now, tz, use_llm=use_llm)
        return nxt or Reply("All set — everything's handled.", action, parser, checkin_context=None)

    outcome = action.checkin_outcome
    reason = _reason_from(action, text)
    if outcome is None:
        if action.intent == "set_state":
            # "Too tired" 这种回答：按没做 + 原因理解
            outcome = "missed"
        else:
            return Reply(
                f"I didn't quite catch that — {_checkin_question(session, ev, now, tz)}",
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
        refine_note = ""
        if goal is not None and goal.status == "active":
            refine_note = auto_refine_if_low(session, user_id, goal, now, use_llm=use_llm)
        pct = int(goal_svc.progress(goal) * 100) if goal is not None else 0
        text_out = f"Nice — \"{_block_title(session, ev)}\" marked done! Goal progress is at {pct}%."
        if goal is not None and sum(1 for s in goal.steps if s.status == "done") == 1:
            # 第一次签到顺带校正水平：澄清时不追问，在这里补一句
            text_out += "\nQuick check: did that step feel too hard or too easy? I'll adjust the plan based on your feedback."
        if refine_note:
            text_out += "\n" + refine_note
        cands: list[Candidate] = []
        nxt_step = goal_svc.next_step(goal) if goal is not None and goal.status == "active" else None
        if nxt_step is not None:
            cands = _propose(session, user_id, nxt_step, now, tz, goal.deadline)
            if cands:
                text_out += f"\nNext up: \"{nxt_step.title}\" ({nxt_step.est_min} min) — do any of these times work?"
        nxt = _advance(session, user_id, ctx, now, tz, use_llm=use_llm)
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
        return _offer_recovery(session, user_id, ctx, ev, now, tz, parser, use_llm=use_llm)
    ctx.stage = "reason"
    gentle = "Partial progress still counts." if outcome == "partial" else "No worries, it happens."
    return Reply(f"{gentle} What got in the way?", action, parser, checkin_context=ctx)


def _checkin_reason(session, user_id, ctx, action, text, now, tz, parser, *, use_llm: bool = True) -> Reply:
    ev = session.get(Event, ctx.event_ids[0]) if ctx.event_ids else None
    if ev is None:
        nxt = _advance(session, user_id, ctx, now, tz, use_llm=use_llm)
        return nxt or Reply("All set — everything's handled.", action, parser, checkin_context=None)
    reason = _reason_from(action, text)
    if reason is None:
        # 不想说或没听懂：不追问到底，按通用恢复处理
        return _offer_recovery(session, user_id, ctx, ev, now, tz, parser,
                               intro="OK, let's not dwell on the reason. ", use_llm=use_llm)
    ctx.reason = reason
    recovery.attach_reason(session, ev.id, reason)
    return _offer_recovery(session, user_id, ctx, ev, now, tz, parser, use_llm=use_llm)


_ORDINAL = re.compile(
    r"^\s*(?:选|挑|就|the|number|no\.?|#)?\s*第?\s*"
    r"([一1二2三3]|one|two|three|first|second|third)\s*(?:个|张|条|项|one)?\s*[。\.!！]?\s*$",
    re.I,
)
_ORDINAL_INDEX = {
    "一": 0, "1": 0, "one": 0, "first": 0,
    "二": 1, "2": 1, "two": 1, "second": 1,
    "三": 2, "3": 2, "three": 2, "third": 2,
}


def _checkin_recovery(session, user_id, ctx, action, text, now, tz, parser, *, use_llm: bool = True) -> Reply:
    if _DECLINE.search(text):
        return decline_recovery(session, user_id, ctx, now, tz, use_llm=use_llm)
    m = _ORDINAL.match(text)
    if m:
        idx = _ORDINAL_INDEX[m.group(1).lower()]
        if idx < len(ctx.candidates):
            return accept_recovery(session, user_id, ctx, ctx.candidates[idx], now, tz,
                                   use_llm=use_llm)
        return Reply(
            f"There are only {len(ctx.candidates)} candidates — pick number 1 to {len(ctx.candidates)}.",
            action, parser, checkin_context=ctx,
        )
    return Reply(
        'Just pick one of the times above (you can say "the first one"); '
        'if you\'d rather not reschedule, say "skip it".',
        action, parser, checkin_context=ctx,
    )


_GOAL_OPTIONS: tuple[tuple[str, re.Pattern], ...] = (
    ("archive", re.compile(r"归档|放弃|\barchive\b|\bgive up\b|\blet it go\b", re.I)),
    ("pause", re.compile(r"暂停|\bpause\b|\bon hold\b", re.I)),
    ("extend_14", re.compile(r"14|十四|two weeks", re.I)),
    ("extend_7", re.compile(r"延后|推迟|延期|7|七|\bpush\b|\bextend\b|\bpostpone\b|\bdelay\b|\ba week\b", re.I)),
)


def _match_goal_option(text: str) -> str | None:
    if _DECLINE.search(text):
        return "decline"
    for kind, pattern in _GOAL_OPTIONS:
        if pattern.search(text):
            return kind
    return None


def _checkin_goal_options(session, user_id, ctx, action, text, now, tz, parser, *, use_llm: bool = True) -> Reply:
    goal = session.get(Goal, ctx.goal_id) if ctx.goal_id is not None else None
    choice = _match_goal_option(text)
    if goal is None or choice == "decline":
        text_out = "OK — the goal stays as is. Tell me whenever you want to adjust it."
    elif choice == "archive":
        goal_svc.archive_goal(session, goal)
        text_out = f"Archived \"{goal.title}\". Letting go of a goal that doesn't fit is progress too."
    elif choice == "pause":
        goal_svc.pause_goal(session, goal, now=as_utc_naive(now, tz))
        text_out = f"\"{goal.title}\" is paused, and its future blocks are off the calendar. Tell me when you want to resume."
    elif choice == "extend_14":
        deadline = goal_svc.extend_deadline(session, goal, 14, today=now.date())
        text_out = f"Deadline pushed to {deadline:%b %d} — two extra weeks of buffer, no pressure."
    elif choice == "extend_7":
        deadline = goal_svc.extend_deadline(session, goal, 7, today=now.date())
        text_out = f"Deadline pushed to {deadline:%b %d} — breathe and take it slow."
    else:
        return Reply(
            'I didn\'t quite catch that — you can say "push 7 days", "push 14 days", "pause", or "archive".',
            action, parser, checkin_context=ctx,
        )
    metrics.record(
        session, user_id, metrics.RECOVERY_ACTION, now,
        goal_id=ctx.goal_id, step_id=ctx.step_id,
        event_id=ctx.event_ids[0] if ctx.event_ids else None,
        payload={"reason": "unwilling", "choice": choice},
    )
    nxt = _advance(session, user_id, ctx, now, tz, use_llm=use_llm)
    if nxt is None:
        return Reply(text_out, action, parser, checkin_context=None)
    nxt.text = text_out + "\n" + nxt.text
    return nxt


def decline_goal_options(
    session: Session,
    user_id: int,
    ctx: CheckinContext,
    now: datetime,
    tz: str = "Asia/Shanghai",
    *,
    use_llm: bool = True,
) -> Reply:
    """goal_options 阶段的明确拒绝（"算了/先不"）：目标保持原样，进入队列下一个块。"""
    metrics.record(
        session, user_id, metrics.RECOVERY_ACTION, now,
        goal_id=ctx.goal_id, step_id=ctx.step_id,
        event_id=ctx.event_ids[0] if ctx.event_ids else None,
        payload={"reason": "unwilling", "choice": "decline"},
    )
    text = "OK — the goal stays as is. Tell me whenever you want to adjust it."
    nxt = _advance(session, user_id, ctx, now, tz, use_llm=use_llm)
    if nxt is None:
        return Reply(text, Action(intent="chat", raw="", confidence=0.5), "checkin",
                     checkin_context=None)
    nxt.text = text + "\n" + nxt.text
    return nxt


def accept_recovery(
    session: Session,
    user_id: int,
    ctx: CheckinContext,
    candidate: Candidate,
    now: datetime,
    tz: str = "Asia/Shanghai",
    *,
    use_llm: bool = True,
) -> Reply:
    """用户选定恢复候选：创建新块并写 RescheduleLog（旧块保留，供恢复率统计）。"""
    ev = session.get(Event, ctx.event_ids[0]) if ctx.event_ids else None
    step = session.get(Step, ctx.step_id) if ctx.step_id is not None else None
    if ev is None or step is None:
        return Reply("That block is already gone.", Action(intent="chat", raw="", confidence=0.5),
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
    metrics.record(
        session, user_id, metrics.PROPOSAL_ACCEPTED, now,
        goal_id=step.goal_id, step_id=step.id, event_id=ev.id,
        payload={"recovery": True},
    )
    session.flush()
    text = f"OK — \"{step.title}\" moved to {candidate.start:%a %b %d %H:%M}."
    nxt = _advance(session, user_id, ctx, now, tz, use_llm=use_llm)
    if nxt is None:
        return Reply(text + " Take it one block at a time.",
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
    *,
    use_llm: bool = True,
) -> Reply:
    """用户拒绝恢复候选：步骤保持 todo，进入下一个块。"""
    nxt = _advance(session, user_id, ctx, now, tz, use_llm=use_llm)
    text = "OK — I'll leave that block unscheduled; the step stays on your list."
    if nxt is None:
        return Reply(text, Action(intent="chat", raw="", confidence=0.5), "checkin",
                     checkin_context=None)
    nxt.text = text + "\n" + nxt.text
    return nxt
