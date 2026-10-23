"""统一入口：LLM 优先，失败回落到规则解析。

日期归规则负责（README 铁律 2）：LLM 只抄录原文时间片段 when_text，
具体 start/deadline 由 natdate 算出；natdate 解析失败才采用 LLM 给的时间，
并降低一档置信度。
"""

from __future__ import annotations

import logging
from datetime import datetime

from pydantic import ValidationError

from ..config import settings
from .action import Action
from .client import LLMClient
from .natdate import extract_time, extract_window
from .prompts import PROMPT_VERSION, build_system
from .rules import extract_dates, parse_checkin_answer, parse_with_rules

log = logging.getLogger(__name__)

LLM_DATE_PENALTY = 0.2


def parse(
    text: str,
    now: datetime,
    use_llm: bool | None = None,
    context: dict | None = None,
) -> tuple[Action, str]:
    """返回 (action, parser_used)，parser_used 会写进日志，用于评测归因。

    context 携带对话状态（如 {"expecting": "checkin"}）：签到语境下先把
    "一半"/"太累了"/"done" 这类短回答按 outcome/reason 解释，
    明确是新指令时照常走 LLM/规则流程。
    """
    if context and context.get("expecting") == "checkin":
        answer = parse_checkin_answer(text, now)
        if answer is not None:
            return answer.normalized(now), "rules:checkin"

    wants_llm = settings.llm_ready if use_llm is None else (use_llm and bool(settings.llm_api_key))
    if wants_llm:
        try:
            action = _parse_with_llm(text, now)
            if action is not None:
                action = _reconcile_dates(action, text, now)
                # 明显是“删目标”的句子不允许被 LLM 读成“新建目标”。
                if action.intent == "create_goal":
                    guard = parse_with_rules(text, now)
                    if guard.intent == "delete_goal":
                        log.info("LLM 把删目标读成建目标，改用规则结果: %s", text)
                        return guard.normalized(now), "rules:guard"
                return action.normalized(now), f"llm:{PROMPT_VERSION}"
            log.warning("LLM 输出为空，回落规则解析: %s", text)
        except Exception as exc:  # noqa: BLE001 - 兜底优先，不能让对话崩
            log.warning("LLM 解析失败(%s)，回落规则解析: %s", exc, text)
    return parse_with_rules(text, now).normalized(now), "rules:v1"


def _reconcile_dates(action: Action, text: str, now: datetime) -> Action:
    """以 natdate 的结果为准改写 start/deadline/window；规则抓不到才信 LLM 的时间。"""
    if action.intent in {"schedule_step", "schedule_all"}:
        phrase = action.when_text or text
        window = extract_window(phrase, now)
        if window is not None:
            action.window = window
            return action
        if action.intent == "schedule_step":
            found = extract_dates(phrase, now)
            if found and extract_time(phrase):
                action.start = found[-1][1]
        return action
    if action.intent not in {"add_event", "move_event", "create_goal"}:
        return action
    found = extract_dates(action.when_text or text, now)
    if action.intent == "create_goal":
        if found:
            action.deadline = max(d for _, d in found).date()
        elif action.deadline is not None:
            action.confidence = max(0.0, action.confidence - LLM_DATE_PENALTY)
        return action
    if found:
        picked = found[-1][1] if action.intent == "move_event" else found[0][1]
        action.start = picked
        action.end = None  # normalized 按 duration 重算，避免 LLM 的 end 对新 start 不上
    elif action.start is not None:
        action.confidence = max(0.0, action.confidence - LLM_DATE_PENALTY)
    return action


def _parse_with_llm(text: str, now: datetime) -> Action | None:
    client = LLMClient()
    system = build_system(now.isoformat(), settings.tz, now.strftime("%A"))
    data = client.complete_json(system, text)
    if not data:
        return None
    try:
        action = Action.model_validate(data)
    except ValidationError as exc:
        log.info("结构化输出校验失败: %s", exc)
        return None
    action.raw = text
    return action
