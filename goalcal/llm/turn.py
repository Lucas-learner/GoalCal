"""L1 轮引擎：理解 + 路由 + 生成回复，每轮最多一次模型调用。

输入是用户消息 + DialogContext + 少量上下文快照；输出是严格校验的
TurnDecision（relation/intent/reply/...）。模型只提议，不执行：
Action 由 to_action 重建后交给 agent.execute（白名单 + 确定性执行），
日期一律由 natdate 从 when_text 重算，模型给的时间字段直接被忽略。

失败（未配置、异常、JSON 非法、意图越界）返回 None，调用方回退到
rules + 确定性话术；连续失败熔断 5 分钟，避免每轮白等 8s。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ValidationError

from ..auth import AuthError
from .. import telemetry
from . import respond
from .action import Action, Intent
from .client import LLMUnavailable
from .natdate import has_explicit_clock
from .parser import reconcile_dates
from .rules import ADVICE_QUESTION, NO_SCHEDULE, calendar_write_request, parse_with_rules

log = logging.getLogger(__name__)

PROMPT_VERSION = "l1.v3"

Relation = Literal["new_command", "in_flow", "accept_offer", "reject_offer", "resume", "clarify"]

SYSTEM = """You are GoalCal's turn engine: understand one user message, decide how it
relates to the current conversation, and draft the reply — in ONE JSON object.
Now: {now} ({tz}), {weekday}.

Input payload: the user's message, the current dialog context (kind/stage/subject/
return_to/offers — only currently open offers are listed), and a snapshot (recent
conversation, active goals with next steps, upcoming events).

Output ONLY JSON:
{{"relation": "...", "intent": "...", "reply": "...",
  "title": null, "when_text": null, "target": null, "duration_min": null,
  "state": null, "progress_target": null, "topic": null, "offer": null,
  "offer_intent": null, "offer_target": null, "offer_when_text": null}}

relation:
- "new_command": the message starts or switches to a new task/command.
- "in_flow": the message answers or continues the current dialog topic.
- "accept_offer": the message accepts the most recent assistant offer
  ("好", "可以", "sure", "yes please").
- "reject_offer": the message declines the most recent assistant offer without
  starting anything new ("不用", "先不", "算了", "no thanks", "not now").
- "resume": the user wants to return to a paused/interrupted topic in return_to
  ("我想恢复", "back to what we discussed").
- "clarify": the message is too ambiguous; ask one short clarifying question.

intent: one of add_event, move_event, delete_event, query_events, create_goal,
delete_goal, resume_goal (user wants to resume a PAUSED goal; put the goal title
in target if named), schedule_step, schedule_all, refine_steps, log_progress,
set_state, chat. Use "chat" when nothing should be executed.

Rules:
1. Never claim you created, moved, deleted, scheduled or logged anything — code
   executes the intent after you. Your reply acknowledges or answers only.
2. Do NOT compute dates. Copy the time phrase verbatim into when_text
   ("tomorrow 10am", "明天下午", "in 30 days"); a deterministic parser resolves it.
3. reply: 1-3 short sentences, in the user's language, structured as
   acknowledge (respond to what they actually said) → anchor (tie it to their
   words or the current context) → at most ONE optional offer. If you make an
   offer, copy it into "offer" verbatim. Use Markdown: blank lines between
   paragraphs and "- " lists for two or more items; never inline a list.
