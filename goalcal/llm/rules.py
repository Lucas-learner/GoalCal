"""规则解析器（无 LLM）。

两个用途：
1. 没有 API key / 网络时，应用照样能跑（M1 开发、demo 兜底）。
2. 作为评测里的 baseline：复杂 pipeline 至少要打得过它。
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta

from dateparser.search import search_dates

from .action import Action
from .natdate import find_dates

GOAL_MARKERS = re.compile(
    r"\b(i want to|i'd like to|my goal|help me (to )?|goal is)\b|我想|我的目标|目标是|希望",
    re.I,
)
ADD_MARKERS = re.compile(
    r"^\s*(add|schedule|create|put|book|block|set up)\b|帮我(加|安排|建|创建)|添加|安排一个|加一个",
    re.I,
)
MOVE_MARKERS = re.compile(r"\b(move|reschedule|shift|push|postpone)\b|改到|移到|挪到|推迟|提前", re.I)
DELETE_MARKERS = re.compile(r"\b(delete|cancel|remove|drop)\b|删除|取消|去掉", re.I)
QUERY_MARKERS = re.compile(
    r"\b(what do i have|what'?s on|show|list|am i free|free time|my schedule|do i have)\b|我有什么|空闲|日程|安排",
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
        cleaned = re.sub(re.escape(s) + r"(?:'s|’s)?", " ", cleaned, flags=re.I)
    cleaned = re.sub(
        r"^\s*(add|schedule|create|put|book|block|set up|move|reschedule|shift|push|postpone|"
        r"delete|cancel|remove|drop|please|can you|could you|i want to|i'd like to|"
        r"帮我|请|把|我要|我想)\s*",
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

    # 2) 建目标（"I want to ... in 30 days"）
    if GOAL_MARKERS.search(raw):
        deadline = None
        if dates:
            deadline = max(d for _, d in dates).date()
        m = re.search(r"\b(?:in|within|by)\s+(\d+)\s+days?\b|(\d+)\s*天", raw, re.I)
        if m:
            days = int(m.group(1) or m.group(2))
            deadline = (now + timedelta(days=days)).date()
        title = re.sub(r"^(i want to|i'd like to|help me (to )?|my goal is|我想|我的目标是)\s*", "", raw, flags=re.I)
        return Action(
            intent="create_goal",
            title=title[:120],
            deadline=deadline,
            confidence=0.65,
            raw=raw,
        )

    # 3) 报进度
    if DONE_MARKERS.search(raw) and not ADD_MARKERS.search(raw):
        target = _clean_title(raw, spans)
        return Action(intent="log_progress", progress_target=target, confidence=0.6, raw=raw)

    # 4) 删 / 改 / 查
    if DELETE_MARKERS.search(raw):
        target = _clean_title(raw, spans)
        return Action(intent="delete_event", target=target, title=target, confidence=0.65, raw=raw)

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
