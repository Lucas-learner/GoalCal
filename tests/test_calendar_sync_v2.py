"""Comprehensive tests for Calendar Sync V2.2 (V2.0 scheduling + V2.1 CalDAV + V2.2 Google/Outlook OAuth & Webhooks)."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from urllib.parse import parse_qs, urlsplit
import uuid

import httpx2
import pytest
from sqlalchemy import create_engine, select

from goalcal import auth, crypto, db, migrations, safe_fetch, storage
from goalcal.auth_proxy import app
from goalcal.domain import bidirectional_sync, calendar_sync, goals as goal_service
from goalcal.domain.sync_providers import CalDavAdapter, GoogleCalendarAdapter, OutlookCalendarAdapter
from goalcal.models import CalendarConnection, CalendarSource, Event, EventSyncLink, Goal, Step, SyncOutbox
from goalcal.web.http import api

ORIGIN = "https://goalcal.test"
PASSWORD = "long-sync-v2-password"


@pytest.fixture()
def accounts(tmp_path, monkeypatch):
    path = tmp_path / "sync_v2.db"
    monkeypatch.setattr(db, "settings", replace(db.settings, db_url=f"sqlite:///{path}"))
    monkeypatch.setattr(storage, "database_path", lambda url=None: path)
    monkeypatch.setattr(migrations, "database_path", lambda url=None: path)
    monkeypatch.setattr(crypto, "database_path", lambda url=None: path)
    db.init_db()
    ids = []
    tokens = []
    names = []
    for _ in range(2):
        name = "syncv2_" + uuid.uuid4().hex[:10]
        names.append(name)
        ids.append(auth.create_user(name, PASSWORD, admin=True))
        tokens.append(auth.login(name, PASSWORD))
    return names, ids, tokens


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


def proxy_request(method, path, *, cookies=None, headers=None, json_body=None):
    async def run():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=ORIGIN,
            cookies=cookies or {},
        ) as client:
            return await client.request(method, path, headers=headers or {}, json=json_body, follow_redirects=False)

    import asyncio
    return asyncio.run(run())


def test_crypto_roundtrip_and_production_key_file(tmp_path, monkeypatch):
    db_file = tmp_path / "prod" / "goalcal.db"
    db_file.parent.mkdir(parents=True)
    monkeypatch.setenv("GOALCAL_PRODUCTION", "true")
    monkeypatch.delenv("GOALCAL_CREDENTIAL_KEY", raising=False)
    monkeypatch.setattr(crypto, "get_settings", lambda: replace(db.settings, production=True, db_url=f"sqlite:///{db_file}"))
    monkeypatch.setattr(crypto, "database_path", lambda url=None: db_file)

    encrypted = crypto.encrypt_secrets({"refresh_token": "sec-123", "password": "app-pw"})
    assert "sec-123" not in encrypted
    assert crypto.decrypt_secrets(encrypted) == {"refresh_token": "sec-123", "password": "app-pw"}
    key_file = db_file.parent / ".credential_key"
    assert key_file.exists()
    assert (key_file.stat().st_mode & 0o777) == 0o600

    signed = crypto.sign_oauth_state({"uid": 7, "provider": "google", "nonce": "abc"})
    assert crypto.verify_oauth_state(signed)["uid"] == 7
    with pytest.raises(crypto.CredentialError):
        crypto.verify_oauth_state(signed + "tamper")


def test_migration_v12_to_v13_preserves_existing_data(tmp_path):
    db_path = tmp_path / "v12.db"
    with sqlite3.connect(db_path) as con:
        con.executescript(
            """
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, email TEXT, password_hash TEXT, disabled BOOLEAN NOT NULL DEFAULT 0, name TEXT NOT NULL DEFAULT 'demo', tz TEXT NOT NULL DEFAULT 'Asia/Shanghai', created_at DATETIME);
            CREATE TABLE goals (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), draft_id TEXT, title TEXT, outcome TEXT, deadline DATE, status TEXT, pinned BOOLEAN NOT NULL DEFAULT 0, created_at DATETIME, template_key TEXT, outline TEXT);
            CREATE TABLE calendar_sources (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), provider VARCHAR(32) NOT NULL, direction VARCHAR(16) NOT NULL, name VARCHAR(120) NOT NULL, url VARCHAR(2000) NOT NULL, enabled BOOLEAN NOT NULL DEFAULT 1, sync_interval_minutes INTEGER NOT NULL DEFAULT 30, last_synced_at DATETIME, last_status VARCHAR(20) NOT NULL DEFAULT 'idle', last_error VARCHAR(500) NOT NULL DEFAULT '', last_added INTEGER NOT NULL DEFAULT 0, last_updated INTEGER NOT NULL DEFAULT 0, last_removed INTEGER NOT NULL DEFAULT 0, last_skipped INTEGER NOT NULL DEFAULT 0, etag VARCHAR(255), last_modified VARCHAR(128), sync_token VARCHAR(500), created_at DATETIME, updated_at DATETIME);
            CREATE TABLE calendar_feeds (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE REFERENCES users(id), token VARCHAR(64) NOT NULL UNIQUE, enabled BOOLEAN NOT NULL DEFAULT 1, include_blocks BOOLEAN NOT NULL DEFAULT 1, include_habits BOOLEAN NOT NULL DEFAULT 1, include_manual BOOLEAN NOT NULL DEFAULT 1, created_at DATETIME, rotated_at DATETIME, last_accessed_at DATETIME);
            CREATE TABLE events (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), title VARCHAR(200) NOT NULL, start_utc DATETIME NOT NULL, end_utc DATETIME NOT NULL, kind VARCHAR(20) NOT NULL, status VARCHAR(20) NOT NULL, source VARCHAR(20) NOT NULL, uid VARCHAR(200), sync_source_id INTEGER REFERENCES calendar_sources(id) ON DELETE CASCADE, step_id INTEGER, resolved_at DATETIME, created_at DATETIME);
            INSERT INTO users (id, username, name, tz) VALUES (1, 'alice', 'Alice', 'Asia/Shanghai');
            INSERT INTO calendar_sources (id, user_id, provider, direction, name, url) VALUES (10, 1, 'ics_url', 'inbound', 'Work', 'https://cal.example/work.ics');
            INSERT INTO events (id, user_id, title, start_utc, end_utc, kind, status, source, uid, sync_source_id) VALUES (50, 1, 'Standup', '2030-01-10 02:00:00', '2030-01-10 02:30:00', 'external', 'planned', 'sync', 'u1', 10);
            PRAGMA user_version = 12;
            """
        )

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        migrations.migrate(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == migrations.VERSION
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"calendar_connections", "event_sync_links", "sync_outbox"} <= tables
        ev_cols = {r[1] for r in con.execute("PRAGMA table_info(events)").fetchall()}
        assert "all_day" in ev_cols
        src_cols = {r[1] for r in con.execute("PRAGMA table_info(calendar_sources)").fetchall()}
        assert {"connection_id", "color", "blocks_scheduling", "all_day_mode", "writable", "is_goalcal_calendar", "webhook_channel_id"} <= src_cols
        row = con.execute("SELECT title, blocks_scheduling, all_day_mode FROM events JOIN calendar_sources ON events.sync_source_id=calendar_sources.id WHERE events.id=50").fetchone()
        assert row == ("Standup", 1, "ignore")


def test_v20_all_day_non_blocking_and_conflict_detection_flow(accounts, monkeypatch):
    _, ids, tokens = accounts
    uid, token = ids[0], tokens[0]

    target_day = (datetime.now(timezone.utc) + timedelta(days=3)).date()
    all_day_ics = f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:holiday-1
SUMMARY:Company Holiday
DTSTART;VALUE=DATE:{target_day.strftime('%Y%m%d')}
DTEND;VALUE=DATE:{(target_day + timedelta(days=1)).strftime('%Y%m%d')}
END:VEVENT
BEGIN:VEVENT
UID:meeting-busy-1
SUMMARY:Budget Review
DTSTART:{target_day.strftime('%Y%m%d')}T020000Z
DTEND:{target_day.strftime('%Y%m%d')}T030000Z
END:VEVENT
END:VCALENDAR
""".encode()

    monkeypatch.setattr(
        calendar_sync,
        "fetch_source_content",
        lambda *_args, **_kwargs: safe_fetch.FetchResponse(content=all_day_ics, not_modified=False),
    )

    # Create a goal & step scheduled overlapping Budget Review? First create goal & schedule step at target_day 02:00-02:45Z
    s = db.new_session()
    try:
        goal = Goal(user_id=uid, title="Deep Work Goal")
        s.add(goal)
        s.flush()
        step = Step(goal_id=goal.id, title="Write architecture doc", est_min=45)
        s.add(step)
        s.flush()
        step_id = step.id
        block = Event(
            user_id=uid,
            title="Write architecture doc",
            start_utc=datetime(target_day.year, target_day.month, target_day.day, 2, 0),
            end_utc=datetime(target_day.year, target_day.month, target_day.day, 2, 45),
            kind="block",
            status="planned",
            source="web",
            step_id=step_id,
        )
        s.add(block)
        s.commit()
    finally:
        s.close()

    # Add ICS source with all_day_mode='show' and color='#2563eb' -> all-day shows up AND conflict detected!
    r_add = api_request(
        token,
        "POST",
        "/calendar/sync/sources",
        {
            "name": "Corp Calendar",
            "url": "https://cal.example/corp.ics",
            "color": "#2563eb",
            "all_day_mode": "show",
            "blocks_scheduling": True,
        },
    )
    assert r_add.status_code == 200
    src = r_add.json()["source"]
    assert src["all_day_mode"] == "show"
    assert src["color"] == "#2563eb"

    # Calendar events shows both all-day holiday (non-blocking) & Budget Review + sync_conflict
    win_start = f"{target_day.isoformat()}T00:00:00Z"
    win_end = f"{(target_day + timedelta(days=1)).isoformat()}T00:00:00Z"
    r_events = api_request(token, "GET", f"/calendar/events?start={win_start}&end={win_end}")
    assert r_events.status_code == 200
    payload = r_events.json()
    by_title = {e["title"]: e for e in payload["events"]}
    assert by_title["Company Holiday"]["all_day"] is True
    assert by_title["Company Holiday"]["blocks_scheduling"] is False
    assert by_title["Budget Review"]["blocks_scheduling"] is True
    assert len(payload["sync_conflicts"]) == 1
    conflict = payload["sync_conflicts"][0]
    assert conflict["external_title"] == "Budget Review"
    assert conflict["source_color"] == "#2563eb"

    # All-day show does NOT block proposing/scheduling a step that day
    r_prop = api_request(token, "POST", f"/steps/{step_id}/proposals", {"window_start": win_start, "window_end": win_end})
    assert r_prop.status_code == 200
    prop = r_prop.json()
    assert len(prop["candidates"]) > 0

    # Accept a conflict-free slot -> sync_conflicts clears automatically
    r_acc = api_request(token, "POST", f"/proposals/{prop['id']}/accept", {"version": prop["version"], "index": 0})
    assert r_acc.status_code == 200
    r_events_after = api_request(token, "GET", f"/calendar/events?start={win_start}&end={win_end}")
    assert r_events_after.json()["sync_conflicts"] == []

    # Toggle blocks_scheduling=False -> Budget Review becomes non-blocking Reference only
    r_patch = api_request(
        token,
        "PATCH",
        f"/calendar/sync/sources/{src['id']}",
        {"version": src["version"], "blocks_scheduling": False},
    )
    assert r_patch.status_code == 200
    assert r_patch.json()["source"]["blocks_scheduling"] is False


