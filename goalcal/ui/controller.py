from __future__ import annotations

import re
import time as time_mod
from datetime import datetime, time, timedelta

import streamlit as st
from sqlalchemy import select

from .. import agent, conversation, dialog
from ..domain import calendar_service as cal
from ..domain import goals as goal_svc
from ..domain import planner
from ..domain.scheduler import Candidate
from ..llm import turn as turn_engine
from ..llm.action import Action, Intent
from ..llm.natdate import extract_time, extract_window, find_dates, has_explicit_clock
from ..llm.router import turn_client
from ..llm.rules import ADVICE_QUESTION, NO_SCHEDULE, calendar_write_request, parse_with_rules
from ..models import Event, Goal, Step
from ..timeutil import as_local, as_utc_naive
from . import runtime

# chat 提议允许携带的可执行意图白名单（与 Intent 同集；模型仍不能给 ID/日期）
_OFFER_INTENTS = frozenset(Intent.__args__)


def _in_chinese(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)

STATE_KEYS = (
    "pending",
    "pending_action",
    "goal_brief",
    "goal_draft",
    "goal_intake",
    "event_draft",
    "checkin_context",
    "replan_draft",
    "dialog_context",
)


def _persist() -> None:
    if not st.session_state.get("auth_token"):
        return
    _refresh_context()  # 所有 UI 入口（schedule_step/accept/confirm…）都在这里同步对话上下文
    if any(message["role"] == "user" for message in st.session_state["messages"]):
        st.session_state["has_user_history"] = True
        st.session_state["chat_scroll_to_bottom"] = True
    messages = st.session_state["messages"]
    has_new_messages = any(not message.get("client_id") for message in messages)
    state = {key: st.session_state.get(key) for key in STATE_KEYS}
    with runtime.session_scope("conversation.save") as session:
        conversation.save_state(session, runtime.user_id(), messages, state)
    if has_new_messages and len(messages) > conversation.PAGE_SIZE:
        st.session_state["messages"] = messages[-conversation.PAGE_SIZE:]
        st.session_state["chat_has_more"] = True


def ensure_state() -> None:
    st.session_state.setdefault("messages", [])
    st.session_state.setdefault("has_user_history", False)
    st.session_state.setdefault("chat_has_more", False)
    for key in STATE_KEYS:
        st.session_state.setdefault(key, None)
    if st.session_state.get("conversation_loaded"):
        return
    # 整页重进/新浏览器会话：清空旧消息与任务状态，回到指引首页。
    # 应用内切换页面（Goals/Calendar/Settings → Chat）保持同一 session_state，
    # conversation_loaded 仍在，不会触发这里的重置。
    with runtime.session_scope("conversation.reset") as session:
        conversation.reset_state(session, runtime.user_id())
    _fresh_chat_state()
    _persist()


def _fresh_chat_state() -> None:
    """Reset only conversation UI state; account, goals and calendar remain intact."""
    st.session_state["messages"] = [
        {"role": "assistant", "content": "Hi — tell me a goal, or add something to your calendar."}
    ]
    st.session_state["chat_has_more"] = False
    st.session_state["has_user_history"] = False
    st.session_state["chat_scroll_to_bottom"] = False
    for key in STATE_KEYS:
        st.session_state[key] = None
    st.session_state["conversation_loaded"] = True
    st.session_state.pop("chat_anchor", None)
    st.session_state.pop("last_parser", None)
    # 刚清空的首次渲染不自动签到：指引不被过期块的提问压住；
    # 用户发出第一条消息后 run_turn 内的签到逻辑照常恢复
    st.session_state["checkin_suppressed_once"] = True
    st.session_state["checkin_suppressed_until_user"] = False


def new_conversation(*, open_chat: bool = False) -> None:
    """Delete persisted chat history and pending dialog work for this user."""
    with runtime.session_scope("conversation.new") as session:
        conversation.reset_state(session, runtime.user_id())
    _fresh_chat_state()
    _persist()
    if open_chat:
        st.switch_page(st.session_state["chat_page"])
    st.rerun()


def begin_goal() -> None:
    for key in (
        "pending", "pending_action", "goal_brief", "goal_draft",
        "event_draft", "checkin_context", "replan_draft",
    ):
        st.session_state[key] = None
    st.session_state["goal_intake"] = True
    st.session_state["messages"].append({
        "role": "assistant",
        "content": "What would you like to achieve? Tell me the goal, and a deadline if you have one.",
    })
    _persist()


def load_earlier() -> None:
    """向前一页 prepend 更早的消息，记录原首条为滚动锚点，不回到底部。"""
    messages = st.session_state.get("messages") or []
    anchor = next((message for message in messages if message.get("id")), None)
    with runtime.session_scope("conversation.load_earlier") as session:
        if anchor is None:
            # 会话内截断（_persist）后内存消息只有 client_id 没有数据库 id：
            # 先从数据库重取最新一页恢复锚点，否则按钮点击无响应
            latest = conversation.load_page(session, runtime.user_id())
            messages = latest["messages"]
            if not messages:
                st.session_state["chat_has_more"] = False
                return
            st.session_state["messages"] = messages
            anchor = messages[0]
        page = conversation.load_page(session, runtime.user_id(), before_id=anchor["id"])
    if not page["messages"]:
        st.session_state["chat_has_more"] = False
        return
    st.session_state["messages"] = page["messages"] + messages
    st.session_state["chat_has_more"] = page["has_more"]
    st.session_state["chat_anchor"] = anchor.get("client_id") or f"db_{anchor['id']}"
    st.rerun()


def _record_reply(reply) -> None:
    st.session_state["messages"].append({"role": "assistant", "content": reply.text})
    st.session_state["goal_brief"] = reply.goal_brief
    st.session_state["goal_draft"] = getattr(reply, "goal_draft", None)
    st.session_state["event_draft"] = getattr(reply, "event_draft", None)
    st.session_state["pending_action"] = getattr(reply, "pending_action", None)
    st.session_state["last_parser"] = reply.parser
    ctx = reply.checkin_context
    origin = "recovery" if ctx is not None and ctx.stage == "recovery" else "chat"
    if reply.batch:
        st.session_state["pending"] = {"batch": reply.batch, "origin": "goal"}
    elif reply.candidates:
        st.session_state["pending"] = {
            "candidates": reply.candidates,
            "step_id": reply.step_id,
            "origin": origin,
        }
    else:
        st.session_state["pending"] = None
    _register_reply_offers(reply)


def _register_reply_offers(reply) -> None:
    """把本轮回复里的真实提议统一登记到 DialogContext.offers（唯一登记点）。

    覆盖确定性流程（目标草稿/候选时段/批量/日历预览/危险动作确认/恢复候选），
    L1 文本提议已在 _apply_decision 里登记。同 kind+subject 刷新而非堆叠；
    状态已消失的 open 提议由下一次 dialog.derive 统一关掉。
    """
    ctx = st.session_state.get("dialog_context")
    if ctx is None:
        return
    now_ts = time_mod.time()
    checkin = getattr(reply, "checkin_context", None)
    draft = getattr(reply, "goal_draft", None)
    if draft is not None:
        dialog.register_offer(
            ctx, f'Review the draft for "{draft.brief.title}" — create it?',
            kind="goal_draft", subject={"title": draft.brief.title}, ts=now_ts,
        )
    if getattr(reply, "event_draft", None) is not None:
        dialog.register_offer(ctx, "Add the previewed events to your calendar?",
                              kind="event_draft", ts=now_ts)
    if getattr(reply, "pending_action", None) is not None:
        action = reply.pending_action.action
        dialog.register_offer(
            ctx, f"Confirm {action.intent}?", kind="pending_action",
            subject={"intent": action.intent, "target": action.target or action.title},
            ts=now_ts,
        )
    if getattr(reply, "batch", None):
        dialog.register_offer(ctx, "Book them all?", kind="batch", ts=now_ts)
    elif getattr(reply, "candidates", None):
        if checkin is not None and checkin.stage == "recovery":
            dialog.register_offer(
                ctx, "Take the first suggested slot?", kind="recovery",
                subject={"step_id": checkin.step_id}, ts=now_ts,
            )
        else:
            dialog.register_offer(
                ctx, "Choose one of the suggested times?", kind="candidates",
                subject={"step_id": getattr(reply, "step_id", None)}, ts=now_ts,
            )
    if checkin is not None and checkin.stage == "goal_options":
        # 三选一（延后/暂停/归档）：登记供 reject 识别，但不映射"好"为单一接受
        dialog.register_offer(
            ctx, "Put it off, pause, or archive — pick one?", kind="checkin_options",
            subject={"goal_id": checkin.goal_id}, ts=now_ts,
        )


