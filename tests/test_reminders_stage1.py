"""Tests for Stage 1 proactive reminders: Schema v14, pure decision engine, send-time guard, mailer & API."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
import sqlite3
import uuid

import httpx2
import pytest
from sqlalchemy import create_engine, select

from goalcal import auth, db, mailer, migrations, storage
from goalcal.domain import reminder_engine, reminders
from goalcal.models import CalendarSource, Checkin, Event, Goal, NotificationOutbox, Step, User
from goalcal.web.http import api
from goalcal.web.models import Habit, HabitOccurrence, HabitRule

ORIGIN = "https://goalcal.test"
PASSWORD = "long-stage1-reminder-password"


@pytest.fixture()
def accounts(tmp_path, monkeypatch):
    path = tmp_path / "reminders_stage1.db"
    monkeypatch.setattr(db, "settings", replace(db.settings, db_url=f"sqlite:///{path}"))
    monkeypatch.setattr(storage, "database_path", lambda url=None: path)
    monkeypatch.setattr(migrations, "database_path", lambda url=None: path)
    db.init_db()
    ids = []
    tokens = []
    for idx in range(2):
        name = f"rem_{idx}_{uuid.uuid4().hex[:8]}"
        ids.append(auth.create_user(name, PASSWORD, admin=True, email=f"{name}@example.com"))
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


def test_migration_v13_to_v14_preserves_data_and_creates_outbox(tmp_path):
    db_path = tmp_path / "v13.db"
    with sqlite3.connect(db_path) as con:
        con.executescript(
            """
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, email TEXT, password_hash TEXT, disabled BOOLEAN NOT NULL DEFAULT 0, name TEXT NOT NULL DEFAULT 'demo', tz TEXT NOT NULL DEFAULT 'Asia/Shanghai', created_at DATETIME);
            CREATE TABLE goals (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), draft_id TEXT, title TEXT, outcome TEXT, deadline DATE, status TEXT, pinned BOOLEAN NOT NULL DEFAULT 0, created_at DATETIME, template_key TEXT, outline TEXT);
            CREATE TABLE calendar_connections (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), provider VARCHAR(32) NOT NULL, account_label VARCHAR(200) NOT NULL DEFAULT '', server_url VARCHAR(500) NOT NULL DEFAULT '', username VARCHAR(200) NOT NULL DEFAULT '', encrypted_secrets TEXT NOT NULL DEFAULT '', token_expires_at DATETIME, status VARCHAR(24) NOT NULL DEFAULT 'active', last_error VARCHAR(500) NOT NULL DEFAULT '', created_at DATETIME, updated_at DATETIME);
            CREATE TABLE calendar_sources (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), connection_id INTEGER REFERENCES calendar_connections(id) ON DELETE CASCADE, provider VARCHAR(32) NOT NULL, direction VARCHAR(16) NOT NULL, name VARCHAR(120) NOT NULL, url VARCHAR(2000) NOT NULL DEFAULT '', remote_calendar_id VARCHAR(500) NOT NULL DEFAULT '', color VARCHAR(16) NOT NULL DEFAULT '#6b7f89', blocks_scheduling BOOLEAN NOT NULL DEFAULT 1, all_day_mode VARCHAR(16) NOT NULL DEFAULT 'ignore', writable BOOLEAN NOT NULL DEFAULT 0, is_goalcal_calendar BOOLEAN NOT NULL DEFAULT 0, enabled BOOLEAN NOT NULL DEFAULT 1, sync_interval_minutes INTEGER NOT NULL DEFAULT 30, last_synced_at DATETIME, last_status VARCHAR(20) NOT NULL DEFAULT 'idle', last_error VARCHAR(500) NOT NULL DEFAULT '', last_added INTEGER NOT NULL DEFAULT 0, last_updated INTEGER NOT NULL DEFAULT 0, last_removed INTEGER NOT NULL DEFAULT 0, last_skipped INTEGER NOT NULL DEFAULT 0, etag VARCHAR(255), last_modified VARCHAR(128), sync_token VARCHAR(500), webhook_channel_id VARCHAR(128), webhook_resource_id VARCHAR(255), webhook_secret VARCHAR(128), webhook_expires_at DATETIME, created_at DATETIME, updated_at DATETIME);
            CREATE TABLE calendar_feeds (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL UNIQUE REFERENCES users(id), token VARCHAR(64) NOT NULL UNIQUE, enabled BOOLEAN NOT NULL DEFAULT 1, include_blocks BOOLEAN NOT NULL DEFAULT 1, include_habits BOOLEAN NOT NULL DEFAULT 1, include_manual BOOLEAN NOT NULL DEFAULT 1, created_at DATETIME, rotated_at DATETIME, last_accessed_at DATETIME);
            CREATE TABLE events (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), title VARCHAR(200) NOT NULL, start_utc DATETIME NOT NULL, end_utc DATETIME NOT NULL, kind VARCHAR(20) NOT NULL, status VARCHAR(20) NOT NULL, source VARCHAR(20) NOT NULL, uid VARCHAR(200), sync_source_id INTEGER REFERENCES calendar_sources(id) ON DELETE CASCADE, all_day BOOLEAN NOT NULL DEFAULT 0, step_id INTEGER, resolved_at DATETIME, created_at DATETIME);
            INSERT INTO users (id, username, email, name, tz) VALUES (1, 'alice', 'alice@example.com', 'Alice', 'Asia/Shanghai');
            INSERT INTO goals (id, user_id, title, status) VALUES (1, 1, 'Launch v14', 'active');
            PRAGMA user_version = 13;
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
        assert "notification_outbox" in tables
        assert con.execute("SELECT email FROM users WHERE id=1").fetchone()[0] == "alice@example.com"


