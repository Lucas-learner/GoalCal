"""Tests for calendar synchronization: outbound .ics feed, inbound URL sync, and background worker."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import sqlite3
import uuid

import httpx2
from icalendar import Calendar
import pytest
from sqlalchemy import create_engine, select

from goalcal import auth, db, migrations, safe_fetch, storage, tenancy
from goalcal.auth_proxy import app
from goalcal.domain import calendar_sync, goals as goal_service
from goalcal.models import CalendarFeed, CalendarSource, Event, Interaction
from goalcal.web.models import Habit, HabitOccurrence, HabitRule

ORIGIN = "https://goalcal.test"
PASSWORD = "long-sync-test-password"

_SAMPLE_ICS_V1 = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//External//EN
BEGIN:VEVENT
UID:ext-meeting-1
SUMMARY:Weekly Sync
DTSTART:20300110T020000Z
DTEND:20300110T030000Z
END:VEVENT
BEGIN:VEVENT
UID:ext-meeting-2
SUMMARY:Design Review
DTSTART:20300111T040000Z
DTEND:20300111T050000Z
END:VEVENT
END:VCALENDAR
"""

_SAMPLE_ICS_V2 = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//External//EN
BEGIN:VEVENT
UID:ext-meeting-1
SUMMARY:Weekly Sync (Updated)
DTSTART:20300110T030000Z
DTEND:20300110T040000Z
END:VEVENT
BEGIN:VEVENT
UID:ext-meeting-2
STATUS:CANCELLED
SUMMARY:Design Review
DTSTART:20300111T040000Z
DTEND:20300111T050000Z
END:VEVENT
BEGIN:VEVENT
UID:ext-meeting-3
SUMMARY:Client Call
DTSTART:20300112T060000Z
DTEND:20300112T070000Z
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture()
def accounts(tmp_path, monkeypatch):
    path = tmp_path / "sync.db"
    monkeypatch.setattr(db, "settings", replace(db.settings, db_url=f"sqlite:///{path}"))
    monkeypatch.setattr(storage, "database_path", lambda url=None: path)
    monkeypatch.setattr(migrations, "database_path", lambda url=None: path)
    db.init_db()
    ids = []
    tokens = []
    names = []
    for _ in range(2):
        name = "sync_" + uuid.uuid4().hex[:10]
        names.append(name)
        ids.append(auth.create_user(name, PASSWORD, admin=True))
        tokens.append(auth.login(name, PASSWORD))
    return names, ids, tokens


def api_request(token, method, path, body=None, *, key=None, headers=None):
    async def run():
        h = {"Origin": ORIGIN, "Idempotency-Key": key or uuid.uuid4().hex}
        h.update(headers or {})
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=ORIGIN,
            cookies={"goalcal_session": token or ""},
        ) as client:
            return await client.request(method, "/api/v1" + path, json=body, headers=h)

    return asyncio.run(run())


def raw_request(method, path, *, headers=None):
    async def run():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=ORIGIN,
        ) as client:
            return await client.request(method, path, headers=headers or {})

    return asyncio.run(run())


def test_normalize_ics_url_accepts_webcal_and_rejects_private():
    assert safe_fetch.normalize_ics_url("webcal://calendar.example/a.ics") == "https://calendar.example/a.ics"
    assert safe_fetch.normalize_ics_url("webcals://calendar.example/a.ics") == "https://calendar.example/a.ics"
    with pytest.raises(safe_fetch.FetchError):
        safe_fetch.normalize_ics_url("webcal://127.0.0.1/private.ics")
    with pytest.raises(safe_fetch.FetchError):
        safe_fetch.normalize_ics_url("https://user:pass@calendar.example/a.ics")