def start_checkin_if_needed() -> None:
    if st.session_state.pop("checkin_suppressed_once", False):
        return  # 会话刚被整页重置：首次渲染把指引留给用户，不用过期块提问压住
    if st.session_state.get("checkin_suppressed_until_user"):
        return  # 用户刚结束一次签到：在用户下次发消息前（run_turn 消费）不自动重问
    if any(st.session_state.get(key) is not None for key in (
        "checkin_context", "goal_brief", "goal_draft", "goal_intake",
        "event_draft", "pending", "pending_action",
    )):
        return
    with runtime.session_scope("start_checkin_if_needed") as session:
        ctx, question = agent.begin_checkin(session, runtime.user_id(), runtime.now(), runtime.timezone())
    if ctx is not None:
        st.session_state["checkin_context"] = ctx
        st.session_state["messages"].append({"role": "assistant", "content": question})
        _persist()


def confirm_goal_draft(schedule_first: bool, *, from_chat: bool = False) -> None:
    draft = st.session_state.get("goal_draft")
    if draft is None:
        return
    if not from_chat:
        st.session_state["messages"].append({
            "role": "user", "content": "Create & schedule" if schedule_first else "Save goal"
        })
    with runtime.session_scope("goal_draft.confirm") as session:
        reply = agent.confirm_goal_draft(
            session, runtime.user_id(), draft, schedule_first, runtime.now(), runtime.timezone()
        )
    _record_reply(reply)
    _persist()
    st.rerun()


def cancel_goal_draft() -> None:
    if st.session_state.get("goal_draft") is None:
        return
    st.session_state["goal_draft"] = None
    st.session_state["messages"].append({"role": "assistant", "content": "Draft discarded. No goal was created."})
    _persist()
    st.rerun()


def confirm_event_draft(approved: bool) -> None:
    draft = st.session_state.get("event_draft")
    if draft is None:
        return
    # 先清状态再写库：按钮/文字重复触发不会二次执行同一草稿
    st.session_state["event_draft"] = None
    with runtime.session_scope("event_draft.confirm") as session:
        reply = agent.confirm_event_draft(
            session, runtime.user_id(), draft, approved, runtime.now(), runtime.timezone()
        )
    _record_reply(reply)
    _persist()
    st.rerun()


def _event_draft_verbatim(text: str) -> bool:
    """日历预览显示中：逐字确认/取消是零成本短路，不调模型。"""
    if st.session_state.get("event_draft") is None:
        return False
    decision = text.strip().lower().strip("。.!！")
    if re.fullmatch(
        r"添加|确认添加|确认|好|好的|嗯|可以|安排吧|加吧|"
        r"yes|confirm|ok|okay|go ahead|add( them| it| all)?( to( my)? calendar)?",
        decision,
    ):
        confirm_event_draft(True)
        return True
    if re.fullmatch(r"取消|算了|不用|先不|不要|不需要|no|nope|cancel|never mind", decision):
        confirm_event_draft(False)
        return True
    return False


_ORDINALS = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5,
    "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5,
    "first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4, "sixth": 5,
    "one": 0, "two": 1, "three": 2, "four": 3, "five": 4, "six": 5,
}


def _pending_verbatim(text: str) -> bool:
    """候选时段显示中：序号选择与时间窗口重滤是零成本短路，不调模型。"""
    pending = st.session_state.get("pending")
    if not pending or pending.get("batch"):
        return False
    normalized = text.strip().lower().strip("。.!！")
    match = re.fullmatch(
        r"(?:第)?([一二三四五六]|[0-9]+|first|second|third|fourth|fifth|sixth|one|two|three|four|five|six)(?:个|张|条|项)?",
        normalized,
    )
    if match:
        index = _ORDINALS.get(match.group(1))
        candidates = pending["candidates"]
        if index is None or index >= len(candidates):
            st.session_state["messages"].append({
                "role": "assistant", "content": f"Choose a number from 1 to {len(candidates)}."
            })
            _persist()
            return True
        if pending.get("origin") == "recovery":
            accept_recovery(pending["step_id"], candidates[index])
        else:
            accept(pending["step_id"], candidates[index])
        return True
    if re.fullmatch(
        r"取消|算了|不用了?|先不|不要|不需要|cancel|never mind|no thanks|skip( it)?",
        normalized,
    ):
        # 与拒绝提议同语义：退出候选流程；恢复候选走 decline_recovery 推进队列
        if pending.get("origin") == "recovery" and st.session_state.get("checkin_context") is not None:
            dismiss_recovery()
            return True
        st.session_state["pending"] = None
        st.session_state["messages"].append(
            {"role": "assistant", "content": "OK — I've put those time suggestions away."}
        )
        _persist()
        return True
    now = runtime.now()
    action = parse_with_rules(text, now)
    dates = find_dates(text, now)
    exact_time_reply = (
        action.intent == "schedule_step"
        or (action.intent == "add_event" and action.title == "Untitled")
        or (action.intent == "move_event" and action.target in {"改到", "换到", "改成"})
    )
    if has_explicit_clock(text) and exact_time_reply:
        hour, minute = extract_time(text)
        day = dates[-1][1].date() if dates else now.date()
        start = datetime.combine(day, time(hour, minute), tzinfo=now.tzinfo)
        if start <= now:
            message = ("这个时间已经过去了，请选一个更晚的时间。" if _in_chinese(text)
                       else "That time has already passed. Choose a later time.")
            st.session_state["messages"].append({"role": "assistant", "content": message})
            _persist()
            return True
        with runtime.session_scope("pending.exact_time") as session:
            step = _owned_step(session, pending["step_id"])
            minutes = step.est_min if step is not None else None
        if minutes is None:
            st.session_state["pending"] = None
            st.session_state["messages"].append({
                "role": "assistant", "content": "That step no longer exists. Nothing was booked."
            })
            _persist()
            return True
        candidate = Candidate(
            start=start,
            end=start + timedelta(minutes=minutes),
            score=1.0,
            reasons=["You picked this exact time."],
        )
        if pending.get("origin") == "recovery":
            accept_recovery(pending["step_id"], candidate)
        else:
            accept(pending["step_id"], candidate)
        return True
    window = extract_window(text, now)
    if re.fullmatch(r"(?:this|the)?\s*weekend|本周末|这个周末|这周末", normalized):
        window = _chip_window("weekend", now)
    if window and dates:
        day = dates[-1][1].date()
        window = (
            window[0].replace(year=day.year, month=day.month, day=day.day),
            window[1].replace(year=day.year, month=day.month, day=day.day),
        )
    window_reply = (
        action.intent not in agent.NEW_COMMAND_INTENTS
        or (action.intent == "add_event" and action.title == "Untitled")
        or (action.intent == "move_event" and action.target in {"改到", "换到", "改成"})
    )
    if window and window_reply:
        with runtime.session_scope("pending.refilter") as session:
            candidates, _ = agent.propose_for_step(
                session, runtime.user_id(), pending["step_id"], runtime.now(), runtime.timezone(), window=window
            )
        if candidates:
            pending["candidates"] = candidates
            message = "Updated the suggested times."
        else:
            message = "Nothing free in that window — kept the current suggestions."
        st.session_state["messages"].append({"role": "assistant", "content": message})
        _persist()
        return True
    return False