def test_email_preferences_and_quiet_hours_validation():
    normalized = reminders.normalize_email_digests({
        "enabled": True,
        "morning_focus": True,
        "morning_time": "08:00",
        "evening_wrapup": True,
        "evening_time": "21:15",
        "weekly_habit_nudge": False,
        "goal_stall_nudge": True,
        "weekly_review": True,
        "weekly_review_time": "20:00",
        "quiet_start": "22:30",
        "quiet_end": "07:30",
        "daily_cap": 4,
    })
    assert normalized["daily_cap"] == 4
    assert normalized["evening_time"] == "21:15"

    for bad in (
        {"enabled": "true"},
        {"evening_time": "25:00"},
        {"quiet_start": "9:00"},
        {"daily_cap": True},
        {"daily_cap": 0},
        {"daily_cap": 11},
    ):
        with pytest.raises(ValueError):
            reminders.normalize_email_digests(bad)

    assert reminder_engine.is_in_quiet_hours(datetime(2026, 11, 25, 23, 0), "22:30", "07:30") is True
    assert reminder_engine.is_in_quiet_hours(datetime(2026, 11, 25, 6, 45), "22:30", "07:30") is True
    assert reminder_engine.is_in_quiet_hours(datetime(2026, 11, 25, 12, 0), "22:30", "07:30") is False
    assert reminder_engine.is_in_quiet_hours(datetime(2026, 11, 25, 13, 15), "13:00", "14:00") is True
    assert reminder_engine.is_in_quiet_hours(datetime(2026, 11, 25, 13, 15), "12:00", "12:00") is False


