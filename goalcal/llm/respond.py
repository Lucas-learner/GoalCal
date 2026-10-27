"""普通对话回复：先理解，再建议。

只处理 intent=chat 的自由文本；写操作的结果文案仍是 agent 里的确定性代码。
只读少量最近对话、active goals/下一步、近期日程和当前时间；
LLM 不可用或失败返回 None，由调用方回退到确定性话术。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from .. import conversation
from ..config import settings
from ..domain import calendar_service as cal
from ..domain import goals as goal_svc
from ..timeutil import as_local, as_utc_naive
from .client import LLMClient

log = logging.getLogger(__name__)

SYSTEM = """You are GoalCal, a warm and low-key planning companion.
Now: {now} ({tz}), {weekday}.

The user's message is ordinary chat — it does NOT ask to add, move, delete or
schedule anything. The JSON payload has their message plus a small context
snapshot (recent conversation, active goals with next steps, upcoming events).

Rules:
1. Structure the reply as: acknowledge (answer the question or recognize the
   feeling — respond to what they actually said first) → anchor (tie it to
   their words or to a real goal/event from the context) → at most ONE optional
   offer ("Want me to …?"), only if it genuinely fits, never as a done deal.
2. Never claim you created, moved, deleted, scheduled or logged anything in a
   chat reply. GoalCal can write to the calendar through its action flow; never
   claim the application cannot do that. If an action needs a clock time or
   confirmation, ask for that detail.
3. Use only facts from the context. Do not invent events, goals or progress.
4. One to three short sentences. Match the user's language. Use Markdown:
   blank lines between paragraphs and "- " lists for two or more items.
"""


def chat_reply(
    session,
    user_id: int,
    text: str,
    now: datetime,
    tz: str,
    *,
    client=None,
) -> str | None:
    """自由对话回复；未启用、失败或返回空时给 None。"""
    if not settings.llm_ready:
        return None
    payload = {"message": text, "context": context_snapshot(session, user_id, now, tz)}
    system = SYSTEM.format(
        now=now.strftime("%Y-%m-%d %H:%M"), tz=tz, weekday=now.strftime("%A")
    )
    try:
        reply = (client or LLMClient()).complete_text(
            system, json.dumps(payload, ensure_ascii=False)
        )
    except Exception:  # noqa: BLE001 - 闲聊失败必须能回退
        log.warning("LLM fallback or validation failure")
        return None
    return reply.strip() or None


def context_snapshot(session, user_id: int, now: datetime, tz: str) -> dict:
    """喂给对话模型的上下文快照：少量最近消息 + 目标/日程概览（L1 与闲聊共用）。"""
    recent = [
        f"{m['role']}: {m['content'][:120]}"
        for m in conversation.load_recent(session, user_id, limit=6)
    ]
    goals = []
    for goal in goal_svc.active_goals(session, user_id):
        step = goal_svc.next_step(goal)
        goals.append(
            {
                "title": goal.title,
                "progress_pct": round(goal_svc.progress(goal) * 100),
                "next_step": step.title if step is not None else None,
                "days_left": (goal.deadline - now.date()).days if goal.deadline else None,
            }
        )
    start_utc = as_utc_naive(now, tz)
    events = cal.events_between(session, user_id, start_utc, start_utc + timedelta(days=3))
    upcoming = [
        {"title": e.title, "start": as_local(e.start_utc, tz).strftime("%a %b %d %H:%M")}
        for e in events[:8]
    ]
    return {
        "now": now.strftime("%Y-%m-%d %H:%M"),
        "recent_conversation": recent,
        "goals": goals,
        "upcoming_events": upcoming,
    }


def fallback(session, user_id: int, text: str = "") -> str | None:
    chinese = any("\u4e00" <= char <= "\u9fff" for char in text)
    if text.strip().lower().rstrip("!。！") in {"thanks", "thank you", "谢谢", "多谢"}:
        return "不客气。" if chinese else "You're welcome."
    generic = (
        "我在听。你可以继续说说，或告诉我现在最想推进的一件事。"
        if chinese
        else "I'm listening — tell me more, or what you'd like to move forward."
    )
    for goal in goal_svc.active_goals(session, user_id):
        step = goal_svc.next_step(goal)
        if step is None:
            continue
        if chinese:
            suggestion = f"我在听。想推进「{goal.title}」时，我可以帮你为「{step.title}」找时间。"
        else:
            suggestion = (
                f"I'm listening. When you want to move “{goal.title}” forward, "
                f"I can find time for “{step.title}” ({step.est_min} min)."
            )
        # 不机械复读：上一条助手消息就是这句建议时，退到通用回应
        recent = conversation.load_recent(session, user_id, limit=1)
        if recent and recent[-1]["role"] == "assistant" and recent[-1]["content"] == suggestion:
            return generic
        return suggestion
    return generic if chinese else None
