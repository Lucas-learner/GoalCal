"""Tests for multi-goal global weekly balanced scheduling (Phase 1)."""
from __future__ import annotations

from dataclasses import replace
import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx2
from sqlalchemy import create_engine, select

from goalcal import auth, db, memory, migrations, storage
from goalcal.domain import scheduler, weekly_planner
from goalcal.domain.scheduler import BusyBlock, Preferences
from goalcal.domain.weekly_planner import WeeklyGoalInput, WeeklyStepInput
from goalcal.models import Checkin, Event, Goal, Step
from goalcal.web import core as web_core, http as web_http
from goalcal.web.models import WebWeeklyPlan

SH = ZoneInfo("Asia/Shanghai")


def _avail_cfg(*, max_daily: int = 120) -> dict:
    return {
        "available_windows": [{"days": list(range(7)), "start": "08:00", "end": "22:00"}],
        "protected_windows": [{"days": list(range(7)), "start": "12:00", "end": "13:00"}],
        "preferred_windows": [{"days": list(range(7)), "start": "09:00", "end": "12:00"}],
        "buffer_minutes": 10,
        "max_goal_minutes_per_day": max_daily,
    }


def _prefs(max_daily: int = 120) -> Preferences:
    return Preferences(
        max_goal_minutes_per_day=max_daily,
        buffer_minutes=10,
        web_availability=_avail_cfg(max_daily=max_daily),
    )


def test_resolve_week_window_weekday_and_sunday():
    # Wednesday 2026-11-25 -> this_week (Mon 2026-11-23 .. Sun 2026-11-29)
    wed = datetime(2026, 11, 25, 10, 30, tzinfo=SH)
    scope, wstart, wend, win_s, win_e = weekly_planner.resolve_week_window(wed, "auto")
    assert scope == "this_week"
    assert wstart == date(2026, 11, 23)
    assert wend == date(2026, 11, 29)
    assert win_s == wed
    assert win_e == datetime(2026, 11, 30, 0, 0, tzinfo=SH)

    # Sunday 2026-11-29 -> auto switches to next_week (Mon 2026-11-30 .. Sun 2026-12-06)
    sun = datetime(2026, 11, 29, 20, 0, tzinfo=SH)
    scope2, wstart2, wend2, win_s2, win_e2 = weekly_planner.resolve_week_window(sun, "auto")
    assert scope2 == "next_week"
    assert wstart2 == date(2026, 11, 30)
    assert wend2 == date(2026, 12, 6)
    assert win_s2 == datetime(2026, 11, 30, 0, 0, tzinfo=SH)
    assert win_e2 == datetime(2026, 12, 7, 0, 0, tzinfo=SH)


def test_plan_week_balanced_multi_goal_spread_and_sequence():
    now_local = datetime(2026, 11, 25, 8, 0, tzinfo=SH)  # Wed
    scope, wstart, wend, win_s, win_e = weekly_planner.resolve_week_window(now_local, "auto")
    g1 = WeeklyGoalInput(
        goal_id=1,
        title="Launch Site",
        pinned=True,
        deadline=date(2026, 11, 29),
        total_todo_minutes=135,
        recent_activity=True,
        steps=(
            WeeklyStepInput(step_id=101, seq=0, title="Design spec", est_min=45, energy=2),
            WeeklyStepInput(step_id=102, seq=1, title="Build API", est_min=45, energy=2),
            WeeklyStepInput(step_id=103, seq=2, title="Write docs", est_min=45, energy=1),
        ),
    )
    g2 = WeeklyGoalInput(
        goal_id=2,
        title="Learn Piano",
        pinned=False,
        deadline=None,
        total_todo_minutes=90,
        recent_activity=False,  # triggers anti-starvation boost
        steps=(
            WeeklyStepInput(step_id=201, seq=0, title="Scales", est_min=45, energy=1),
            WeeklyStepInput(step_id=202, seq=1, title="Chords", est_min=45, energy=1),
        ),
    )

    res = weekly_planner.plan_week(
        [g1, g2],
        now_local,
        (win_s, win_e),
        busy=[],
        prefs=_prefs(max_daily=120),
        scope=scope,
        week_start=wstart,
        week_end=wend,
        strategy="balanced",
        lang="en",
    )

    assert len(res.items) >= 4
    by_goal = {}
    for item in res.items:
        by_goal.setdefault(item.goal_id, []).append(item)

    # Both goals must receive steps (multi-goal balance + anti-starvation)
    assert 1 in by_goal and 2 in by_goal
    # Strict intra-goal step ordering preserved
    for gid, seq_steps in ((1, [101, 102, 103]), (2, [201, 202])):
        planned_ids = [b.step_id for b in by_goal[gid]]
        assert planned_ids == seq_steps[: len(planned_ids)]
        for a, b in zip(by_goal[gid], by_goal[gid][1:]):
            assert b.start >= a.end
    # Total goal minutes per day never exceeds daily limit
    per_day = {}
    for item in res.items:
        d = item.start.date()
        per_day[d] = per_day.get(d, 0) + item.est_min
    assert all(mins <= 120 for mins in per_day.values())


