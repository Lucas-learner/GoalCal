"""从对话上文提取助手刚提议的时间块，转成可校验的 add_event Action 列表。

仅当用户明确要求写入日历、且当前消息本身没有时间信息时由 agent 调用；
普通 chat、模糊闲聊不触发。LLM 只输出结构化草稿（最多 5 个），
日期 reconcile / 有效性校验 / 去重 / 写库全部在执行层，预览阶段不写库。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, time, timedelta
from typing import Any

from pydantic import BaseModel, field_validator

from .. import conversation
from ..config import settings
from .action import Action
from .router import plan_client
from .natdate import find_dates
from .parser import LLM_DATE_PENALTY

log = logging.getLogger(__name__)

MAX_EVENTS = 5
MIN_CONFIDENCE = 0.5

SYSTEM = """You are GoalCal's calendar extraction helper.
Today is {now} ({tz}). Weekday: {weekday}.

The user just asked to put events into their calendar. Extract from the recent
conversation ONLY the NEW time blocks that the assistant proposed and the user
is now referring to. Never copy events or classes that were described as
already on the user's schedule, and never invent times.

Return ONE JSON object:
{{"events":[{{"title":"...","when_text":"今天下午 15:30-16:15","start":null,"end":null,"duration_min":45,"confidence":0.9}}]}}

Rules:
1. At most 5 events, each with an explicit or clearly implied time in the conversation.
2. Copy the time phrase for each block into when_text verbatim, including the
   day and the time range when stated ("明天下午 15:30-16:15").
3. Leave start and end null whenever when_text captures the phrase. Only fill
   them yourself (ISO 8601 local time) when the phrase cannot be quoted
   verbatim, and in that case confidence MUST be below 0.7.