# 草稿/预览流程里算“明确新指令”的意图：命中即退出预览，落到通用执行。
# 与 rules.NEW_COMMAND_INTENTS 同源，另加状态/进度两类。
_DRAFT_EXIT_INTENTS = agent.NEW_COMMAND_INTENTS | {"set_state", "log_progress"}


def _goal_draft_verbatim(text: str) -> bool:
    """目标草稿显示中：逐字确认/保存/取消是零成本短路，不调模型。"""
    if st.session_state.get("goal_draft") is None:
        return False
    decision = text.strip().lower()
    if re.fullmatch(r"创建并排期|确认创建|create( & schedule)?|create and schedule", decision):
        confirm_goal_draft(True, from_chat=True)
        return True
    if re.fullmatch(r"仅保存|保存目标|save( goal)?", decision):
        confirm_goal_draft(False, from_chat=True)
        return True
    if re.fullmatch(r"取消|算了|不建了|不用了?|先不|不要|cancel|never mind|no thanks", decision):
        cancel_goal_draft()
        return True
    return False


def _goal_intake_turn(text: str) -> bool:
    if not st.session_state.get("goal_intake"):
        return False
    if planner.cancellation_requested(text):
        st.session_state["goal_intake"] = None
        st.session_state["messages"].append({
            "role": "assistant", "content": "Okay — no goal was created."
        })
        _persist()
        return True
    now = runtime.now()
    parsed = parse_with_rules(text, now)
    if parsed.intent != "create_goal":
        guided = ("我想要" + text) if _in_chinese(text) else ("I want to " + text)
        parsed = parse_with_rules(guided, now)
    if parsed.intent != "create_goal":
        parsed = Action(intent="create_goal", title=text.strip(), raw=text, confidence=0.9)
    parsed.raw = text
    st.session_state["goal_intake"] = None
    with runtime.session_scope("goal.intake") as session:
        reply = agent.execute(
            session, runtime.user_id(), parsed.normalized(now), "goal:intake",
            now, runtime.timezone(),
        )
    _record_reply(reply)
    _persist()
    return True


def _revise_goal_draft(text: str, now, tz, *, use_llm: bool = True) -> str:
    draft = st.session_state["goal_draft"]
    revised = agent.revise_goal_draft(draft, text, now, tz, use_llm=use_llm)
    if revised is None:
        return "I couldn't revise the preview this time. Your current draft is unchanged."
    st.session_state["goal_draft"] = revised
    return (f"Plan rebuilt for the {revised.deadline:%b %d, %Y} deadline. "
            "Review it below, then create it or keep adjusting.")


def _refresh_context() -> dialog.DialogContext:
    """把分散的草稿键解释成唯一权威 DialogContext；return_to/clarify 计数继承自上一轮。"""
    ctx = dialog.derive(
        st.session_state, st.session_state.get("dialog_context"), now_ts=time_mod.time()
    )
    st.session_state["dialog_context"] = ctx
    return ctx


def _rules_turn(session, text: str, now, tz, *, llm_attempted: bool):
    """规则降级路径：L1 未配置或失败时使用，行为与旧管线一致。

    返回 (reply, follow_up, preserved)；preserved=True 表示 reply 是字符串、
    当前草稿/候选状态保持不变（不经过 _record_reply 的状态清算）。
    llm_attempted=True（L1 已尝试但失败）时整轮不再调任何 LLM：
    解析走规则，建目标/改草稿走确定性模板。
    """
    allow_llm = not llm_attempted
    use_llm = None if allow_llm else False
    uid = runtime.user_id()
    dctx = st.session_state.get("dialog_context")
    answer = text.strip().lower().strip("。.!！")
    offer = dialog.latest_open_offer(dctx) if dctx is not None else None
    if offer is not None and answer in {"好", "好的", "可以", "行", "确认", "yes", "sure", "yes please", "go ahead"}:
        result = _accept_offer(session, uid, dctx, offer, text, now, tz, use_llm=allow_llm)
        if result is not None and result is not _UNHANDLED:
            _mark_offer_accepted(dctx, offer)
            return result
    if offer is not None and _generic_calendar_acceptance(text, now, offer):
        result = _accept_offer(session, uid, dctx, offer, text, now, tz, use_llm=allow_llm)
        if result is not None and result is not _UNHANDLED:
            _mark_offer_accepted(dctx, offer)
            return result
    if offer is not None and answer in {"算了", "不用", "先不", "不需要", "no thanks", "never mind"}:
        decline = turn_engine.TurnDecision(relation="reject_offer", intent="chat", reply="")
        return _reject_offer(session, uid, dctx, decline, now, tz)
    if st.session_state.get("pending_action") is not None:
        if parse_with_rules(text, now).intent in _DRAFT_EXIT_INTENTS:
            st.session_state["pending_action"] = None
        else:
            return "Please confirm or cancel the pending change.", None, True
    if st.session_state.get("goal_draft") is not None:
        action = parse_with_rules(text, now)
        if action.intent in _DRAFT_EXIT_INTENTS:
            st.session_state["goal_draft"] = None
        else:
            return _revise_goal_draft(text, now, tz, use_llm=allow_llm), None, True
    pending = st.session_state.get("pending")
    if pending and not pending.get("batch") and calendar_write_request(text):
        return ("请先选一个建议时段，我再写进日历。" if _in_chinese(text)
                else "Choose one of the suggested times first, then I'll book it."), None, True
    if pending and not pending.get("batch") and not agent.looks_like_new_command(text, now):
        return "Pick 1, 2 or 3, or tell me another time window.", None, True
    if st.session_state.get("event_draft") is not None:
        if agent.looks_like_new_command(text, now):
            st.session_state["event_draft"] = None
        else:
            return (
                "The calendar preview above isn't saved yet — "
                'say "add to calendar" to confirm, or "cancel" to drop it.'
            ), None, True
    brief = st.session_state.get("goal_brief")
    if brief is not None and agent.looks_like_new_command(text, now):
        brief = None
        st.session_state["goal_brief"] = None
    ctx = st.session_state.get("checkin_context")
    follow_up = None
    if brief is not None:
        reply = agent.continue_goal(session, uid, brief, text, now, tz, use_llm=allow_llm)
    elif ctx is not None:
        reply = agent.continue_checkin(session, uid, ctx, text, now, tz, use_llm=use_llm)
        if reply is None:
            reply = agent.handle(session, uid, text, now, tz, use_llm=use_llm)
            follow_up = agent.checkin_prompt(session, ctx, now, tz) or None
        else:
            st.session_state["checkin_context"] = reply.checkin_context
    else:
        parsed = parse_with_rules(text, now)
        if parsed.intent in {"schedule_step", "refine_steps"}:
            named = _named_goal(session, uid, text)
            if named is not None and goal_svc.next_step(named) is not None:
                parsed.progress_target = goal_svc.next_step(named).title
        reply = _existing_goal_desire(session, uid, parsed, text, now)
        if reply is None:
            if parsed.intent == "schedule_step":
                if parsed.window is None and parsed.start is None:
                    parsed.window = _inherit_schedule_window(session, uid, now)
                if not _bind_schedule_target(session, uid, parsed, text):
                    return _schedule_target_question(text)
                reply = agent.execute(session, uid, parsed.normalized(now), "rules:v1", now, tz,
                                      use_llm=allow_llm)
            else:
                reply = agent.handle(session, uid, text, now, tz, use_llm=use_llm)
    return reply, follow_up, False