def test_plan_week_front_loads_early_days_to_maximize_weekly_throughput():
    assert Preferences().max_goal_minutes_per_day == 240
    now_local = datetime(2026, 11, 25, 8, 0, tzinfo=SH)  # Wed, 5 days remaining
    scope, wstart, wend, win_s, win_e = weekly_planner.resolve_week_window(now_local, "auto")
    g1 = WeeklyGoalInput(
        goal_id=1,
        title="Backend API",
        pinned=True,
        deadline=None,
        total_todo_minutes=90,
        recent_activity=True,
        steps=(
            WeeklyStepInput(step_id=11, seq=0, title="Schema", est_min=45, energy=1),
            WeeklyStepInput(step_id=12, seq=1, title="Routes", est_min=45, energy=1),
        ),
    )
    g2 = WeeklyGoalInput(
        goal_id=2,
        title="Writing",
        pinned=False,
        deadline=None,
        total_todo_minutes=90,
        recent_activity=True,
        steps=(
            WeeklyStepInput(step_id=21, seq=0, title="Outline", est_min=45, energy=1),
            WeeklyStepInput(step_id=22, seq=1, title="Draft", est_min=45, energy=1),
        ),
    )

    res = weekly_planner.plan_week(
        [g1, g2],
        now_local,
        (win_s, win_e),
        busy=[],
        prefs=_prefs(max_daily=240),
        scope=scope,
        week_start=wstart,
        week_end=wend,
        strategy="balanced",
    )

    assert len(res.items) == 4
    # Front-loaded: all 4 steps (180 min <= 240 min daily cap) pack onto day 0 (Wed 2026-11-25),
    # leaving later days open for rolling next-phase steps once completed.
    assert all(item.start.date() == date(2026, 11, 25) for item in res.items)
    # Intra-goal order still holds
    g1_items = [i for i in res.items if i.goal_id == 1]
    g2_items = [i for i in res.items if i.goal_id == 2]
    assert [i.step_id for i in g1_items] == [11, 12] and g1_items[1].start >= g1_items[0].end
    assert [i.step_id for i in g2_items] == [21, 22] and g2_items[1].start >= g2_items[0].end