def test_pure_reminder_rules_evening_morning_weekly_stall_and_review(accounts):
    ids, _ = accounts
    uid = ids[0]

    s = db.new_session()
    try:
        user = s.get(User, uid)
        reminders.set_email_digests(
            s,
            uid,
            {
                "enabled": True,
                "morning_focus": True,
                "morning_time": "08:30",
                "evening_wrapup": True,
                "evening_time": "21:00",
                "weekly_habit_nudge": True,
                "goal_stall_nudge": True,
                "weekly_review": True,
                "weekly_review_time": "19:30",
                "quiet_start": "22:30",
                "quiet_end": "07:30",
                "daily_cap": 5,
            },
        )
        # Goal 1: active with morning block + 2 unresolved evening blocks + stalled Goal 2
        g1 = Goal(user_id=uid, title="Write Book", created_at=datetime(2026, 11, 1, 0, 0))
        g2 = Goal(user_id=uid, title="Learn Rust", created_at=datetime(2026, 11, 10, 0, 0))
        s.add_all([g1, g2])
        s.flush()
        s1 = Step(goal_id=g1.id, seq=0, title="Outline Ch1", outcome="10 headings", est_min=45)
        s2 = Step(goal_id=g2.id, seq=0, title="Read Ch1 Ownership", outcome="2 exercises", est_min=30)
        s.add_all([s1, s2])
        s.flush()
        s.add(Checkin(step_id=s2.id, ts=datetime(2026, 11, 12, 10, 0), verdict="partial", minutes=20))

        # Thursday 2026-11-26 in Asia/Shanghai (UTC+8):
        # 09:00-09:45 local = 01:00-01:45 UTC (already ended before 21:00 local, still planned -> unresolved!)
        # 10:00-10:30 local = 02:00-02:30 UTC (also unresolved -> merged into ONE email!)
        s.add_all([
            Event(
                user_id=uid,
                title="Outline Ch1",
                start_utc=datetime(2026, 11, 26, 1, 0),
                end_utc=datetime(2026, 11, 26, 1, 45),
                kind="block",
                status="planned",
                source="web",
                step_id=s1.id,
            ),
            Event(
                user_id=uid,
                title="Draft Intro",
                start_utc=datetime(2026, 11, 26, 2, 0),
                end_utc=datetime(2026, 11, 26, 2, 30),
                kind="block",
                status="planned",
                source="web",
                step_id=s1.id,
            ),
            Event(
                user_id=uid,
                title="Finished earlier",
                start_utc=datetime(2026, 11, 26, 3, 0),
                end_utc=datetime(2026, 11, 26, 3, 30),
                kind="block",
                status="done",
                resolved_at=datetime(2026, 11, 26, 3, 30),
                source="web",
            ),
        ])

        # Flexible weekly habit: target 3/week, 0 done -> Mon (7d left > 3+1) silent; Thu (4d left <= 3+1) fires!
        h_weekly = Habit(
            user_id=uid,
            status="active",
            timezone="Asia/Shanghai",
            start_date=date(2026, 11, 1),
            confirmed_through=date(2026, 11, 26),
        )
        s.add(h_weekly)
        s.flush()
        s.add(
            HabitRule(
                user_id=uid,
                habit_id=h_weekly.id,
                effective_date=date(2026, 11, 1),
                definition=json.dumps({
                    "title": "Run 5km",
                    "est_min": 30,
                    "mode": "weekly",
                    "weekdays": [],
                    "start_time": None,
                    "weekly_target": 3,
                }),
            )
        )
        s.commit()

        # Morning focus on Thu 2026-11-26 08:35 local = 00:35 UTC
        morning_cands = reminder_engine.evaluate_user_email_reminders(
            s, user, datetime(2026, 11, 26, 0, 35, tzinfo=timezone.utc), base_url="https://goalcal.zhang.sarl"
        )
        assert [c["kind"] for c in morning_cands] == ["morning_focus"]
        assert "10 headings" in morning_cands[0]["payload"]["blocks"][0]["outcome"]

        # Weekly habit window on Mon 2026-11-23 11:35 local = 03:35 UTC -> NOT triggered yet (7 days > 3+1)
        mon_cands = reminder_engine.evaluate_user_email_reminders(
            s, user, datetime(2026, 11, 23, 3, 35, tzinfo=timezone.utc)
        )
        assert mon_cands == []

        # Weekly habit window on Thu 2026-11-26 11:35 local = 03:35 UTC -> triggers! (4 days <= 3+1)
        thu_noon = reminder_engine.evaluate_user_email_reminders(
            s, user, datetime(2026, 11, 26, 3, 35, tzinfo=timezone.utc)
        )
        assert [c["kind"] for c in thu_noon] == ["weekly_habit_nudge"]
        assert thu_noon[0]["dedupe_key"] == "email:weekly_habit_nudge:2026-11-23"

        # Goal stall on Thu 2026-11-26 15:05 local = 07:05 UTC -> detects Learn Rust (g1 has open future/today block?)
        # Wait at 07:05 UTC (15:05 local), g1's blocks ended at 02:30 UTC so g1 has NO future block either and no checkin!
        stall_cands = reminder_engine.evaluate_user_email_reminders(
            s, user, datetime(2026, 11, 26, 7, 5, tzinfo=timezone.utc)
        )
        assert [c["kind"] for c in stall_cands] == ["goal_stall"]
        titles = {g["title"] for g in stall_cands[0]["payload"]["goals"]}
        assert "Learn Rust" in titles

        # Evening merged wrap-up on Thu 2026-11-26 21:05 local = 13:05 UTC -> 1 merged email with 2 blocks
        evening_cands = reminder_engine.evaluate_user_email_reminders(
            s, user, datetime(2026, 11, 26, 13, 5, tzinfo=timezone.utc)
        )
        assert [c["kind"] for c in evening_cands] == ["evening_wrapup"]
        assert len(evening_cands[0]["payload"]["blocks"]) == 2
        assert evening_cands[0]["dedupe_key"] == "email:evening_wrapup:2026-11-26"

        # Sunday weekly review 19:35 local (2026-11-29 11:35 UTC)
        review_cands = reminder_engine.evaluate_user_email_reminders(
            s, user, datetime(2026, 11, 29, 11, 35, tzinfo=timezone.utc)
        )
        assert [c["kind"] for c in review_cands] == ["weekly_review"]
        assert review_cands[0]["payload"]["blocks_done"] == 1
    finally:
        s.close()


