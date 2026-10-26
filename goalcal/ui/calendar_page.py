from __future__ import annotations

from datetime import datetime, timedelta

import streamlit as st

from ..domain import calendar_service as cal
from ..models import Event, Step
from ..timeutil import as_local, as_utc_naive
from . import calendar_view, runtime


def _dismiss_dialog() -> None:
    calendar_view.dismiss_dialog(st.session_state)


def _delete_event(event_id: int) -> None:
    with runtime.session_scope("delete_calendar_event") as session:
        event = session.get(Event, event_id)
        title = event.title if event is not None else ""
        if event is not None:
            cal.delete_event(session, event)
    _dismiss_dialog()
    runtime.flash(f"Deleted “{title}”." if title else "Event already gone.")
    st.rerun()


def _create_event(title: str, start: datetime, minutes: int) -> None:
    with runtime.session_scope("create_quick_event") as session:
        cal.create_event(
            session, runtime.user_id(), title, start,
            start + timedelta(minutes=minutes), source="ui", tz=runtime.timezone(),
        )
    _dismiss_dialog()
    runtime.flash(f"Added “{title}” on {start:%a %b %d, %H:%M}.")
    st.rerun()


@st.dialog("Event details", on_dismiss=_dismiss_dialog)
def _event_details(event_id: int) -> None:
    with runtime.session_scope("event_detail_dialog") as session:
        row = session.get(Event, event_id)
        detail = None
        if row is not None:
            step_title = None
            if row.step_id is not None:
                step = session.get(Step, row.step_id)
                if step is not None:
                    step_title = f"{step.goal.title} · {step.title}"
            detail = {
                "title": row.title,
                "start": as_local(row.start_utc, runtime.timezone()),
                "end": as_local(row.end_utc, runtime.timezone()),
                "kind": row.kind,
                "source": row.source,
                "step": step_title,
            }
    if detail is None:
        st.info("That event is already gone.")
        if st.button("Close", key=f"close_gone_{event_id}", use_container_width=True):
            _dismiss_dialog()
            st.rerun()
        return
    st.markdown(f"**{detail['title']}**")
    st.caption(
        f"{detail['start']:%a %b %d, %H:%M}–{detail['end']:%H:%M} · "
        f"{detail['kind']} · {detail['source']}"
    )
    if detail["step"]:
        st.caption(f"Goal step: {detail['step']}")
    if st.session_state.get("confirm_delete_id") == event_id:
        st.warning("Delete this event? This cannot be undone.")
        yes, no = st.columns(2)
        if yes.button("Yes, delete", key=f"confirm_{event_id}", use_container_width=True):
            _delete_event(event_id)
        if no.button("Keep it", key=f"keep_{event_id}", use_container_width=True):
            st.session_state["confirm_delete_id"] = None
            st.rerun()
    else:
        delete, close = st.columns(2)
        if delete.button("Delete event", key=f"delete_{event_id}", use_container_width=True):
            st.session_state["confirm_delete_id"] = event_id
            st.rerun()
        if close.button("Close", key=f"close_{event_id}", use_container_width=True):
            _dismiss_dialog()
            st.rerun()


@st.dialog("New event", on_dismiss=_dismiss_dialog)
def _new_event(start: datetime) -> None:
    with st.form("new_event_form", clear_on_submit=True):
        title = st.text_input("Title", placeholder="e.g. Coffee with Amy")
        picked_time = st.time_input("Start", value=start.time())
        minutes = st.selectbox("Duration", [30, 45, 60, 90, 120], index=2)
        submitted = st.form_submit_button("Add event", use_container_width=True, type="primary")
    if st.button("Cancel", key="cancel_new_event", use_container_width=True):
        _dismiss_dialog()
        st.rerun()
    if submitted:
        if not title.strip():
            st.warning("Give the event a title first.")
        else:
            picked = datetime.combine(start.date(), picked_time, tzinfo=runtime.zone())
            _create_event(title.strip(), picked, int(minutes))


def _remember_range(state: dict) -> None:
    callback = state.get("callback")
    payload = state.get(callback, {})
    if callback == "datesSet":
        view = payload.get("view", {})
        start = payload.get("startStr") or view.get("currentStart")
        end = payload.get("endStr") or view.get("currentEnd")
        if start and end:
            st.session_state["_calendar_start"] = datetime.fromisoformat(start.replace("Z", "+00:00")).date().isoformat()
            st.session_state["_calendar_end"] = datetime.fromisoformat(end.replace("Z", "+00:00")).date().isoformat()


def render(*, active: bool = True) -> None:
    with st.container(key="calendar_heading"):
        if active:
            st.title("Calendar")
            st.caption("Your local schedule and goal blocks. Calendar connections are managed in Settings.")
    if active and (flash := st.session_state.pop("flash", None)):
        st.toast(flash)
    current = runtime.now()
    start_date = st.session_state.get("_calendar_start", (current - timedelta(days=7)).date().isoformat())
    end_date = st.session_state.get("_calendar_end", (current + timedelta(days=28)).date().isoformat())
    start_local = datetime.combine(datetime.fromisoformat(start_date).date(), datetime.min.time(), tzinfo=runtime.zone())
    end_local = datetime.combine(datetime.fromisoformat(end_date).date(), datetime.min.time(), tzinfo=runtime.zone())
    with runtime.session_scope("calendar.read") as session:
        events = cal.events_between(
            session, runtime.user_id(), as_utc_naive(start_local, runtime.timezone()),
            as_utc_naive(end_local, runtime.timezone()),
        )
        fc_events = cal.to_fullcalendar(events, runtime.timezone())
    state = calendar_view.render(
        fc_events, height=680, tz=runtime.timezone(),
        initial_date=st.session_state.get("_calendar_date", current.date().isoformat()),
        initial_view=st.session_state.get("_calendar_view", "timeGridWeek"),
        key=f"goalcal_calendar_{st.session_state.get('_calendar_generation', 0)}",
    )
    if state:
        signature = repr(state)
        if signature != st.session_state.get("_cal_sig"):
            st.session_state["_cal_sig"] = signature
            calendar_view.remember_view(st.session_state, state)
            _remember_range(state)
            callback = state.get("callback")
            if not active and callback != "datesSet":
                return
            if callback == "eventClick":
                st.session_state["selected_event_id"] = int(state["eventClick"]["event"]["id"])
                st.session_state["confirm_delete_id"] = None
                st.session_state["new_event_start"] = None
            elif callback == "dateClick":
                st.session_state["confirm_delete_id"] = None
                info = state["dateClick"]
                picked = calendar_view.calendar_wall_time(info["date"], runtime.timezone())
                if info.get("allDay"):
                    picked = picked.replace(hour=9, minute=0, second=0, microsecond=0)
                st.session_state["new_event_start"] = picked
                st.session_state["selected_event_id"] = None
            elif callback == "eventChange":
                moved = state["eventChange"]["event"]
                start = calendar_view.calendar_wall_time(moved["start"], runtime.timezone())
                end = calendar_view.calendar_wall_time(moved["end"], runtime.timezone())
                with runtime.session_scope("calendar.move") as session:
                    row = session.get(Event, int(moved["id"]))
                    if row is not None and end > start:
                        cal.move_event(session, row, start, runtime.timezone())
                        row.end_utc = as_utc_naive(end, runtime.timezone())
                        runtime.flash(f"Updated “{row.title}”.")
                st.rerun()
            elif callback == "datesSet":
                st.rerun()
    if active and (selected_id := st.session_state.get("selected_event_id")):
        _event_details(selected_id)
    if active and (new_start := st.session_state.get("new_event_start")):
        _new_event(new_start)
