from __future__ import annotations

import streamlit as st

from .. import agent
from ..domain import planner
from . import chat as chat_ui
from . import controller, current, runtime

SLOGAN = "What do you want to make time for?"

ADD_EVENT_EXAMPLE = "Add a meeting tomorrow 10am"
REVIEW_QUERY = "What's on this week?"
HELP_QUERY = "What can you do?"


def home_suggestions(status: dict) -> list[dict]:
    """首页情境建议（纯函数）：状态直接藏进建议句，一行一条。

    有目标/日程：选择目标并排期 → 查询本周日程 → 新目标；
    没有任何可行动作时（空账号或目标已全部完成）：同样三层结构换成
    Create a goal / Add an event / 产品能力，绝不出现无目标的排期按钮。
    """
    suggestions: list[dict] = []
    if status["next_step_id"] is not None:
        suggestions.append({
            "key": "home_suggestion_goal",
            "label": "Find time for my goal",
            "action": "run_turn",
            "text": "Find time for my goal",
        })
    if status["next_event_title"] is not None:
        suggestions.append({
            "key": "home_suggestion_event",
            "label": "Review my calendar",
            "action": "run_turn",
            "text": REVIEW_QUERY,
        })
    if suggestions:
        suggestions.append({
            "key": "home_suggestion_new",
            "label": "Start a new goal",
            "action": "begin_goal",
        })
        return suggestions
    return suggestions + [
        {"key": "home_suggestion_new", "label": "Start a new goal", "action": "begin_goal"},
        {"key": "home_suggestion_event", "label": "Add an event",
         "action": "run_turn", "text": ADD_EVENT_EXAMPLE},
        {"key": "home_suggestion_help", "label": "See what I can do",
         "action": "run_turn", "text": HELP_QUERY},
    ]


def _render_home() -> list[dict]:
    """空首页先渲染 slogan，建议等输入框之后再渲染。"""
    with st.container(key="home_slogan"):
        st.markdown(SLOGAN)
    return home_suggestions(current.load())


def _render_home_suggestions(suggestions: list[dict]) -> None:
    with st.container(key="home_suggestions"):
        for suggestion in suggestions:
            if st.button(suggestion["label"], key=suggestion["key"]):
                if suggestion["action"] == "schedule_step":
                    controller.schedule_step(suggestion["step_id"])
                elif suggestion["action"] == "begin_goal":
                    controller.begin_goal()
                else:
                    controller.run_turn(suggestion["text"])
                st.rerun()


def _render_pending_action() -> None:
    with st.container(border=True):
        st.caption("Confirm action")
        yes, no = st.columns(2)
        if yes.button("Confirm", key="confirm_pending_action", type="primary", use_container_width=True):
            controller.run_turn("yes")
            st.rerun()
        if no.button("Cancel", key="cancel_pending_action", use_container_width=True):
            controller.run_turn("cancel")
            st.rerun()


def _render_pending() -> None:
    pending = st.session_state.get("pending")
    if not pending:
        return
    with st.container(border=True):
        if pending.get("batch"):
            chat_ui.render_batch(pending["batch"], controller.accept_batch, controller.dismiss_pending)
            return
        candidates = pending["candidates"]
        step_id = pending["step_id"]
        on_accept = controller.accept_recovery if pending.get("origin") == "recovery" else controller.accept
        on_dismiss = controller.dismiss_recovery if pending.get("origin") == "recovery" else controller.dismiss_pending
        on_filter = lambda key: controller.refilter_pending(step_id, key)
        heading, dismiss = st.columns([4, 1])
        heading.caption("Suggested times")
        if dismiss.button("Dismiss", key=f"dismiss_pending_{step_id}", use_container_width=True):
            on_dismiss()
        chat_ui.render_proposals(
            candidates[:3], step_id, on_accept, on_dismiss=None,
            on_filter=on_filter, now=runtime.now(),
        )
        if len(candidates) > 3:
            with st.expander(f"More times ({len(candidates) - 3})"):
                chat_ui.render_proposals(
                    candidates[3:], step_id, on_accept, on_dismiss=None,
                    on_filter=None, now=runtime.now(), start_index=3,
                )


