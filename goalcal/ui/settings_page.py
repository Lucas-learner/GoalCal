from __future__ import annotations

import json

import streamlit as st

from .. import feedback, memory, metrics
from ..config import settings
from ..domain import ics_import
from ..safe_fetch import fetch_ics
from . import runtime


FEEDBACK_CATEGORY_LABELS = {
    "功能问题": "Feature issue",
    "AI 理解或计划问题": "AI understanding or planning",
    "速度或连接问题": "Speed or connection",
    "建议或其他": "Suggestion or other",
}


def _pct(value: float | None) -> str:
    return f"{value:.0%}" if value is not None else "—"


def _parse_window(value: str) -> list[list[int]]:
    result = []
    for part in value.split(","):
        start, end = part.strip().split("-")
        result.append([int(start), int(end)])
    return result


def _render_preferences() -> None:
    st.subheader("Scheduling preferences")
    st.caption("These explicit preferences guide future suggestions. You can change or reset them anytime.")
    with runtime.session_scope("preferences.read") as session:
        rows = memory.list_preferences(session, runtime.user_id(), include_expired=True)
        insight = memory.preferred_window_insight(session, runtime.user_id())
    values = {row.key: json.loads(row.value) for row in rows if row.scope == "global"}
    if values.get(memory.LEARNING_ENABLED_KEY, True) and insight:
        start, end = insight["window"]
        st.caption(
            f"History insight: {start:02d}:00–{end:02d}:00 works best across "
            f"{insight['samples']} resolved goal blocks. Explicit preferred windows take priority."
        )
    with st.form("scheduling_preferences"):
        start, end = st.columns(2)
        day_start = start.number_input("Day starts", 0, 23, int(values.get("day_start_hour", 8)))
        day_end = end.number_input("Day ends", 1, 24, int(values.get("day_end_hour", 22)))
        maximum, buffer = st.columns(2)
        max_minutes = maximum.number_input(
            "Max goal minutes per day", 15, 480, int(values.get("max_goal_minutes_per_day", 120)), step=15
        )
        buffer_minutes = buffer.number_input("Buffer minutes", 0, 60, int(values.get("buffer_minutes", 10)), step=5)
        windows = st.text_input(
            "Preferred windows",
            value=",".join(f"{start:02d}-{end:02d}" for start, end in values.get("preferred_windows", [[9, 12], [14, 18], [20, 22]])),
            help="Comma-separated 24-hour ranges, for example 09-12,14-18",
        )
        learn_from_history = st.checkbox(
            "Learn preferred windows from goal history",
            value=bool(values.get(memory.LEARNING_ENABLED_KEY, True)),
        )
        lunch = st.text_input("Protected lunch window", value="-".join(str(v) for v in values.get("lunch", [12, 13])))
        saved = st.form_submit_button("Save preferences")
    if saved:
        try:
            if day_start >= day_end:
                raise ValueError("Day starts must be earlier than day ends.")
            updates = {
                "day_start_hour": int(day_start),
                "day_end_hour": int(day_end),
                "max_goal_minutes_per_day": int(max_minutes),
                "buffer_minutes": int(buffer_minutes),
                "preferred_windows": _parse_window(windows),
                "lunch": _parse_window(lunch)[0],
                memory.LEARNING_ENABLED_KEY: learn_from_history,
            }
            with runtime.session_scope("preferences.save") as session:
                for key, value in updates.items():
                    memory.set_preference(session, runtime.user_id(), key, value)
        except (ValueError, TypeError) as exc:
            st.error(f"Preferences were not saved: {exc}")
        else:
            st.success("Preferences saved. New scheduling suggestions will use them.")
    with st.expander("Reset scheduling preferences"):
        st.caption("Restore GoalCal defaults. Your goals and conversations are not affected.")
        if st.button("Reset preferences"):
            with runtime.session_scope("preferences.reset") as session:
                for row in rows:
                    if row.key in memory.SCHEDULER_KEYS or row.key == memory.LEARNING_ENABLED_KEY:
                        memory.delete_preference(session, runtime.user_id(), row.key, row.scope)
            runtime.flash("Scheduling preferences reset to defaults.")
            st.rerun()


def _render_calendar_import() -> None:
    st.subheader("Calendar import")
    st.caption("One-way ICS import. This does not enable Google or Apple two-way sync.")
    uploaded = st.file_uploader("Choose an .ics file", type=["ics"])
    url = st.text_input("…or ICS subscription URL", key="ics_url")
    if st.button("Import / Refresh", key="ics_import"):
        content = None
        try:
            if uploaded is not None:
                content = uploaded.getvalue()
            elif url.strip():
                content = fetch_ics(url.strip())
        except Exception:
            runtime.flash("无法下载日历：请检查公开 HTTP/HTTPS 地址、大小及网络连接。")
            st.rerun()
        if content is None:
            runtime.flash("Pick an .ics file or paste a URL first.")
            st.rerun()
        try:
            with runtime.session_scope("ics.import") as session:
                result = ics_import.import_ics(session, runtime.user_id(), content, runtime.timezone())
            runtime.flash(f"Imported: {result.added} added · {result.updated} updated · {result.skipped} skipped")
        except Exception:
            runtime.flash("导入失败：文件无效或超过大小、事件数、重复规则限制；本次导入已回滚。")
        st.rerun()


def render() -> None:
    st.title("Settings")
    if flash := st.session_state.pop("flash", None):
        st.toast(flash)
    _render_preferences()
    st.divider()
    _render_calendar_import()
    st.divider()
    st.subheader("Last 7 days")
    with runtime.session_scope("metrics.read") as session:
        uid = runtime.user_id()
        acceptance = metrics.acceptance_rate(session, uid, runtime.now())
        recovery = metrics.recovery_rate_72h(session, uid, runtime.now())
        progress = metrics.progress_rate(session, uid, runtime.now())
    first, second, third = st.columns(3)
    first.metric("Acceptance", _pct(acceptance))
    second.metric("Recovery 72h", _pct(recovery))
    third.metric("Progress", _pct(progress))
    backend = "LLM + rules fallback" if settings.llm_ready else "rules only (no API key)"
    st.caption(f"Parser: {backend} · Last turn: {st.session_state.get('last_parser', '—')}")
    st.divider()
    with st.expander("Feedback and suggestions", expanded=False):
        st.caption("Feedback is saved locally for the maintainers. Chat content is not added to runtime logs automatically.")
        with st.form("trial_feedback", clear_on_submit=True):
            feedback_category = st.selectbox(
                "Category", feedback.CATEGORIES, key="feedback_category",
                format_func=FEEDBACK_CATEGORY_LABELS.__getitem__,
            )
            feedback_message = st.text_area(
                "What happened? What would you like us to improve?", max_chars=2000,
                help="Describe the steps, expected result, and actual result. Do not include passwords or API keys.",
            )
            feedback_sent = st.form_submit_button("Send feedback")
        if feedback_sent:
            if len(feedback_message.strip()) < 5:
                st.warning("Please enter at least 5 characters of feedback.")
            else:
                with runtime.session_scope("feedback.submit") as session:
                    feedback_id = feedback.submit(
                        session, feedback_category, feedback_message, st.session_state.get("last_error_id")
                    )
                st.success(f"Feedback saved as #{feedback_id}. Thank you!")
