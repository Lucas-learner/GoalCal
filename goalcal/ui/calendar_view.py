from __future__ import annotations

import streamlit as st
from datetime import datetime
from zoneinfo import ZoneInfo


def calendar_wall_time(value: str, tz: str) -> datetime:
    """The component transports account wall-clock values using UTC coercion."""
    return datetime.fromisoformat(value).replace(tzinfo=ZoneInfo(tz))


def remember_view(session_state, callback):
    """Keep the visible day/week when clearing an old component callback."""
    kind = callback.get("callback")
    view = callback.get(kind, {}).get("view", {})
    if view.get("type") in {"timeGridWeek", "timeGridDay", "listWeek"}:
        session_state["_calendar_view"] = view["type"]
    try:
        date = datetime.fromisoformat(view["currentStart"]).date().isoformat()
    except (KeyError, ValueError, TypeError):
        return
    session_state["_calendar_date"] = date


def dismiss_dialog(session_state):
    for key in ("selected_event_id", "confirm_delete_id", "new_event_start", "_cal_sig"):
        session_state.pop(key, None)
    # The third-party component retains its last callback and does not attach a
    # click sequence number. Remount only on close so stale clicks cannot replay
    # and a fresh click on the very same event/time can be recognized again.
    session_state["_calendar_generation"] = session_state.get("_calendar_generation", 0) + 1


def render(events: list[dict], height: int = 560, initial_date: str | None = None, tz: str = "Asia/Shanghai",
           key: str = "goalcal_calendar", initial_view: str = "timeGridWeek") -> dict | None:
    try:
        from streamlit_calendar import calendar
    except Exception:  # pragma: no cover - 组件缺失时降级，保证 app 不崩
        st.warning("streamlit-calendar is not installed — showing a list view instead.")
        for ev in events:
            st.write(f"{ev['start'][:16]} — {ev['title']}")
        return None

    options = {
        "timeZone": "UTC",
        "now": datetime.now(ZoneInfo(tz)).replace(tzinfo=None).isoformat(),
        "initialView": initial_view,
        "firstDay": 1,
        "editable": True,
        "selectable": True,
        "nowIndicator": True,
        "expandRows": True,
        "stickyHeaderDates": True,
        "headerToolbar": {
            "left": "prev,next today",
            "center": "title",
            "right": "timeGridWeek,timeGridDay,listWeek",
        },
        "buttonText": {"today": "Today", "week": "Week", "day": "Day", "list": "List"},
        "dayHeaderFormat": {"weekday": "short", "month": "numeric", "day": "numeric", "omitCommas": True},
        "slotLabelFormat": {"hour": "2-digit", "minute": "2-digit", "hour12": False},
        "slotMinTime": "06:00:00",
        "slotMaxTime": "24:00:00",
        "scrollTime": "08:00:00",
        "height": height,
        "allDaySlot": False,
        "eventDisplay": "block",
        "dayMaxEventRows": 3,
        "eventTimeFormat": {"hour": "2-digit", "minute": "2-digit", "hour12": False},
    }
    if initial_date:
        options["initialDate"] = initial_date
    custom_css = """
    .fc .fc-toolbar-title { font-size: 1.05rem; font-weight: 600; }
    .fc .fc-button { padding: 0.15em 0.55em; font-size: 0.8rem; }
    .fc .fc-col-header-cell-cushion { font-size: 0.8rem; }
    .fc .fc-timegrid-slot-label-cushion { font-size: 0.75rem; }
    """
    # The packaged component has no named-zone plugin. Send account wall time
    # without an offset and decode callbacks symmetrically on the server.
    wall_events = [dict(ev, start=datetime.fromisoformat(ev["start"]).replace(tzinfo=None).isoformat(),
                        end=datetime.fromisoformat(ev["end"]).replace(tzinfo=None).isoformat()) for ev in events]
    return calendar(
        events=wall_events,
        options=options,
        custom_css=custom_css,
        callbacks=["eventClick", "dateClick", "eventChange"],
        key=key,
    )