def test_outbound_feed_lifecycle_filtering_etag_and_rotation(accounts):
    names, ids, tokens = accounts
    token0 = tokens[0]
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    # Initial state: feed not yet enabled
    state = api_request(token0, "GET", "/calendar/sync")
    assert state.status_code == 200
    assert state.json()["feed"]["enabled"] is False
    assert state.json()["feed"]["path"] is None
    assert state.json()["sources"] == []

    # Invalid token always returns 404
    assert raw_request("GET", "/calendar/feed/not-a-valid-token.ics").status_code == 404
    assert raw_request("GET", "/calendar/feed/" + ("a" * 40) + ".txt").status_code == 404

    # Seed diverse events for user 0: block (done), habit, manual external, imported, synced, skipped
    with tenancy.session_scope(token0) as s:
        uid = s.info["principal"].user_id
        goal = goal_service.create_goal(
            s,
            uid,
            "跑半马",
            step_defs=[{"title": "5 公里轻松跑", "est_min": 45, "milestone": "基础耐力"}],
        )
        step = goal.steps[0]
        ev_block = Event(
            user_id=uid,
            title="跑半马 · 5 公里轻松跑",
            start_utc=(now + timedelta(days=1)).replace(tzinfo=None),
            end_utc=(now + timedelta(days=1, minutes=45)).replace(tzinfo=None),
            kind="block",
            status="done",
            source="proposal",
            step_id=step.id,
        )
        ev_manual = Event(
            user_id=uid,
            title="牙医复诊",
            start_utc=(now + timedelta(days=2)).replace(tzinfo=None),
            end_utc=(now + timedelta(days=2, hours=1)).replace(tzinfo=None),
            kind="external",
            status="planned",
            source="web",
        )
        ev_imported = Event(
            user_id=uid,
            title="导入会议不应再导出",
            start_utc=(now + timedelta(days=2, hours=2)).replace(tzinfo=None),
            end_utc=(now + timedelta(days=2, hours=3)).replace(tzinfo=None),
            kind="external",
            status="planned",
            source="import",
            uid="imp-1",
        )
        ev_skipped = Event(
            user_id=uid,
            title="已取消的块",
            start_utc=(now + timedelta(days=3)).replace(tzinfo=None),
            end_utc=(now + timedelta(days=3, hours=1)).replace(tzinfo=None),
            kind="block",
            status="skipped",
            source="proposal",
            step_id=step.id,
        )
        ev_habit = Event(
            user_id=uid,
            title="晚间阅读",
            start_utc=(now + timedelta(days=1, hours=5)).replace(tzinfo=None),
            end_utc=(now + timedelta(days=1, hours=5, minutes=30)).replace(tzinfo=None),
            kind="habit",
            status="planned",
            source="habit",
        )
        s.add_all([ev_block, ev_manual, ev_imported, ev_skipped, ev_habit])
        s.flush()
        habit = Habit(
            user_id=uid,
            timezone="Asia/Shanghai",
            start_date=now.date(),
            status="active",
        )
        s.add(habit)
        s.flush()
        s.add(
            HabitRule(
                user_id=uid,
                habit_id=habit.id,
                effective_date=now.date(),
                definition='{"title":"晚间阅读","mode":"weekly","weekdays":[],"est_min":30,"start_time":null,"weekly_target":3}',
            )
        )
        s.add(
            HabitOccurrence(
                id="occ-" + uuid.uuid4().hex[:10],
                user_id=uid,
                habit_id=habit.id,
                day=(now + timedelta(days=1)).date(),
                event_id=ev_habit.id,
                status="pending",
            )
        )

    # Enable feed (default includes blocks + habits + manual, excludes imported & skipped)
    en = api_request(
        token0,
        "PUT",
        "/calendar/sync/feed",
        {"enabled": True, "include_blocks": True, "include_habits": True, "include_manual": True},
    )
    assert en.status_code == 200, en.text
    feed_dto = en.json()["feed"]
    assert feed_dto["enabled"] is True
    feed_path = feed_dto["path"]
    assert feed_path.startswith("/calendar/feed/") and feed_path.endswith(".ics")

    # Fetch public ICS feed without cookies
    resp = raw_request("GET", feed_path)
    assert resp.status_code == 200
    assert "text/calendar" in resp.headers["content-type"]
    assert resp.headers["cache-control"] == "private, no-cache"
    assert "noindex" in resp.headers["x-robots-tag"]
    etag = resp.headers["etag"]
    assert etag

    parsed = Calendar.from_ical(resp.content)
    summaries = [str(ev.get("SUMMARY")) for ev in parsed.walk("VEVENT")]
    assert "✓ 跑半马 · 5 公里轻松跑" in summaries
    assert "晚间阅读" in summaries
    assert "牙医复诊" in summaries
    assert "导入会议不应再导出" not in summaries
    assert "已取消的块" not in summaries

    # Conditional GET returns 304 Not Modified
    resp_304 = raw_request("GET", feed_path, headers={"If-None-Match": etag})
    assert resp_304.status_code == 304
    assert resp_304.content == b""

    # HEAD returns 200 with empty body
    resp_head = raw_request("HEAD", feed_path)
    assert resp_head.status_code == 200
    assert resp_head.content == b""

    # Filter toggles exclude habits & manual events
    upd = api_request(
        token0,
        "PUT",
        "/calendar/sync/feed",
        {
            "enabled": True,
            "include_blocks": True,
            "include_habits": False,
            "include_manual": False,
            "version": feed_dto["version"],
        },
    )
    assert upd.status_code == 200
    filtered = Calendar.from_ical(raw_request("GET", feed_path).content)
    assert [str(ev.get("SUMMARY")) for ev in filtered.walk("VEVENT")] == ["✓ 跑半马 · 5 公里轻松跑"]

    # Rotate token invalidates old URL immediately
    rot = api_request(token0, "POST", "/calendar/sync/feed/rotate", {"version": upd.json()["feed"]["version"]})
    assert rot.status_code == 200
    new_path = rot.json()["feed"]["path"]
    assert new_path != feed_path
    assert raw_request("GET", feed_path).status_code == 404
    assert raw_request("GET", new_path).status_code == 200

    # Disabling user account immediately cuts off feed access
    auth.manage(names[0], disabled=True)
    assert raw_request("GET", new_path).status_code == 404


