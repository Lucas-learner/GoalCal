from __future__ import annotations

from datetime import datetime, timedelta

import streamlit as st

from goalcal import agent
from goalcal.config import settings
from goalcal.db import get_engine, init_db, session_scope
from goalcal.domain import calendar_service as cal
from goalcal.domain import goals as goal_svc
from goalcal.models import Base, Event
from goalcal.seed import seed_if_empty
from goalcal.timeutil import as_local, as_utc_naive, local_now, now_utc
from goalcal.ui import calendar_view, chat as chat_ui, progress

st.set_page_config(page_title="GoalCal", page_icon="📆", layout="wide")
init_db()
TZ = settings.tz


def _reset_demo() -> None:
    from goalcal.db import new_session

    Base.metadata.drop_all(get_engine())
    Base.metadata.create_all(get_engine())
    with session_scope() as s:
        seed_if_empty(s, TZ)
    st.session_state.pop("messages", None)
    st.session_state.pop("pending", None)
    st.session_state["user_id"] = None


def _ensure_state() -> None:
    if st.session_state.get("user_id"):
        return
    with session_scope() as s:
        user = seed_if_empty(s, TZ)
        st.session_state["user_id"] = user.id
    if "messages" not in st.session_state:
        st.session_state["messages"] = [
            {
                "role": "assistant",
                "content": "Hi — tell me a goal, or just add something to your calendar.",
            }
        ]
    st.session_state.setdefault("pending", None)


def _now() -> datetime:
    return local_now(TZ) + timedelta(days=st.session_state.get("day_offset", 0))


def _run_turn(text: str) -> None:
    st.session_state["messages"].append({"role": "user", "content": text})
    with session_scope() as s:
        reply = agent.handle(s, st.session_state["user_id"], text, _now(), TZ)
    st.session_state["messages"].append({"role": "assistant", "content": reply.text})
    st.session_state["pending"] = (
        {"candidates": reply.candidates, "step_id": reply.step_id} if reply.candidates else None
    )
    st.session_state["last_parser"] = reply.parser


def _accept(step_id: int, candidate) -> None:
    with session_scope() as s:
        msg = agent.accept(s, st.session_state["user_id"], step_id, candidate, TZ)
    st.session_state["messages"].append({"role": "assistant", "content": msg})
    st.session_state["pending"] = None
    st.rerun()


_ensure_state()

# ---------------------------------------------------------------- sidebar

with st.sidebar:
    st.header("Demo controls")
    st.slider(
        "Time machine (days ahead)", 0, 14, 0,
        help="F4 的提醒/重排是时间触发的，演示时用它快进。",
        key="day_offset",
    )
    st.caption(f"Now: {_now():%a %b %d, %H:%M}")

    st.markdown("**How do you feel right now?**")
    c1, c2, c3 = st.columns(3)
    for col, label, text in [
        (c1, "😴 Tired", "I'm tired"),
        (c2, "🔥 Busy", "I'm busy"),
        (c3, "🫥 Stuck", "I'm uninspired"),
    ]:
        if col.button(label, use_container_width=True):
            _run_turn(text)
            st.rerun()

    st.divider()
    backend = "LLM + rules fallback" if settings.llm_ready else "rules only (no API key)"
    st.caption(f"Parser: {backend}")
    st.caption(f"Last turn: {st.session_state.get('last_parser', '—')}")
    if st.button("Reset demo data", use_container_width=True):
        _reset_demo()
        st.rerun()

# ---------------------------------------------------------------- header

st.title("📆 GoalCal")
st.caption("A calendar that advances your goals, not just your meetings.")

chat_col, cal_col = st.columns([1.05, 1], gap="large")

with chat_col:
    chat_ui.render_messages(st.session_state["messages"])

    pending = st.session_state.get("pending")
    if pending:
        chat_ui.render_proposals(
            pending["candidates"], pending["step_id"], _accept
        )

    prompt = st.chat_input("Tell me a goal, or add something to your calendar…")
    if prompt:
        _run_turn(prompt)
        st.rerun()

with cal_col:
    start_utc = now_utc() - timedelta(days=7)
    end_utc = now_utc() + timedelta(days=28)
    with session_scope() as s:
        events = cal.events_between(s, st.session_state["user_id"], start_utc, end_utc)
        fc_events = cal.to_fullcalendar(events, TZ)

    state = calendar_view.render(fc_events)

    if state and state.get("callback") == "eventChange":
        ev = state["eventChange"]["event"]
        start = datetime.fromisoformat(ev["start"])
        end = datetime.fromisoformat(ev["end"])
        with session_scope() as s:
            row = s.get(Event, int(ev["id"]))
            if row is not None:
                row.start_utc = as_utc_naive(start, TZ)
                row.end_utc = as_utc_naive(end, TZ)
                row.status = "moved"
        st.rerun()

    st.subheader("Goals")
    with session_scope() as s:
        goals = goal_svc.active_goals(s, st.session_state["user_id"])
        snaps = [goal_svc.snapshot(g) for g in goals]
    if not snaps:
        st.info("No active goal yet — tell me one in the chat.")
    for snap in snaps:
        progress.render_goal(snap)
