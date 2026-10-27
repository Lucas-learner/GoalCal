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
1. Respond to what they actually said first: answer the question or acknowledge
   the feeling. Understand before suggesting.
2. Offer at most ONE optional next action, only if it genuinely fits, phrased as
   an offer ("Want me to …?"), never as a done deal.
3. Never claim you created, moved, deleted, scheduled or logged anything — you
   cannot act in this mode.
4. Use only facts from the context. Do not invent events, goals or progress.
5. One to three short sentences of plain text. Match the user's language.
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
    payload = {"message": text, "context": _context(session, user_id, now, tz)}
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


def _context(session, user_id: int, now: datetime, tz: str) -> dict:
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
    for goal in goal_svc.active_goals(session, user_id):
        step = goal_svc.next_step(goal)
        if step is not None:
            if chinese:
                return f"我在听。想推进「{goal.title}」时，我可以帮你为「{step.title}」找时间。"
            return (
                f"I'm listening. When you want to move “{goal.title}” forward, "
                f"I can find time for “{step.title}” ({step.est_min} min)."
            )
    if chinese:
        return "我在听。你可以继续说说，或告诉我现在最想推进的一件事。"
    return None
