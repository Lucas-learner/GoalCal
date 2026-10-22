from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from goalcal.domain import calendar_service as cal
from goalcal.timeutil import as_local, as_utc_naive

TZ = "Asia/Shanghai"
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=ZoneInfo(TZ))


def _event(session, user, title, start, minutes=60, kind="external"):
    return cal.create_event(
        session, user.id, title, start, start + timedelta(minutes=minutes),
        kind=kind, tz=TZ,
    )


def test_create_event_converts_local_to_utc(session, user):
    ev = _event(session, user, "Meeting", NOW)
    assert ev.start_utc == datetime(2026, 10, 21, 1, 0)
    assert ev.end_utc == datetime(2026, 10, 21, 2, 0)


def test_create_event_clamps_invalid_range(session, user):
    ev = cal.create_event(session, user.id, "Broken", NOW, NOW, tz=TZ)
    assert ev.end_utc - ev.start_utc == timedelta(minutes=60)


def test_events_between_uses_half_open_overlap(session, user):
    _event(session, user, "Before", NOW - timedelta(days=2), 60)
    inside = _event(session, user, "Inside", NOW + timedelta(hours=1), 60)
    _event(session, user, "After", NOW + timedelta(days=2), 60)

    got = cal.events_between(
        session, user.id,
        as_utc_naive(NOW, TZ), as_utc_naive(NOW + timedelta(days=1), TZ),
    )
    assert [e.id for e in got] == [inside.id]


def test_find_event_prefers_upcoming_over_past(session, user):
    _event(session, user, "Gym", NOW - timedelta(days=10))
    future = _event(session, user, "Gym", NOW + timedelta(days=1))

    found = cal.find_event(session, user.id, "gym", now_utc=as_utc_naive(NOW, TZ))
    assert found is not None and found.id == future.id


def test_find_event_falls_back_to_fuzzy_match(session, user):
    ev = _event(session, user, "Dentist appointment", NOW + timedelta(days=2))
    found = cal.find_event(session, user.id, "dentist apointment", as_utc_naive(NOW, TZ))
    assert found is not None and found.id == ev.id


def test_move_event_preserves_duration_and_marks_moved(session, user):
    ev = _event(session, user, "Workout", NOW, 90)
    cal.move_event(session, ev, NOW + timedelta(days=2, hours=3), TZ)
    assert ev.status == "moved"
    assert ev.end_utc - ev.start_utc == timedelta(minutes=90)
    assert as_local(ev.start_utc, TZ).hour == 12


def test_to_fullcalendar_uses_local_time_and_kind_color(session, user):
    ev = _event(session, user, "Deep work", NOW, 45, kind="block")
    out = cal.to_fullcalendar([ev], TZ)
    assert out[0]["start"] == "2026-10-21T09:00:00+08:00"
    assert out[0]["end"] == "2026-10-21T09:45:00+08:00"
    assert out[0]["color"] == {"external": "#94a3b8", "block": "#0f766e", "buffer": "#e2e8f0"}["block"]
    assert out[0]["extendedProps"]["kind"] == "block"
