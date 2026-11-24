"""Habit dates, reservations and history use the same service across views."""
from datetime import datetime, timedelta, timezone

import pytest

from goalcal.domain.goals import create_goal
from goalcal.web.core import ApiError, Service

NOW = datetime(2026, 11, 17, 1, tzinfo=timezone.utc)  # Tuesday 09:00 in Shanghai
TUESDAY = "2026-11-17"
BASE = {"title": "Read", "est_min": 30, "mode": "fixed", "weekdays": list(range(7)), "start_date": TUESDAY}


@pytest.fixture
def service(session, user):
    svc = Service(session, user.id, user.tz, NOW)
    svc.save_preferences({**svc.preferences(), "available_windows": [
        {"days": list(range(7)), "start": "00:00", "end": "24:00"}],
        "protected_windows": [], "buffer_minutes": 0})
    return svc


def act(svc, item, action, **changes):
    return svc.habits.act(item["habit_id"], {"date": item["date"], "version": item["version"],
                                            "action": action, **changes})


def test_fixed_occurrence_shared_between_views_and_expires_without_carryover(service):
    habit = service.habits.create(BASE)["habit"]
    today = service.today()
    item, = today["habits"]["items"]
    assert today["habit_next"]["id"] == item["id"]
    assert not today["events"] and not item["start"]
    scheduled = act(service, item, "schedule", start="2026-11-17T02:00:00Z")["occurrence"]
    assert scheduled["id"] == item["id"]
    assert service.today()["events"][0]["habit"] == scheduled
    completed = act(service, scheduled, "done")
    assert service.today()["events"][0]["status"] == "done"
    event = service.create_event({"title": "Freed time", "start": scheduled["start"], "end": scheduled["end"]})
    with pytest.raises(ApiError, match="冲突"):
        service.undo(completed["operation_id"])
    service.undo(event["operation_id"])
    service.undo(completed["operation_id"])
    assert service.today()["events"][0]["status"] == "planned"

    tomorrow = Service(service.session, service.user_id, service.tz, NOW + timedelta(days=1))
    assert [i["date"] for i in tomorrow.today()["habits"]["items"]] == ["2026-11-18"]
    assert not tomorrow.today()["recovery"]
    history = tomorrow.habits.detail(habit["id"], {})["history"]
    assert [(i["date"], i["status"]) for i in history] == [("2026-11-18", "pending"), (TUESDAY, "missed")]
    assert act(tomorrow, history[-1], "done")["occurrence"]["status"] == "done"
    old_history = tomorrow.habits.detail(habit["id"], {"through": "2026-10-21"})
    assert not old_history["history"]
    assert [(item["date"], item["status"]) for item in old_history["week_items"]] == [
        (TUESDAY, "done"), *((f"2026-11-{day}", "pending") for day in range(18, 23)),
    ]


def test_timed_defaults_respect_existing_slots_and_goal_budget(service):
    service.save_preferences({**service.preferences(), "max_goal_minutes_per_day": 30})
    service.create_event({"title": "Meeting", "start": "2026-11-19T11:00:00Z", "end": "2026-11-19T12:00:00Z"})
    body = {**BASE, "weekdays": [1, 3], "start_time": "19:00"}
    preview = service.habits.preview(body)
    assert preview["slots"][1]["reason"] == "schedule_conflict"
    habit = service.habits.create(body)["habit"]
    assert habit["next"]["start"] == "2026-11-17T11:00:00Z"
    thursday = service.habits.detail(habit["id"], {"date": "2026-11-19"})["occurrence"]
    assert thursday["reason"] == "schedule_conflict"
    act(service, thursday, "schedule", start="2026-11-19T12:00:00Z")
    assert service.habits.detail(habit["id"], {"date": "2026-11-26"})["occurrence"]["start"].endswith("11:00:00Z")
    goal = create_goal(service.session, service.user_id, "Run", step_defs=[{"title": "Train", "est_min": 30}])
    step = service.serialize_step(goal.steps[0])
    overlapped = service.schedule_step(step["id"], {"version": step["version"], "start": "2026-11-17T11:00:00Z"})
    assert "重合" in overlapped["message"]
    assert service.schedule_step(step["id"], {"version": step["version"], "start": "2026-11-17T12:00:00Z"})


