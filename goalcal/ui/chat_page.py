from __future__ import annotations

import streamlit as st

from .. import agent
from ..domain import planner
from . import chat as chat_ui
from . import controller, runtime


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
        st.caption("GOAL DRAFT")
        st.markdown(f"**{draft.brief.title}**")
        st.caption(f"Deadline · {draft.deadline:%b %d, %Y}")
        if draft.outline:
            st.markdown(" → ".join(draft.outline))
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
        main = st.container(width=760)
    with main:
        st.title("GoalCal")
        st.caption("Tell me what you want to move forward. I’ll help with the next step.")
        with st.container(height=460, border=True):
            chat_ui.render_messages(st.session_state["messages"][-50:])
            if st.session_state.get("pending_action") is not None:
                with st.container(border=True):
                    st.caption("CONFIRM ACTION")
                    yes, no = st.columns(2)
                    if yes.button("Confirm", key="confirm_pending_action", type="primary", use_container_width=True):
                        controller.run_turn("yes")
                        st.rerun()
                    if no.button("Cancel", key="cancel_pending_action", use_container_width=True):
                        controller.run_turn("cancel")
                        st.rerun()
            elif st.session_state.get("goal_draft") is not None:
                _render_goal_draft()
            elif st.session_state.get("pending") is not None:
                _render_pending()
            else:
                _render_quick_replies()
        prompt = st.chat_input("Tell me a goal, schedule time, or share your progress…")
        if prompt:
            controller.run_turn(prompt)
            st.rerun()
