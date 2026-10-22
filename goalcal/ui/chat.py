from __future__ import annotations

from datetime import datetime

import streamlit as st

from ..domain.scheduler import Candidate


def render_proposals(
    candidates: list[Candidate], step_id: int | None, on_accept, on_dismiss=None
) -> None:
    """候选时段卡片：演示时最稳的交互，也是 acceptance rate 的埋点位置。"""
    if not candidates or step_id is None:
        return
    head, dismiss = st.columns([4, 1])
    head.markdown("**Suggested slots**")
    if on_dismiss is not None and dismiss.button(
        "Dismiss", key=f"dismiss_{step_id}", use_container_width=True
    ):
        on_dismiss()
    for i, c in enumerate(candidates, 1):
        label = f"{c.start:%a %b %d · %H:%M}–{c.end:%H:%M} · {c.minutes} min · {c.score:.0%} match"
        with st.container(border=True):
            st.markdown(f"**{label}**")
            st.caption(" · ".join(c.reasons))
            if st.button("Book this", key=f"book_{step_id}_{i}_{c.start.isoformat()}", use_container_width=True, type="primary"):
                on_accept(step_id, c)


def render_messages(messages: list[dict]) -> None:
    for msg in messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])


def append(messages: list[dict], role: str, content: str) -> None:
    messages.append({"role": role, "content": content})