4. duration_min only when no end time is stated.
5. Use the conversation's language for titles. Output only JSON, no markdown.
6. If the conversation contains no schedulable proposal, return {{"events":[]}}.
"""


class EventItem(BaseModel):
    title: str
    when_text: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    duration_min: int | None = None
    confidence: float = 0.0

    @field_validator("title")
    @classmethod
    def _title(cls, value: Any) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("must not be empty")
        return value[:120]


class EventPlan(BaseModel):
    events: list[EventItem]


_LATE_PERIODS = {"下午", "晚上", "傍晚", "今晚", "明晚"}


def _clock_times(text: str) -> list[tuple[int, int]]:
    """按出现顺序抽出片段里所有钟点：(15, 30)、(16, 15)。

    支持 15:30 / 3:30pm / 3pm / 下午3点半 / 15点05分；按起点位置去重。
    """
    found: list[tuple[int, int, int]] = []  # (pos, hour, minute)
    for m in re.finditer(
        r"(上午|下午|晚上|傍晚|早上|中午|今晚|明晚)?\s*(\d{1,2}):(\d{2})\s*(am|pm)?",
        text,
        re.I,
    ):
        hour, minute = int(m.group(2)), int(m.group(3))
        meridiem = (m.group(4) or "").lower()
        period = m.group(1) or ""
        if meridiem == "pm" or (period in _LATE_PERIODS and hour < 12):
            hour = hour % 12 + 12
        elif meridiem == "am":
            hour %= 12
        found.append((m.start(), hour, minute))
    for m in re.finditer(r"\b(\d{1,2})\s*(am|pm)\b", text, re.I):
        if any(s <= m.start() < s + 6 for s, _, _ in found):  # 3:30pm 已被上面吃掉
            continue
        hour = int(m.group(1)) % 12
        if m.group(2).lower() == "pm":
            hour += 12
        found.append((m.start(), hour, 0))
    for m in re.finditer(
        r"(上午|下午|晚上|傍晚|早上|中午|今晚|明晚)?\s*(\d{1,2})\s*点\s*(半|(\d{1,2})\s*分?)?",
        text,
    ):
        hour = int(m.group(2))
        if m.group(1) in _LATE_PERIODS and hour < 12:
            hour += 12
        minute = 30 if m.group(3) == "半" else int(m.group(4) or 0)
        found.append((m.start(), hour, minute))
    found.sort()
    # LLM 逐字引用的原文可能有 "24:00"/"29点75分" 这类越界钟点，直接丢弃，
    # 否则 time(hour, minute) 会抛 ValueError 击穿整轮回退
    return [(hour, minute) for _, hour, minute in found if hour <= 23 and minute <= 59]


def _resolve(item: EventItem, now: datetime) -> Action | None:
    """把一条 LLM 草稿 reconcile 成带本地时间的 Action；无效返回 None。

    日期以 natdate 对 when_text 的解析为准；钟点从 when_text 里逐个抽，
    第一个当 start、第二个当 end。LLM 自报的 start/end 只在原文无法
    逐字引用时采用，并降低一档置信度。
    """
    tz = now.tzinfo
    phrase = item.when_text or ""
    confidence = item.confidence

    day = None
    found = find_dates(phrase, now) if phrase else []
    if found:
        day = found[0][1].date()
    times = _clock_times(phrase) if phrase else []

    start = end = None
    if times:
        if day is None:
            day = now.date()
        start = datetime.combine(day, time(*times[0]), tzinfo=tz)
        if len(times) > 1:
            end = datetime.combine(day, time(*times[1]), tzinfo=tz)
        # 只有钟点没有日期：整个块已过就顺推到明天（与 rules.extract_dates 同口径）
        if not found and (end or start + timedelta(minutes=60)) <= now:
            start += timedelta(days=1)
            if end is not None:
                end += timedelta(days=1)
    elif day is not None and item.start is not None:
        # when_text 只有日期没有钟点：用 reconcile 出的日期 + LLM 报的钟点
        llm_start = item.start.replace(tzinfo=tz) if item.start.tzinfo is None else item.start
        start = datetime.combine(day, llm_start.timetz()).replace(tzinfo=tz)
        if item.end is not None:
            llm_end = item.end.replace(tzinfo=tz) if item.end.tzinfo is None else item.end
            end = datetime.combine(day, llm_end.timetz()).replace(tzinfo=tz)
        confidence = max(0.0, confidence - LLM_DATE_PENALTY)
    elif item.start is not None:
        start = item.start if item.start.tzinfo is not None else item.start.replace(tzinfo=tz)
        if item.end is not None:
            end = item.end if item.end.tzinfo is not None else item.end.replace(tzinfo=tz)
        confidence = max(0.0, confidence - LLM_DATE_PENALTY)

    if start is None:
        return None
    if end is None:
        end = start + timedelta(minutes=item.duration_min or 60)
    if end <= start:
        log.warning("event_plan: end <= start, dropped: %s", item.title)
        return None
    if confidence < MIN_CONFIDENCE:
        return None
    return Action(
        intent="add_event",
        title=item.title,
        start=start,
        end=end,
        duration_min=int((end - start).total_seconds() // 60),
        confidence=confidence,
        raw=phrase,
    )


def extract_events(
    session,
    user_id: int,
    text: str,
    now: datetime,
    tz: str,
    *,
    client=None,
) -> list[Action] | None:
    """从最近对话提取要写入日历的时间块；未启用、失败、全无效时返回 None。

    只读少量上文（最近 8 条），不写库。复用 plan 层客户端的认证、配额、
    遥测与写事务禁外部请求保护。ready 按 plan 层判定：只配 LLM_API_KEY_PLAN
    也可生效。
    """
    if not settings.llm_enabled:
        return None
    client = client or plan_client()
    if not getattr(client, "ready", True):
        return None
    recent = conversation.load_recent(session, user_id, limit=8)
    payload = {
        "instruction": text,
        "now": now.strftime("%Y-%m-%d %H:%M"),
        "conversation": [
            {"role": m["role"], "content": m["content"][:300]} for m in recent
        ],
    }
    system = SYSTEM.format(
        now=now.strftime("%Y-%m-%d %H:%M"), tz=tz, weekday=now.strftime("%A")
    )
    try:
        data = client.complete_json(
            system, json.dumps(payload, ensure_ascii=False), temperature=0.0
        )
        plan = EventPlan.model_validate(data)
    except Exception:  # noqa: BLE001 - 提取失败（含 JSON 校验失败）必须能回退到追问
        log.warning("LLM fallback or validation failure")
        return None

    actions: list[Action] = []
    seen: set[tuple[str, datetime]] = set()
    for item in plan.events[:MAX_EVENTS]:
        action = _resolve(item, now)
        if action is None:
            continue
        key = (action.title.casefold(), action.start)
        if key in seen:
            continue
        seen.add(key)
        actions.append(action)
    return actions or None
