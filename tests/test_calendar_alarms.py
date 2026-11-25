"""Tests for Stage 0 proactive reminders: native calendar alarms, outcomes, and deep links."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import uuid
from xml.sax.saxutils import escape as xml_escape

import httpx2
from icalendar import Calendar
import pytest
from sqlalchemy import select

from goalcal import auth, crypto, db, migrations, storage
from goalcal.domain import bidirectional_sync, calendar_sync, reminders
from goalcal.domain.sync_providers import CalDavAdapter, GoogleCalendarAdapter, OutlookCalendarAdapter
from goalcal.models import CalendarFeed, Event, Goal, Step, SyncOutbox, User
from goalcal.web.http import api

ORIGIN = "https://goalcal.test"
PASSWORD = "long-alarm-test-password"


@pytest.fixture()
def accounts(tmp_path, monkeypatch):
    path = tmp_path / "alarms.db"
    monkeypatch.setattr(db, "settings", replace(db.settings, db_url=f"sqlite:///{path}"))
    monkeypatch.setattr(storage, "database_path", lambda url=None: path)
    monkeypatch.setattr(migrations, "database_path", lambda url=None: path)
    monkeypatch.setattr(crypto, "database_path", lambda url=None: path)
    db.init_db()
    ids = []
    tokens = []
    for _ in range(2):
        name = "alarm_" + uuid.uuid4().hex[:10]
        ids.append(auth.create_user(name, PASSWORD, admin=True))
        tokens.append(auth.login(name, PASSWORD))
    return ids, tokens


def api_request(token, method, path, body=None, *, key=None, headers=None):
    async def run():
        h = {"Origin": ORIGIN, "Idempotency-Key": key or uuid.uuid4().hex}
        h.update(headers or {})
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=api),
            base_url=ORIGIN,
            cookies={"goalcal_session": token},
        ) as client:
            return await client.request(method, path, json=body, headers=h)

    import asyncio
    return asyncio.run(run())


def test_reminder_normalization_resolution_and_deep_link(monkeypatch):
    assert reminders.normalize_calendar_alarms(
        {"enabled": True, "block_minutes": 0, "habit_minutes": 15, "manual_minutes": None}
    ) == {
        "enabled": True,
        "block_minutes": 0,
        "habit_minutes": 15,
        "manual_minutes": None,
    }

    for bad in (
        {"enabled": "yes"},
        {"enabled": True, "block_minutes": True},
        {"enabled": True, "block_minutes": -1},
        {"enabled": True, "habit_minutes": 1441},
        {"enabled": True, "manual_minutes": "10"},
    ):
        with pytest.raises(ValueError):
            reminders.normalize_calendar_alarms(bad)

    cfg = {"enabled": True, "block_minutes": 10, "habit_minutes": 0, "manual_minutes": None}
    assert reminders.resolve_event_alarm_minutes(cfg, kind="block", effective_status="planned") == 10
    assert reminders.resolve_event_alarm_minutes(cfg, kind="block", effective_status="moved") == 10
    assert reminders.resolve_event_alarm_minutes(cfg, kind="habit", effective_status="planned") == 0
    assert reminders.resolve_event_alarm_minutes(cfg, kind="external", effective_status="planned") is None
    assert reminders.resolve_event_alarm_minutes(cfg, kind="block", effective_status="done") is None
    assert reminders.resolve_event_alarm_minutes(cfg, kind="block", effective_status="planned", all_day=True) is None
    assert reminders.resolve_event_alarm_minutes({**cfg, "enabled": False}, kind="block", effective_status="planned") is None

    # UTC 2026-11-25 18:00 -> Asia/Shanghai 2026-11-26 02:00
    link = reminders.event_deep_link("https://goalcal.zhang.sarl/", 42, datetime(2026, 11, 25, 18, 0), "Asia/Shanghai")
    assert link == "https://goalcal.zhang.sarl/app/calendar?date=2026-11-26&event=42"

    monkeypatch.setenv("GOALCAL_PUBLIC_URL", "https://goalcal.zhang.sarl")
    assert (
        reminders.event_deep_link(None, 7, datetime(2026, 11, 25, 2, 0), "Asia/Shanghai")
        == "https://goalcal.zhang.sarl/app/calendar?date=2026-11-25&event=7"
    )


def test_ics_feed_includes_valarm_outcome_deep_link_and_respects_status(accounts):
    ids, _ = accounts
    uid = ids[0]
    day = (datetime.now(timezone.utc) + timedelta(days=3)).date()

    s = db.new_session()
    try:
        user = s.get(User, uid)
        feed = CalendarFeed(user_id=uid, token="tok_" + uuid.uuid4().hex, enabled=True)
        s.add(feed)
        goal = Goal(user_id=uid, title="Prepare Talk")
        s.add(goal)
        s.flush()
        step_todo = Step(goal_id=goal.id, seq=0, title="Draft Outline", outcome="3 key bullet points", est_min=30)
        step_done = Step(goal_id=goal.id, seq=1, title="Collect References", outcome="5 links", est_min=20, status="done")
        s.add_all([step_todo, step_done])
        s.flush()
        ev_planned = Event(
            user_id=uid,
            title="Draft Outline",
            start_utc=datetime(day.year, day.month, day.day, 2, 0),
            end_utc=datetime(day.year, day.month, day.day, 2, 30),
            kind="block",
            status="planned",
            source="web",
            step_id=step_todo.id,
        )
        ev_done = Event(
            user_id=uid,
            title="Collect References",
            start_utc=datetime(day.year, day.month, day.day, 3, 0),
            end_utc=datetime(day.year, day.month, day.day, 3, 20),
            kind="block",
            status="done",
            source="web",
            step_id=step_done.id,
        )
        s.add_all([ev_planned, ev_done])
        s.commit()

        raw_default, etag_default = calendar_sync.build_ics_feed(
            s, user, feed, base_url="https://goalcal.zhang.sarl"
        )
        cal = Calendar.from_ical(raw_default)
        vevents = {str(v.get("SUMMARY")): v for v in cal.walk("VEVENT")}

        planned_v = vevents["Draft Outline"]
        alarms_planned = list(planned_v.walk("VALARM"))
        assert len(alarms_planned) == 1
        assert str(alarms_planned[0].get("ACTION")) == "DISPLAY"
        assert alarms_planned[0].get("TRIGGER").dt == timedelta(minutes=-10)
        assert "3 key bullet points" in str(alarms_planned[0].get("DESCRIPTION"))
        desc = str(planned_v.get("DESCRIPTION"))
        assert "Outcome: 3 key bullet points" in desc
        assert f"https://goalcal.zhang.sarl/app/calendar?date={day.isoformat()}&event={ev_planned.id}" in desc

        done_v = vevents["✓ Collect References"]
        assert list(done_v.walk("VALARM")) == []

        # Set block_minutes = 0 (at start time)
        reminders.set_calendar_alarms(
            s, uid, {"enabled": True, "block_minutes": 0, "habit_minutes": 10, "manual_minutes": None}
        )
        s.flush()
        raw_zero, etag_zero = calendar_sync.build_ics_feed(s, user, feed, base_url="https://goalcal.zhang.sarl")
        assert etag_zero != etag_default
        cal_zero = Calendar.from_ical(raw_zero)
        planned_zero = [v for v in cal_zero.walk("VEVENT") if str(v.get("SUMMARY")) == "Draft Outline"][0]
        assert list(planned_zero.walk("VALARM"))[0].get("TRIGGER").dt == timedelta(0)

        # Disable reminders completely
        reminders.set_calendar_alarms(
            s, uid, {"enabled": False, "block_minutes": 0, "habit_minutes": 10, "manual_minutes": None}
        )
        s.flush()
        raw_off, etag_off = calendar_sync.build_ics_feed(s, user, feed, base_url="https://goalcal.zhang.sarl")
        assert etag_off != etag_zero
        assert list(Calendar.from_ical(raw_off).walk("VALARM")) == []
    finally:
        s.close()


def test_native_providers_serialize_reminders(monkeypatch):
    # CalDAV VEVENT
    _, ics_with_alarm = CalDavAdapter._build_vevent_ics if hasattr(CalDavAdapter, "_build_vevent_ics") else (None, None)
    from goalcal.domain.sync_providers import _build_vevent_ics

    _, raw_alarm = _build_vevent_ics({
        "event_id": 11,
        "title": "Write spec",
        "description": "Outcome: 1 page",
        "alarm_description": "Write spec — 1 page",
        "reminder_minutes": 15,
        "start_utc": "2030-01-10T02:00:00Z",
        "end_utc": "2030-01-10T02:45:00Z",
    })
    alarms = list(Calendar.from_ical(raw_alarm).walk("VALARM"))
    assert len(alarms) == 1
    assert alarms[0].get("TRIGGER").dt == timedelta(minutes=-15)
    assert "Write spec — 1 page" in str(alarms[0].get("DESCRIPTION"))

    _, raw_no_alarm = _build_vevent_ics({
        "event_id": 12,
        "title": "Done task",
        "reminder_minutes": None,
        "start_utc": "2030-01-10T03:00:00Z",
        "end_utc": "2030-01-10T03:30:00Z",
    })
    assert list(Calendar.from_ical(raw_no_alarm).walk("VALARM")) == []

    # Google & Outlook payloads
    captured_google: list[dict] = []
    monkeypatch.setattr(
        GoogleCalendarAdapter,
        "_api",
        classmethod(lambda cls, method, path, secrets_dict, *, body=None: (captured_google.append(body) or (200, {"id": "g1", "etag": "e1"}))),
    )
    GoogleCalendarAdapter.push_upsert(
        "gc-cal",
        {"access_token": "tok"},
        {
            "event_id": 11,
            "title": "Write spec",
            "start_utc": "2030-01-10T02:00:00Z",
            "end_utc": "2030-01-10T02:45:00Z",
            "reminder_minutes": 15,
        },
    )
    GoogleCalendarAdapter.push_upsert(
        "gc-cal",
        {"access_token": "tok"},
        {
            "event_id": 12,
            "title": "Done task",
            "start_utc": "2030-01-10T03:00:00Z",
            "end_utc": "2030-01-10T03:30:00Z",
            "reminder_minutes": None,
        },
    )
    assert captured_google[0]["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 15}]}
    assert captured_google[1]["reminders"] == {"useDefault": False, "overrides": []}

    captured_outlook: list[dict] = []
    monkeypatch.setattr(
        OutlookCalendarAdapter,
        "_api",
        classmethod(lambda cls, method, path, secrets_dict, *, body=None, extra_headers=None: (captured_outlook.append(body) or (200, {"id": "o1", "changeKey": "ck1"}))),
    )
    OutlookCalendarAdapter.push_upsert(
        "gc-cal",
        {"access_token": "tok"},
        {
            "event_id": 11,
            "title": "Write spec",
            "start_utc": "2030-01-10T02:00:00Z",
            "end_utc": "2030-01-10T02:45:00Z",
            "reminder_minutes": 15,
        },
    )
    OutlookCalendarAdapter.push_upsert(
        "gc-cal",
        {"access_token": "tok"},
        {
            "event_id": 12,
            "title": "Done task",
            "start_utc": "2030-01-10T03:00:00Z",
            "end_utc": "2030-01-10T03:30:00Z",
            "reminder_minutes": None,
        },
    )
    assert captured_outlook[0]["isReminderOn"] is True
    assert captured_outlook[0]["reminderMinutesBeforeStart"] == 15
    assert captured_outlook[1]["isReminderOn"] is False
    assert "reminderMinutesBeforeStart" not in captured_outlook[1]


def test_calendar_alarms_api_requeues_upcoming_and_prevents_sync_loop(accounts, monkeypatch):
    ids, tokens = accounts
    uid_a, token_a, token_b = ids[0], tokens[0], tokens[1]

    remote_store: dict[str, bytes] = {}
    day = (datetime.now(timezone.utc) + timedelta(days=3)).date()
    principal_url = "https://caldav.icloud.com/99/principal/"
    home_url = "https://caldav.icloud.com/99/calendars/"
    goalcal_url_box: list[str] = []

    def fake_request(method, url, *, headers=None, body=None, timeout=15.0, max_bytes=5*1024*1024, follow_redirects=3, require_https=True):
        m = method.upper()
        if m == "PROPFIND" and url == "https://caldav.icloud.com/":
            xml = f'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:"><D:response><D:href>/</D:href><D:propstat><D:prop><D:current-user-principal><D:href>{principal_url}</D:href></D:current-user-principal></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response></D:multistatus>'
            return 207, {}, xml.encode()
        if m == "PROPFIND" and url == principal_url:
            xml = f'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><D:response><D:href>{principal_url}</D:href><D:propstat><D:prop><C:calendar-home-set><D:href>{home_url}</D:href></C:calendar-home-set></D:prop></D:propstat></D:response></D:multistatus>'
            return 207, {}, xml.encode()
        if m == "PROPFIND" and url == home_url:
            xml = '<?xml version="1.0"?><D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"></D:multistatus>'
            return 207, {}, xml.encode()
        if m == "MKCALENDAR":
            goalcal_url_box.append(url)
            return 201, {}, b""
        if m == "PUT":
            remote_store[url] = body or b""
            return 201, {"etag": '"etag-put"'}, b""
        if m == "REPORT":
            items_xml = [
                f'<D:response><D:href>{xml_escape(href)}</D:href><D:propstat><D:prop><D:getetag>"etag-gc"</D:getetag><C:calendar-data>{xml_escape(raw.decode())}</C:calendar-data></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>'
                for href, raw in remote_store.items()
            ]
            xml = f'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><D:sync-token>https://caldav.icloud.com/sync/a</D:sync-token>{"".join(items_xml)}</D:multistatus>'
            return 207, {}, xml.encode()
        return 404, {}, b""

    monkeypatch.setattr("goalcal.domain.sync_providers.safe_http_request", fake_request)
    monkeypatch.setenv("GOALCAL_PUBLIC_URL", "https://goalcal.zhang.sarl")

    # Seed one future planned block and one already-done block
    s = db.new_session()
    try:
        goal = Goal(user_id=uid_a, title="Deep Focus")
        s.add(goal)
        s.flush()
        step1 = Step(goal_id=goal.id, seq=0, title="Write Draft", outcome="500 words", est_min=45)
        step2 = Step(goal_id=goal.id, seq=1, title="Old Done", outcome="Notes", est_min=20, status="done")
        s.add_all([step1, step2])
        s.flush()
        ev_future = Event(
            user_id=uid_a,
            title="Write Draft",
            start_utc=datetime(day.year, day.month, day.day, 8, 0),
            end_utc=datetime(day.year, day.month, day.day, 8, 45),
            kind="block",
            status="planned",
            source="web",
            step_id=step1.id,
        )
        ev_done = Event(
            user_id=uid_a,
            title="Old Done",
            start_utc=datetime(day.year, day.month, day.day, 6, 0),
            end_utc=datetime(day.year, day.month, day.day, 6, 20),
            kind="block",
            status="done",
            source="web",
            step_id=step2.id,
        )
        s.add_all([ev_future, ev_done])
        s.commit()
    finally:
        s.close()

    # Check GET /calendar/sync defaults
    state0 = api_request(token_a, "GET", "/calendar/sync")
    assert state0.status_code == 200
    assert state0.json()["alarms"] == {
        "enabled": True,
        "block_minutes": 10,
        "habit_minutes": 10,
        "manual_minutes": None,
        "version": None,
    }

    # Invalid alarm value rejected (422)
    r_bad = api_request(
        token_a,
        "PUT",
        "/calendar/sync/alarms",
        {"enabled": True, "block_minutes": 2000, "habit_minutes": 10, "manual_minutes": None},
    )
    assert r_bad.status_code == 422

    # Connect CalDAV two-way (backfills both blocks initially)
    r_conn = api_request(
        token_a,
        "POST",
        "/calendar/sync/connections/caldav",
        {"preset": "icloud", "username": "alice@icloud.com", "password": "abcd-efgh-ijkl-mnop", "enable_two_way": True},
    )
    assert r_conn.status_code == 200
    assert bidirectional_sync.process_outbox_once() == 2

    # Update alarms via PUT -> persists, sets version, requeues ONLY the 1 upcoming planned block
    r_upd = api_request(
        token_a,
        "PUT",
        "/calendar/sync/alarms",
        {"enabled": True, "block_minutes": 15, "habit_minutes": 0, "manual_minutes": None},
    )
    assert r_upd.status_code == 200
    upd_data = r_upd.json()
    assert upd_data["queued_push"] == 1
    assert upd_data["alarms"]["block_minutes"] == 15
    assert upd_data["alarms"]["habit_minutes"] == 0
    assert upd_data["alarms"]["manual_minutes"] is None
    ver1 = upd_data["alarms"]["version"]
    assert isinstance(ver1, str) and len(ver1) > 0

    # User B remains isolated with defaults
    assert api_request(token_b, "GET", "/calendar/sync").json()["alarms"]["block_minutes"] == 10

    # Stale version conflict returns 409
    r_upd2 = api_request(
        token_a,
        "PUT",
        "/calendar/sync/alarms",
        {"enabled": True, "block_minutes": 30, "habit_minutes": 5, "manual_minutes": None, "version": ver1},
    )
    assert r_upd2.status_code == 200
    assert api_request(
        token_a,
        "PUT",
        "/calendar/sync/alarms",
        {"enabled": True, "block_minutes": 5, "habit_minutes": 5, "manual_minutes": None, "version": ver1},
    ).status_code == 409

    # Drain outbox -> remote .ics now has -PT30M VALARM + Outcome + deep link
    assert bidirectional_sync.process_outbox_once() == 1
    draft_ics = next(raw for raw in remote_store.values() if b"Write Draft" in raw)
    assert b"TRIGGER:-PT30M" in draft_ics
    assert b"500 words" in draft_ics
    assert f"https://goalcal.zhang.sarl/app/calendar?date={day.isoformat()}".encode() in draft_ics

    # Pull back GoalCal calendar -> no bounce-back outbox items
    gc_src = api_request(token_a, "GET", "/calendar/sync").json()["sources"][0]
    r_pull = api_request(token_a, "POST", f"/calendar/sync/sources/{gc_src['id']}/sync", {})
    assert r_pull.status_code == 200
    assert r_pull.json()["updated"] == 0
    s = db.new_session()
    try:
        pending = list(s.scalars(select(SyncOutbox).where(SyncOutbox.user_id == uid_a, SyncOutbox.status == "pending")))
        assert pending == []
    finally:
        s.close()