def test_plan_week_strategies_and_unscheduled_diagnostics():
    now_local = datetime(2026, 11, 27, 18, 0, tzinfo=SH)  # Fri evening
    scope, wstart, wend, win_s, win_e = weekly_planner.resolve_week_window(now_local, "auto")
    # Remaining days: Fri (18-22), Sat, Sun. Cap daily limit tightly to force competition & overflow.
    g_pinned = WeeklyGoalInput(
        goal_id=1,
        title="Pinned Goal",
        pinned=True,
        deadline=date(2026, 12, 20),
        total_todo_minutes=180,
        recent_activity=True,
        steps=(
            WeeklyStepInput(step_id=11, seq=0, title="P1", est_min=60, energy=1),
            WeeklyStepInput(step_id=12, seq=1, title="P2", est_min=60, energy=1),
            WeeklyStepInput(step_id=13, seq=2, title="P3", est_min=60, energy=1),
        ),
    )
    g_urgent = WeeklyGoalInput(
        goal_id=2,
        title="Urgent Goal",
        pinned=False,
        deadline=date(2026, 11, 28),  # Sat
        total_todo_minutes=120,
        recent_activity=True,
        steps=(
            WeeklyStepInput(step_id=21, seq=0, title="U1", est_min=60, energy=2),
            WeeklyStepInput(step_id=22, seq=1, title="U2", est_min=60, energy=2),
        ),
    )
    g_too_long = WeeklyGoalInput(
        goal_id=3,
        title="Oversized Goal",
        pinned=False,
        deadline=None,
        total_todo_minutes=200,
        recent_activity=False,
        steps=(WeeklyStepInput(step_id=31, seq=0, title="Huge step", est_min=200, energy=2),),
    )

    # Pinned strategy favors pinned goal when capacity is scarce
    pinned_res = weekly_planner.plan_week(
        [g_pinned, g_urgent],
        now_local,
        (win_s, win_e),
        busy=[BusyBlock(datetime(2026, 11, 28, 8, 0, tzinfo=SH), datetime(2026, 11, 28, 22, 0, tzinfo=SH))],
        prefs=_prefs(max_daily=60),
        scope=scope,
        week_start=wstart,
        week_end=wend,
        strategy="pinned",
    )
    assert any(i.goal_id == 1 for i in pinned_res.items)

    # Deadline strategy favors urgent goal before Sat deadline
    dl_res = weekly_planner.plan_week(
        [g_pinned, g_urgent],
        now_local,
        (win_s, win_e),
        busy=[BusyBlock(datetime(2026, 11, 29, 8, 0, tzinfo=SH), datetime(2026, 11, 29, 22, 0, tzinfo=SH))],
        prefs=_prefs(max_daily=60),
        scope=scope,
        week_start=wstart,
        week_end=wend,
        strategy="deadline",
    )
    assert dl_res.items and any(i.goal_id == 2 for i in dl_res.items)

    # Diagnostic reason for unscheduled oversized step
    diag_res = weekly_planner.plan_week(
        [g_too_long],
        now_local,
        (win_s, win_e),
        busy=[],
        prefs=_prefs(max_daily=90),
        scope=scope,
        week_start=wstart,
        week_end=wend,
        strategy="balanced",
        lang="zh",
    )
    assert not diag_res.items
    assert len(diag_res.unscheduled) == 1
    assert diag_res.unscheduled[0].reason_code in {"daily_limit_reached", "step_too_long"}


def _seed_user(tmp_path, monkeypatch):
    db_file = tmp_path / "weekly_planner.db"
    monkeypatch.setattr(db, "settings", replace(db.settings, db_url=f"sqlite:///{db_file}"))
    monkeypatch.setattr(storage, "database_path", lambda url=None: db_file)
    monkeypatch.setattr(migrations, "database_path", lambda url=None: db_file)
    db.init_db()
    alice_id = auth.create_user("alice", "alice-pw-1234", email="alice@example.com", tz="Asia/Shanghai", require_invite=False)
    bob_id = auth.create_user("bob", "bob-pw-1234", email="bob@example.com", tz="Asia/Shanghai", require_invite=False)
    return (alice_id, auth.login("alice", "alice-pw-1234")), (bob_id, auth.login("bob", "bob-pw-1234"))