def test_flexible_weeks_move_record_and_preserve_target_until_monday(service):
    body = {**BASE, "mode": "weekly", "weekly_target": 3}
    habit = service.habits.create(body)["habit"]
    scheduled = service.habits.act(habit["id"], {"date": "2026-11-20", "habit_version": habit["version"],
                                                 "action": "schedule"})["occurrence"]
    offer = service.today()["habits"]["weeks"][0]["offer"]
    detail = service.habits.detail(habit["id"], {})
    assert detail["week_items"] == [scheduled]
    assert detail["progress"]["offer"] == offer
    act(service, offer, "done")
    with pytest.raises(ApiError) as error:
        act(service, scheduled, "schedule", target_date="2026-11-23")
    assert error.value.code == "invalid_time"
    with pytest.raises(ApiError) as error:
        act(service, scheduled, "schedule", target_date=TUESDAY)
    assert error.value.code == "duplicate_day"
    moved = act(service, scheduled, "schedule", target_date="2026-11-21")["occurrence"]
    assert moved["id"] == scheduled["id"]
    assert moved["date"] == "2026-11-21"
    wednesday = Service(service.session, service.user_id, service.tz, NOW + timedelta(days=1))
    updated_body = {**body, "weekly_target": 2}
    preview = wednesday.habits.preview({**updated_body, "habit_id": habit["id"]})
    assert preview["effective_date"] == "2026-11-23"
    updated = wednesday.habits.update(habit["id"], {
        **updated_body, "version": habit["version"], "effective_date": preview["effective_date"],
    })["habit"]
    assert (updated["progress"]["done"], updated["progress"]["target"]) == (1, 3)
    assert wednesday.habits.detail(habit["id"], {})["scheduled"][0]["id"] == moved["id"]
    following = Service(service.session, service.user_id, service.tz, NOW + timedelta(days=6))
    assert following.today()["habits"]["weeks"][0]["target"] == 2
    assert following.today()["habits"]["weeks"][0]["done"] == 0


def test_paused_periods_are_excluded_and_resume_releases_future_slots(service):
    habit = service.habits.create({**BASE, "start_time": "12:00"})["habit"]
    completed = act(service, habit["next"], "done")["occurrence"]
    paused = service.habits.status(habit["id"], {"version": habit["version"], "status": "paused"})["habit"]
    assert not paused["next"]
    assert service.today()["habits"]["items"][0]["id"] == completed["id"]
    friday = Service(service.session, service.user_id, service.tz, NOW + timedelta(days=3, hours=5))
    assert friday.habits.detail(habit["id"], {"date": "2026-11-18"})["occurrence"] is None
    resumed = friday.habits.status(habit["id"], {"version": paused["version"], "status": "active"})["habit"]
    assert resumed["next"]["date"] == "2026-11-20"
    assert resumed["next"]["reason"] == "time_passed"
    assert [(item["date"], item["status"]) for item in friday.habits.detail(habit["id"], {})["history"]] == [
        ("2026-11-20", "pending"), (TUESDAY, "done"),
    ]


def test_habit_slots_and_next_unscheduled_advance(service):
    habit = service.habits.create(BASE)["habit"]
    assert habit["next_unscheduled"]["date"] == TUESDAY
    slots = service.habits.slots(habit["id"], {"start_date": TUESDAY, "end_date": TUESDAY, "period": "afternoon"})
    assert slots["candidates"]
    assert all(c["date"] == TUESDAY for c in slots["candidates"])
    first = slots["candidates"][0]
    act(service, habit["next_unscheduled"], "schedule", start=first["start"], target_date=first["date"])
    refreshed = service.habits.list()["habits"][0]
    assert refreshed["next_unscheduled"]["date"] == "2026-11-18"

    weekly = service.habits.create({**BASE, "title": "Run", "mode": "weekly", "weekly_target": 1})["habit"]
    assert weekly["next_unscheduled"] is None
    assert weekly["next_week"]["week"] == "2026-11-16"
    week_slots = service.habits.slots(weekly["id"], {"start_date": TUESDAY, "end_date": "2026-11-22", "period": "all"})
    assert week_slots["candidates"]
    pick = week_slots["candidates"][0]
    service.habits.act(weekly["id"], {
        "date": pick["date"], "target_date": pick["date"], "start": pick["start"],
        "habit_version": weekly["version"], "action": "schedule",
    })
    updated_weekly = next(h for h in service.habits.list()["habits"] if h["id"] == weekly["id"])
    assert updated_weekly["next_week"]["week"] == "2026-11-23"


