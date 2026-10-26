from __future__ import annotations

from datetime import datetime

import streamlit as st

from ..domain.scheduler import Candidate

# 候选卡片上方的窗口快捷过滤：(chip key, 展示文案)
FILTER_CHIPS = [
    ("today_pm", "Today PM"),
    ("tonight", "Tonight"),
    ("tomorrow", "Tomorrow"),
    ("weekend", "This weekend"),
]


def _day_label(start: datetime, today) -> str:
    delta = (start.date() - today).days
    name = {0: "Today", 1: "Tomorrow"}.get(delta)
    return f"{name} · {start:%a %b %d}" if name else f"{start:%a %b %d}"


def render_proposals(
    candidates: list[Candidate],
    step_id: int | None,
    on_accept,
    on_dismiss=None,
    on_filter=None,
    now: datetime | None = None,
    start_index: int = 0,
) -> None:
    """对话内的编号候选卡片；on_filter 收到 chip key（any 表示不过滤）。"""
    if not candidates or step_id is None:
        return
    if on_filter is not None:
        keys = ["any"] + [k for k, _ in FILTER_CHIPS]
        labels = ["Any time"] + [label for _, label in FILTER_CHIPS]
        sel_key = f"filter_sel_{step_id}"
        st.selectbox(
            "Window",
            range(len(labels)),
            key=sel_key,
            format_func=lambda i: labels[i],
            label_visibility="collapsed",
            on_change=lambda: on_filter(keys[st.session_state[sel_key]]),
        )
    today = (now or datetime.now(tz=candidates[0].start.tzinfo)).date()
    last_day = None
    for i, c in enumerate(candidates, start_index + 1):
        if c.start.date() != last_day:
            last_day = c.start.date()
            st.caption(_day_label(c.start, today))
        slot, book = st.columns([3, 1])
        slot.markdown(f"**{i}. {c.start:%H:%M}–{c.end:%H:%M}** · {c.minutes} min")
        if c.reasons:
            slot.caption(c.reasons[0])
        if book.button(
            "Book",
            key=f"book_{step_id}_{i}_{c.start.isoformat()}",
            use_container_width=True,
            type="primary",
        ):
            on_accept(step_id, c)
    if on_dismiss is not None:
        if st.button("Dismiss", key=f"dismiss_{step_id}", use_container_width=True):
            on_dismiss()


def render_batch(batch: list[dict], on_accept, on_dismiss=None) -> None:
    """批量排期卡片：列出每块（步骤标题 + 起止时间），一个 Book all / Dismiss。"""
    if not batch:
        return
    head, dismiss = st.columns([4, 1])
    head.markdown("**Batch plan**")
    if on_dismiss is not None and dismiss.button(
        "Dismiss", key="dismiss_batch", use_container_width=True
    ):
        on_dismiss()
    for item in batch:
        with st.container(border=True):
            st.markdown(f"**{item['title']}**")
            st.caption(f"{item['start']:%a %b %d · %H:%M}–{item['end']:%H:%M}")
    if st.button("Book all", key="book_all", use_container_width=True, type="primary"):
        on_accept(batch)


def render_messages(messages: list[dict]) -> None:
    for msg in messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])


def append(messages: list[dict], role: str, content: str) -> None:
    messages.append({"role": role, "content": content})
