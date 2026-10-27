from __future__ import annotations

import re
from datetime import datetime, time, timedelta

import streamlit as st
from sqlalchemy import select

from .. import agent, conversation
from ..domain import calendar_service as cal
from ..domain import goals as goal_svc
from ..llm.natdate import extract_window, find_dates
from ..llm.rules import parse_with_rules
from ..models import Event, Goal, Step
from ..timeutil import as_local, as_utc_naive
from . import runtime

STATE_KEYS = (
    "pending",
    "pending_action",
    "goal_brief",
    "goal_draft",
    "event_draft",
    "checkin_context",
    "replan_draft",
)


def _persist() -> None:
    if not st.session_state.get("auth_token"):
        return
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
    with runtime.session_scope("conversation.load") as session:
        page = conversation.load_page(session, runtime.user_id())
        state = conversation.load_task_state(session, runtime.user_id())
    messages = page["messages"]
    st.session_state["messages"] = messages or [
        {"role": "assistant", "content": "Hi — tell me a goal, or add something to your calendar."}
    ]
    st.session_state["chat_has_more"] = page["has_more"]
    st.session_state["has_user_history"] = page["has_user_history"]
    # 重开对话回到底部；空会话不滚动
    st.session_state["chat_scroll_to_bottom"] = page["has_user_history"]
    for key in STATE_KEYS:
        st.session_state[key] = state.get(key)
    st.session_state["conversation_loaded"] = True
    if not messages:
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


def start_checkin_if_needed() -> None:
    if any(st.session_state.get(key) is not None for key in (
        "checkin_context", "goal_brief", "goal_draft", "event_draft", "pending", "pending_action"
    )):
        return
    with runtime.session_scope("start_checkin_if_needed") as session:
        ctx, question = agent.begin_checkin(session, runtime.user_id(), runtime.now(), runtime.timezone())
    if ctx is not None:
        st.session_state["checkin_context"] = ctx
        st.session_state["messages"].append({"role": "assistant", "content": question})
        _persist()


def confirm_goal_draft(schedule_first: bool) -> None:
    draft = st.session_state.get("goal_draft")
    if draft is None:
        return
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


def _run_event_draft_turn(text: str) -> bool:
    """日历预览显示中：确认/取消/新指令退出，其余回复不自动写入。"""
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
    if agent.looks_like_new_command(text, runtime.now()):
        st.session_state["event_draft"] = None
        return False
    st.session_state["messages"].append({
        "role": "assistant",
        "content": "The calendar preview above isn't saved yet — "
        'say "add to calendar" to confirm, or "cancel" to drop it.',
    })
    _persist()
    return True


_ORDINALS = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5,
    "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5,
    "first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4, "sixth": 5,
    "one": 0, "two": 1, "three": 2, "four": 3, "five": 4, "six": 5,
}


def _run_pending_turn(text: str) -> bool:
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
    window = extract_window(text, runtime.now())
    dates = find_dates(text, runtime.now())
    if window and dates:
        day = dates[-1][1].date()
        window = (
            window[0].replace(year=day.year, month=day.month, day=day.day),
            window[1].replace(year=day.year, month=day.month, day=day.day),
        )
    action = parse_with_rules(text, runtime.now())
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
    if agent.looks_like_new_command(text, runtime.now()):
        return False
    st.session_state["messages"].append({
        "role": "assistant", "content": "Pick 1, 2 or 3, or tell me another time window."
    })
    _persist()
    return True


def _run_goal_draft_turn(text: str) -> bool:
    if st.session_state.get("goal_draft") is None:
        return False
    decision = text.strip().lower()
    if re.fullmatch(r"创建并排期|确认创建|create( & schedule)?|create and schedule", decision):
        confirm_goal_draft(True)
        return True
    if re.fullmatch(r"仅保存|保存目标|save( goal)?", decision):
        confirm_goal_draft(False)
        return True
    if re.fullmatch(r"取消|算了|不建了|cancel|never mind", decision):
        cancel_goal_draft()
        return True
    action = parse_with_rules(text, runtime.now())
    if action.intent in {
        "add_event", "move_event", "delete_event", "delete_goal", "query_events",
        "schedule_step", "schedule_all", "set_state", "log_progress", "create_goal",
    }:
        st.session_state["goal_draft"] = None
        return False
    draft = st.session_state["goal_draft"]
    with runtime.session_scope("goal_draft.revise") as session:
        revised = agent.revise_goal_draft(draft, text, runtime.now(), runtime.timezone())
    if revised is None:
        message = "I couldn't revise the preview this time. Your current draft is unchanged."
    else:
        st.session_state["goal_draft"] = revised
        message = "Preview updated. Review it below, then create it or keep adjusting."
    st.session_state["messages"].append({"role": "assistant", "content": message})
    _persist()
    return True