4. offer_intent: when your offer maps to an executable action ("want me to find
   time for it?" → schedule_step), set the intent it would trigger — no IDs,
   no dates; code resolves those on accept. For schedule_step or refine_steps,
   set offer_target to the EXACT title of the relevant active goal from context.
   For schedule_step, copy any
   proposed time phrase into offer_when_text ("今晚", "tomorrow evening").
   Never offer to write a calendar event if you cannot identify its subject.
   Leave null when the offer belongs
   to the current flow (confirming a draft, picking a slot) or is not
   executable — such offers are not tracked as acceptable.
5. topic: a few words naming what this conversation is now about.
6. When the dialog context shows a pending preview/candidates/checkin, prefer
   in_flow or accept_offer over new_command unless the message clearly starts
   something else.
7. You CAN add events and schedule goal steps through the structured intent.
   Never say this chat cannot write to the calendar. If the user asks to book
   a broad period ("tonight") without a clock time, route to schedule_step
   for an identified goal or add_event to ask for a specific time; do not
   claim it was booked. A suggestion question or a negated request must not
   trigger a write intent.
8. For a direct schedule_step request, set progress_target to the EXACT next_step
   title from context when you can identify the goal. With multiple plausible
   goals, ask which goal instead of choosing the first one.
9. For create_goal, copy a user-specified deadline into when_text. Never invent
   one: the application confirms a default timeline before generating the plan.
"""


class TurnDecision(BaseModel):
    """L1 唯一允许输出的结构；字段白名单校验，多余字段直接丢弃。"""

    relation: Relation
    intent: Intent
    reply: str = ""
    title: str | None = None
    when_text: str | None = None
    target: str | None = None
    duration_min: int | None = None
    state: Literal["tired", "busy", "uninspired", "fresh"] | None = None
    progress_target: str | None = None
    topic: str | None = None
    offer: str | None = None
    # 闲聊提议对应的可执行意图（白名单校验在 controller；模型不给 ID/日期）
    offer_intent: str | None = None
    offer_target: str | None = None
    offer_when_text: str | None = None


def to_action(decision: TurnDecision, text: str, now: datetime) -> Action:
    """把决定重建为 Action：时间由 natdate 重算，删目标误判按规则否决。

    duration  clamp 到 5–480 分钟：模型给的极端值不能写出跨多日的事件。
    """
    # Deterministic two-way guard: advice questions and explicit negations never
    # write, while an explicit calendar command cannot disappear into chat prose.
    if decision.intent in {"add_event", "schedule_step", "schedule_all", "create_goal"} and (
        ADVICE_QUESTION.search(text) or NO_SCHEDULE.search(text)
    ):
        return Action(intent="chat", raw=text, confidence=0.2)
    if decision.intent == "chat" and calendar_write_request(text):
        return parse_with_rules(text, now).normalized(now)
    duration = decision.duration_min
    if duration is not None:
        duration = max(5, min(480, duration))
    action = Action(
        intent=decision.intent,
        title=decision.title,
        when_text=decision.when_text,
        target=decision.target,
        duration_min=duration,
        state=decision.state,
        progress_target=decision.progress_target,
        confidence=0.8,
        raw=text,
    )
    action = reconcile_dates(action, text, now)
    if action.intent == "add_event" and not has_explicit_clock(action.when_text or text):
        action.start = action.end = None
    if action.intent == "create_goal":
        guard = parse_with_rules(text, now)
        if guard.intent == "delete_goal":
            return guard.normalized(now)
    return action.normalized(now)


@dataclass
class CircuitBreaker:
    """连续失败 threshold 次后熔断 cooldown 秒；冷却结束放行一次探测。

    进程级全局状态：匹配当前单用户自托管部署；若未来多用户共进程，
    需要按 user_id 拆分。
    """

    threshold: int = 3
    cooldown_seconds: float = 300
    failures: int = 0
    opened_at: float | None = None

    def allow(self) -> bool:
        if self.opened_at is None:
            return True
        if time.monotonic() - self.opened_at >= self.cooldown_seconds:
            self.opened_at = None
            self.failures = 0
            return True
        return False

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold and self.opened_at is None:
            self.opened_at = time.monotonic()


_BREAKER = CircuitBreaker()


def reset_breaker() -> None:
    """测试隔离：每个用例开始前清空熔断状态。"""
    _BREAKER.failures = 0
    _BREAKER.opened_at = None


def decide(
    session,
    user_id: int,
    text: str,
    now: datetime,
    tz: str,
    dialog_ctx,
    *,
    client=None,
    breaker: CircuitBreaker | None = None,
) -> TurnDecision | None:
    """返回本轮决定；不可用/失败/非法一律 None（调用方走降级路径）。"""
    if client is None:
        return None
    breaker = breaker or _BREAKER
    if not breaker.allow():
        telemetry.emit("llm_skipped", user_id=user_id, layer="turn", reason="circuit_open")
        return None
    try:
        payload = {
            "message": text,
            "dialog": {
                "kind": dialog_ctx.kind,
                "stage": dialog_ctx.stage,
                "subject": dialog_ctx.subject,
                "return_to": [entry.get("kind") for entry in dialog_ctx.return_to],
                "offers": [
                    offer.get("text", "")
                    for offer in dialog_ctx.offers
                    if offer.get("status", "open") == "open"
                ],
            },
            "context": respond.context_snapshot(session, user_id, now, tz),
        }
    except Exception:  # 上下文装配（DB 查询）失败不是模型故障，不计入熔断
        log.warning("L1 context assembly failed")
        return None
    system = SYSTEM.format(
        now=now.strftime("%Y-%m-%d %H:%M"), tz=tz, weekday=now.strftime("%A")
    )
    try:
        data = client.complete_json(system, json.dumps(payload, ensure_ascii=False))
        if not isinstance(data, dict) or not {"relation", "intent", "reply"}.issubset(data):
            raise ValueError("L1 decision missing required fields")
        decision = TurnDecision.model_validate(data)
    except AuthError:
        raise  # expired login must reach runtime.session_scope, not trip the circuit
    except LLMUnavailable:
        # 认证前提/配额/写事务保护是确定性守卫，不计入熔断
        return None
    except (ValueError, ValidationError) as exc:
        breaker.record_failure()
        telemetry.emit("llm_invalid", user_id=user_id, layer="turn",
                       error_type=type(exc).__name__)
        log.warning("L1 invalid output")
        return None
    except Exception as exc:  # noqa: BLE001 - 任何失败都必须能降级
        breaker.record_failure()
        log.warning("L1 fallback or validation failure")
        return None
    breaker.record_success()
    return decision