def _existing_goal_desire(session, uid, action: Action, text: str, now):
    """A short wish matching an existing goal's next step is not a new goal."""
    if action.intent != "create_goal" or action.deadline is not None:
        return None
    title = (action.title or "").strip().casefold()
    if len(title) < 2:
        return None
    matches = []
    for goal in goal_svc.active_goals(session, uid):
        step = goal_svc.next_step(goal)
        if step is not None and (title in goal.title.casefold() or title in step.title.casefold()):
            matches.append((goal, step))
    if len(matches) != 1:
        return None
    goal, step = matches[0]
    chinese = _in_chinese(text)
    offer_text = (
        f"要我给「{goal.title}」的下一步找个时间吗？" if chinese
        else f'Want me to find time for the next step of “{goal.title}”?'
    )
    dctx = st.session_state.get("dialog_context")
    if dctx is not None:
        prior_period = _schedule_window_source(session, uid, now)
        dialog.register_offer(
            dctx, offer_text, kind="chat", intent="schedule_step",
            payload={"goal_id": goal.id, "step_id": step.id,
                     "when_text": prior_period or text},
        )
    message = (
        f"这可以接着「{goal.title}」推进。{offer_text}" if chinese
        else f'You can pick up “{goal.title}”. {offer_text}'
    )
    return agent.Reply(message, Action(intent="chat", raw=text, confidence=0.5), "rules:context")


def _bind_schedule_target(session, uid, action: Action, *fragments: str) -> bool:
    """Bind a generic scheduling request to one real step, or report ambiguity."""
    if action.intent != "schedule_step" or action.progress_target or action.title:
        return True
    goals = goal_svc.active_goals(session, uid)
    if len(goals) <= 1:
        return True
    context = " ".join(part for part in fragments if part).casefold()
    matches = []
    for goal in goals:
        step = goal_svc.next_step(goal)
        if step is not None and (
            goal.title.casefold() in context or step.title.casefold() in context
        ):
            matches.append(step)
    if len(matches) == 1:
        action.progress_target = matches[0].title
        return True
    return False


def _schedule_target_question(text: str):
    prompt = ("想安排哪个目标？请说出目标或下一步。" if _in_chinese(text)
              else "Which goal should I schedule? Name the goal or its next step.")
    dctx = st.session_state.get("dialog_context")
    if dctx is not None:
        dialog.register_offer(
            dctx, prompt, kind="chat", intent="schedule_step",
            payload={"awaiting_target": True, "when_text": text},
        )
    return prompt, None, True


_GOAL_WORDS = {"learn", "goal", "finish", "complete", "practice", "start", "first", "my", "the"}
_GOAL_BOOKING = re.compile(r"\b(book|schedule|find time for)\b|安排|排期|找时间", re.I)
_DEADLINE_CHANGE = re.compile(r"\b(deadline|timeline|due date)\b|期限|截止", re.I)


def _named_goal(session, uid, text: str):
    """Resolve a goal explicitly named by the user, including distinctive short names."""
    lowered = text.casefold()
    goals = goal_svc.active_goals(session, uid)
    matched = []
    for goal in goals:
        title = goal.title.casefold()
        words = re.findall(r"[a-z0-9]+", title)
        tokens = [word for word in words if len(word) >= 4 and word not in _GOAL_WORDS]
        chinese = re.findall(r"[\u4e00-\u9fff]+", title)
        tokens.extend(chunk[-2:] for chunk in chinese if len(chunk) >= 2)
        if title in lowered or any(re.search(rf"\b{re.escape(token)}\b", lowered)
                                   for token in tokens if re.fullmatch(r"[a-z0-9]+", token)) \
                or any(token in lowered for token in tokens if not re.fullmatch(r"[a-z0-9]+", token)):
            matched.append(goal)
    return matched[0] if len(matched) == 1 else None


def _schedule_window_source(session, uid, now):
    """Find a period in the immediately preceding advice or choice exchange."""
    recent = conversation.load_recent(session, uid, limit=2)
    if not recent or recent[-1]["role"] != "assistant":
        return None
    assistant_text = recent[-1]["content"]
    choice_question = ("?" in assistant_text or "？" in assistant_text) and bool(re.search(
        r"哪一个|哪个|选|挑|which|choose|pick", assistant_text, re.I
    ))
    prior_advice = (len(recent) == 2 and recent[0]["role"] == "user"
                    and bool(ADVICE_QUESTION.search(recent[0]["content"])))
    if not (choice_question or prior_advice):
        return None
    for message in reversed(recent):
        if extract_window(message["content"], now) is not None:
            return message["content"]
    return None


def _inherit_schedule_window(session, uid, now):
    source = _schedule_window_source(session, uid, now)
    return extract_window(source, now) if source else None


_L1_PARSER = f"l1:{turn_engine.PROMPT_VERSION}"


# offer.kind 为 "chat"（L1 文本提议）且无可执行 intent 时，按当前话题映射到具体流程
_CHAT_OFFER_FLOW = {
    "goal_draft": "goal_draft",
    "pending_candidates": "candidates",
    "pending_batch": "batch",
    "event_draft": "event_draft",
    "checkin": "recovery",
}

_OFFER_STALE = (
    "That offer is no longer on the table — nothing was changed. "
    "Tell me what you'd like to do and I'll pick it up."
)

_OFFER_NONE = (
    "There's no open offer to accept right now — "
    "if you meant one of the options above, just name it."
)

# 提议无单一确定性接受映射（如 goal_options 三选一）：按 in_flow 走当前流程
_UNHANDLED = object()


def _mark_offer_accepted(dctx, offer: dict) -> None:
    if not (offer.get("kind") == "chat" and offer.get("intent") in {"schedule_step", "refine_steps"}
            and not (offer.get("payload") or {}).get("step_id")):
        dialog.accept_offer(dctx, offer)


def _bind_pending_offer_target(session, uid, offer: dict, text: str, now, tz) -> bool:
    payload = offer.get("payload") or {}
    if not payload.get("awaiting_target"):
        return False
    hint = text.strip().casefold()
    matches = []
    for goal in goal_svc.active_goals(session, uid):
        step = agent.next_unscheduled_step(session, uid, goal, now, tz)
        if step is not None and (goal.title.casefold() in hint or step.title.casefold() in hint):
            matches.append((goal, step))
    if not matches:
        named = _named_goal(session, uid, text)
        if named is not None:
            step = agent.next_unscheduled_step(session, uid, named, now, tz)
            if step is not None:
                matches.append((named, step))
    if len(matches) != 1:
        return False
    goal, step = matches[0]
    payload.update(goal_id=goal.id, step_id=step.id)
    payload.pop("awaiting_target", None)
    offer["payload"] = payload
    return True


def _generic_calendar_acceptance(text: str, now, offer: dict | None) -> bool:
    """'Put it in my calendar' accepts an open scheduling offer if no new target is named."""
    return bool(
        offer and offer.get("kind") == "chat" and offer.get("intent") == "schedule_step"
        and calendar_write_request(text) and not has_explicit_clock(text)
        and parse_with_rules(text, now).title in {None, "Untitled"}
    )