def test_habit_start_and_execution_checkin(service):
    habit = service.habits.create(BASE)["habit"]
    started = act(service, habit["next"], "start")
    execution = started["execution"]
    assert execution["status"] == "running"
    assert execution["step_id"] is None
    assert execution["habit_id"] == habit["id"]
    assert execution["step_title"] == BASE["title"]
    assert service.today()["execution"]["id"] == execution["id"]

    # Cannot complete/skip/pause while execution is running
    with pytest.raises(ApiError) as exc:
        act(service, started["occurrence"], "done")
    assert exc.value.code == "execution_active"

    # Extend by 15 minutes
    extended = service.checkin_execution(execution["id"], {"version": execution["version"], "outcome": "extend"})["execution"]
    assert extended["expected_end_at"] > execution["expected_end_at"]

    # Pause/adjust releases occurrence back to pending unscheduled
    adjusted = service.checkin_execution(extended["id"], {"version": extended["version"], "outcome": "adjust"})["execution"]
    assert adjusted["status"] == "adjusted"
    assert service.today()["execution"] is None
    occ_after = service.habits.detail(habit["id"], {"date": TUESDAY})["occurrence"]
    assert occ_after["status"] == "pending"
    assert occ_after["start"] is None

    # Restart and complete via execution check-in
    restarted = act(service, occ_after, "start")["execution"]
    completed = service.checkin_execution(restarted["id"], {"version": restarted["version"], "outcome": "done"})["execution"]
    assert completed["status"] == "completed"
    occ_done = service.habits.detail(habit["id"], {"date": TUESDAY})["occurrence"]
    assert occ_done["status"] == "done"

    # Deleting requires archiving first, then cleanly removes habit & records
    with pytest.raises(ApiError) as exc:
        service.habits.delete(habit["id"], {"version": habit["version"]})
    assert exc.value.code == "invalid_state"
    arch_res = service.habits.status(habit["id"], {"version": habit["version"], "status": "archived"})
    assert "设置 → 已归档" in arch_res["message"] or "Settings → Archived" in arch_res["message"]
    archived = arch_res["habit"]
    deleted = service.habits.delete(habit["id"], {"version": archived["version"]})
    assert "删除" in deleted["message"] or "Deleted" in deleted["message"]
    assert service.habits.list()["habits"] == []


def test_habit_pin_direct_update_and_goal_deadline_edit(service):
    h1 = service.habits.create({**BASE, "title": "First"})["habit"]
    h2 = service.habits.create({**BASE, "title": "Second"})["habit"]
    assert [h["id"] for h in service.habits.list()["habits"]] == [h1["id"], h2["id"]]

    # Pin second habit -> floats to top
    pinned = service.habits.status(h2["id"], {"version": h2["version"], "pinned": True})["habit"]
    assert pinned["pinned"] is True
    assert [h["id"] for h in service.habits.list()["habits"]] == [h2["id"], h1["id"]]

    # Direct rule update without effective_date succeeds immediately
    updated = service.habits.update(h2["id"], {
        **BASE, "title": "Second Updated", "est_min": 45, "version": pinned["version"],
    })["habit"]
    assert updated["upcoming_rule"]["rule"]["est_min"] == 45

    # Goal deadline edit & clear
    from goalcal.models import Goal
    goal_row = Goal(user_id=service.user_id, title="Learn Rust", status="active")
    service.session.add(goal_row)
    service.session.flush()
    goal = service.goal(goal_row.id)
    with_dl = service.update_goal(goal["id"], {"version": goal["version"], "deadline": "2026-12-20"})["goal"]
    assert with_dl["deadline"] == "2026-12-20"
    cleared = service.update_goal(goal["id"], {"version": with_dl["version"], "deadline": ""})["goal"]
    assert cleared["deadline"] is None


