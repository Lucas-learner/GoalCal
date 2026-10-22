"""规则解析器（无 LLM）。

两个用途：
1. 没有 API key / 网络时，应用照样能跑（M1 开发、demo 兜底）。
2. 作为评测里的 baseline：复杂 pipeline 至少要打得过它。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

try:  # dateparser 是可选兜底：没装也能跑（规则层已覆盖大部分说法）
    from dateparser.search import search_dates
except ImportError:  # pragma: no cover - 依赖缺失时的降级路径
    search_dates = None

from .action import Action
from .natdate import extract_time, find_dates

GOAL_MARKERS = re.compile(
    r"\b(i want to|i'd like to|i plan to|i'm going to|my goal|help me (to )?|goal is)\b|"
    r"我想|我想要|我的目标|目标是|希望|"
    r"我要(?:去|开始|坚持|在)?(?:学|练|跑|写|做|读|弹|通过|考|减|完成|掌握|成为)",
    re.I,
)
ADD_MARKERS = re.compile(
    r"^\s*(add|schedule|create|put|book|block|set up)\b|帮我(加|安排|建|创建)|添加|安排一个|加一个",
    re.I,
)
MOVE_MARKERS = re.compile(r"\b(move|reschedule|shift|push|postpone)\b|改到|移到|挪到|推迟|提前", re.I)
DELETE_MARKERS = re.compile(r"\b(delete|cancel|remove|drop)\b|删除|取消|去掉", re.I)
DELETE_GOAL_MARKERS = re.compile(
    r"\b(delete|remove|archive|cancel|drop)\b.*\bgoals?\b|"
    r"\bgoals?\b.*\b(delete|remove|archive|cancel|drop)\b|"
    r"(删除|删掉|取消).*目标|目标.*(删除|删掉|取消)",
    re.I,
)
QUERY_MARKERS = re.compile(
    r"\b(what do i have|what'?s on|show|list|am i free|free time|my schedule|do i have)\b|我有什么|空闲|日程|安排",
    re.I,
)
SCHEDULE_STEP_MARKERS = re.compile(
    r"\b(find time|make time|schedule (the )?next|next step|work on (my )?goal)\b|"
    r"找个?时间|安排(一下)?下一步|下一步|推进目标",
    re.I,
)
DONE_MARKERS = re.compile(
    r"\b(done|finished|completed|wrote|did|made|published|recorded)\b|完成|写完|做完|做好了|发布了", re.I
)
STATE_MARKERS = {
    "tired": re.compile(r"\b(tired|exhausted|sleepy|drained|no energy)\b|累|疲惫|困", re.I),
    "busy": re.compile(r"\b(busy|swamped|packed|no time)\b|忙|没时间", re.I),
    "uninspired": re.compile(r"\b(uninspired|stuck|blocked|no idea|blank)\b|没灵感|卡住|没思路", re.I),
    "fresh": re.compile(r"\b(fresh|energized|motivated|ready)\b|精神|有劲|状态好", re.I),
}

DURATION_MIN = re.compile(r"(\d+)\s*(minutes?|mins?|m)\b|(\d+)\s*分钟", re.I)
DURATION_HR = re.compile(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h)\b|(\d+(?:\.\d+)?)\s*小时", re.I)


def _extract_duration(text: str) -> int | None:
    if m := DURATION_HR.search(text):
        value = m.group(1) or m.group(3)
        return int(float(value) * 60)
    if m := DURATION_MIN.search(text):
        value = m.group(1) or m.group(3)
        return int(value)
    return None


def _extract_dates(text: str, now: datetime) -> list[tuple[str, datetime]]:
    found = find_dates(text, now)
    if found:
        return found

    # 只有时间、没有日期（"add standup at 10" / "every morning at 10"）
    # 默认今天；如果已经过了，就放到明天。不要让 dateparser 猜出一整年后。
    explicit = extract_time(text)
    if explicit and re.search(
        r"\b(?:at|on)\b|上午|下午|晚上|早上|中午|傍晚|点", text, re.I
    ):
        hour, minute = explicit
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return [("", candidate)]

    if search_dates is None:
        return []
    # 规则没覆盖到的口语说法，最后再交给 dateparser
    try:
        parsed = search_dates(
            text,
            languages=["en", "zh"],
            settings={
                "RELATIVE_BASE": now.replace(tzinfo=None),
                "PREFER_DATES_FROM": "future",
                "RETURN_AS_TIMEZONE_AWARE": False,
            },
        )
    except Exception:
        parsed = None
    return [(s, _attach(d, now)) for s, d in (parsed or [])]


def _attach(dt: datetime, now: datetime, default_hour: int = 9) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=now.tzinfo)
    # dateparser 对“Wednesday”只给日期不给时间 → 默认上午 9 点
    if dt.hour == 0 and dt.minute == 0:
        dt = dt.replace(hour=default_hour)
    return dt


def _clean_title(text: str, spans: list[str]) -> str:
    cleaned = text
    for s in spans:
        if not s:  # 只有时间没有日期时 span 为空
            continue
        cleaned = re.sub(re.escape(s) + r"(?:'s|’s)?", " ", cleaned, flags=re.I)
    # 时间片段不属于标题：10am / 4 pm / 10:30 / 上午 10 点 / at 10 / noon
    cleaned = re.sub(
        r"\b(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm|o'clock)\b", " ", cleaned, flags=re.I
    )
    cleaned = re.sub(r"\b\d{1,2}:\d{2}\b", " ", cleaned)
    cleaned = re.sub(r"\bat\s+\d{1,2}\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"(?:上午|下午|晚上|早上|中午|傍晚)?\s*\d{1,2}\s*点", " ", cleaned)
    cleaned = re.sub(
        r"\b(noon|midday|midnight|morning|afternoon|evening|night|tonight|every|each)\b|"
        r"上午|下午|晚上|中午|早上|傍晚",
        " ",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(r"找个?时间|找时间|安排(一下)?", " ", cleaned)
    cleaned = re.sub(
        r"^\s*(add|schedule|create|put|book|block|set up|move|reschedule|shift|push|postpone|"
        r"delete|cancel|remove|drop|please|can you|could you|i want to|i'd like to|"
        r"帮我|请|安排|把|我要|我想)\s*",
        "",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(
        r"\b(to|at|on|by|from|for|tomorrow|today|tonight|next week)\b|"
        r"\d+(\.\d+)?\s*(minutes?|mins?|hours?|hrs?|分钟|小时)",
        " ",
        cleaned,
        flags=re.I,
    )
    cleaned = re.sub(r"^\s*(a|an|the)\s+", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.'\"")
    return cleaned[:80]


def _goal_target(text: str) -> str:
    """从 “delete "track my goal"” / “删除目标 track my goal” 里取目标名。"""
    quoted = re.search(r"[\"'“”‘’]([^\"'“”‘’]+)[\"'“”‘’]", text)
    if quoted:
        return quoted.group(1).strip()[:120]

    # 只去掉开头/结尾的指令词，不能把标题里的 "goal" 一起删掉
    cleaned = re.sub(
        r"^\s*(?:delete|remove|archive|cancel|drop)\b", " ", text, flags=re.I
    )
    cleaned = re.sub(r"^\s*(?:删除|删掉|取消)\s*|^\s*把\s*", " ", cleaned)
    cleaned = re.sub(
        r"^\s*(?:(?:the|my|this|that|a|an)\s+)?goals?\b", " ", cleaned, flags=re.I
    )
    cleaned = re.sub(r"^\s*(?:(?:我的?|这个|那个)\s*)?目标\s*", " ", cleaned)
    cleaned = re.sub(r"^\s*(?:my|the|this|that|a|an)\s+", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.'\"“”")
    return cleaned[:120]


def parse_with_rules(text: str, now: datetime) -> Action:
    raw = text.strip()
    low = raw.lower()
    dates = _extract_dates(raw, now)
    spans = [s for s, _ in dates]
    duration = _extract_duration(raw)

    # 1) 状态
    for label, pattern in STATE_MARKERS.items():
        if pattern.search(raw) and len(raw) < 60:
            return Action(intent="set_state", state=label, confidence=0.7, raw=raw)

    # 2) 删目标（"delete my goal" / 删除目标 X）——必须早于 create_goal，
    #    否则 "delete my goal" 会因为出现 "my goal" 被误判成新建目标。
    if DELETE_GOAL_MARKERS.search(raw):
        target = _goal_target(raw)
        return Action(
            intent="delete_goal",
            target=target or None,
            title=target or None,
            confidence=0.75,
            raw=raw,
        )

    # 3) 删日程
    if DELETE_MARKERS.search(raw):
        target = _clean_title(raw, spans)
        return Action(intent="delete_event", target=target, title=target, confidence=0.65, raw=raw)

    # 4) 给目标步骤找时间（"安排下一步" / "find time for the next step"）
    if SCHEDULE_STEP_MARKERS.search(raw):
        target = _clean_title(raw, spans)
        if re.fullmatch(r"(下一步|next step)", target, re.I):
            target = None
        return Action(
            intent="schedule_step",
            progress_target=target or None,
            confidence=0.7,
            raw=raw,
        )

    # 5) 建目标（"I want to ... in 30 days"）
    if GOAL_MARKERS.search(raw):
        deadline = None
        if dates:
            deadline = max(d for _, d in dates).date()
        m = re.search(r"\b(?:in|within|by)\s+(\d+)\s+days?\b|(\d+)\s*天", raw, re.I)
        if m:
            days = int(m.group(1) or m.group(2))
            deadline = (now + timedelta(days=days)).date()
        title = re.sub(
            r"^(i want to|i'd like to|i plan to|i'm going to|help me (to )?|my goal is|"
            r"我想|我想要|我要|希望|我的目标是)\s*",
            "",
            raw,
            flags=re.I,
        )
        return Action(
            intent="create_goal",
            title=title[:120],
            deadline=deadline,
            confidence=0.65,
            raw=raw,
        )

    # 6) 报进度
    if DONE_MARKERS.search(raw) and not ADD_MARKERS.search(raw):
        target = _clean_title(raw, spans)
        return Action(intent="log_progress", progress_target=target, confidence=0.6, raw=raw)

    # 7) 改 / 查

    if MOVE_MARKERS.search(raw):
        # "move tomorrow's workout to Friday"：日期里最后一个当作目标时间
        target = _clean_title(raw.split(" to ")[0] if " to " in low else raw, spans)
        when = _attach(dates[-1][1], now) if dates else None
        return Action(
            intent="move_event",
            target=target,
            title=target,
            start=when,
            confidence=0.65 if when else 0.35,
            raw=raw,
        )

    if QUERY_MARKERS.search(raw):
        return Action(intent="query_events", confidence=0.6, raw=raw)

    # 5) 加日程
    if ADD_MARKERS.search(raw) or (dates and duration):
        when = _attach(dates[0][1], now) if dates else None
        title = _clean_title(raw, spans)
        if when and when.replace(hour=0, minute=0) < now and not dates:
            when = None
        return Action(
            intent="add_event" if when else "chat",
            title=title or "Untitled",
            start=when,
            duration_min=duration or 60,
            confidence=0.7 if when else 0.3,
            raw=raw,
        )

    return Action(intent="chat", confidence=0.2, raw=raw)