def _accept_offer(session, uid, dctx, offer, text, now, tz, *, use_llm: bool = True):
    """把最近 open 提议映射到当前确定性流程执行。

    返回 (reply, follow_up, preserved)；提议与实际状态不匹配时返回 None，
    由调用方把提议失效并诚实提示，绝不执行错流程；无单一映射返回 _UNHANDLED。
    """
    kind = offer.get("kind", "chat")
    if kind == "chat":
        intent = offer.get("intent")
        payload = offer.get("payload") or {}
        if intent in {"schedule_step", "refine_steps"}:
            step_id = payload.get("step_id")
            if step_id is None:
                payload["awaiting_target"] = True
                offer["payload"] = payload
                prompt = ("想安排哪个目标？说出目标名称，我会按它的下一步找时间。"
                          if _in_chinese(text) else
                          "Which goal should I find time for? Name it and I'll use that one.")
                return prompt, None, True
            step = session.get(Step, step_id)
            if (step is None or step.goal.user_id != uid or step.goal.status != "active"
                    or step.status != "todo" or step.goal_id != payload.get("goal_id")):
                return None
            if intent == "refine_steps":
                action = Action(intent="refine_steps", raw=offer.get("text", ""),
                                progress_target=step.title, confidence=0.8)
                return agent.execute(session, uid, action, _L1_PARSER, now, tz,
                                     use_llm=use_llm), None, False
            window = extract_window(payload.get("when_text") or "", now)
            candidates, _ = agent.propose_for_step(session, uid, step_id, now, tz, window=window)
            action = Action(intent="schedule_step", raw=text, confidence=0.8)
            if not candidates:
                message = (f"这段时间没有适合「{step.title}」的空档，换个时间试试。"
                           if _in_chinese(text) else
                           f'I couldn\'t find a free time for “{step.title}” in that window. '
                           "Tell me another time to try.")
                return agent.Reply(
                    message, action, _L1_PARSER,
                ), None, False
            message = (f"为「{step.title}」找到了 {len(candidates)} 个时段。选一个我再写进日历。"
                       if _in_chinese(text) else
                       f'Here are {len(candidates)} times for “{step.title}”. '
                       "Pick one and I'll book it.")
            return agent.Reply(
                message, action, _L1_PARSER, candidates=candidates,
                goal_id=step.goal_id, step_id=step.id,
            ), None, False
        if intent in _OFFER_INTENTS:
            # 可执行闲聊提议（"要不要我帮你找时间？"）：用 intent 构造 Action 走白名单
            # 执行；危险动作与 L1 决策同口径——压置信度，强制 pending_action 确认
            action = Action(
                intent=intent, raw=offer.get("text", ""), confidence=0.8,
                title=payload.get("title"), when_text=payload.get("when_text"),
            )
            if intent == "add_event":
                action = turn_engine.to_action(
                    turn_engine.TurnDecision(relation="new_command", intent="add_event",
                                             reply="", title=action.title,
                                             when_text=action.when_text),
                    action.raw, now,
                )
            if intent in agent.RISKY_INTENTS:
                action.confidence = 0.4
            return agent.execute(session, uid, action, _L1_PARSER, now, tz,
                                 use_llm=use_llm), None, False
        if dctx.kind == "checkin":
            checkin = st.session_state.get("checkin_context")
            if checkin is not None and checkin.stage == "recovery":
                kind = "recovery"
            elif checkin is not None and checkin.stage == "goal_options":
                kind = "checkin_options"
            else:
                return None
        else:
            kind = _CHAT_OFFER_FLOW.get(dctx.kind)
        if kind is None:
            return None
    if kind == "checkin_options":
        return _UNHANDLED
    if kind == "pending_action":
        pending_action = st.session_state.get("pending_action")
        if pending_action is not None:
            st.session_state["pending_action"] = None
            return agent.confirm_action(session, uid, pending_action, True, now, tz), None, False
    if kind == "goal_draft":
        draft = st.session_state.get("goal_draft")
        if draft is not None:
            return agent.confirm_goal_draft(session, uid, draft, True, now, tz), None, False
    elif kind == "candidates":
        pending = st.session_state.get("pending")
        if pending and not pending.get("batch") and pending.get("candidates"):
            prompt = ("请选择一个建议时段，例如说“第一个”，我再写进日历。"
                      if _in_chinese(text) else
                      'Choose a suggested time, such as "first", and I’ll book it.')
            return prompt, None, True
    elif kind == "batch":
        pending = st.session_state.get("pending")
        if pending and pending.get("batch"):
            message = agent.accept_batch(session, uid, pending["batch"], now, tz)
            return agent.Reply(
                message, Action(intent="schedule_all", raw=text, confidence=0.9), _L1_PARSER
            ), None, False
    elif kind == "event_draft":
        draft = st.session_state.get("event_draft")
        if draft is not None:
            st.session_state["event_draft"] = None  # 先清状态再写库，重复触发不二次执行
            return agent.confirm_event_draft(session, uid, draft, True, now, tz), None, False
    elif kind == "recovery":
        ctx = st.session_state.get("checkin_context")
        if ctx is not None and ctx.stage == "recovery" and ctx.candidates:
            reply = agent.accept_recovery(session, uid, ctx, ctx.candidates[0], now, tz)
            st.session_state["checkin_context"] = reply.checkin_context
            return reply, None, False
    return None


def _chat_offer_payload(session, uid, decision, text: str) -> dict:
    """Resolve an L1 offer to a stable, owned goal step before it can be accepted."""
    payload = {
        "title": decision.title,
        "when_text": decision.offer_when_text or decision.when_text or decision.offer or "",
    }
    if decision.offer_intent not in {"schedule_step", "refine_steps"}:
        return payload
    goals = goal_svc.active_goals(session, uid)
    hint = (decision.offer_target or "").strip().casefold()
    matched = [goal for goal in goals if hint and goal.title.casefold() == hint]
    if not matched:
        context = " ".join(filter(None, (text, decision.reply, decision.offer,
                                          decision.topic, decision.progress_target)))
        matched = [goal for goal in goals if goal.title.casefold() in context.casefold()]
    if not matched:
        named = _named_goal(session, uid, " ".join(filter(None, (text, decision.reply,
                                                       decision.offer, decision.offer_target))))
        if named is not None:
            matched = [named]
    if not matched and len(goals) == 1:
        matched = goals
    if len(matched) == 1:
        step = goal_svc.next_step(matched[0])
        if step is not None:
            payload.update(goal_id=matched[0].id, step_id=step.id)
    return payload


def _reject_offer(session, uid, dctx, decision, now, tz):
    """拒绝最近提议：状态绑定提议的拒绝 = 退出该流程（与逐字"取消/算了"同语义）；
    chat 建议只标记拒绝，不动任何状态。返回 (reply, follow_up, preserved)。"""
    offer = dialog.latest_open_offer(dctx)
    ack = decision.reply.strip()
    if offer is not None:
        dialog.decline_offer(dctx, offer)
        dialog.close_open_offers(dctx)  # 回绝最近提议即回绝整轮提议
    offer_kind = offer.get("kind") if offer else None

    # checkin 流程的拒绝必须走确定性 decline 语义，让队列前进/签到结束
    if dctx.kind == "checkin" or offer_kind == "recovery":
        ctx = st.session_state.get("checkin_context")
        if ctx is not None and ctx.stage == "recovery":
            reply = agent.decline_recovery(session, uid, ctx, now, tz)
            st.session_state["checkin_context"] = reply.checkin_context
            return reply, None, False  # _record_reply 会同步 pending/候选状态
        if ctx is not None and ctx.stage == "goal_options":
            reply = agent.decline_goal_options(session, uid, ctx, now, tz)
            st.session_state["checkin_context"] = reply.checkin_context
            return reply, None, False
        if ctx is not None:
            # outcome/reason/batch：明确结束当前签到；在用户下次发消息前不自动重问
            st.session_state["checkin_context"] = None
            st.session_state["checkin_suppressed_until_user"] = True
            return (ack or "OK — I'll stop asking about that for now. "
                    "Tell me whenever you want to reschedule."), None, True
        return ack or "OK — I'll leave it as is.", None, True

    if offer_kind == "goal_draft" and st.session_state.get("goal_draft") is not None:
        st.session_state["goal_draft"] = None
        return ack or "OK — draft discarded. No goal was created.", None, True
    if offer_kind == "event_draft" and st.session_state.get("event_draft") is not None:
        st.session_state["event_draft"] = None
        return ack or "OK — the calendar preview was discarded; nothing was added.", None, True
    if offer_kind in ("candidates", "batch") and st.session_state.get("pending"):
        st.session_state["pending"] = None
        return ack or "OK — I've put those time suggestions away.", None, True
    if offer_kind == "pending_action" and st.session_state.get("pending_action") is not None:
        st.session_state["pending_action"] = None
        return ack or "OK — nothing was changed.", None, True
    # chat 建议或无 open 提议：只确认拒绝
    return ack or "OK — I'll leave it as is.", None, True