def test_weekly_plan_api_accept_partial_undo_replace_and_isolation(tmp_path, monkeypatch):
    (alice_id, alice_token), (bob_id, bob_token) = _seed_user(tmp_path, monkeypatch)

    # Populate two goals for Alice
    with web_http.scoped(alice_token, write=True) as (session, _):
        memory.set_preference(session, alice_id, "web_availability", _avail_cfg(max_daily=120), scope="scheduler")
        g1 = Goal(user_id=alice_id, title="Goal Alpha", pinned=True, deadline=date(2026, 11, 29), status="active")
        g2 = Goal(user_id=alice_id, title="Goal Beta", pinned=False, status="active")
        session.add_all([g1, g2])
        session.flush()
        s1 = Step(goal_id=g1.id, seq=0, title="Alpha 1", est_min=45, energy=2, kind="deep", status="todo")
        s2 = Step(goal_id=g1.id, seq=1, title="Alpha 2", est_min=45, energy=1, kind="deep", status="todo")
        s3 = Step(goal_id=g2.id, seq=0, title="Beta 1", est_min=45, energy=1, kind="research", status="todo")
        session.add_all([s1, s2, s3])
        session.flush()
        s1_id, s2_id, s3_id = s1.id, s2.id, s3.id

    now = datetime(2026, 11, 25, 0, 30, tzinfo=timezone.utc)  # 08:30 Asia/Shanghai on Wed 2026-11-25

    # 1. Create plan
    with web_http.scoped(alice_token, write=True) as (session, _):
        svc = web_core.Service(session, alice_id, "Asia/Shanghai", now, lang="zh")
        plan = svc.create_weekly_plan({"scope": "auto", "strategy": "balanced", "keep_existing_blocks": True})
        assert plan["scope"] == "this_week"
        assert plan["week_start"] == "2026-11-23"
        assert plan["week_end"] == "2026-11-29"
        assert len(plan["items"]) >= 2
        plan_id = plan["id"]
        plan_ver = plan["version"]
        picked_step_ids = [plan["items"][0]["step_id"], plan["items"][-1]["step_id"]]
        picked_step_ids = list(dict.fromkeys(picked_step_ids))

    # 2. Cross-tenant isolation: Bob cannot accept Alice's weekly plan
    with web_http.scoped(bob_token, write=True) as (session, _):
        bob_svc = web_core.Service(session, bob_id, "Asia/Shanghai", now, lang="zh")
        try:
            bob_svc.accept_weekly_plan(plan_id, {"version": plan_ver, "step_ids": picked_step_ids})
            raise AssertionError("Expected not_found for cross-tenant access")
        except web_core.ApiError as exc:
            assert exc.status == 404

    # 3. Accept partial subset -> atomic WebOperation creation
    with web_http.scoped(alice_token, write=True) as (session, _):
        svc = web_core.Service(session, alice_id, "Asia/Shanghai", now, lang="zh")
        acc = svc.accept_weekly_plan(plan_id, {"version": plan_ver, "step_ids": picked_step_ids})
        assert acc["accepted_count"] == len(picked_step_ids)
        assert acc["operation_id"]
        op_id = acc["operation_id"]
        created_ids = [ev["id"] for ev in acc["events"]]
        events_now = session.scalars(select(Event).where(Event.user_id == alice_id, Event.id.in_(created_ids))).all()
        assert all(ev.status == "planned" for ev in events_now)

    # 4. Batch Undo cancels all created events
    with web_http.scoped(alice_token, write=True) as (session, _):
        svc = web_core.Service(session, alice_id, "Asia/Shanghai", now, lang="zh")
        svc.undo(op_id)
        events_after = session.scalars(select(Event).where(Event.user_id == alice_id, Event.id.in_(created_ids))).all()
        assert all(ev.status == "skipped" for ev in events_after)

    # 5. Re-plan with keep_existing_blocks=False replaces an open future block and Undo restores it
    with web_http.scoped(alice_token, write=True) as (session, _):
        svc = web_core.Service(session, alice_id, "Asia/Shanghai", now, lang="zh")
        existing = Event(
            user_id=alice_id,
            title="Alpha 1",
            start_utc=datetime(2026, 11, 25, 13, 0),  # 21:00 SH
            end_utc=datetime(2026, 11, 25, 13, 45),
            kind="block",
            status="planned",
            source="web",
            step_id=s1_id,
        )
        session.add(existing)
        session.flush()
        old_ev_id = existing.id

        plan2 = svc.create_weekly_plan({"scope": "this_week", "strategy": "balanced", "keep_existing_blocks": False})
        s1_item = next(i for i in plan2["items"] if i["step_id"] == s1_id)
        assert s1_item["replaces_event_id"] == old_ev_id
        acc2 = svc.accept_weekly_plan(plan2["id"], {"version": plan2["version"], "step_ids": [i["step_id"] for i in plan2["items"]]})
        op2 = acc2["operation_id"]
        old_row = session.get(Event, old_ev_id)
        assert old_row.status == "skipped"

        # Undo restores old_row and cancels newly created ones
        svc.undo(op2)
        assert session.get(Event, old_ev_id).status == "planned"

    # 6. Stale availability invalidates open weekly plan (409)
    with web_http.scoped(alice_token, write=True) as (session, _):
        svc = web_core.Service(session, alice_id, "Asia/Shanghai", now, lang="zh")
        plan3 = svc.create_weekly_plan({"scope": "this_week", "strategy": "balanced", "keep_existing_blocks": True})
        memory.set_preference(session, alice_id, "web_availability", _avail_cfg(max_daily=90), scope="scheduler")
        try:
            svc.accept_weekly_plan(plan3["id"], {"version": plan3["version"]})
            raise AssertionError("Expected stale_proposal ApiError")
        except web_core.ApiError as exc:
            assert exc.code == "stale_proposal"
            assert exc.status == 409

    # 7. Real HTTP API endpoints POST /weekly-plans and POST /weekly-plans/{id}/accept
    async def _http_flow():
        headers = {"Origin": "https://goalcal.test", "Idempotency-Key": "weekly-plan-http-1"}
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=web_http.api),
            base_url="https://goalcal.test",
            cookies={"goalcal_session": alice_token},
        ) as client:
            r1 = await client.post("/weekly-plans", json={"scope": "next_week", "strategy": "balanced"}, headers=headers)
            assert r1.status_code == 200
            data = r1.json()
            assert data["scope"] == "next_week"
            assert data["items"]
            r2 = await client.post(
                f"/weekly-plans/{data['id']}/accept",
                json={"version": data["version"], "step_ids": [data["items"][0]["step_id"]]},
                headers={**headers, "Idempotency-Key": "weekly-plan-http-2"},
            )
            assert r2.status_code == 200
            assert r2.json()["accepted_count"] == 1
            assert r2.json()["operation_id"]

    import asyncio
    asyncio.run(_http_flow())


