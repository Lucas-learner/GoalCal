"""统一对话上下文：任何时刻只有一个当前话题。

DialogContext 是 run_turn 路由和 L1 提示词看到的唯一对话状态结构，
替代 pending_action / goal_draft / pending / event_draft / goal_brief /
checkin_context 五层隐式优先级。kind/stage/subject 由 derive() 从持久化的
草稿键确定性地解释出来（不产生第二份需要手工同步的真相）；return_to 栈、
clarify 计数和 offers 是新增状态，随上下文一起存进 conversation.state_json。

offers 是带生命周期的提议记录：助手发出的每个提议（确定性的草稿/候选/预览
和 L1 文本提议 alike）登记为 {id, text, kind, subject, created_ts, expires_ts,
status}。status ∈ open|accepted|declined|expired；模型只能看到 open 提议的
文本，永远不提供或选择 ID——接受/拒绝由代码把"最近一条 open 提议"映射到当前
确定性流程。旧存档里只有 {"text", "ts"} 的记录由 _normalize_offer 补齐。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field


@dataclass
class DialogContext:
    kind: str = "idle"  # idle | pending_action | goal_draft | pending_candidates
    # | pending_batch | event_draft | goal_brief | goal_intake | checkin
    stage: str | None = None  # checkin 阶段等子状态
    subject: dict = field(default_factory=dict)  # 当前话题实体：标题/步骤ID等
    offers: list[dict] = field(default_factory=list)  # 提议记录（含已关闭的少量历史）
    return_to: list[dict] = field(default_factory=list)  # 被打断话题的压栈
    clarify_count: int = 0


MAX_RETURN_TO = 3
MAX_OFFERS = 5  # open 与已关闭的一起的总量上限：只保留少量历史
OFFER_TTL_SECONDS = 600  # 临时生命周期：超时未兑现的提议自动 expired

OFFER_OPEN = "open"
OFFER_ACCEPTED = "accepted"
OFFER_DECLINED = "declined"
OFFER_EXPIRED = "expired"

# 绑定到具体会话状态的提议 kind → 期望的 ctx.kind；状态消失即失效
_STATE_OFFER_KIND = {
    "goal_draft": "goal_draft",
    "candidates": "pending_candidates",
    "batch": "pending_batch",
    "event_draft": "event_draft",
    "recovery": "checkin",
    "checkin_options": "checkin",  # goal_options 三选一：登记但无单一接受映射
    "pending_action": "pending_action",
}


def _normalize_offer(offer: dict, now_ts: float) -> dict:
    """旧存档的 {"text", "ts"} 记录补齐为完整结构；已有字段不动。"""
    created = offer.get("created_ts", offer.get("ts", now_ts))
    return {
        "id": offer.get("id") or uuid.uuid4().hex[:8],
        "text": offer.get("text", ""),
        "kind": offer.get("kind", "chat"),
        "subject": offer.get("subject", {}),
        "intent": offer.get("intent"),  # chat 提议对应的可执行意图（白名单外的视为 None）
        "payload": offer.get("payload") or {},
        "created_ts": created,
        "expires_ts": offer.get("expires_ts", created + OFFER_TTL_SECONDS),
        "status": offer.get("status", OFFER_OPEN),
    }


def expire_offers(ctx: DialogContext, *, now_ts: float | None = None) -> None:
    """把超时的 open 提议标记为 expired（保留在列表里作少量历史）。"""
    now = time.time() if now_ts is None else now_ts
    for offer in ctx.offers:
        expires = offer.get("expires_ts")
        if offer.get("status") == OFFER_OPEN and expires is not None and now >= expires:
            offer["status"] = OFFER_EXPIRED


def register_offer(
    ctx: DialogContext,
    text: str,
    *,
    kind: str = "chat",
    subject: dict | None = None,
    intent: str | None = None,
    payload: dict | None = None,
    ts: float | None = None,
) -> dict | None:
    """登记助手本轮发出的提议；同 kind+subject 的 open 提议刷新而不是堆叠。"""
    text = (text or "").strip()
    if not text:
        return None
    subject = subject or {}
    now = time.time() if ts is None else ts
    for offer in reversed(ctx.offers):
        if (
            offer.get("status") == OFFER_OPEN
            and offer.get("kind", "chat") == kind
            and offer.get("subject", {}) == subject
        ):
            offer["text"] = text
            offer["intent"] = intent
            offer["payload"] = payload or {}
            offer["created_ts"] = now
            offer["expires_ts"] = now + OFFER_TTL_SECONDS
            ctx.offers.remove(offer)
            ctx.offers.append(offer)  # 刷新后视为最新提议
            return offer
    offer = {
        "id": uuid.uuid4().hex[:8],
        "text": text,
        "kind": kind,
        "subject": subject,
        "intent": intent,
        "payload": payload or {},
        "created_ts": now,
        "expires_ts": now + OFFER_TTL_SECONDS,
        "status": OFFER_OPEN,
    }
    ctx.offers.append(offer)
    del ctx.offers[:-MAX_OFFERS]
    return offer


def latest_open_offer(ctx: DialogContext, *, now_ts: float | None = None) -> dict | None:
    """最近一条仍 open 且未过期的提议；没有则 None。"""
    expire_offers(ctx, now_ts=now_ts)
    for offer in reversed(ctx.offers):
        if offer.get("status") == OFFER_OPEN:
            return offer
    return None


def accept_offer(ctx: DialogContext, offer: dict | None = None, *, now_ts: float | None = None) -> dict | None:
    """标记提议被接受；其余 open 提议同时失效（话题已推进）。"""
    offer = offer if offer is not None else latest_open_offer(ctx, now_ts=now_ts)
    if offer is None:
        return None
    offer["status"] = OFFER_ACCEPTED
    for other in ctx.offers:
        if other is not offer and other.get("status") == OFFER_OPEN:
            other["status"] = OFFER_EXPIRED
    return offer


def decline_offer(ctx: DialogContext, offer: dict | None = None, *, now_ts: float | None = None) -> dict | None:
    """标记提议被拒绝；只动这一条，不清草稿等会话状态。

    调用方若确认用户回绝了整轮提议（reject_offer），再补 close_open_offers。
    """
    offer = offer if offer is not None else latest_open_offer(ctx, now_ts=now_ts)
    if offer is not None:
        offer["status"] = OFFER_DECLINED
    return offer


def close_open_offers(ctx: DialogContext) -> None:
    """用户另起话题：所有 open 提议失效。"""
    for offer in ctx.offers:
        if offer.get("status") == OFFER_OPEN:
            offer["status"] = OFFER_EXPIRED


def _expire_orphan_state_offers(ctx: DialogContext) -> None:
    """状态绑定提议的一致性守卫：对应会话状态已消失（取消/确认/重置）即失效。

    derive() 是唯一解释点，因此任何路径（按钮、逐字短路、会话重置）清掉状态，
    下一次 derive 都会把残留的 open 提议关掉，不会接受错流程。
    """
    for offer in ctx.offers:
        if offer.get("status") != OFFER_OPEN:
            continue
        kind = offer.get("kind")
        expected = _STATE_OFFER_KIND.get(kind)
        if expected is None:
            continue
        if kind == "recovery":
            # 恢复候选同时落在 checkin(recovery) 与 pending(origin=recovery) 两处状态
            alive = (ctx.kind == "checkin" and ctx.stage == "recovery") or (
                ctx.kind == "pending_candidates" and ctx.subject.get("origin") == "recovery"
            )
        else:
            alive = ctx.kind == expected
        if not alive:
            offer["status"] = OFFER_EXPIRED


def derive(state, prev: DialogContext | None = None, *, now_ts: float | None = None) -> DialogContext:
    """从持久化状态键解释当前话题；return_to/clarify_count/offers 从上一轮继承。

    state 只需支持 .get：controller 传 st.session_state，测试传普通 dict。
    offers 继承时规范化旧记录、按 TTL 标记过期、并按当前话题关掉状态已消失的
    提议；subject.topic（L1 写的话题摘要）仅在 kind 不变时继承，话题切换即清理。
    """
    ctx = DialogContext()
    if prev is not None:
        ctx.return_to = list(prev.return_to)
        ctx.clarify_count = prev.clarify_count
        now = time.time() if now_ts is None else now_ts
        ctx.offers = [_normalize_offer(offer, now) for offer in prev.offers]
        expire_offers(ctx, now_ts=now)
        del ctx.offers[:-MAX_OFFERS]

    pending_action = state.get("pending_action")
    if pending_action is not None:
        action = pending_action.action
        ctx.kind = "pending_action"
        ctx.subject = {"intent": action.intent, "target": action.target or action.title}
    elif state.get("goal_draft") is not None:
        ctx.kind = "goal_draft"
        ctx.subject = {"title": state["goal_draft"].brief.title}
    elif state.get("pending"):
        pending = state["pending"]
        if pending.get("batch") is not None:
            ctx.kind = "pending_batch"
        else:
            ctx.kind = "pending_candidates"
            ctx.subject = {"step_id": pending.get("step_id"), "origin": pending.get("origin")}
    elif state.get("event_draft") is not None:
        ctx.kind = "event_draft"
        ctx.subject = {"titles": [a.title for a in state["event_draft"].events][:4]}
    elif state.get("goal_brief") is not None:
        ctx.kind = "goal_brief"
        ctx.subject = {"title": state["goal_brief"].title}
    elif state.get("goal_intake"):
        ctx.kind = "goal_intake"
    elif state.get("checkin_context") is not None:
        checkin = state["checkin_context"]
        ctx.kind = "checkin"
        ctx.stage = checkin.stage
        ctx.subject = {"step_id": checkin.step_id, "goal_id": checkin.goal_id}
    _expire_orphan_state_offers(ctx)
    if (
        prev is not None
        and prev.kind == ctx.kind
        and prev.subject.get("topic")
        and "topic" not in ctx.subject
    ):
        ctx.subject["topic"] = prev.subject["topic"]
    return ctx


def push_return_to(ctx: DialogContext, kind: str, **payload) -> None:
    """打断当前话题时压栈，供“我想恢复”弹栈续聊；最多保留最近几层。"""
    ctx.return_to.append({"kind": kind, **payload})
    del ctx.return_to[:-MAX_RETURN_TO]