def _apply_decision(session, text: str, dctx: dialog.DialogContext, decision, now, tz):
    """L1 决定的路由：流程内回答交给确定性流程；新指令经白名单执行。

    退出草稿/候选/预览/签到流程需要规则二次确认，防止模型把流程内短回答
    误判成新指令。返回 (reply, follow_up, preserved)，口径同 _rules_turn。
    """
    uid = runtime.user_id()
    relation = decision.relation
    if decision.topic:
        dctx.subject["topic"] = decision.topic[:120]
    if relation != "clarify":
        dctx.clarify_count = 0
    follow_up = None
    kind = dctx.kind

    if relation == "new_command" and _generic_calendar_acceptance(
        text, now, dialog.latest_open_offer(dctx)
    ):
        offer = dialog.latest_open_offer(dctx)
        result = _accept_offer(session, uid, dctx, offer, text, now, tz)
        if result is not None and result is not _UNHANDLED:
            _mark_offer_accepted(dctx, offer)
            return result
    if relation == "new_command":
        dialog.close_open_offers(dctx)  # 用户另起话题：旧提议立即失效，不再跨轮残留

    if relation == "accept_offer":
        offer = dialog.latest_open_offer(dctx)
        if offer is None:
            return _OFFER_NONE, None, True
        result = _accept_offer(session, uid, dctx, offer, text, now, tz)
        if result is _UNHANDLED:
            pass  # 无单一映射（goal_options 三选一）：按 in_flow 走当前流程
        elif result is not None:
            _mark_offer_accepted(dctx, offer)
            return result
        else:
            offer["status"] = "expired"  # 提议与实际状态不匹配：失效并诚实提示，不执行
            return _OFFER_STALE, None, True
    if relation == "reject_offer":
        return _reject_offer(session, uid, dctx, decision, now, tz)
    if relation == "clarify":
        # 不循环追问，且不进任何流程分支（含糊修订不得触发 L2 重写草稿）
        dctx.clarify_count += 1
        if dctx.clarify_count >= 3:
            dctx.clarify_count = 0  # 停止追问：明确"我先不猜"，回到 idle 不循环
            reply_text = agent.CLARIFY_STOP
        elif dctx.clarify_count == 2:
            reply_text = agent.clarify_options(session, uid)  # 第二轮换确定性选项清单
        else:
            reply_text = decision.reply.strip() or agent.CLARIFY
        return reply_text, None, True  # preserved：草稿/候选/签到状态原样保留
    # 只有带可执行 intent 的闲聊提议才登记为可接受；纯文本提议不登记（M1）
    if decision.offer and decision.offer_intent in _OFFER_INTENTS:
        dialog.register_offer(
            dctx, decision.offer, kind="chat", intent=decision.offer_intent,
            payload=_chat_offer_payload(session, uid, decision, text),
        )

    if kind == "pending_action":
        if relation == "new_command" and agent.looks_like_new_command(text, now):
            st.session_state["pending_action"] = None
        else:
            return "Please confirm or cancel the pending change.", None, True
    elif kind == "goal_draft":
        if parse_with_rules(text, now).intent in _DRAFT_EXIT_INTENTS:
            st.session_state["goal_draft"] = None
        else:
            return _revise_goal_draft(text, now, tz), None, True
    elif kind == "pending_candidates":
        if calendar_write_request(text):
            return ("请先选一个建议时段，我再写进日历。" if _in_chinese(text)
                    else "Choose one of the suggested times first, then I'll book it."), None, True
        if relation != "new_command" or not agent.looks_like_new_command(text, now):
            return (
                decision.reply.strip() or "Pick 1, 2 or 3, or tell me another time window."
            ), None, True
        # 新指令且规则确认：落到通用执行，_record_reply 会清掉候选
    elif kind == "event_draft":
        if relation == "new_command" and agent.looks_like_new_command(text, now):
            st.session_state["event_draft"] = None
        else:
            return (
                decision.reply.strip() or
                "The calendar preview above isn't saved yet — "
                'say "add to calendar" to confirm, or "cancel" to drop it.'
            ), None, True
    elif kind == "checkin":
        ctx = st.session_state["checkin_context"]
        if relation == "new_command" and agent.looks_like_new_command(text, now):
            follow_up = agent.checkin_prompt(session, ctx, now, tz) or None
        else:
            reply = agent.continue_checkin(session, uid, ctx, text, now, tz, use_llm=False)
            if reply is not None:
                st.session_state["checkin_context"] = reply.checkin_context
                return reply, None, False
            follow_up = agent.checkin_prompt(session, ctx, now, tz) or None
    elif kind == "goal_brief":
        brief = st.session_state["goal_brief"]
        if relation == "new_command" and agent.looks_like_new_command(text, now):
            dialog.push_return_to(dctx, "goal_brief", brief=brief)  # 打断压栈，可恢复
            st.session_state["goal_brief"] = None
        else:
            return agent.continue_goal(session, uid, brief, text, now, tz), None, False

    if relation == "resume" and dctx.return_to:
        entry = dctx.return_to.pop()
        brief = entry.get("brief")
        if entry.get("kind") == "goal_brief" and brief is not None:
            prompt = planner.clarification_text(brief)
            reply_text = f"{decision.reply.strip()}\n{prompt}" if decision.reply.strip() else prompt
            return agent.Reply(
                reply_text, Action(intent="chat", raw=text, confidence=0.5),
                _L1_PARSER, goal_brief=brief,
            ), None, False
    action = turn_engine.to_action(decision, text, now)
    if action.intent in {"schedule_step", "refine_steps"}:
        named = _named_goal(session, uid, text)
        if named is not None:
            step = goal_svc.next_step(named)
            if step is not None:
                action.progress_target = step.title
    if action.intent == "schedule_step" and action.window is None and action.start is None:
        action.window = _inherit_schedule_window(session, uid, now)
    if action.intent == "chat" and decision.intent != "chat" and NO_SCHEDULE.search(text):
        return "好，暂时不安排，日历没有变动。", None, True
    if not _bind_schedule_target(session, uid, action, text, decision.reply, decision.topic or ""):
        return _schedule_target_question(text)
    if action.intent in agent.RISKY_INTENTS:
        # L1 提议的危险动作一律压低置信度，强制走 pending_action 确认路径
        action.confidence = min(action.confidence, 0.4)
    chat_text = decision.reply.strip() or None
    if action.intent == "chat" and decision.intent != "chat":
        chat_text = None  # 被安全守卫否决的写动作不能复用模型的“已安排”文案
    reply = agent.execute(
        session, uid, action, _L1_PARSER, now, tz,
        chat_text=chat_text, use_llm=action.intent != "chat",
    )
    return reply, follow_up, False