def test_conversational_goal_replan_flow(service):
    from types import SimpleNamespace
    from goalcal.models import Goal, Step
    from goalcal.web import assistant as asst

    goal_row = Goal(user_id=service.user_id, title="学会做播客", status="active")
    service.session.add(goal_row)
    service.session.flush()
    service.session.add_all([
        Step(goal_id=goal_row.id, seq=0, title="定主题", est_min=30, milestone="准备", status="done"),
        Step(goal_id=goal_row.id, seq=1, title="买设备", est_min=45, milestone="准备", status="todo"),
        Step(goal_id=goal_row.id, seq=2, title="录第一期", est_min=60, milestone="录制", status="todo"),
    ])
    service.session.flush()
    principal = SimpleNamespace(user_id=service.user_id, tz=service.tz)

    # Turn 1: bare replan request asks user what to adjust
    body1 = {"text": "帮我重新规划「学会做播客」", "version": "0", "page_goal_id": goal_row.id}
    dec1 = asst.prepare_turn(service.session, principal, body1, service.now, lang="zh")
    assert dec1["_replan_ask"]["goal_id"] == goal_row.id
    res1 = asst.turn(service, {**body1, "_decision": dec1})
    assert "你想怎么调整" in res1["messages"][-1]["content"]
    assert "replan_goal" not in res1

    # Turn 2: user gives concrete adjustment -> drafts replan_goal preview without mutating DB
    body2 = {"text": "把剩下步骤改轻松一点，每步不超过 20 分钟", "version": res1["version"], "page_goal_id": goal_row.id}
    dec2 = asst.prepare_turn(service.session, principal, body2, service.now, lang="zh")
    assert "_replan" in dec2
    res2 = asst.turn(service, {**body2, "_decision": dec2})
    replan = res2["replan_goal"]
    assert replan["goal_id"] == goal_row.id
    assert replan["steps_changed"] is True
    assert len(replan["steps"]) >= 1
    assert all(s["est_min"] <= 20 for s in replan["steps"])
    # Still unmutated before confirmation
    before = service.goal(goal_row.id)
    assert [s["title"] for s in before["steps"] if s["status"] == "todo"] == ["买设备", "录第一期"]

    # Confirm & apply replan -> done step kept, todo steps replaced, card cleared
    applied = service.replan_goal(goal_row.id, {
        "version": replan["goal_version"],
        "steps": [{"title": "试录 5 分钟样片", "est_min": 20, "milestone": "实操", "energy": 2}],
        "outline": ["准备", "实操"],
        "deadline": "2026-12-30",
    })["goal"]
    assert applied["deadline"] == "2026-12-30"
    assert [s["title"] for s in applied["steps"] if s["status"] == "done"] == ["定主题"]
    assert [s["title"] for s in applied["steps"] if s["status"] == "todo"] == ["试录 5 分钟样片"]
    assert "replan_goal" not in asst.read_state(service)


