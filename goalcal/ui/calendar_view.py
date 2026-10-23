from __future__ import annotations

import streamlit as st


def render(events: list[dict], height: int = 560, initial_date: str | None = None) -> dict | None:
    try:
        from streamlit_calendar import calendar
    except Exception:  # pragma: no cover - 组件缺失时降级，保证 app 不崩
        st.warning("streamlit-calendar is not installed — showing a list view instead.")
        for ev in events:
            st.write(f"{ev['start'][:16]} — {ev['title']}")
        return None

    options = {
        "initialView": "timeGridWeek",
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
        "dayHeaderFormat": {"weekday": "short", "month": "numeric", "day": "numeric"},
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
    return calendar(
        events=events,
        options=options,
        callbacks=["eventClick", "dateClick", "eventChange"],
        key="goalcal_calendar",
    )
