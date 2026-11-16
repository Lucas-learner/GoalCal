from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from goalcal.domain import ics_import
from goalcal.models import Event

TZ = "Asia/Shanghai"
NOW = datetime(2026, 10, 22, 4, 0)  # UTC naive

_SINGLE = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:evt-1
SUMMARY:Dentist
DTSTART:20261024T060000Z
DTEND:20261024T070000Z
END:VEVENT
END:VCALENDAR
"""

_SINGLE_UPDATED = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:evt-1
SUMMARY:Dentist (moved)
DTSTART:20261024T080000Z
DTEND:20261024T090000Z
END:VEVENT
END:VCALENDAR
"""


def _import(session, user, content, now=NOW):
    return ics_import.import_ics(session, user.id, content, TZ, now=now)


def _events(session, user):
    return list(session.scalars(select(Event).where(Event.user_id == user.id)))


def test_import_single_event(session, user):
    result = _import(session, user, _SINGLE)
    assert (result.added, result.updated, result.skipped) == (1, 0, 0)

    ev = _events(session, user)[0]
    assert ev.title == "Dentist"
    assert ev.kind == "external"
    assert ev.source == "import"
    assert ev.uid == "evt-1"
    assert ev.start_utc == datetime(2026, 10, 24, 6, 0)
    assert ev.end_utc == datetime(2026, 10, 24, 7, 0)


def test_reimport_updates_in_place_by_uid(session, user):
    _import(session, user, _SINGLE)
    result = _import(session, user, _SINGLE_UPDATED)
    assert (result.added, result.updated, result.skipped) == (0, 1, 0)

    events = _events(session, user)
    assert len(events) == 1
    assert events[0].title == "Dentist (moved)"
    assert events[0].start_utc == datetime(2026, 10, 24, 8, 0)


def test_weekly_rrule_expands_with_numbered_uids(session, user):
    content = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:evt-weekly
SUMMARY:Team sync
DTSTART:20261026T090000Z
DTEND:20261026T093000Z
RRULE:FREQ=WEEKLY;COUNT=3
END:VEVENT
END:VCALENDAR
"""
    result = _import(session, user, content)
    assert (result.added, result.skipped) == (3, 0)

    events = sorted(_events(session, user), key=lambda ev: ev.start_utc)
    assert [ev.uid for ev in events] == ["evt-weekly#1", "evt-weekly#2", "evt-weekly#3"]
    assert events[0].start_utc == datetime(2026, 10, 26, 9, 0)
    assert events[1].start_utc == datetime(2026, 11, 2, 9, 0)
    assert events[2].start_utc == datetime(2026, 11, 9, 9, 0)
    for ev in events:
        assert ev.end_utc - ev.start_utc == timedelta(minutes=30)


def test_unsupported_frequency_is_skipped(session, user):
    content = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:evt-monthly
SUMMARY:Rent
DTSTART:20261101T090000Z
DTEND:20261101T093000Z
RRULE:FREQ=MONTHLY;COUNT=2
END:VEVENT
END:VCALENDAR
"""
    result = _import(session, user, content)
    assert (result.added, result.skipped) == (0, 1)
    assert _events(session, user) == []


def test_all_day_event_is_skipped(session, user):
    content = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:evt-allday
SUMMARY:Holiday
DTSTART;VALUE=DATE:20261026
DTEND;VALUE=DATE:20261027
END:VEVENT
END:VCALENDAR
"""
    result = _import(session, user, content)
    assert (result.added, result.skipped) == (0, 1)
    assert _events(session, user) == []


def test_tzid_event_converts_to_utc(session, user):
    content = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:evt-tz
SUMMARY:Call NY
DTSTART;TZID=America/New_York:20261026T100000
DTEND;TZID=America/New_York:20261026T110000
END:VEVENT
END:VCALENDAR
"""
    result = _import(session, user, content)
    assert result.added == 1
    ev = _events(session, user)[0]
    assert ev.start_utc == datetime(2026, 10, 26, 14, 0)  # EDT = UTC-4
    assert ev.end_utc == datetime(2026, 10, 26, 15, 0)


def test_missing_dtend_defaults_to_sixty_minutes(session, user):
    content = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:evt-noend
SUMMARY:Quick call
DTSTART:20261024T060000Z
END:VEVENT
END:VCALENDAR
"""
    result = _import(session, user, content)
    assert result.added == 1
    ev = _events(session, user)[0]
    assert ev.end_utc - ev.start_utc == timedelta(minutes=60)


def test_duration_used_when_dtend_missing(session, user):
    content = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:evt-duration
SUMMARY:Standup
DTSTART:20261024T060000Z
DURATION:PT15M
END:VEVENT
END:VCALENDAR
"""
    result = _import(session, user, content)
    assert result.added == 1
    ev = _events(session, user)[0]
    assert ev.end_utc - ev.start_utc == timedelta(minutes=15)


def test_floating_time_uses_user_tz(session, user):
    content = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Test//EN
BEGIN:VEVENT
UID:evt-float
SUMMARY:Lunch
DTSTART:20261024T120000
DTEND:20261024T130000
END:VEVENT
END:VCALENDAR
"""
    result = _import(session, user, content)
    assert result.added == 1
    ev = _events(session, user)[0]
    assert ev.start_utc == datetime(2026, 10, 24, 4, 0)  # 12:00 Asia/Shanghai = 04:00 UTC
