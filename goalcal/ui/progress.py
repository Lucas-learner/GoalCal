from __future__ import annotations

import streamlit as st


def render_goal(goal: dict) -> None:
    with st.container(border=True):
        head = f"**{goal['title']}**"
        meta = []
        if goal.get("days_left") is not None:
            meta.append(f"{goal['days_left']} days left")
        meta.append(f"{goal['done']}/{goal['total']} steps")
        st.markdown(head)
        st.caption(" · ".join(meta))
        st.progress(min(1.0, goal["progress"]), text=f"{goal['progress'] * 100:.0f}%")

        nxt = goal.get("next_step")
        if nxt is not None:
            st.markdown(f"**Next:** {nxt.title}")
            st.caption(f"{nxt.est_min} min · {nxt.kind} · energy {nxt.energy}/2")

        with st.expander("All steps"):
            for s in goal["steps"]:
                icon = {"done": "✅", "scheduled": "📌", "todo": "○", "skipped": "↷"}[s.status]
                st.write(f"{icon} {s.seq}. {s.title} · {s.est_min}m")


def render_summary(goal: dict) -> None:
    c1, c2, c3 = st.columns(3)
    c1.metric("Progress", f"{goal['progress'] * 100:.0f}%")
    c2.metric("Steps done", f"{goal['done']}/{goal['total']}")
    c3.metric("Days left", goal.get("days_left") or "—")