def test_inbound_source_crud_sync_pruning_and_isolation(accounts, monkeypatch):
    _, ids, tokens = accounts
    token0, token1 = tokens

    payloads = {
        "https://cal.example/work.ics": safe_fetch.FetchResponse(
            content=_SAMPLE_ICS_V1, etag='"v1"', last_modified="Mon, 01 Jan 2030 00:00:00 GMT"
        ),
    }

    def fake_fetch(url, *, etag=None, last_modified=None, allow_webcal=True):
        normalized = safe_fetch.normalize_ics_url(url) if allow_webcal else url
        item = payloads.get(normalized)
        if item is None:
            raise safe_fetch.FetchError("Unsupported response")
        if isinstance(item, Exception):
            raise item
        if etag and item.etag == etag:
            return safe_fetch.FetchResponse(content=None, not_modified=True, etag=item.etag, last_modified=item.last_modified)
        return item

    monkeypatch.setattr(safe_fetch, "fetch_ics_conditional", fake_fetch)
    monkeypatch.setattr(calendar_sync, "fetch_ics_conditional", fake_fetch)

    # Create source using webcal:// URL -> normalized to https:// and synced immediately
    created = api_request(
        token0,
        "POST",
        "/calendar/sync/sources",
        {"name": "公司日历", "url": "webcal://cal.example/work.ics", "sync_interval_minutes": 30},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["added"] == 2
    src = body["source"]
    assert src["url"] == "https://cal.example/work.ics"
    assert src["last_status"] == "ok"
    assert src["provider"] == "ics_url"

    # Duplicate URL rejected with 409
    dup = api_request(
        token0,
        "POST",
        "/calendar/sync/sources",
        {"name": "重复日历", "url": "https://cal.example/work.ics"},
    )
    assert dup.status_code == 409

    # Unreachable URL fails fast with 422 and does not create a broken source
    bad = api_request(
        token0,
        "POST",
        "/calendar/sync/sources",
        {"name": "无效", "url": "https://cal.example/missing.ics"},
    )
    assert bad.status_code == 422

    # Cross-tenant isolation: user 1 cannot see, update, sync, or delete user 0's source
    assert api_request(token1, "GET", "/calendar/sync").json()["sources"] == []
    assert api_request(token1, "PATCH", f"/calendar/sync/sources/{src['id']}", {"version": src["version"], "name": "hack"}).status_code == 404
    assert api_request(token1, "POST", f"/calendar/sync/sources/{src['id']}/sync", {}).status_code == 404
    assert api_request(token1, "DELETE", f"/calendar/sync/sources/{src['id']}", {"version": src["version"]}).status_code == 404

    # Verify events are tagged with source="sync" and sync_source_id, and excluded from outbound feed
    en = api_request(token0, "PUT", "/calendar/sync/feed", {"enabled": True, "include_blocks": True, "include_habits": True, "include_manual": True})
    feed_cal = Calendar.from_ical(raw_request("GET", en.json()["feed"]["path"]).content)
    assert list(feed_cal.walk("VEVENT")) == []  # synced external events are never re-exported

    # Update external calendar: 1 moved/renamed, 1 cancelled, 1 added -> sync prunes cancelled & updates
    payloads["https://cal.example/work.ics"] = safe_fetch.FetchResponse(content=_SAMPLE_ICS_V2, etag='"v2"')
    synced = api_request(token0, "POST", f"/calendar/sync/sources/{src['id']}/sync", {})
    assert synced.status_code == 200, synced.text
    sdata = synced.json()
    assert sdata["ok"] is True
    assert (sdata["added"], sdata["updated"], sdata["removed"]) == (1, 1, 1)

    with tenancy.session_scope(token0) as s:
        evs = sorted(s.scalars(select(Event).where(Event.sync_source_id == src["id"])), key=lambda e: e.start_utc)
        assert [e.title for e in evs] == ["Weekly Sync (Updated)", "Client Call"]
        # Attach an Interaction referencing a synced event to verify safe FK cleanup on delete
        s.add(Interaction(user_id=ids[0], ts_sim=datetime.now(), kind="test", event_id=evs[0].id))

    # Fetch error persists last_status="error" without removing existing events
    payloads["https://cal.example/work.ics"] = safe_fetch.FetchError("Download deadline exceeded")
    failed = api_request(token0, "POST", f"/calendar/sync/sources/{src['id']}/sync", {})
    assert failed.status_code == 200
    assert failed.json()["ok"] is False
    assert failed.json()["source"]["last_status"] == "error"
    assert failed.json()["source"]["last_error"]

    # Update settings with stale version -> 409; with fresh version -> 200
    assert api_request(token0, "PATCH", f"/calendar/sync/sources/{src['id']}", {"version": src["version"], "name": "新名字"}).status_code == 409
    fresh_ver = failed.json()["source"]["version"]
    patched = api_request(
        token0,
        "PATCH",
        f"/calendar/sync/sources/{src['id']}",
        {"version": fresh_ver, "name": "工作日程", "sync_interval_minutes": 60, "enabled": False},
    )
    assert patched.status_code == 200
    assert patched.json()["source"]["name"] == "工作日程"
    assert patched.json()["source"]["enabled"] is False

    # Delete source cleanly removes source and all its synced events with zero FK errors
    deleted = api_request(
        token0,
        "DELETE",
        f"/calendar/sync/sources/{src['id']}",
        {"version": patched.json()["source"]["version"]},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["removed_events"] == 2
    with tenancy.session_scope(token0) as s:
        assert list(s.scalars(select(Event).where(Event.sync_source_id == src["id"]))) == []
    with storage.connect() as con:
        assert not con.execute("PRAGMA foreign_key_check").fetchall()


def test_background_periodic_worker_respects_interval_and_etag(accounts, monkeypatch):
    _, ids, tokens = accounts
    token0 = tokens[0]

    calls = []
    etag_now = ['"e1"']

    def fake_fetch(url, *, etag=None, last_modified=None, allow_webcal=True):
        calls.append((url, etag))
        if etag == etag_now[0]:
            return safe_fetch.FetchResponse(content=None, not_modified=True, etag=etag)
        return safe_fetch.FetchResponse(content=_SAMPLE_ICS_V1, etag=etag_now[0])

    monkeypatch.setattr(safe_fetch, "fetch_ics_conditional", fake_fetch)
    monkeypatch.setattr(calendar_sync, "fetch_ics_conditional", fake_fetch)

    created = api_request(
        token0,
        "POST",
        "/calendar/sync/sources",
        {"name": "Team", "url": "https://cal.example/team.ics", "sync_interval_minutes": 30},
    )
    assert created.status_code == 200
    calls.clear()

    # Immediate tick: not due yet -> 0 synced
    now = datetime.now(timezone.utc)
    assert calendar_sync.sync_due_sources_once(now=now + timedelta(minutes=10)) == 0
    assert calls == []

    # After 35 minutes: due -> sends ETag -> gets 304 Not Modified cleanly
    assert calendar_sync.sync_due_sources_once(now=now + timedelta(minutes=35)) == 1
    assert calls == [("https://cal.example/team.ics", '"e1"')]


def test_exdate_skipped_in_recurring_ics(session, user):
    from goalcal.domain import ics_import

    raw = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:rec-1
SUMMARY:Standup
DTSTART:20300107T090000Z
DTEND:20300107T093000Z
RRULE:FREQ=DAILY;COUNT=3
EXDATE:20300108T090000Z
END:VEVENT
END:VCALENDAR
"""
    res = ics_import.import_ics(session, user.id, raw, "Asia/Shanghai", now=datetime(2030, 1, 1, 0, 0))
    assert res.added == 2
    starts = sorted(ev.start_utc for ev in session.scalars(select(Event).where(Event.user_id == user.id)))
    assert starts == [datetime(2030, 1, 7, 9, 0), datetime(2030, 1, 9, 9, 0)]


def test_v11_to_v12_migration_adds_sync_tables_and_event_column(tmp_path):
    path = tmp_path / "v11.db"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, tz TEXT, created_at DATETIME)")
        con.execute(
            "CREATE TABLE events (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), "
            "title TEXT, start_utc DATETIME, end_utc DATETIME, kind TEXT, status TEXT, source TEXT, "
            "uid TEXT, step_id INTEGER, resolved_at DATETIME, created_at DATETIME)"
        )
        con.execute("INSERT INTO users VALUES (1, 'demo', 'Asia/Shanghai', CURRENT_TIMESTAMP)")
        con.execute("PRAGMA user_version=11")
    engine = create_engine(f"sqlite:///{path}")
    migrations.migrate(engine)
    with sqlite3.connect(path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == migrations.VERSION
        ev_cols = {r[1] for r in con.execute("PRAGMA table_info(events)").fetchall()}
        assert "sync_source_id" in ev_cols
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"calendar_sources", "calendar_feeds"} <= tables
        assert not con.execute("PRAGMA foreign_key_check").fetchall()
    engine.dispose()
