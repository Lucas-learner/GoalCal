from __future__ import annotations

import logging
from datetime import datetime

from pydantic import ValidationError

from ..config import settings
from .action import Action
from .client import LLMClient
from .prompts import PROMPT_VERSION, build_system
from .rules import parse_with_rules

log = logging.getLogger(__name__)


def parse(text: str, now: datetime, use_llm: bool | None = None) -> tuple[Action, str]:
    """统一入口：LLM 优先，失败/低置信度回落到规则解析。

    返回 (action, parser_used)，parser_used 会写进日志，用于评测归因。
    """
    wants_llm = settings.llm_ready if use_llm is None else (use_llm and bool(settings.llm_api_key))
    if wants_llm:
        try:
            action = _parse_with_llm(text, now)
            if action is not None:
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