def test_v21_caldav_two_way_push_pull_loop_prevention_and_tenancy(accounts, monkeypatch):
    _, ids, tokens = accounts
    uid, token_a, token_b = ids[0], tokens[0], tokens[1]

    remote_store: dict[str, bytes] = {}
    day = (datetime.now(timezone.utc) + timedelta(days=4)).date()
    principal_url = "https://caldav.icloud.com/12345/principal/"
    home_url = "https://caldav.icloud.com/12345/calendars/"
    work_cal_url = "https://caldav.icloud.com/12345/calendars/work/"
    goalcal_url_box: list[str] = []
    deleted_hrefs: list[str] = []

    def fake_request(method, url, *, headers=None, body=None, timeout=15.0, max_bytes=5*1024*1024, follow_redirects=3, require_https=True):
        m = method.upper()
        if m == "PROPFIND" and url == "https://caldav.icloud.com/":
            xml = f'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:"><D:response><D:href>/</D:href><D:propstat><D:prop><D:current-user-principal><D:href>{principal_url}</D:href></D:current-user-principal></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response></D:multistatus>'
            return 207, {}, xml.encode()
        if m == "PROPFIND" and url == principal_url:
            xml = f'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><D:response><D:href>{principal_url}</D:href><D:propstat><D:prop><C:calendar-home-set><D:href>{home_url}</D:href></C:calendar-home-set></D:prop></D:propstat></D:response></D:multistatus>'
            return 207, {}, xml.encode()
        if m == "PROPFIND" and url == home_url:
            xml = f'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav" xmlns:A="http://apple.com/ns/ical/"><D:response><D:href>{work_cal_url}</D:href><D:propstat><D:prop><D:displayname>Work iCloud</D:displayname><D:resourcetype><D:collection/><C:calendar/></D:resourcetype><A:calendar-color>#0f766eff</A:calendar-color><C:supported-calendar-component-set><C:comp name="VEVENT"/></C:supported-calendar-component-set></D:prop></D:propstat></D:response></D:multistatus>'
            return 207, {}, xml.encode()
        if m == "MKCALENDAR":
            goalcal_url_box.append(url)
            return 201, {}, b""
        if m == "PUT":
            remote_store[url] = body or b""
            return 201, {"etag": '"etag-put-1"'}, b""
        if m == "DELETE":
            deleted_hrefs.append(url)
            remote_store.pop(url, None)
            return 204, {}, b""
        if m == "REPORT":
            items_xml = []
            if goalcal_url_box and url == goalcal_url_box[0]:
                for href, raw in remote_store.items():
                    items_xml.append(
                        f'<D:response><D:href>{href}</D:href><D:propstat><D:prop><D:getetag>"etag-gc"</D:getetag><C:calendar-data>{raw.decode()}</C:calendar-data></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>'
                    )
            else:
                work_ics = f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:icloud-work-1