def test_outbox_idempotency_send_time_guards_and_backoff(accounts, monkeypatch):
    ids, _ = accounts
    uid = ids[0]
    sent_mails: list[dict] = []
    should_fail = {"flag": False}

    def fake_send_email(*, to_address, subject, text_body, html_body=None):
        if should_fail["flag"]:
            raise mailer.MailerError("temporary SMTP timeout")
        sent_mails.append({"to": to_address, "subject": subject, "text": text_body, "html": html_body})

    monkeypatch.setattr(mailer, "smtp_capabilities", lambda: {"configured": True, "from_address": "noreply@goalcal.test"})
    monkeypatch.setattr(mailer, "send_email", fake_send_email)

    s = db.new_session()
    try:
        reminders.set_email_digests(
            s,
            uid,
            {
                "enabled": True,
                "morning_focus": False,
                "morning_time": "08:30",
                "evening_wrapup": True,
                "evening_time": "21:00",
                "weekly_habit_nudge": False,
                "goal_stall_nudge": False,
                "weekly_review": False,
                "weekly_review_time": "19:30",
                "quiet_start": "22:30",
                "quiet_end": "07:30",
                "daily_cap": 3,
            },
        )
        ev = Event(
            user_id=uid,
            title="Finish report",
            start_utc=datetime(2026, 11, 26, 2, 0),
            end_utc=datetime(2026, 11, 26, 3, 0),
            kind="block",
            status="planned",
            source="web",
        )
        src = CalendarSource(user_id=uid, provider="ics_url", direction="inbound", name="Work", url="https://cal.example/w.ics", blocks_scheduling=True)
        s.add_all([ev, src])
        s.flush()
        ev_id = ev.id
        # External blocking meeting active right at 21:05 local (13:05 UTC) -> send-time guard should DEFER!
        ext_ev = Event(
            user_id=uid,
            title="Late sync call",
            start_utc=datetime(2026, 11, 26, 13, 0),
            end_utc=datetime(2026, 11, 26, 13, 10),
            kind="external",
            status="planned",
            source="sync",
            sync_source_id=src.id,
        )
        s.add(ext_ev)
        s.commit()
    finally:
        s.close()

    t_2105 = datetime(2026, 11, 26, 13, 5, tzinfo=timezone.utc)
    # First plan enqueues 1 row; second plan with same window is idempotent (0 new rows)
    assert reminder_engine.plan_due_notifications_once(now=t_2105) == 1
    assert reminder_engine.plan_due_notifications_once(now=t_2105) == 0

    # At 13:05 UTC user is busy in external meeting -> deferred, no mail sent
    assert reminder_engine.process_notification_outbox_once(now=t_2105) == 0
    assert sent_mails == []

    # Advance 20 min (meeting ended, quiet_hours not reached yet at 21:25 local):
    # Suppose transient SMTP failure first -> increments attempts and sets backoff
    t_2125 = datetime(2026, 11, 26, 13, 25, tzinfo=timezone.utc)
    should_fail["flag"] = True
    assert reminder_engine.process_notification_outbox_once(now=t_2125) == 0
    s = db.new_session()
    try:
        row = s.scalar(select(NotificationOutbox).where(NotificationOutbox.user_id == uid))
        assert row.status == "pending"
        assert row.attempts == 1
    finally:
        s.close()

    # Now resolve the block BEFORE retry succeeds -> send-time guard SKIPS sending!
    s = db.new_session()
    try:
        row = s.get(Event, ev_id)
        row.status = "done"
        row.resolved_at = datetime(2026, 11, 26, 13, 26)
        s.commit()
    finally:
        s.close()

    should_fail["flag"] = False
    t_retry = datetime(2026, 11, 26, 13, 30, tzinfo=timezone.utc)
    assert reminder_engine.process_notification_outbox_once(now=t_retry) == 0
    assert sent_mails == []
    s = db.new_session()
    try:
        row = s.scalar(select(NotificationOutbox).where(NotificationOutbox.user_id == uid))
        assert row.status == "skipped"
        assert row.last_error == "resolved_before_send"
    finally:
        s.close()

    # Create a new morning focus item for next day and send successfully
    s = db.new_session()
    try:
        reminders.set_email_digests(
            s,
            uid,
            {
                "enabled": True,
                "morning_focus": True,
                "morning_time": "08:30",
                "evening_wrapup": False,
                "evening_time": "21:00",
                "weekly_habit_nudge": False,
                "goal_stall_nudge": False,
                "weekly_review": False,
                "weekly_review_time": "19:30",
                "quiet_start": "22:30",
                "quiet_end": "07:30",
                "daily_cap": 3,
            },
        )
        s.add(
            Event(
                user_id=uid,
                title="Morning Deep Work",
                start_utc=datetime(2026, 11, 27, 1, 0),
                end_utc=datetime(2026, 11, 27, 2, 0),
                kind="block",
                status="planned",
                source="web",
            )
        )
        s.commit()
    finally:
        s.close()

    t_morning = datetime(2026, 11, 27, 0, 35, tzinfo=timezone.utc)
    assert reminder_engine.plan_due_notifications_once(now=t_morning) == 1
    assert reminder_engine.process_notification_outbox_once(now=t_morning) == 1
    assert len(sent_mails) == 1
    assert "Morning Deep Work" in sent_mails[0]["text"]
    assert "<html" in sent_mails[0]["html"].lower()


