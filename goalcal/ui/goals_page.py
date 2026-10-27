from __future__ import annotations

import streamlit as st

from ..domain import goals as goal_svc
from . import controller, progress, runtime


def _render_detail(snapshot, blocks) -> None:
    draft = st.session_state.get("replan_draft")
    if draft and draft.get("goal_id") != snapshot["id"]:
        draft = None
    progress.render_goal(
        snapshot,
        blocks,
        on_schedule=controller.schedule_step,
        on_complete=controller.complete_step,
        on_unschedule=controller.unschedule_step,
        on_add_step=controller.add_step,
        on_delete_step=controller.delete_step,
        on_edit_step=controller.edit_step,
        on_archive_goal=controller.archive_goal,
        on_preview_replan=controller.preview_replan,
        on_apply_replan=controller.apply_replan,
        on_discard_replan=controller.discard_replan,
        replan_draft=draft,
    )


def _render_directory(snapshots, selected_id: int, blocks) -> None:
    st.caption("Your goals")
    for snapshot in snapshots:
        with st.container(border=True):
            if st.button(
                snapshot["title"],
                key=f"select_goal_{snapshot['id']}",
                use_container_width=True,
                type="primary" if snapshot["id"] == selected_id else "secondary",
            ):
                st.session_state["selected_goal_id"] = snapshot["id"]
                st.rerun()
            st.progress(min(1.0, snapshot["progress"]))
            next_step = snapshot.get("next_step")
            if next_step is not None:
                st.caption(f"Next · {next_step.title}")
                if block := blocks.get(next_step.id):
                    st.caption(f"Scheduled · {block['start']:%a %b %d, %H:%M}")
            else:
                st.caption(f"{snapshot['done']}/{snapshot['total']} steps completed")


def render() -> None:
    st.title("Goals")
    if flash := st.session_state.pop("flash", None):
        st.toast(flash)
    heading, new_goal = st.columns([4, 1])
    heading.caption("A quiet view of your plans. New goals start in Chat.")
    if new_goal.button("New goal", use_container_width=True):
        st.switch_page(st.session_state["chat_page"])
    with runtime.session_scope("goals.read") as session:
        goals = goal_svc.active_goals(session, runtime.user_id())
        step_ids = [step.id for goal in goals for step in goal.steps]
        blocks = controller.blocks_by_step(session, runtime.user_id(), step_ids)
        snapshots = [goal_svc.snapshot(goal, now=runtime.now()) for goal in goals]
    if not snapshots:
        st.info("No active goal yet — tell me one in Chat.")
        return
    selected_id = st.session_state.get("selected_goal_id")
    ids = [snapshot["id"] for snapshot in snapshots]
    if selected_id not in ids:
        selected_id = ids[0]
        st.session_state["selected_goal_id"] = selected_id
    snapshot = next(item for item in snapshots if item["id"] == selected_id)
    if len(snapshots) == 1:
        _render_detail(snapshot, blocks)
        return
    directory, detail = st.columns([1, 2], gap="large")
    with directory:
        _render_directory(snapshots, selected_id, blocks)
    with detail:
        _render_detail(snapshot, blocks)
