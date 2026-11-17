"""Direct service tests use real ORM data and transactions, with an injected clock."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from goalcal.domain import goals, scheduler
from goalcal.memory import get_scheduler_preferences, set_preference
from goalcal.models import Checkin, Event, UserPreference
from goalcal.web.core import ApiError, Service, version_token
from goalcal.web.models import WebDraft, WebExecution, WebOperation, WebProposal

NOW = datetime(2026, 11, 17, 1, tzinfo=timezone.utc)  # Tue 09:00 Shanghai


@pytest.fixture
def service(session, user):
    return Service(session, user.id, user.tz, NOW)


@pytest.fixture
def goal(session, user):
    return goals.create_goal(session, user.id, "学习钢琴", deadline=(NOW + timedelta(days=30)).date(),
                             step_defs=[{"seq": 1, "title": "练习第一段", "est_min": 45, "milestone": "入门"},
                                        {"seq": 2, "title": "练习第二段", "est_min": 30, "milestone": "入门"}],
                             outline=["入门"], now=NOW.replace(tzinfo=None))


def availability(service, **changes):
    return service.save_preferences({**service.preferences(), **changes})


def proposal(service, goal):
    result = service.propose(goal.steps[0].id, {})
    assert result["candidates"]
    return result


def accepted(service, goal):
    result = proposal(service, goal)
    return service.accept(result["id"], {"version": result["version"], "index": 0})


def assert_error(code, call):
    with pytest.raises(ApiError) as error:
        call()
    assert error.value.code == code
    return error.value


def test_read_dtos_and_today_never_write(session, service, goal):
    before = set(session.new) | set(session.dirty)
    today = service.today()
    assert today["date"] == "2026-11-17"
    assert today["next"]["goal"]["title"] == goal.title
    assert today["next"]["event"] is None
    assert today["execution"] is None
    assert service.me()["timezone"] == "Asia/Shanghai"
    assert service.goals()["goals"][0]["steps"][0]["title"] == "练习第一段"
    assert not (set(session.new) | set(session.dirty)) - before
    assert "state_json" not in json.dumps(today)


def test_tenant_isolation_on_plain_session(session, service, goal):
    other = goals.ensure_user(session, "other")
    other_goal = goals.create_goal(session, other.id, "private")
    foreign = Service(session, other.id, other.tz, NOW)
    foreign_draft = foreign.draft({"title": "private draft", "deadline": "2026-12-01"})
    for call in (lambda: service.goal(other_goal.id),
                 lambda: service.propose(other_goal.steps[0].id, {}),
                 lambda: service.create_goal({"draft_id": foreign_draft["id"], "version": "1"})):
        assert_error("not_found", call)
    result = accepted(service, goal)
    foreign_proposal = service._get(WebProposal, result["proposal"]["id"])
    assert_error("not_found", lambda: foreign.accept(foreign_proposal.id, {"version": "2", "index": 0}))
    assert_error("not_found", lambda: foreign.undo(result["operation_id"]))


def test_draft_create_and_reuse(session, service):
    draft = service.draft({"title": "学习钢琴", "deadline": "2026-12-01"})
    assert "template" in draft["source"]
    assert draft["outline"] and draft["steps"]
    result = service.create_goal({"draft_id": draft["id"], "version": draft["version"],
                                  "title": "每天练琴", "deadline": "2026-12-02", "steps": draft["steps"]})
    assert result["goal"]["title"] == "每天练琴"
    assert session.get(WebDraft, draft["id"]).goal_id == result["goal"]["id"]
    assert_error("version_conflict", lambda: service.create_goal({"draft_id": draft["id"], "version": "1"}))


def test_prepared_draft_and_validation(service):
    prepared = {"outline": ["阶段"], "source": "llm", "steps": [{"title": "验证成果", "milestone": "阶段", "est_min": 25}]}
    draft = service.draft({"title": "自定义", "deadline": None, "_prepared": prepared})
    assert draft["source"] == "llm"
    assert draft["steps"][0]["est_min"] == 25
    assert_error("validation", lambda: service.draft({"title": "", "deadline": None}))
    assert_error("validation", lambda: service.draft({"title": "目标", "deadline": "2026-01-01"}))
    assert_error("validation", lambda: service.draft({"title": "目标", "_prepared": {"steps": []}}))


def test_legacy_change_invalidates_versions(session, service, goal):
    dto = service.goal(goal.id)
    goal.title = "旧界面改名"
    session.flush()
    assert_error("version_conflict", lambda: service.update_goal(goal.id, {"version": dto["version"], "pinned": True}))
    dto = service.goal(goal.id)
    updated = service.update_goal(goal.id, {"version": dto["version"], "pinned": True})
    assert updated["goal"]["pinned"] is True


def test_goal_pause_and_step_completion_soft_cancel(session, service, goal):
    event = accepted(service, goal)["event"]
    service.update_goal(goal.id, {"version": version_token(goal), "status": "paused"})
    assert session.get(Event, event["id"]).status == "skipped"
    assert session.get(Event, event["id"]) is not None
    assert_error("invalid_state", lambda: service.propose(goal.steps[0].id, {}))
    service.update_goal(goal.id, {"version": version_token(goal), "status": "active"})
    step = goal.steps[0]
    service.update_step(step.id, {"version": version_token(step), "status": "done"})
    assert step.status == "done"
    assert len(list(session.scalars(select(Checkin)))) == 1


def test_aware_dates_range_overlap_and_move(session, service):
    assert_error("validation", lambda: service.create_event({"title": "x", "start": "2026-11-17T01:00:00", "end": "2026-11-17T02:00:00Z"}))
    assert_error("validation", lambda: service.events("2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z"))
    event = service.create_event({"title": "会议", "start": "2026-11-17T10:00:00+08:00", "end": "2026-11-17T11:00:00+08:00"})["event"]
    assert event["start"] == "2026-11-17T02:00:00Z"
    assert_error("schedule_conflict", lambda: service.create_event({"title": "冲突", "start": event["start"], "end": event["end"]}))
    moved = service.move_event(event["id"], {"version": event["version"], "start": "2026-11-17T03:00:00Z", "end": "2026-11-17T04:00:00Z"})
    assert moved["operation_id"]
    restored = service.undo(moved["operation_id"])["event"]
    assert restored["start"] == event["start"]
    assert_error("already_undone", lambda: service.undo(moved["operation_id"]))


def test_accept_replaces_only_open_history_and_undo(session, service, goal):
    first = accepted(service, goal)
    old_event = session.get(Event, first["event"]["id"])
    history = Event(user_id=service.user_id, title="历史", step_id=goal.steps[0].id,
                    kind="block", status="done", start_utc=(NOW - timedelta(days=1)).replace(tzinfo=None),
                    end_utc=(NOW - timedelta(days=1) + timedelta(minutes=45)).replace(tzinfo=None))
    session.add(history)
    session.flush()
    second = accepted(service, goal)
    assert old_event.status == "skipped"
    assert history.status == "done"
    service.undo(second["operation_id"])
    assert old_event.status == "planned"
    assert history.status == "done"
    assert session.get(Event, second["event"]["id"]).status == "skipped"
    assert session.get(WebProposal, second["proposal"]["id"]).status == "accepted"


def test_undo_rejects_followup_edit(session, service, goal):
    result = accepted(service, goal)
    event = result["event"]
    service.move_event(event["id"], {"version": event["version"], "start": "2026-11-18T01:00:00Z", "end": "2026-11-18T01:45:00Z"})
    assert_error("undo_conflict", lambda: service.undo(result["operation_id"]))
    assert not session.get(WebOperation, result["operation_id"]).undone


def test_undo_cancel_rejects_restoration_conflict(service):
    event = service.create_event({"title": "原日程", "start": "2026-11-17T02:00:00Z", "end": "2026-11-17T03:00:00Z"})["event"]
    cancelled = service.cancel_event(event["id"], {"version": event["version"]})
    service.create_event({"title": "新日程", "start": event["start"], "end": event["end"]})
    assert_error("schedule_conflict", lambda: service.undo(cancelled["operation_id"]))


def test_proposals_supersede_and_accept_once(session, service, goal):
    first = proposal(service, goal)
    second = proposal(service, goal)
    assert session.get(WebProposal, first["id"]).status == "superseded"
    assert_error("version_conflict", lambda: service.accept(first["id"], {"version": first["version"], "index": 0}))
    service.accept(second["id"], {"version": second["version"], "index": 0})
    assert_error("version_conflict", lambda: service.accept(second["id"], {"version": second["version"], "index": 0}))


def test_proposal_revalidates_conflicts_step_and_prefs(session, service, goal):
    p = proposal(service, goal)
    candidate = p["candidates"][0]
    service.create_event({"title": "临时会议", "start": candidate["start"], "end": candidate["end"]})
    assert_error("schedule_conflict", lambda: service.accept(p["id"], {"version": p["version"], "index": 0}))
    p = proposal(service, goal)
    goal.steps[0].title = "改过的步骤"
    session.flush()
    assert_error("stale_proposal", lambda: service.accept(p["id"], {"version": p["version"], "index": 0}))
    p = proposal(service, goal)
    availability(service, buffer_minutes=15)
    assert_error("stale_proposal", lambda: service.accept(p["id"], {"version": p["version"], "index": 0}))


def test_proposal_expiry(service, goal):
    p = proposal(service, goal)
    later = Service(service.session, service.user_id, service.tz, NOW + timedelta(minutes=31))
    assert later.serialize_proposal(service.session.get(WebProposal, p["id"]))["status"] == "expired"
    assert_error("stale_proposal", lambda: later.accept(p["id"], {"version": p["version"], "index": 0}))


def test_availability_defaults_preserve_old_preferences(session, service):
    set_preference(session, service.user_id, "day_start_hour", 10, scope="scheduler")
    set_preference(session, service.user_id, "lunch", [13, 14], scope="scheduler")
    set_preference(session, service.user_id, "unrelated", {"keep": True})
    session.flush()
    config = service.preferences()
    assert config["available_windows"][0]["start"] == "10:00"
    assert config["protected_windows"][0]["start"] == "13:00"
    availability(service, available_windows=[])
    assert get_scheduler_preferences(session, service.user_id).day_start_hour == 10
    assert session.scalar(select(UserPreference).where(UserPreference.key == "unrelated")).value == '{"keep": true}'


def test_empty_available_means_no_slots_new_and_legacy(session, service, goal):
    availability(service, available_windows=[])
    assert service.propose(goal.steps[0].id, {})["candidates"] == []
    prefs = get_scheduler_preferences(session, service.user_id)
    assert scheduler.propose(scheduler.Requirement(), NOW, [], prefs=prefs) == []
    assert_error("outside_availability", lambda: service.start_step(goal.steps[0].id, {"version": version_token(goal.steps[0])}))


def test_weekday_minute_protection_and_adjacent_windows(session, service, goal):
    availability(service, available_windows=[{"days": [1], "start": "09:10", "end": "10:00"}, {"days": [1], "start": "10:00", "end": "12:00"}],
                 protected_windows=[{"days": [1], "start": "10:15", "end": "10:45"}],
                 preferred_windows=[{"days": [1], "start": "11:00", "end": "12:00"}], buffer_minutes=0)
    prefs = get_scheduler_preferences(session, service.user_id)
    candidates = scheduler.propose(scheduler.Requirement(est_min=45), NOW.astimezone(service.zone), [], prefs=prefs, days=1, top_n=20)
    assert candidates
    for candidate in candidates:
        assert candidate.start.weekday() == 1
        assert candidate.start.hour >= 9
        assert not (candidate.start.hour == 9 and candidate.start.minute < 10)
        assert candidate.end <= datetime(2026, 11, 17, 12, tzinfo=service.zone)
        assert not (candidate.start < datetime(2026, 11, 17, 10, 45, tzinfo=service.zone)
                    and candidate.end > datetime(2026, 11, 17, 10, 15, tzinfo=service.zone))
    assert candidates[0].start.hour == 11  # preference changes rank only


def test_new_available_can_extend_beyond_legacy_hours(session, service):
    availability(service, available_windows=[{"days": [1], "start": "23:00", "end": "24:00"}], protected_windows=[])
    prefs = get_scheduler_preferences(session, service.user_id)
    candidates = scheduler.propose(scheduler.Requirement(est_min=30), NOW.astimezone(service.zone), [], prefs=prefs, days=1)
    assert candidates and all(c.start.hour == 23 for c in candidates)


def test_daily_limit_counts_earlier_work(session, service, goal):
    availability(service, max_goal_minutes_per_day=60, buffer_minutes=0)
    session.add(Event(user_id=service.user_id, title="已经完成", kind="block", status="done", step_id=goal.steps[1].id,
                      start_utc=(NOW - timedelta(hours=1)).replace(tzinfo=None), end_utc=NOW.replace(tzinfo=None)))
    session.flush()
    result = service.propose(goal.steps[0].id, {"window_start": "2026-11-17T01:00:00Z", "window_end": "2026-11-17T14:00:00Z"})
    assert result["candidates"] == []
    assert_error("daily_limit", lambda: service.start_step(goal.steps[0].id, {"version": version_token(goal.steps[0])}))


def test_availability_version_and_validation(service):
    config = service.preferences()
    changed = service.save_preferences({**config, "buffer_minutes": 15})
    assert changed["version"] != config["version"]
    assert_error("version_conflict", lambda: service.save_preferences(config))
    assert_error("validation", lambda: service.save_preferences({**changed, "available_windows": [{"days": [7], "start": "09:00", "end": "10:00"}]}))
    assert_error("validation", lambda: service.save_preferences({**changed, "buffer_minutes": True}))


def test_start_reentry_completion_and_dedupe(session, service, goal):
    step = goal.steps[0]
    result = service.start_step(step.id, {"version": version_token(step)})
    execution = result["execution"]
    assert result["event"]["start"] == "2026-11-17T01:00:00Z"
    assert execution["expected_end_at"] == "2026-11-17T01:45:00Z"
    assert service.today()["execution"]["id"] == execution["id"]
    assert_error("execution_active", lambda: service.start_step(goal.steps[1].id, {"version": version_token(goal.steps[1])}))
    assert_error("execution_active", lambda: service.cancel_event(result["event"]["id"], {"version": result["event"]["version"]}))
    later = Service(session, service.user_id, service.tz, NOW + timedelta(minutes=40))
    done = later.checkin_execution(execution["id"], {"version": execution["version"], "outcome": "done"})
    assert done["execution"]["status"] == "completed"
    assert step.status == "done" and step.minutes_spent == 40
    assert session.scalar(select(Checkin)).event_id == result["event"]["id"]
    assert_error("version_conflict", lambda: later.checkin_execution(execution["id"], {"version": execution["version"], "outcome": "done"}))
    assert len(list(session.scalars(select(Checkin)))) == 1


def test_start_scheduled_event_uses_actual_now(session, service, goal):
    event = accepted(service, goal)["event"]
    result = service.start_event(event["id"], {"version": event["version"]})
    assert result["event"]["start"] == "2026-11-17T01:00:00Z"
    assert result["event"]["end"] == "2026-11-17T01:45:00Z"
    assert_error("already_started", lambda: service.start_event(event["id"], {"version": result["event"]["version"]}))


def test_extend_revalidates_conflict_and_adjust(session, service, goal):
    started = service.start_step(goal.steps[0].id, {"version": version_token(goal.steps[0])})
    execution = started["execution"]
    service.create_event({"title": "下一场会议", "start": "2026-11-17T02:00:00Z", "end": "2026-11-17T03:00:00Z"})
    later = Service(session, service.user_id, service.tz, NOW + timedelta(minutes=40))
    assert_error("schedule_conflict", lambda: later.checkin_execution(execution["id"], {"version": execution["version"], "outcome": "extend"}))
    assert session.get(WebExecution, execution["id"]).version == 1
    adjusted = later.checkin_execution(execution["id"], {"version": execution["version"], "outcome": "adjust"})
    assert adjusted["execution"]["status"] == "adjusted"
    assert adjusted["event"]["status"] == "partial"
    assert goal.steps[0].status == "todo"


def test_extend_success_and_window_rejection(session, service, goal):
    started = service.start_step(goal.steps[0].id, {"version": version_token(goal.steps[0])})
    execution = started["execution"]
    later = Service(session, service.user_id, service.tz, NOW + timedelta(minutes=40))
    extended = later.checkin_execution(execution["id"], {"version": execution["version"], "outcome": "extend"})
    assert extended["execution"]["expected_end_at"] == "2026-11-17T02:00:00Z"
    availability(service, available_windows=[{"days": [1], "start": "09:00", "end": "10:00"}])
    assert_error("outside_availability", lambda: later.checkin_execution(execution["id"], {"version": extended["execution"]["version"], "outcome": "extend"}))


def test_overdue_checkin_and_execution_dedupe(session, service, goal):
    event = accepted(service, goal)["event"]
    later = Service(session, service.user_id, service.tz, NOW + timedelta(days=8))
    done = later.checkin_event(event["id"], {"version": event["version"], "outcome": "partial"})
    assert done["event"]["status"] == "partial" and goal.steps[0].status == "todo"
    assert_error("version_conflict", lambda: later.checkin_event(event["id"], {"version": event["version"], "outcome": "done"}))
    assert len(list(session.scalars(select(Checkin)))) == 1


def test_running_checkin_not_in_recovery(session, service, goal):
    started = service.start_step(goal.steps[0].id, {"version": version_token(goal.steps[0])})
    later = Service(session, service.user_id, service.tz, NOW + timedelta(hours=1))
    assert started["event"]["id"] not in [e["id"] for e in later.today()["recovery"]]
    assert_error("execution_active", lambda: later.checkin_event(started["event"]["id"], {"version": started["event"]["version"], "outcome": "done"}))


def test_scheduler_does_not_round_into_past():
    now = NOW.replace(second=1)
    result = scheduler.propose(scheduler.Requirement(), now, [], days=1)
    assert result and all(candidate.start >= now for candidate in result)


def test_refill_continues_remaining_outline_once(session, service, goal):
    goal.outline = json.dumps(["入门", "进阶"])
    for step in goal.steps:
        step.status = "done"
    session.flush()
    version = service.goal(goal.id)["version"]
    result = service.refill(goal.id, {"version": version})
    assert result["goal"]["outline"] == ["入门", "进阶"]
    assert any(s["milestone"] == "进阶" and s["status"] == "todo" for s in result["goal"]["steps"])
    assert_error("invalid_state", lambda: service.refill(goal.id, {"version": service.goal(goal.id)["version"]}))


def test_early_completion_releases_unused_time(session, service, goal):
    availability(service, buffer_minutes=0)
    started = service.start_step(goal.steps[0].id, {"version": version_token(goal.steps[0])})
    later = Service(session, service.user_id, service.tz, NOW + timedelta(minutes=10))
    done = later.checkin_execution(started["execution"]["id"], {"version":"1", "outcome":"done"})
    assert done["event"]["end"] == "2026-11-17T01:10:00Z"
    assert done["execution"]["expected_end_at"] == "2026-11-17T01:45:00Z"
    assert later.start_step(goal.steps[1].id, {"version":version_token(goal.steps[1])})["execution"]["status"] == "running"


def test_legacy_cannot_mutate_running_execution(session, service, goal):
    from goalcal.domain import calendar_service
    started = service.start_step(goal.steps[0].id, {"version": version_token(goal.steps[0])})
    event = session.get(Event, started["event"]["id"])
    with pytest.raises(ValueError, match="正在执行"):
        goals.complete_step(session, goal.steps[0], now=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="正在执行"):
        goals.pause_goal(session, goal, NOW.replace(tzinfo=None))
    with pytest.raises(ValueError, match="正在执行"):
        calendar_service.delete_event(session, event)


def test_web_proposal_metrics_and_recovery_undo(session, service, goal):
    from goalcal import metrics
    old = Event(user_id=service.user_id,step_id=goal.steps[0].id,title="未完成",
                start_utc=(NOW-timedelta(hours=2)).replace(tzinfo=None),
                end_utc=(NOW-timedelta(hours=1)).replace(tzinfo=None),
                resolved_at=NOW.replace(tzinfo=None),kind="block",status="partial")
    session.add(old); session.flush()
    result = accepted(service, goal)
    assert metrics.acceptance_rate(session, service.user_id, NOW) == 1
    assert metrics.recovery_rate_72h(session, service.user_id, NOW) == 1
    service.undo(result["operation_id"])
    assert metrics.recovery_rate_72h(session, service.user_id, NOW) == 0