def _render_goal_draft() -> None:
    draft = st.session_state.get("goal_draft")
    if draft is None:
        return
    with st.container(border=True):
        st.caption("Goal draft")
        st.markdown(f"**{draft.brief.title}**")
        st.caption(f"Deadline · {draft.deadline:%b %d, %Y}")
        if draft.outline:
            st.caption(" → ".join(draft.outline))
        for index, step in enumerate(draft.step_defs, 1):
            st.markdown(f"{index}. {step['title']} · {step['est_min']} min")
        if draft.brief.assumptions:
            st.caption("Assumptions: " + "; ".join(draft.brief.assumptions))
        st.caption("Nothing is saved yet. Reply with changes, or choose an action.")
        create, save, cancel = st.columns(3)
        if create.button("Create & schedule", key="create_goal_schedule", type="primary", use_container_width=True):
            controller.confirm_goal_draft(True)
        if save.button("Save goal", key="save_goal_draft", use_container_width=True):
            controller.confirm_goal_draft(False)
        if cancel.button("Cancel", key="cancel_goal_draft", use_container_width=True):
            controller.cancel_goal_draft()


def _render_event_draft() -> None:
    draft = st.session_state.get("event_draft")
    if draft is None:
        return
    with st.container(border=True):
        st.caption("Calendar preview")
        for action in draft.events:
            st.markdown(f"**{action.title}** · {action.start:%a %b %d, %H:%M}–{action.end:%H:%M}")
        st.caption("Nothing is saved yet. Confirm to add them all at once.")
        add, cancel = st.columns(2)
        if add.button("Add to calendar", key="confirm_event_draft", type="primary", use_container_width=True):
            controller.confirm_event_draft(True)
        if cancel.button("Cancel", key="cancel_event_draft", use_container_width=True):
            controller.confirm_event_draft(False)


def _render_quick_replies() -> None:
    brief = st.session_state.get("goal_brief")
    if brief is not None:
        st.caption("Quick answers")
        options = planner.quick_options(brief)
        for row_idx, row in enumerate((options[:2], options[2:])):
            if row:
                cols = st.columns(len(row))
                for col_idx, (col, option) in enumerate(zip(cols, row)):
                    if col.button(option, key=f"goal_option_{row_idx}_{col_idx}", use_container_width=True):
                        controller.run_turn(option)
                        st.rerun()
        return
    ctx = st.session_state.get("checkin_context")
    if ctx is not None:
        options = agent.checkin_quick_options(ctx)
        if options:
            st.caption("Quick replies")
            cols = st.columns(len(options))
            for idx, (col, option) in enumerate(zip(cols, options)):
                if col.button(option, key=f"checkin_option_{ctx.stage}_{idx}", use_container_width=True):
                    controller.run_turn(option)
                    st.rerun()


def render() -> None:
    if flash := st.session_state.pop("flash", None):
        st.toast(flash)
    with st.container(horizontal_alignment="center"):
        main = st.container(width=740)
    with main:
        messages = st.session_state["messages"]
        idle = (
            st.session_state.get("pending_action") is None
            and st.session_state.get("goal_draft") is None
            and st.session_state.get("pending") is None
            and st.session_state.get("event_draft") is None
            and not st.session_state.get("goal_intake")
        )
        home = (
            not st.session_state.get("has_user_history")
            and idle
            # 只有预置欢迎消息才算空首页：任何对话动作（含 Goals 页发起的
            # 排期/预订，不产生用户消息）之后都留在消息流，首页不再回来
            and len(messages) <= 1
        )
        suggestions = _render_home() if home else []
        if not home:
            if st.session_state.get("chat_has_more"):
                if st.button("Load earlier messages", key="load_earlier"):
                    controller.load_earlier()
            chat_ui.render_messages(messages)
            # 单一主任务：任意时刻只显示一个待处理面板
            if st.session_state.get("pending_action") is not None:
                _render_pending_action()
            elif st.session_state.get("goal_draft") is not None:
                _render_goal_draft()
            elif st.session_state.get("pending") is not None:
                _render_pending()
            elif st.session_state.get("event_draft") is not None:
                _render_event_draft()
            else:
                _render_quick_replies()
        prompt = st.chat_input("Tell me a goal, schedule time, or share your progress…")
        if home:
            _render_home_suggestions(suggestions)
        anchor = st.session_state.pop("chat_anchor", None)
        to_bottom = st.session_state.pop("chat_scroll_to_bottom", False)
        if not home and anchor is not None:
            chat_ui.scroll_to_message(anchor)
        elif not home and to_bottom:
            chat_ui.scroll_to_bottom()
    if prompt:
        controller.run_turn(prompt)
        st.rerun()