def test_migration_v15_to_v16_creates_web_weekly_plans(tmp_path):
    db_path = tmp_path / "v15.db"
    with sqlite3.connect(db_path) as con:
        con.executescript(
            """
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, email TEXT, password_hash TEXT, disabled BOOLEAN NOT NULL DEFAULT 0, name TEXT NOT NULL DEFAULT 'demo', tz TEXT NOT NULL DEFAULT 'Asia/Shanghai', created_at DATETIME);
            CREATE TABLE goals (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), draft_id TEXT, title TEXT, outcome TEXT, deadline DATE, status TEXT, pinned BOOLEAN NOT NULL DEFAULT 0, created_at DATETIME, template_key TEXT, outline TEXT);
            CREATE TABLE web_executions (id VARCHAR(32) NOT NULL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), event_id INTEGER NOT NULL, step_id INTEGER, habit_occurrence_id VARCHAR(40), status VARCHAR(20) NOT NULL, started_at DATETIME NOT NULL, expected_end_at DATETIME NOT NULL, resolved_at DATETIME, version INTEGER NOT NULL);
            CREATE TABLE habits (id INTEGER PRIMARY KEY, pinned BOOLEAN NOT NULL DEFAULT 0);
            CREATE TABLE events (id INTEGER PRIMARY KEY, sync_source_id INTEGER, all_day BOOLEAN NOT NULL DEFAULT 0);
            PRAGMA user_version = 15;
            """
        )

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        migrations.migrate(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == migrations.VERSION == 16
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "web_weekly_plans" in tables