def run_turn(text: str) -> None:
    st.session_state["messages"].append({"role": "user", "content": text})
    st.session_state.pop("checkin_suppressed_until_user", None)  # 用户发消息即恢复签到逻辑
    if _goal_intake_turn(text):
        return
    pending_action = st.session_state.get("pending_action")
    if pending_action is not None:
        decision = text.strip().lower()
        if re.fullmatch(r"yes|confirm|ok|okay|确认|是|好的", decision):
            with runtime.session_scope("confirm_action") as session:
                reply = agent.confirm_action(
                    session, runtime.user_id(), pending_action, True, runtime.now(), runtime.timezone()
                )
            st.session_state["pending_action"] = None
            _record_reply(reply)
            _persist()
            return
        if re.fullmatch(r"no|cancel|never mind|取消|不|算了", decision):
            with runtime.session_scope("cancel_action") as session:
                reply = agent.confirm_action(
                    session, runtime.user_id(), pending_action, False, runtime.now(), runtime.timezone()
                )
            st.session_state["pending_action"] = None
            _record_reply(reply)
            _persist()
            return
    # 零成本短路：逐字确认/序号/时间窗口，不调模型
    if _goal_draft_verbatim(text) or _pending_verbatim(text) or _event_draft_verbatim(text):
        return

    now = runtime.now()
    brief = st.session_state.get("goal_brief")
    if brief is not None and (
        planner.cancellation_requested(text)
        or planner.parse_explicit_date(text, now.date()) is not None
        or planner.parse_duration_days(text) is not None
    ):
        with runtime.session_scope("goal.timeline") as session:
            reply = agent.continue_goal(session, runtime.user_id(), brief, text, now,
                                        runtime.timezone())
        _record_reply(reply)
        _persist()
        return

    if st.session_state.get("goal_draft") is None and _DEADLINE_CHANGE.search(text):
        deadline = planner.parse_explicit_date(text, now.date())
        if deadline is None:
            days = planner.parse_duration_days(text)
            deadline = now.date() + timedelta(days=days) if days is not None else None
        if deadline is not None:
            with runtime.session_scope("goal.update_deadline") as session:
                named = _named_goal(session, runtime.user_id(), text)
                pending = st.session_state.get("pending")
                step = (session.get(Step, pending["step_id"])
                        if pending and pending.get("step_id") else None)
                goals = goal_svc.active_goals(session, runtime.user_id())
                goal = named or (step.goal if step is not None and step.goal.user_id == runtime.user_id()
                                 else None) or (goals[0] if len(goals) == 1 else None)
                if goal is None:
                    message = ("请说明要修改哪个目标的期限。" if _in_chinese(text)
                               else "Which goal's deadline should I change?")
                elif deadline <= now.date():
                    message = ("请给一个未来的截止日期。" if _in_chinese(text)
                               else "Please choose a future deadline.")
                else:
                    goal.deadline = deadline
                    session.flush()
                    st.session_state["pending"] = None
                    dialog.close_open_offers(_refresh_context())
                    message = (f"已将「{goal.title}」的期限改为 {deadline:%Y年%m月%d日}。"
                               "原有步骤保留，可以继续调整计划。" if _in_chinese(text) else
                               f'Deadline for “{goal.title}” updated to {deadline:%b %d, %Y}. '
                               "Existing steps remain; you can revise the plan next.")
            st.session_state["messages"].append({"role": "assistant", "content": message})
            _persist()
            return

    pending = st.session_state.get("pending")
    if pending and not pending.get("batch") and _GOAL_BOOKING.search(text):
        with runtime.session_scope("goal.switch_schedule") as session:
            named = _named_goal(session, runtime.user_id(), text)
            current = session.get(Step, pending["step_id"])
            if named is not None and current is not None and current.goal_id != named.id:
                step = goal_svc.next_step(named)
                if step is not None:
                    dialog.close_open_offers(_refresh_context())
                    action = Action(intent="schedule_step", raw=text,
                                    progress_target=step.title, confidence=0.9)
                    reply = agent.execute(session, runtime.user_id(), action, "goal:switch",
                                          now, runtime.timezone(), use_llm=False)
                else:
                    reply = None
            else:
                reply = None
        if reply is not None:
            _record_reply(reply)
            _persist()
            return

    dctx = _refresh_context()
    now = runtime.now()
    tz = runtime.timezone()
    answer = text.strip().lower().strip("。.!！")
    offer = dialog.latest_open_offer(dctx)
    if offer is not None and (offer.get("payload") or {}).get("awaiting_target"):
        with runtime.session_scope("conversation.offer_target") as session:
            matched = _bind_pending_offer_target(session, runtime.user_id(), offer, text, now, tz)
            result = (_accept_offer(session, runtime.user_id(), dctx, offer, text, now, tz,
                                    use_llm=False) if matched else None)
        if result is not None and result is not _UNHANDLED:
            _mark_offer_accepted(dctx, offer)
            reply, follow_up, preserved = result
            if preserved:
                st.session_state["messages"].append({"role": "assistant", "content": reply})
            else:
                _record_reply(reply)
            if follow_up:
                st.session_state["messages"].append({"role": "assistant", "content": follow_up})
            _persist()
            return
    if offer is not None and answer in {"好", "好的", "可以", "行", "确认", "yes", "sure", "yes please", "go ahead"}:
        with runtime.session_scope("conversation.accept_offer") as session:
            result = _accept_offer(session, runtime.user_id(), dctx, offer, text, now, tz,
                                   use_llm=False)
        if result is not None and result is not _UNHANDLED:
            _mark_offer_accepted(dctx, offer)
            reply, follow_up, preserved = result
            if preserved:
                st.session_state["messages"].append({"role": "assistant", "content": reply})
            else:
                _record_reply(reply)
            if follow_up:
                st.session_state["messages"].append({"role": "assistant", "content": follow_up})
            _persist()
            return
    with runtime.session_scope("run_turn") as session:
        with st.spinner("Thinking…"):
            client = turn_client()
            decision = turn_engine.decide(
                session, runtime.user_id(), text, now, tz, dctx, client=client
            )
            if decision is not None:
                reply, follow_up, preserved = _apply_decision(session, text, dctx, decision, now, tz)
            else:
                reply, follow_up, preserved = _rules_turn(
                    session, text, now, tz, llm_attempted=client is not None
                )
            if (
                not preserved
                and st.session_state.get("checkin_context") is None
                and reply.goal_brief is None
                and reply.event_draft is None
            ):
                new_ctx, question = agent.begin_checkin(session, runtime.user_id(), now, tz)
                if new_ctx is not None:
                    st.session_state["checkin_context"] = new_ctx
                    follow_up = follow_up or question
    if preserved:
        st.session_state["messages"].append({"role": "assistant", "content": reply})
    else:
        _record_reply(reply)
    if follow_up:
        st.session_state["messages"].append({"role": "assistant", "content": follow_up})
    _persist()


def accept(step_id: int, candidate) -> None:
    previous_user = next((item["content"] for item in reversed(st.session_state["messages"])
                          if item["role"] == "user"), "")
    with runtime.session_scope("accept") as session:
        message = agent.accept(session, runtime.user_id(), step_id, candidate,
                               runtime.now(), runtime.timezone(),
                               chinese=_in_chinese(previous_user))
    st.session_state["messages"].append({"role": "assistant", "content": message})
    st.session_state["pending"] = None
    _persist()
    st.rerun()


def accept_batch(batch: list[dict]) -> None:
    with runtime.session_scope("accept_batch") as session:
        message = agent.accept_batch(session, runtime.user_id(), batch, runtime.now(), runtime.timezone())
    st.session_state["messages"].append({"role": "assistant", "content": message})
    st.session_state["pending"] = None
    _persist()
    st.rerun()


def dismiss_pending() -> None:
    st.session_state["pending"] = None
    _persist()
    st.rerun()


_CHIP_WINDOWS = {"today_pm": (0, 12, 18), "tonight": (0, 18, 23), "tomorrow": (1, 8, 22)}


def _chip_window(chip: str, current: datetime):
    if chip == "any":
        return None
    if chip == "weekend":
        day = current.date() + timedelta(days=(5 - current.weekday()) % 7)
        lo, hi = 8, 22
    else:
        offset, lo, hi = _CHIP_WINDOWS[chip]
        day = current.date() + timedelta(days=offset)
    return (
        datetime.combine(day, time(hour=lo), tzinfo=runtime.zone()),
        datetime.combine(day, time(hour=hi), tzinfo=runtime.zone()),
    )


def refilter_pending(step_id: int, chip: str) -> None:
    window = _chip_window(chip, runtime.now())
    with runtime.session_scope("refilter_pending") as session:
        candidates, _ = agent.propose_for_step(
            session, runtime.user_id(), step_id, runtime.now(), runtime.timezone(), window=window
        )
    if candidates and st.session_state.get("pending"):
        st.session_state["pending"]["candidates"] = candidates
    else:
        runtime.flash("Nothing free in that window — kept the current suggestions.")
    _persist()
    st.rerun()


def accept_recovery(step_id: int, candidate) -> None:
    ctx = st.session_state.get("checkin_context")
    with runtime.session_scope("accept_recovery") as session:
        reply = agent.accept_recovery(session, runtime.user_id(), ctx, candidate, runtime.now(), runtime.timezone())
    st.session_state["checkin_context"] = reply.checkin_context
    _record_reply(reply)
    _persist()
    st.rerun()


def dismiss_recovery() -> None:
    ctx = st.session_state.get("checkin_context")
    with runtime.session_scope("dismiss_recovery") as session:
        reply = agent.decline_recovery(session, runtime.user_id(), ctx, runtime.now(), runtime.timezone())
    st.session_state["checkin_context"] = reply.checkin_context
    _record_reply(reply)
    _persist()
    st.rerun()


