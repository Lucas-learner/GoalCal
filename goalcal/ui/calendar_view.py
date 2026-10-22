from __future__ import annotations

import streamlit as st


def render(events: list[dict], height: int = 560) -> dict | None:
    try:
        from streamlit_calendar import calendar
    except Exception:  # pragma: no cover - 组件缺失时降级，保证 app 不崩
        st.warning("streamlit-calendar is not installed — showing a list view instead.")
        for ev in events:
            st.write(f"{ev['start'][:16]} — {ev['title']}")
        return None

    options = {
        "initialView": "timeGridWeek",
        "editable": True,
        "selectable": True,
        "nowIndicator": True,
        "headerToolbar": {
            "left": "prev,next today",
            "center": "title",
            "right": "timeGridWeek,timeGridDay,listWeek",
        },
        "slotMinTime": "07:00:00",
        "slotMaxTime": "23:00:00",
        "scrollTime": "08:00:00",
        "height": height,
        "allDaySlot": False,
        "eventTimeFormat": {"hour": "2-digit", "minute": "2-digit", "hour12": False},
    }
    return calendar(
        events=events,
        options=options,
        callbacks=["eventClick", "dateClick", "eventChange"],
        key="goalcal_calendar",
    )