def test_replan_deadline_multiturn_and_chat_confirm(service):
    from datetime import date
    from types import SimpleNamespace
    from goalcal.models import Goal, Step
    from goalcal.web import assistant as asst

    goal_row = Goal(user_id=service.user_id, title="自由泳 200 米连续游", status="active", deadline=date(2027, 2, 21))
    service.session.add(goal_row)
    service.session.flush()
    service.session.add_all([
        Step(goal_id=goal_row.id, seq=0, title="浮板打腿 20 分钟", est_min=20, milestone="基础", status="todo"),
        Step(goal_id=goal_row.id, seq=1, title="侧身换气练习", est_min=25, milestone="换气", status="todo"),
        Step(goal_id=goal_row.id, seq=2, title="连续游 100 米测试", est_min=20, milestone="耐力", status="todo"),
    ])
    service.session.flush()
    principal = SimpleNamespace(user_id=service.user_id, tz=service.tz)

    # Turn 1: kickoff
    b1 = {"text": "帮我重新规划「自由泳 200 米连续游」", "version": "0", "page_goal_id": goal_row.id}
    d1 = asst.prepare_turn(service.session, principal, b1, service.now, lang="zh")
    r1 = asst.turn(service, {**b1, "_decision": d1})
    assert "2027-02-21" in r1["messages"][-1]["content"]

    # Turn 2: underspecified "修改截止时间" MUST stay in replan and ask for target date
    b2 = {"text": "修改截止时间", "version": r1["version"], "page_goal_id": goal_row.id}
    d2 = asst.prepare_turn(service.session, principal, b2, service.now, lang="zh")
    assert "_replan_ask" in d2
    r2 = asst.turn(service, {**b2, "_decision": d2})
    assert "2027-02-21" in r2["messages"][-1]["content"]
    assert "哪一天" in r2["messages"][-1]["content"]

    # Turn 3: user gives relative shift "提前一个月" -> proposes deadline-only change (2027-01-21)
    b3 = {"text": "提前一个月", "version": r2["version"], "page_goal_id": goal_row.id}
    d3 = asst.prepare_turn(service.session, principal, b3, service.now, lang="zh")
    assert "_replan" in d3
    r3 = asst.turn(service, {**b3, "_decision": d3})
    assert r3["replan_goal"]["deadline"] == "2027-01-21"
    assert r3["replan_goal"]["previous_deadline"] == "2027-02-21"
    assert r3["replan_goal"]["steps_changed"] is False

    # Turn 4: user confirms in chat ("确认修改") -> applies deadline while keeping existing todo steps intact
    b4 = {"text": "确认修改", "version": r3["version"], "page_goal_id": goal_row.id}
    d4 = asst.prepare_turn(service.session, principal, b4, service.now, lang="zh")
    assert "_replan_apply" in d4
    r4 = asst.turn(service, {**b4, "_decision": d4})
    assert "2027-01-21" in r4["messages"][-1]["content"]
    assert "replan_goal" not in r4
    updated = service.goal(goal_row.id)
    assert updated["deadline"] == "2027-01-21"
    assert [s["title"] for s in updated["steps"] if s["status"] == "todo"] == [
        "浮板打腿 20 分钟", "侧身换气练习", "连续游 100 米测试",
    ]

    # Turn 5: user clears deadline directly -> deadline="" -> clears goal.deadline
    b5 = {"text": "取消「自由泳 200 米连续游」的截止时间", "version": r4["version"], "page_goal_id": goal_row.id}
    d5 = asst.prepare_turn(service.session, principal, b5, service.now, lang="zh")
    assert "_replan" in d5 and d5["_replan"]["deadline"] == ""
    r5 = asst.turn(service, {**b5, "_decision": d5})
    b6 = {"text": "确认", "version": r5["version"], "page_goal_id": goal_row.id}
    d6 = asst.prepare_turn(service.session, principal, b6, service.now, lang="zh")
    r6 = asst.turn(service, {**b6, "_decision": d6})
    assert service.goal(goal_row.id)["deadline"] is None
    assert "取消" in r6["messages"][-1]["content"]