def test_reminders_api_test_email_and_timezone_update(accounts, monkeypatch):
    ids, tokens = accounts
    uid_a, token_a, token_b = ids[0], tokens[0], tokens[1]

    # 1. GET /reminders returns defaults + calendar sync status
    r0 = api_request(token_a, "GET", "/reminders")
    assert r0.status_code == 200
    d0 = r0.json()
    assert d0["calendar_alarms"]["block_minutes"] == 10
    assert d0["calendar_sync_connected"] == {"two_way": False, "feed": False}
    assert d0["email_digests"]["enabled"] is False
    assert d0["email_channel"]["smtp_configured"] is False

    # 2. Test email endpoint is removed from public user API (404)
    r_removed = api_request(token_a, "POST", "/reminders/email/test", {})
    assert r_removed.status_code == 404

    # 3. PUT /reminders/email persists settings and enforces 409 version & 422 validation
    bad = api_request(token_a, "PUT", "/reminders/email", {"enabled": True, "evening_time": "99:99"})
    assert bad.status_code == 422

    r_put = api_request(
        token_a,
        "PUT",
        "/reminders/email",
        {
            "enabled": True,
            "morning_focus": True,
            "morning_time": "08:15",
            "evening_wrapup": True,
            "evening_time": "21:30",
            "weekly_habit_nudge": True,
            "goal_stall_nudge": False,
            "weekly_review": True,
            "weekly_review_time": "20:00",
            "quiet_start": "23:00",
            "quiet_end": "07:00",
            "daily_cap": 4,
        },
    )
    assert r_put.status_code == 200
    ver1 = r_put.json()["email_digests"]["version"]
    assert r_put.json()["email_digests"]["enabled"] is True

    # User B remains isolated with defaults
    assert api_request(token_b, "GET", "/reminders").json()["email_digests"]["enabled"] is False

    # Version conflict check
    r_put2 = api_request(
        token_a,
        "PUT",
        "/reminders/email",
        {"enabled": True, "daily_cap": 2, "version": ver1},
    )
    assert r_put2.status_code == 200
    assert (
        api_request(
            token_a,
            "PUT",
            "/reminders/email",
            {"enabled": False, "daily_cap": 2, "version": ver1},
        ).status_code
        == 409
    )

    # 4. Internal CLI helper still works when SMTP mocked; public recent_notifications hides test_email & last_error
    sent: list[dict] = []
    monkeypatch.setattr(mailer, "smtp_capabilities", lambda: {"configured": True, "from_address": "bot@goalcal.test"})
    monkeypatch.setattr(mailer, "send_email", lambda **kw: sent.append(kw))
    subj = reminder_engine.send_test_reminder_email(to_address="admin@goalcal.test", lang="zh", tz="Asia/Shanghai")
    assert len(sent) == 1
    assert "GoalCal" in subj

    s = db.new_session()
    try:
        s.add_all([
            NotificationOutbox(
                user_id=uid_a,
                channel="email",
                kind="test_email",
                dedupe_key="test-hidden",
                recipient="a@example.com",
                subject="Admin Test",
                status="sent",
            ),
            NotificationOutbox(
                user_id=uid_a,
                channel="email",
                kind="evening_wrapup",
                dedupe_key="ev-visible",
                recipient="a@example.com",
                subject="GoalCal · 晚间收口",
                status="skipped",
                last_error="resolved_before_send",
            ),
        ])
        s.commit()
    finally:
        s.close()

    recent_a = api_request(token_a, "GET", "/reminders").json()["recent_notifications"]
    assert len(recent_a) == 1
    assert recent_a[0]["kind"] == "evening_wrapup"
    assert "last_error" not in recent_a[0]
    assert api_request(token_b, "GET", "/reminders").json()["recent_notifications"] == []

    # 5. Web timezone selector via PATCH /me
    r_bad_tz = api_request(token_a, "PATCH", "/me", {"timezone": "Mars/Olympus"})
    assert r_bad_tz.status_code == 422

    r_tz = api_request(token_a, "PATCH", "/me", {"timezone": "America/New_York"})
    assert r_tz.status_code == 200
    assert r_tz.json()["timezone"] == "America/New_York"
    assert api_request(token_a, "GET", "/me").json()["timezone"] == "America/New_York"