def schedule_step(step_id: int) -> None:
    with runtime.session_scope("schedule_step") as session:
        now = runtime.now()
        already_scheduled = session.scalar(select(Event).where(
            Event.user_id == runtime.user_id(),
            Event.step_id == step_id,
            Event.kind == "block",
            Event.start_utc >= as_utc_naive(now, runtime.timezone()),
        )) is not None
        candidates, step = agent.propose_for_step(
            session, runtime.user_id(), step_id, now, runtime.timezone()
        )
        title = step.title if step is not None else ""
    if already_scheduled:
        runtime.flash(f"“{title}” already has a future time on your calendar.")
        st.rerun()
    if candidates:
        st.session_state["goal_draft"] = None
        st.session_state["goal_brief"] = None
        st.session_state["goal_intake"] = None
        st.session_state["event_draft"] = None
        st.session_state["pending"] = {"candidates": candidates, "step_id": step_id, "origin": "goal"}
        message = f"Choose a time for **{title}** in Chat."
        st.session_state["messages"].append({"role": "assistant", "content": message})
        _persist()
        st.switch_page(st.session_state["chat_page"])
    runtime.flash(f"No free slot for “{title}” in the next 5 days. Try a shorter step.")
    st.rerun()


def _owned_goal(session, goal_id: int) -> Goal | None:
    """取当前用户的目标；不存在或属于他人时返回 None，写入口据此安静 no-op。"""
    goal = session.get(Goal, goal_id)
    if goal is None or goal.user_id != runtime.user_id():
        return None
    return goal


def _owned_step(session, step_id: int) -> Step | None:
    """取当前用户的步骤；不存在或属于他人时返回 None，写入口据此安静 no-op。"""
    step = session.get(Step, step_id)
    if step is None or step.goal.user_id != runtime.user_id():
        return None
    return step


def complete_step(step_id: int) -> None:
    with runtime.session_scope("complete_step") as session:
        step = _owned_step(session, step_id)
        if step is None or step.status == "done":
            return
        title = step.title
        now_utc = as_utc_naive(runtime.now(), runtime.timezone())
        goal_svc.complete_step(session, step, raw_text="completed from UI", now=now_utc)
        cal.resolve_due_blocks_done(session, step_id, now_utc)
        cal.cancel_future_blocks(session, step_id, now_utc)
        note = agent.auto_refine_if_low(session, runtime.user_id(), step.goal, runtime.now())
    pending = st.session_state.get("pending")
    if pending and pending.get("step_id") == step_id:
        st.session_state["pending"] = None
    runtime.flash(f"“{title}” marked done." + (f" {note}" if note else ""))
    _persist()
    st.rerun()


def unschedule_step(step_id: int, event_id: int) -> None:
    with runtime.session_scope("unschedule_step") as session:
        step = _owned_step(session, step_id)
        event = session.get(Event, event_id)
        # event 必须属于当前用户且确实挂在该 step 上，防止拿自己的 event_id 配别人的 step_id
        if step is None or event is None or event.user_id != runtime.user_id() or event.step_id != step.id:
            return
        cal.delete_event(session, event)
    runtime.flash("Block removed — the step is back to todo.")
    st.rerun()


def add_step(goal_id: int, title: str, minutes: int, energy: int, kind: str) -> None:
    with runtime.session_scope("add_step") as session:
        goal = _owned_goal(session, goal_id)
        if goal is None:
            return
        goal_svc.add_step(session, goal, title, est_min=minutes, energy=energy, kind=kind)
    runtime.flash(f"Added step “{title}”.")
    st.rerun()


def edit_step(step_id: int, title: str, milestone: str, outcome: str, minutes: int, energy: int, kind: str) -> None:
    with runtime.session_scope("edit_step") as session:
        step = _owned_step(session, step_id)
        if step is None:
            return
        goal_svc.update_step(
            session, step, title=title, milestone=milestone, outcome=outcome,
            est_min=minutes, energy=energy, kind=kind,
        )
    runtime.flash(f"Updated step “{title}”.")
    st.rerun()


def delete_step(step_id: int) -> None:
    with runtime.session_scope("delete_step") as session:
        step = _owned_step(session, step_id)
        if step is None:
            return
        title = step.title
        goal_svc.delete_step(session, step)
    pending = st.session_state.get("pending")
    if pending and pending.get("step_id") == step_id:
        st.session_state["pending"] = None
    runtime.flash(f"Deleted step “{title}”.")
    _persist()
    st.rerun()


def archive_goal(goal_id: int) -> None:
    with runtime.session_scope("archive_goal") as session:
        goal = _owned_goal(session, goal_id)
        if goal is None:
            return
        title = goal.title
        step_ids = {step.id for step in goal.steps}
        goal_svc.archive_goal(session, goal)
    pending = st.session_state.get("pending")
    if pending and pending.get("origin") == "goal" and pending.get("step_id") in step_ids:
        st.session_state["pending"] = None
    if (st.session_state.get("replan_draft") or {}).get("goal_id") == goal_id:
        st.session_state.pop("replan_draft", None)
    runtime.flash(f"Archived goal “{title}”.")
    _persist()
    st.rerun()


def toggle_pin(goal_id: int) -> None:
    with runtime.session_scope("toggle_pin") as session:
        goal = _owned_goal(session, goal_id)
        if goal is None:
            return
        goal_svc.set_pinned(session, goal, not goal.pinned)
        pinned = goal.pinned
        title = goal.title
    runtime.flash(f"Pinned “{title}”." if pinned else f"Unpinned “{title}”.")
    _persist()
    st.rerun()


def preview_replan(goal_id: int, instruction: str) -> None:
    if not instruction.strip():
        runtime.flash("Describe what should change first — this avoids blind re-plans.")
        st.rerun()
    with runtime.session_scope("preview_replan") as session:
        goal = _owned_goal(session, goal_id)
        if goal is None:
            return
        title = goal.title
        with st.spinner("Revising plan…"):
            plan = agent.propose_goal_plan(session, goal, runtime.now(), runtime.timezone(), instruction=instruction)
    if not plan:
        runtime.flash("AI couldn't produce a valid revised plan — the old plan was kept.")
        st.rerun()
    st.session_state["replan_draft"] = {"goal_id": goal_id, "plan": plan, "instruction": instruction}
    runtime.flash(f"Preview ready for “{title}” — review it before applying.")
    _persist()
    st.rerun()


def apply_replan(goal_id: int) -> None:
    draft = st.session_state.get("replan_draft")
    if not draft or draft.get("goal_id") != goal_id:
        return
    with runtime.session_scope("apply_replan") as session:
        goal = _owned_goal(session, goal_id)
        if goal is None:
            return
        title = goal.title
        old_step_ids = {step.id for step in goal.steps}
        goal_svc.replace_steps(session, goal, draft["plan"], template_key="llm")
    pending = st.session_state.get("pending")
    if pending and pending.get("origin") == "goal" and pending.get("step_id") in old_step_ids:
        st.session_state["pending"] = None
    st.session_state.pop("replan_draft", None)
    runtime.flash(f"Applied the revised plan to “{title}”.")
    _persist()
    st.rerun()


def discard_replan() -> None:
    st.session_state.pop("replan_draft", None)
    runtime.flash("Discarded the plan preview — nothing changed.")
    _persist()
    st.rerun()


def blocks_by_step(session, user_id: int, step_ids: list[int]) -> dict[int, dict]:
    if not step_ids:
        return {}
    rows = session.scalars(
        select(Event)
        .where(
            Event.user_id == user_id,
            Event.step_id.in_(step_ids),
            Event.kind == "block",
            Event.start_utc >= as_utc_naive(runtime.now(), runtime.timezone()),
        )
        .order_by(Event.start_utc)
    )
    result = {}
    for event in rows:
        result.setdefault(
            event.step_id,
            {"id": event.id, "start": as_local(event.start_utc, runtime.timezone()),
             "end": as_local(event.end_utc, runtime.timezone())},
        )
    return result