SUMMARY:Apple Board Meeting
DTSTART:{day.strftime('%Y%m%d')}T050000Z
DTEND:{day.strftime('%Y%m%d')}T060000Z
END:VEVENT
END:VCALENDAR"""
                items_xml.append(
                    f'<D:response><D:href>{work_cal_url}m1.ics</D:href><D:propstat><D:prop><D:getetag>"e1"</D:getetag><C:calendar-data>{work_ics}</C:calendar-data></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>'
                )
            xml = f'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><D:sync-token>https://caldav.icloud.com/sync/1</D:sync-token>{"".join(items_xml)}</D:multistatus>'
            return 207, {}, xml.encode()
        return 404, {}, b""

    monkeypatch.setattr("goalcal.domain.sync_providers.safe_http_request", fake_request)

    # Seed a Goal + Step so we can schedule it and test outbox push
    s = db.new_session()
    try:
        goal = Goal(user_id=uid, title="Ship Sync V2")
        s.add(goal)
        s.flush()
        step = Step(goal_id=goal.id, title="Verify CalDAV Push", est_min=30)
        s.add(step)
        s.flush()
        goal_id, step_id = goal.id, step.id
        s.commit()
    finally:
        s.close()
    step_ver = api_request(token_a, "GET", f"/goals/{goal_id}").json()["steps"][0]["version"]

    # Connect CalDAV iCloud
    r_conn = api_request(
        token_a,
        "POST",
        "/calendar/sync/connections/caldav",
        {"preset": "icloud", "username": "alice@icloud.com", "password": "abcd-efgh-ijkl-mnop", "enable_two_way": True},
    )
    assert r_conn.status_code == 200
    conn_data = r_conn.json()
    assert len(goalcal_url_box) == 1
    assert "encrypted_secrets" not in conn_data["connection"]

    # Schedule step -> automatically enqueued to SyncOutbox via scoped() flush hook
    start_iso = f"{day.isoformat()}T08:00:00Z"
    r_sched = api_request(token_a, "POST", f"/steps/{step_id}/schedule", {"version": step_ver, "start": start_iso})
    assert r_sched.status_code == 200
    ev_id = r_sched.json()["event"]["id"]

    # Process outbox -> PUT to remote GoalCal CalDAV collection
    pushed = bidirectional_sync.process_outbox_once()
    assert pushed == 1
    assert len(remote_store) == 1
    put_url, put_ics = next(iter(remote_store.items()))
    assert b"Verify CalDAV Push" in put_ics
    assert f"X-GOALCAL-EVENT-ID:{ev_id}".encode() in put_ics

    # Pull back unchanged GoalCal calendar -> loop prevention (0 updated, 0 new outbox items!)
    sources_map = {src["name"]: src for src in api_request(token_a, "GET", "/calendar/sync").json()["sources"]}
    gc_src = sources_map["GoalCal"]
    work_src = sources_map["Work iCloud"]

    r_sync_gc = api_request(token_a, "POST", f"/calendar/sync/sources/{gc_src['id']}/sync", {})
    assert r_sync_gc.status_code == 200
    assert r_sync_gc.json()["updated"] == 0
    assert r_sync_gc.json()["removed"] == 0

    # Sync Work iCloud calendar -> imports Apple Board Meeting
    r_sync_work = api_request(token_a, "POST", f"/calendar/sync/sources/{work_src['id']}/sync", {})
    assert r_sync_work.status_code == 200
    assert r_sync_work.json()["added"] == 1

    # Simulate user dragging event in Apple Calendar (+1 hour) -> pull moves local event without bounce-back
    moved_ics = put_ics.replace(
        f"{day.strftime('%Y%m%d')}T080000Z".encode(),
        f"{day.strftime('%Y%m%d')}T090000Z".encode(),
    ).replace(
        f"{day.strftime('%Y%m%d')}T083000Z".encode(),
        f"{day.strftime('%Y%m%d')}T093000Z".encode(),
    )
    remote_store[put_url] = moved_ics
    r_pull_move = api_request(token_a, "POST", f"/calendar/sync/sources/{gc_src['id']}/sync", {})
    assert r_pull_move.json()["updated"] == 1

    s = db.new_session()
    try:
        ev = s.get(Event, ev_id)
        assert ev.start_utc == datetime(day.year, day.month, day.day, 9, 0)
        assert ev.status == "moved"
        # No duplicate bounce outbox item created for gc_src
        pending_gc = list(s.scalars(select(SyncOutbox).where(SyncOutbox.source_id == gc_src["id"], SyncOutbox.status == "pending")))
        assert pending_gc == []
    finally:
        s.close()

    # Cross-tenant isolation: User B cannot refresh or delete User A's connection
    conn_id = conn_data["connection"]["id"]
    assert api_request(token_b, "POST", f"/calendar/sync/connections/{conn_id}/refresh", {}).status_code == 404
    assert api_request(token_b, "DELETE", f"/calendar/sync/connections/{conn_id}", {"version": conn_data["connection"]["version"]}).status_code == 404

    # Simulate user deleting event in Apple Calendar (STATUS:CANCELLED) -> local block marked skipped, Step stays todo
    remote_store[put_url] = moved_ics.replace(b"STATUS:CONFIRMED", b"STATUS:CANCELLED")
    r_pull_del = api_request(token_a, "POST", f"/calendar/sync/sources/{gc_src['id']}/sync", {})
    assert r_pull_del.json()["removed"] == 1
    s = db.new_session()
    try:
        assert s.get(Event, ev_id).status == "skipped"
        assert s.get(Step, step_id).status == "todo"
    finally:
        s.close()


def test_v22_google_and_outlook_oauth_webhooks_and_sync(accounts, monkeypatch):
    _, ids, tokens = accounts
    uid, token_a = ids[0], tokens[0]

    monkeypatch.setenv("GOOGLE_CLIENT_ID", "g-client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "g-client-secret")
    monkeypatch.setenv("MICROSOFT_CLIENT_ID", "m-client-id")
    monkeypatch.setenv("MICROSOFT_CLIENT_SECRET", "m-client-secret")

    # Capabilities report google & outlook configured
    r_state = api_request(token_a, "GET", "/calendar/sync")
    assert r_state.json()["providers"]["google"]["configured"] is True
    assert r_state.json()["providers"]["outlook"]["configured"] is True

    # 1) Google OAuth start -> redirect to accounts.google.com + signed cookie
    r_g_start = proxy_request("GET", "/_goalcal/oauth/google/start", cookies={"goalcal_session": token_a})
    assert r_g_start.status_code == 302
    loc = r_g_start.headers["location"]
    assert loc.startswith("https://accounts.google.com/")
    state_val = parse_qs(urlsplit(loc).query)["state"][0]
    oauth_cookie = r_g_start.cookies.get("goalcal_oauth_state")
    assert oauth_cookie

    day = (datetime.now(timezone.utc) + timedelta(days=5)).date()
    google_pulls = []

    def fake_google_http(method, url, *, headers=None, body=None, timeout=15.0, max_bytes=5*1024*1024, follow_redirects=3, require_https=True):
        if url == "https://oauth2.googleapis.com/token":
            return 200, {}, json.dumps({"access_token": "g-acc-1", "refresh_token": "g-ref-1", "expires_in": 3600}).encode()
        if url == "https://www.googleapis.com/oauth2/v2/userinfo":
            return 200, {}, json.dumps({"email": "alice@gmail.com"}).encode()
        if url.startswith("https://www.googleapis.com/calendar/v3/users/me/calendarList"):
            return 200, {}, json.dumps({"items": [{"id": "primary", "summary": "Personal", "accessRole": "owner", "backgroundColor": "#2563eb"}]}).encode()
        if method == "POST" and url == "https://www.googleapis.com/calendar/v3/calendars":
            return 200, {}, json.dumps({"id": "gc-cal-id", "summary": "GoalCal"}).encode()
        if url.endswith("/events/watch"):
            return 200, {}, json.dumps({"resourceId": "g-res-1", "expiration": "4102444800000"}).encode()
        if "/calendars/primary/events?" in url:
            google_pulls.append(url)
            return 200, {}, json.dumps({
                "items": [{
                    "id": "g-ev-1",
                    "summary": "Dentist",
                    "status": "confirmed",
                    "start": {"dateTime": f"{day.isoformat()}T10:00:00Z"},
                    "end": {"dateTime": f"{day.isoformat()}T11:00:00Z"},
                }],
                "nextSyncToken": "g-sync-tok-1",
            }).encode()
        if "/calendars/gc-cal-id/events" in url:
            if method == "GET":
                return 200, {}, json.dumps({"items": [], "nextSyncToken": "g-sync-gc-1"}).encode()
            if method == "POST":
                return 200, {}, json.dumps({"id": "g-pushed-1", "etag": '"ge1"'}).encode()
        return 404, {}, b"{}"

    monkeypatch.setattr("goalcal.domain.sync_providers.safe_http_request", fake_google_http)

    r_g_cb = proxy_request(
        "GET",
        f"/_goalcal/oauth/google/callback?code=g-code&state={state_val}",
        cookies={"goalcal_session": token_a, "goalcal_oauth_state": oauth_cookie},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert r_g_cb.status_code == 302
    assert r_g_cb.headers["location"].endswith("connected=google")

    # Verify Google watch webhook triggers sync on Personal calendar
    s = db.new_session()
    try:
        personal_src = s.scalar(select(CalendarSource).where(CalendarSource.user_id == uid, CalendarSource.remote_calendar_id == "primary"))
        assert personal_src is not None
        assert personal_src.webhook_channel_id and personal_src.webhook_secret
        ch_id, ch_sec = personal_src.webhook_channel_id, personal_src.webhook_secret
    finally:
        s.close()

    r_wh = proxy_request(
        "POST",
        "/calendar/webhook/google",
        headers={"X-Goog-Channel-ID": ch_id, "X-Goog-Channel-Token": ch_sec, "X-Goog-Resource-State": "exists"},
    )
    assert r_wh.status_code == 202
    assert len(google_pulls) == 1

    # Bad Google webhook secret returns 404
    assert proxy_request(
        "POST",
        "/calendar/webhook/google",
        headers={"X-Goog-Channel-ID": ch_id, "X-Goog-Channel-Token": "wrong", "X-Goog-Resource-State": "exists"},
    ).status_code == 404

    # 2) Outlook webhook validation handshake (MUST return validationToken as 200 text/plain)
    r_ms_val = proxy_request("POST", "/calendar/webhook/outlook?validationToken=hello-graph-123")
    assert r_ms_val.status_code == 200
    assert r_ms_val.text == "hello-graph-123"

    # Outlook 401 refresh failure marks connection reauth_required
    s = db.new_session()
    try:
        ms_conn = CalendarConnection(
            user_id=uid,
            provider="outlook",
            account_label="bob@outlook.com",
            username="bob@outlook.com",
            encrypted_secrets=crypto.encrypt_secrets({"access_token": "expired", "refresh_token": "bad-ref", "expires_at": "2020-01-01T00:00:00"}),
            status="active",
        )
        s.add(ms_conn)
        s.flush()
        ms_src = CalendarSource(
            user_id=uid,
            connection_id=ms_conn.id,
            provider="outlook",
            direction="inbound",
            name="Outlook Work",
            remote_calendar_id="ms-cal-1",
            enabled=True,
        )
        s.add(ms_src)
        s.commit()
        ms_src_id, ms_conn_id = ms_src.id, ms_conn.id
    finally:
        s.close()

    monkeypatch.setattr(
        "goalcal.domain.sync_providers.safe_http_request",
        lambda *_a, **_kw: (400, {}, b'{"error":"invalid_grant"}'),
    )
    bidirectional_sync.sync_native_source_once(ms_src_id)
    s = db.new_session()
    try:
        assert s.get(CalendarConnection, ms_conn_id).status == "reauth_required"
    finally:
        s.close()
