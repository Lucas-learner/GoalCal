from __future__ import annotations

import streamlit as st

from ..domain.goals import step_display_status

STATUS_ICON = {"done": "✅", "scheduled": "📌", "todo": "○", "dropped": "↷"}
KINDS = ["deep", "admin", "physical", "social", "research"]


def _step_actions(
    step,
    block: dict | None,
    prefix: str,
    on_schedule,
    on_complete,
    on_unschedule,
    on_delete_step,
    on_edit_step=None,
) -> None:
    """一行步骤的操作按钮；图标 + 悬浮提示，大小一致；prefix 保证同一页面里 key 唯一。"""
    cols = st.columns(5)
    if on_schedule is not None and cols[0].button(
        "📅" if block is None else "🔄",
        key=f"{prefix}_find_{step.id}",
        help="Find time" if block is None else "Reschedule",
        use_container_width=True,
    ):
        on_schedule(step.id)
    if on_complete is not None and cols[1].button(
        "✅",
        key=f"{prefix}_done_{step.id}",
        help="Mark done",
        use_container_width=True,
    ):
        on_complete(step.id)
    if block is not None and on_unschedule is not None and cols[2].button(
        "✕",
        key=f"{prefix}_cancel_{step.id}",
        help="Cancel block",
        use_container_width=True,
    ):
        on_unschedule(step.id, block["id"])
    if on_edit_step is not None:
        with cols[3].popover("✏️", help="Edit step"):
            with st.form(f"{prefix}_edit_{step.id}"):
                title = st.text_input(
                    "Title", value=step.title, key=f"{prefix}_edit_title_{step.id}"
                )
                milestone = st.text_input(
                    "Milestone",
                    value=step.milestone,
                    key=f"{prefix}_edit_milestone_{step.id}",
                )
                outcome = st.text_input(
                    "Done looks like",
                    value=step.outcome or "",
                    key=f"{prefix}_edit_outcome_{step.id}",
                )
                c1, c2, c3 = st.columns(3)
                minutes = c1.number_input(
                    "Minutes",
                    min_value=5,
                    max_value=240,
                    value=int(step.est_min),
                    step=5,
                    key=f"{prefix}_edit_min_{step.id}",
                )
                energy = c2.selectbox(
                    "Energy",
                    [0, 1, 2],
                    index=max(0, min(int(step.energy), 2)),
                    key=f"{prefix}_edit_energy_{step.id}",
                )
                kind = c3.selectbox(
                    "Type",
                    KINDS,
                    index=KINDS.index(step.kind) if step.kind in KINDS else 0,
                    key=f"{prefix}_edit_kind_{step.id}",
                )
                if st.form_submit_button("Save changes", use_container_width=True):
                    if title.strip():
                        on_edit_step(
                            step.id,
                            title.strip(),
                            milestone,
                            outcome,
                            int(minutes),
                            int(energy),
                            kind,
                        )
                    else:
                        st.warning("Give the step a title first.")
    if on_delete_step is not None:
        with cols[4].popover("🗑", help="Delete step"):
            st.caption("This also removes its check-ins and future blocks.")
            if st.button("Confirm delete", key=f"{prefix}_del_{step.id}", use_container_width=True):
                on_delete_step(step.id)