def test_assistant_query_pause_disambiguate_and_habit_title(service):
    from datetime import date, datetime
    from types import SimpleNamespace
    from goalcal.models import Event, Goal, Step
    from goalcal.web import assistant as asst

    g1 = Goal(user_id=service.user_id, title="自由泳 200 米连续游", status="active", deadline=date(2027, 2, 21))
    g2 = Goal(user_id=service.user_id, title="完成个人作品集网站", status="active", deadline=date(2027, 1, 10))
    service.session.add_all([g1, g2])
    service.session.flush()
    service.session.add_all([
        Step(goal_id=g1.id, seq=0, title="浮板打腿 20 分钟", est_min=20, milestone="基础", status="todo"),
        Step(goal_id=g2.id, seq=0, title="整理三个代表项目", est_min=45, milestone="内容", status="todo"),
    ])
    from zoneinfo import ZoneInfo
    local_day = service.now.astimezone(ZoneInfo(service.tz)).date()
    ev_start = datetime(local_day.year, local_day.month, local_day.day, 14, 0, tzinfo=ZoneInfo(service.tz)).astimezone(service.now.tzinfo).replace(tzinfo=None)
    ev_end = datetime(local_day.year, local_day.month, local_day.day, 15, 0, tzinfo=ZoneInfo(service.tz)).astimezone(service.now.tzinfo).replace(tzinfo=None)
    service.session.add(Event(
        user_id=service.user_id, title="产品同步会", kind="fixed", status="planned",
        start_utc=ev_start,
        end_utc=ev_end,
    ))
    service.session.flush()
    principal = SimpleNamespace(user_id=service.user_id, tz=service.tz)

    # Query today vs tomorrow must not be hijacked by scheduling and must format local HH:MM
    b1 = {"text": "今天有什么安排？", "version": "0"}
    r1 = asst.turn(service, {**b1, "_decision": asst.prepare_turn(service.session, principal, b1, service.now, lang="zh")})
    assert "今天" in r1["messages"][-1]["content"]
    assert "14:00–15:00" in r1["messages"][-1]["content"]
    assert "产品同步会" in r1["messages"][-1]["content"]

    b2 = {"text": "明天有什么安排？", "version": r1["version"]}
    r2 = asst.turn(service, {**b2, "_decision": asst.prepare_turn(service.session, principal, b2, service.now, lang="zh")})
    assert "明天目前没有安排" in r2["messages"][-1]["content"]

    # Ambiguous replan -> picking "第一个" enters replan (not scheduling!)
    b3 = {"text": "帮我重新规划一下目标", "version": r2["version"]}
    r3 = asst.turn(service, {**b3, "_decision": asst.prepare_turn(service.session, principal, b3, service.now, lang="zh")})
    assert "自由泳 200 米连续游" in r3["messages"][-1]["content"]
    b4 = {"text": "第一个", "version": r3["version"]}
    r4 = asst.turn(service, {**b4, "_decision": asst.prepare_turn(service.session, principal, b4, service.now, lang="zh")})
    assert "你想怎么调整" in r4["messages"][-1]["content"]
    assert r4["proposal"] is None

    # One-shot deadline change with "截止日期改到..." word order
    b5 = {"text": "把「自由泳 200 米连续游」的截止日期改到 2026-12-31", "version": r4["version"]}
    r5 = asst.turn(service, {**b5, "_decision": asst.prepare_turn(service.session, principal, b5, service.now, lang="zh")})
    assert r5["replan_goal"]["deadline"] == "2026-12-31"
    assert r5["replan_goal"]["steps_changed"] is False

    # Pause & resume goal with "完成" in its title must not misroute to log_progress
    b6 = {"text": "帮我暂停「完成个人作品集网站」", "version": r5["version"]}
    r6 = asst.turn(service, {**b6, "_decision": asst.prepare_turn(service.session, principal, b6, service.now, lang="zh")})
    assert service.goal(g2.id)["status"] == "paused"
    b7 = {"text": "帮我恢复「完成个人作品集网站」", "version": r6["version"]}
    r7 = asst.turn(service, {**b7, "_decision": asst.prepare_turn(service.session, principal, b7, service.now, lang="zh")})
    assert service.goal(g2.id)["status"] == "active"

    # Habit draft strips trailing "，每次" cleanly
    b8 = {"text": "我想每周跑三次步，每次 30 分钟", "version": r7["version"]}
    r8 = asst.turn(service, {**b8, "_decision": asst.prepare_turn(service.session, principal, b8, service.now, lang="zh")})
    assert r8["create_habit"] == {"title": "跑步", "weekly_target": 3, "est_min": 30}

    # Create goal captures month-only Chinese deadline ("明年 3 月前")
    b9 = {"text": "我想在明年 3 月前考过雅思 7.0", "version": r8["version"]}
    r9 = asst.turn(service, {**b9, "_decision": asst.prepare_turn(service.session, principal, b9, service.now, lang="zh")})
    assert r9["create_goal"]["deadline"] == "2027-03-31"
