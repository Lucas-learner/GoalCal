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
    r"我要[^。，,;；]{0,15}?(?:学|练|跑|写|做|读|弹|通过|考|减|完成|掌握|成为)",
    re.I,
)
ADD_MARKERS = re.compile(
    r"^\s*(add|schedule|create|put|book|block|set up)\b|"
    r"\bhelp me (?:to )?(add|schedule|create|put|book|block|set up)\b|"
    r"帮我(加|安排|建|创建)|帮忙(加|安排)|添加|安排一个|加一个",
    re.I,
)
MOVE_MARKERS = re.compile(r"\b(move|reschedule|shift|push|postpone)\b|改到|移到|挪到|推迟|提前", re.I)
DELETE_MARKERS = re.compile(r"\b(delete|cancel|remove|drop)\b|删除|删掉|取消|去掉", re.I)
DELETE_GOAL_MARKERS = re.compile(
    r"\b(delete|remove|archive|cancel|drop)\b.*\bgoals?\b|"
    r"\bgoals?\b.*\b(delete|remove|archive|cancel|drop)\b|"
    r"(删除|删掉|取消).*目标|目标.*(删除|删掉|取消)",
    re.I,
)
QUERY_MARKERS = re.compile(
    r"\b(what do i have|what'?s on|show|list|am i free|free time|my schedule|do i have)\b|"
    r"我有什么|有什么安排|有啥安排|有空|空闲|日程",
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
NEGATED_DONE = re.compile(
    r"\b(did not|didn't|haven't|hasn't|not yet|never)\b[^。.;!?]*\b(finish|finished|done|complete|completed)\b|"
    r"没(有)?(做|完成|写|弄|搞)|还没(有)?(做|完成)|未完成",
    re.I,
)
# 中文状态词按“整句短语”匹配：单字子串会把“帮忙”误判成“忙”、“积累”误判成“累”
STATE_MARKERS = {
    "tired": re.compile(
        r"\b(tired|exhausted|sleepy|drained|no energy)\b|"
        r"我(好|很|有点|太|非常)?累|太累|好累|累死了|疲惫|很困|好困|太困|困了|没精神",
        re.I,
    ),
    "busy": re.compile(
        r"\b(busy|swamped|packed|no time)\b|"
        r"我(好|很|有点|太|非常)?忙|太忙|很忙|好忙|忙死了|没时间|时间不够",
        re.I,
    ),
    "uninspired": re.compile(
        r"\b(uninspired|stuck|blocked|no idea|blank)\b|没灵感|卡住|没思路|想不出来",
        re.I,
    ),
    "fresh": re.compile(
        r"\b(fresh|energized|motivated|ready)\b|精神好|精神不错|很有精神|有劲|状态好|状态不错|状态很好",
        re.I,
    ),
}

# 签到/澄清等待回答时，这些意图算“明确的新指令”，其余短回答按当前上下文解释
NEW_COMMAND_INTENTS = {
    "add_event",
    "move_event",
    "delete_event",
    "delete_goal",
    "query_events",
    "schedule_step",
    "create_goal",
}

CHECKIN_OUTCOMES: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b(done|finished|completed)\b|完成了?|做完了?|搞定了|做好了", re.I), "done"),
    (re.compile(r"\b(half|partly|partially|some|started|almost)\b|一半|一部分|部分|做了一点|写了一点", re.I), "partial"),
    (re.compile(r"\b(no|nope|nothing|not yet)\b|没做|没有做|还没|一点(也|都)?没", re.I), "none"),
)

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


def extract_dates(text: str, now: datetime) -> list[tuple[str, datetime]]:
    """抽取 (原文片段, 本地时间) 列表；规则抓不到再退回 dateparser。"""
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
    # 开头的指令词可能叠着出现（"帮我安排…"、"帮忙加个…"），循环剥到不再变化
    leading = re.compile(
        r"^\s*(?:add|schedule|create|put|book|block|set up|move|reschedule|shift|push|postpone|"
        r"delete|cancel|remove|drop|please|can you|could you|i want to|i'd like to|"
        r"i (?:just )?(?:finished|completed|did|made|wrote)|done with|finished with|"
        r"帮我(?:安排|加一?个|建一?个|创建)?|帮忙|请|安排一?个?|加一?个|把|我要|我想|"
        r"完成了?|做完了?|写完了?|做好了)\s*",
        re.I,
    )
    while True:
        stripped = leading.sub("", cleaned)
        if stripped == cleaned:
            break
        cleaned = stripped
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
    dates = extract_dates(raw, now)
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

    # 4) 改期（"改到/挪到" 优先于 "帮我安排"，如 "帮我安排明天改到周五"）
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

    # 5) 给目标步骤找时间（"安排下一步" 要先于泛化的 "帮我安排…" 加日程）
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

    # 6) 加日程（显式指令，早于建目标/查询："帮我安排明天10点开会"、"help me add …"）
    if ADD_MARKERS.search(raw):
        when = _attach(dates[0][1], now) if dates else None
        title = _clean_title(raw, spans)
        return Action(
            intent="add_event" if when else "chat",
            title=title or "Untitled",
            start=when,
            duration_min=duration or 60,
            confidence=0.7 if when else 0.3,
            raw=raw,
        )

    # 7) 建目标（"I want to ... in 30 days"）
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

    # 8) 报进度（否定句 "I did not finish …" / "没做完" 不算完成）
    if DONE_MARKERS.search(raw) and not ADD_MARKERS.search(raw) and not NEGATED_DONE.search(raw):
        target = _clean_title(raw, spans)
        return Action(intent="log_progress", progress_target=target, confidence=0.6, raw=raw)

    # 9) 查
    if QUERY_MARKERS.search(raw):
        return Action(intent="query_events", confidence=0.6, raw=raw)

    # 10) 隐式加日程：只有日期/时间、没有指令词（"明天10点开会"、"周五下午3点半开会"）
    if dates and (duration or extract_time(raw)):
        when = _attach(dates[0][1], now)
        title = _clean_title(raw, spans)
        return Action(
            intent="add_event",
            title=title or "Untitled",
            start=when,
            duration_min=duration or 60,
            confidence=0.6,
            raw=raw,
        )

    return Action(intent="chat", confidence=0.2, raw=raw)


def parse_checkin_answer(text: str, now: datetime) -> Action | None:
    """签到语境下的短回答："一半"/"太累了"/"done" 按 outcome/reason 解释。

    返回 None 表示这不是签到回答（明确的新指令，或看不懂），走正常解析流程。
    """
    raw = (text or "").strip()
    if not raw:
        return None
    action = parse_with_rules(raw, now)
    if action.intent in NEW_COMMAND_INTENTS:
        return None
    for pattern, outcome in CHECKIN_OUTCOMES:
        if not pattern.search(raw):
            continue
        if outcome == "none":
            # "没做，太忙了"：原因记在 state 上
            action.checkin_outcome = "none"
            action.confidence = max(action.confidence, 0.6)
            return action
        return Action(
            intent="log_progress",
            checkin_outcome=outcome,
            confidence=0.8,
            raw=raw,
        )
    if action.intent == "set_state":
        return action
    return None