def render_goal(
    goal: dict,
    blocks_by_step: dict[int, dict] | None = None,
    on_schedule=None,
    on_complete=None,
    on_unschedule=None,
    on_add_step=None,
    on_delete_step=None,
    on_edit_step=None,
    on_archive_goal=None,
    on_preview_replan=None,
    on_apply_replan=None,
    on_discard_replan=None,
    replan_draft: dict | None = None,
) -> None:
    blocks_by_step = blocks_by_step or {}
    with st.container(border=True):
        head = f"**{goal['title']}**"
        meta = []
        if goal.get("days_left") is not None:
            meta.append(f"{goal['days_left']} days left")
        phases = goal.get("phases") or []
        if phases:
            idx = next(
                (i + 1 for i, p in enumerate(phases) if p["state"] == "current"),
                len(phases),
            )
            meta.append(f"Phase {idx}/{len(phases)} · {goal.get('current_phase') or '—'}")
        meta.append(f"{goal['done']}/{goal['total']} steps")
        if goal.get("template_key") == "llm":
            meta.append("AI plan")
        st.markdown(head)
        st.caption(" · ".join(meta))
        st.progress(min(1.0, goal["progress"]), text=f"{goal['progress'] * 100:.0f}%")

        nxt = goal.get("next_step")
        if nxt is not None:
            block = blocks_by_step.get(nxt.id)
            st.markdown(f"**Next:** {nxt.title}")
            detail = f"{nxt.est_min} min · {nxt.kind} · energy {nxt.energy}/2"
            if block is not None:
                detail += f" · 📌 {block['start']:%a %b %d, %H:%M}"
            st.caption(detail)
            _step_actions(
                nxt, block, "next", on_schedule, on_complete, on_unschedule,
                on_delete_step, on_edit_step,
            )
        elif goal["total"] == 0:
            st.caption("No steps yet — add one below.")
        else:
            st.caption("All steps are done. 🎉")

        with st.expander("All steps", expanded=False):
            steps_by_phase: dict[str, list] = {}
            for s in goal["steps"]:
                steps_by_phase.setdefault(s.milestone, []).append(s)
            if phases:
                phase_groups = [
                    (p["name"], steps_by_phase.get(p["name"], []), p["state"])
                    for p in phases
                ]
                stray = [s for s in goal["steps"] if s.milestone not in {p["name"] for p in phases}]
                if stray:
                    phase_groups.append(("Other", stray, "current"))
            else:
                phase_groups = [("", goal["steps"], "current")]

            for phase_name, phase_steps, phase_state in phase_groups:
                if phase_name:
                    icon = {"done": "✅", "current": "▶️", "todo": "○"}[phase_state]
                    suffix = "" if phase_steps else " · Refine when you get there"
                    st.caption(f"{icon} {phase_name}{suffix}")
                for s in phase_steps:
                    block = blocks_by_step.get(s.id)
                    icon = STATUS_ICON[step_display_status(s, block is not None)]
                    line = f"{icon} {s.seq}. {s.title} · {s.est_min}m"
                    if block is not None:
                        line += f" · 📌 {block['start']:%a %b %d %H:%M}"
                    st.markdown(line)
                    if s.status == "todo":
                        _step_actions(
                            s, block, "all", on_schedule, on_complete, on_unschedule,
                            on_delete_step, on_edit_step,
                        )

            if on_add_step is not None:
                with st.form(f"add_step_{goal['id']}"):
                    st.markdown("**Add a step**")
                    title = st.text_input(
                        "Title", key=f"new_step_title_{goal['id']}",
                        placeholder="e.g. Record the intro",
                    )
                    c1, c2, c3 = st.columns(3)
                    est = c1.number_input(
                        "Minutes", min_value=5, max_value=240, value=45, step=5,
                        key=f"new_step_min_{goal['id']}",
                    )
                    energy = c2.selectbox(
                        "Energy", [0, 1, 2], index=1, key=f"new_step_energy_{goal['id']}"
                    )
                    kind = c3.selectbox(
                        "Type", ["deep", "admin", "physical", "social", "research"],
                        key=f"new_step_kind_{goal['id']}",
                    )
                    if st.form_submit_button("Add step", use_container_width=True):
                        if title.strip():
                            on_add_step(goal["id"], title.strip(), int(est), int(energy), kind)
                        else:
                            st.warning("Give the step a title first.")

        if on_preview_replan is not None:
            with st.popover("🔄 Adjust plan with AI", use_container_width=True):
                st.caption(
                    "Not a reroll. Describe what should change, preview the revised plan, "
                    "and apply it only if it fits. Blank requests are ignored."
                )
                with st.form(f"replan_form_{goal['id']}"):
                    instruction = st.text_area(
                        "What should change?",
                        placeholder="e.g. Finer-grained steps focused on problem sets; cap each step at 30 min",
                        height=78,
                        key=f"replan_instruction_{goal['id']}",
                    )
                    if st.form_submit_button(
                        "Preview revised plan", use_container_width=True
                    ):
                        on_preview_replan(goal["id"], instruction.strip())

        if replan_draft and replan_draft.get("goal_id") == goal["id"]:
            plan = replan_draft.get("plan") or []
            with st.container(border=True):
                st.markdown("**Proposed revised plan**")
                st.caption(
                    f"{len(plan)} steps · current plan has {goal['total']} steps "
                    f"({goal['done']} done). Nothing changes until you apply. "
                    "Applying replaces all steps and removes their future calendar blocks."
                )
                if replan_draft.get("instruction"):
                    st.caption(f"Your request: {replan_draft['instruction']}")
                for step_def in plan[:8]:
                    st.markdown(
                        f"{step_def['seq']}. {step_def['title']} · "
                        f"{step_def['est_min']}m"
                    )
                if len(plan) > 8:
                    st.caption(f"…and {len(plan) - 8} more steps")

                with st.expander("Compare with current plan", expanded=False):
                    old_col, new_col = st.columns(2)
                    with old_col:
                        st.caption("Current")
                        for old in goal["steps"]:
                            st.markdown(f"{old.seq}. {old.title}")
                    with new_col:
                        st.caption("Proposed")
                        for step_def in plan:
                            st.markdown(
                                f"{step_def['seq']}. {step_def['title']}"
                            )

                confirm = True
                has_progress = goal["done"] > 0 or any(
                    getattr(step, "minutes_spent", 0) for step in goal["steps"]
                )
                if has_progress:
                    confirm = st.checkbox(
                        "I understand this discards completed steps and logged time",
                        key=f"confirm_replan_{goal['id']}",
                    )
                yes, no = st.columns(2)
                if yes.button(
                    "Apply revised plan",
                    key=f"apply_replan_{goal['id']}",
                    use_container_width=True,
                    type="primary",
                    disabled=not confirm,
                ):
                    on_apply_replan(goal["id"])
                if no.button(
                    "Discard preview",
                    key=f"discard_replan_{goal['id']}",
                    use_container_width=True,
                ):
                    on_discard_replan()

        if on_archive_goal is not None:
            with st.popover("🗑 Archive goal", use_container_width=True):
                st.caption("Future blocks will be removed; progress history stays in the DB.")
                if st.button(
                    "Confirm archive goal", key=f"archive_goal_{goal['id']}",
                    use_container_width=True,
                ):
                    on_archive_goal(goal["id"])


def render_summary(goal: dict) -> None:
    phases = goal.get("phases") or []
    done_phases = sum(1 for p in phases if p["state"] == "done")
    c1, c2, c3 = st.columns(3)
    c1.metric("Progress", f"{goal['progress'] * 100:.0f}%")
    c2.metric("Phases done", f"{done_phases}/{len(phases)}" if phases else "—")
    c3.metric("Days left", goal.get("days_left") or "—")