def run_turn(text: str) -> None:
    st.session_state["messages"].append({"role": "user", "content": text})
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
        st.session_state["pending_action"] = None

    if _run_goal_draft_turn(text) or _run_pending_turn(text) or _run_event_draft_turn(text):
        return
    brief = st.session_state.get("goal_brief")
    if brief is not None and agent.looks_like_new_command(text, runtime.now()):
        brief = None
        st.session_state["goal_brief"] = None
    ctx = st.session_state.get("checkin_context")
    follow_up = None
    with runtime.session_scope("run_turn") as session:
        with st.spinner("Thinking…"):
            if brief is not None:
                reply = agent.continue_goal(session, runtime.user_id(), brief, text, runtime.now(), runtime.timezone())
            elif ctx is not None:
                reply = agent.continue_checkin(session, runtime.user_id(), ctx, text, runtime.now(), runtime.timezone())
                if reply is None:
                    reply = agent.handle(session, runtime.user_id(), text, runtime.now(), runtime.timezone())
                    follow_up = agent.checkin_prompt(session, ctx, runtime.now(), runtime.timezone()) or None
                else:
                    st.session_state["checkin_context"] = reply.checkin_context
            else:
                reply = agent.handle(session, runtime.user_id(), text, runtime.now(), runtime.timezone())
            if (
                st.session_state.get("checkin_context") is None
                and reply.goal_brief is None
                and reply.event_draft is None
            ):
                new_ctx, question = agent.begin_checkin(session, runtime.user_id(), runtime.now(), runtime.timezone())
                if new_ctx is not None:
                    st.session_state["checkin_context"] = new_ctx
                    follow_up = follow_up or question
    _record_reply(reply)
    if follow_up:
        st.session_state["messages"].append({"role": "assistant", "content": follow_up})
    _persist()


def accept(step_id: int, candidate) -> None:
    with runtime.session_scope("accept") as session:
        message = agent.accept(session, runtime.user_id(), step_id, candidate, runtime.now(), runtime.timezone())
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
        candidates, step = agent.propose_for_step(session, runtime.user_id(), step_id, runtime.now(), runtime.timezone())
        title = step.title if step is not None else ""
    if candidates:
        st.session_state["goal_draft"] = None
        st.session_state["goal_brief"] = None
        st.session_state["event_draft"] = None
        st.session_state["pending"] = {"candidates": candidates, "step_id": step_id, "origin": "goal"}
        message = f"Choose a time for **{title}** in Chat."
        st.session_state["messages"].append({"role": "assistant", "content": message})
        _persist()
        st.switch_page(st.session_state["chat_page"])
    runtime.flash(f"No free slot for “{title}” in the next 5 days. Try a shorter step.")
    st.rerun()


def complete_step(step_id: int) -> None:
    with runtime.session_scope("complete_step") as session:
        step = session.get(Step, step_id)
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
        event = session.get(Event, event_id)
        if event is not None:
            cal.delete_event(session, event)
    runtime.flash("Block removed — the step is back to todo.")
    st.rerun()


def add_step(goal_id: int, title: str, minutes: int, energy: int, kind: str) -> None:
    with runtime.session_scope("add_step") as session:
        goal = session.get(Goal, goal_id)
        if goal is not None:
            goal_svc.add_step(session, goal, title, est_min=minutes, energy=energy, kind=kind)
    runtime.flash(f"Added step “{title}”.")
    st.rerun()


def edit_step(step_id: int, title: str, milestone: str, outcome: str, minutes: int, energy: int, kind: str) -> None:
    with runtime.session_scope("edit_step") as session:
        step = session.get(Step, step_id)
        if step is not None:
            goal_svc.update_step(
                session, step, title=title, milestone=milestone, outcome=outcome,
                est_min=minutes, energy=energy, kind=kind,
            )
    runtime.flash(f"Updated step “{title}”.")
    st.rerun()


def delete_step(step_id: int) -> None:
    with runtime.session_scope("delete_step") as session:
        step = session.get(Step, step_id)
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
        goal = session.get(Goal, goal_id)
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


def preview_replan(goal_id: int, instruction: str) -> None:
    if not instruction.strip():
        runtime.flash("Describe what should change first — this avoids blind re-plans.")
        st.rerun()
    with runtime.session_scope("preview_replan") as session:
        goal = session.get(Goal, goal_id)
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
        goal = session.get(Goal, goal_id)
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
