from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from goalcal import agent
from goalcal.domain import calendar_service as cal
from goalcal.domain import goals as goal_svc
from goalcal.domain import planner
from goalcal.llm.action import Action
from goalcal.models import Checkin, Event, StateLog, Step
from goalcal.timeutil import as_local, as_utc_naive

TZ = "Asia/Shanghai"
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=ZoneInfo(TZ))  # Wednesday


def _event(session, user, title, start, minutes=60, kind="external"):
    return cal.create_event(
        session, user.id, title, start, start + timedelta(minutes=minutes),
        kind=kind, tz=TZ,
    )


def test_add_event_stores_utc_and_clean_title(session, user):
    reply = agent.handle(session, user.id, "Add a meeting tomorrow 10am", NOW, TZ)
    assert reply.parser == "rules:v1"
    ev = session.scalar(select(Event))
    assert ev.title == "meeting"
    assert ev.start_utc == datetime(2026, 10, 22, 2, 0)  # 10:00 +08
    assert ev.end_utc == datetime(2026, 10, 22, 3, 0)
    assert "Added" in reply.text


def test_add_event_rejects_past_time(session, user):
    action = Action(intent="add_event", title="Run", when_text="today 8am",
                    raw="Add run today 8am", start=NOW - timedelta(hours=1))
    reply = agent.execute(session, user.id, action, "l1:test", NOW, TZ)
    assert "already passed" in reply.text
    assert session.scalar(select(Event)) is None


def test_stale_calendar_preview_does_not_write_past_event(session, user):
    draft = agent.EventDraft(events=[Action(
        intent="add_event", title="Run", start=NOW - timedelta(minutes=30),
        duration_min=45, raw="earlier proposal",
    )])
    reply = agent.confirm_event_draft(session, user.id, draft, True, NOW, TZ)
    assert "have passed" in reply.text
    assert session.scalar(select(Event)) is None


def test_add_event_without_date_uses_today_or_tomorrow(session, user):
    reply = agent.handle(session, user.id, "add standup every morning at 10", NOW, TZ)
    assert reply.action.intent == "add_event"
    ev = session.scalar(select(Event))
    local = as_local(ev.start_utc, TZ)
    assert local.date() == NOW.date()  # 09:00 的输入里 10:00 还没过
    assert (local.hour, local.minute) == (10, 0)


def test_move_event_keeps_original_time_of_day(session, user):
    _event(session, user, "Workout", NOW + timedelta(days=1, hours=10), 60)  # 明天 19:00
    reply = agent.handle(session, user.id, "Move tomorrow's workout to Friday", NOW, TZ)
    ev = session.scalar(select(Event))
    local = as_local(ev.start_utc, TZ)
    assert local.weekday() == 4  # Friday
    assert (local.hour, local.minute) == (19, 0)
    assert "Moved" in reply.text


def test_move_event_keeps_duration(session, user):
    _event(session, user, "Workout", NOW + timedelta(days=1, hours=10), 90)
    agent.handle(session, user.id, "Move tomorrow's workout to Friday 8am", NOW, TZ)
    ev = session.scalar(select(Event))
    assert int((ev.end_utc - ev.start_utc).total_seconds() // 60) == 90


def test_delete_event(session, user):
    _event(session, user, "Dentist appointment", NOW + timedelta(days=2))
    reply = agent.handle(session, user.id, "Cancel the dentist appointment", NOW, TZ)
    assert session.scalar(select(Event)) is None
    assert "Deleted" in reply.text


def test_delete_missing_event_is_human_readable(session, user):
    reply = agent.handle(session, user.id, "Cancel the dentist appointment", NOW, TZ)
    assert "couldn't find" in reply.text


def test_query_uses_injected_now_not_wall_clock(session, user):
    _event(session, user, "Team sync", datetime(2026, 10, 22, 10, 0, tzinfo=ZoneInfo(TZ)))
    # 传进来的 now 是 2027；如果 _query 退化成用墙钟（2026），就会列出这条事件
    future_now = datetime(2027, 1, 1, 9, 0, tzinfo=ZoneInfo(TZ))
    reply = agent.handle(session, user.id, "What do I have tomorrow?", future_now, TZ)
    assert "Team sync" not in reply.text


def test_query_groups_upcoming_events_by_day(session, user):
    _event(session, user, "Past sync", NOW - timedelta(hours=1))
    _event(session, user, "Morning sync", NOW + timedelta(hours=1))
    _event(session, user, "Design review", NOW + timedelta(hours=4))
    _event(session, user, "Team sync", NOW + timedelta(days=1))

    reply = agent.handle(session, user.id, "What's on this week?", NOW, TZ)

    assert "Past sync" not in reply.text
    assert "**Today · Wed Oct 21**" in reply.text
    assert "**Tomorrow · Thu Oct 22**" in reply.text
    assert "- 10:00 — Morning sync" in reply.text
    assert "- 13:00 — Design review" in reply.text
    assert "- 09:00 — Team sync" in reply.text


def test_create_goal_returns_steps_and_candidates(session, user):
    reply = agent.handle(
        session, user.id, "I want to publish my first short video in 30 days", NOW, TZ
    )
    reply = agent.confirm_goal_draft(session, user.id, reply.goal_draft, True, NOW, TZ)
    assert reply.action.intent == "create_goal"
    assert reply.goal_id is not None and reply.step_id is not None
    goal = goal_svc.active_goals(session, user.id)[0]
    assert len(goal.steps) == 2  # 滚动拆解：只落首批步骤
    assert goal_svc.outline_phases(goal)
    assert 1 <= len(reply.candidates) <= 6
    assert all(c.reasons for c in reply.candidates)


def test_piano_goal_waits_for_timeline_before_generating_plan(session, user, monkeypatch):
    """期限未定时只保留 brief；确认默认期限后才生成计划草稿。"""
    original = agent.generate_outline_plan
    calls = []

    def track(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(agent, "generate_outline_plan", track)
    reply = agent.handle(session, user.id, "我要学会弹钢琴", NOW, TZ)

    assert reply.action.intent == "create_goal"
    assert reply.goal_brief is not None
    assert reply.goal_draft is None
    assert "choose a timeline" in reply.text
    assert calls == []
    reply = agent.continue_goal(session, user.id, reply.goal_brief, "use 90 days", NOW, TZ)
    assert reply.goal_draft is not None
    assert calls == [1]
    assert reply.goal_draft.deadline == NOW.date() + timedelta(days=90)
    assert goal_svc.active_goals(session, user.id) == []


def test_piano_goal_deadline_can_be_added_after_preview(session, user):
    first = agent.handle(session, user.id, "我要学会弹钢琴", NOW, TZ)
    draft = agent.continue_goal(session, user.id, first.goal_brief, "6个月", NOW, TZ).goal_draft
    reply = agent.confirm_goal_draft(session, user.id, draft, True, NOW, TZ)

    assert reply.goal_brief is None
    assert "Goal set" in reply.text
    goal = goal_svc.active_goals(session, user.id)[0]
    assert goal.template_key == "piano"
    assert len(goal.steps) == 2
    assert goal_svc.outline_phases(goal) == ["Setup & baseline", "Posture & scales", "Reading & rhythm", "First piece", "Consolidation"]
    assert goal.deadline == NOW.date() + timedelta(days=180)
    assert 1 <= len(reply.candidates) <= 6


def test_revise_goal_draft_bare_deadline_is_deterministic(session, user, monkeypatch):
    """裸期限修改（"3个月" / "到6月1日"）只换期限，use_llm=False 也不触发 L2。"""
    def no_llm(*args, **kwargs):
        raise AssertionError("裸期限修改不得触发 L2")

    first = agent.handle(session, user.id, "我要学会弹钢琴", NOW, TZ, use_llm=False)
    draft = agent.continue_goal(session, user.id, first.goal_brief, "use 90 days", NOW, TZ,
                                use_llm=False).goal_draft
    monkeypatch.setattr(agent, "generate_plan", no_llm)

    revised = agent.revise_goal_draft(draft, "3个月", NOW, TZ, use_llm=False)
    assert revised is not None
    assert revised.deadline == NOW.date() + timedelta(days=90)
    assert revised.draft_id == draft.draft_id

    revised2 = agent.revise_goal_draft(draft, "到6月1日", NOW, TZ, use_llm=False)
    assert revised2.deadline == date(2027, 6, 1)  # 已过的月日滚到明年


def test_revise_goal_draft_rebuilds_plan_for_new_timeline(session, user, monkeypatch):
    first = agent.handle(session, user.id, "我要学会弹钢琴", NOW, TZ, use_llm=False)
    draft = agent.continue_goal(session, user.id, first.goal_brief, "use 90 days", NOW, TZ,
                                use_llm=False).goal_draft
    seen = []

    def fake_outline(title, deadline, **kwargs):
        seen.append(deadline)
        return ["One-month piano plan"], [{"seq": 0, "title": "First month action",
                                            "est_min": 20, "energy": 1, "kind": "mental"}]

    monkeypatch.setattr(agent, "generate_outline_plan", fake_outline)
    revised = agent.revise_goal_draft(draft, "deadline is one month", NOW, TZ)

    assert seen == [NOW.date() + timedelta(days=30)]
    assert revised.deadline == seen[0]
    assert revised.step_defs[0]["title"] == "First month action"
    assert revised.draft_id == draft.draft_id


def test_revise_goal_draft_mixed_instruction_not_silently_dropped(session, user, monkeypatch):
    """混合指令（期限+结构调整）不得静默只改期限：无 LLM 时诚实返回 None，有 LLM 时走 L2。"""
    mixed = ["拆成两段，3个月", "把第一步改成3个月", "6个月，每周三次"]

    first = agent.handle(session, user.id, "我要学会弹钢琴", NOW, TZ, use_llm=False)
    draft = agent.continue_goal(session, user.id, first.goal_brief, "use 90 days", NOW, TZ,
                                use_llm=False).goal_draft

    for instruction in mixed:
        assert agent.revise_goal_draft(draft, instruction, NOW, TZ, use_llm=False) is None

    captured = {}

    def fake_generate(*args, **kwargs):
        captured.update(kwargs)
        return [
            {
                "seq": i + 1,
                "milestone": f"阶段{i // 4 + 1}",
                "title": f"步骤{i + 1}",
                "outcome": "",
                "est_min": 30,
                "energy": 1,
                "kind": "deep",
            }
            for i in range(8)  # piano 是 complex，校验要求 ≥7 步
        ]

    monkeypatch.setattr(agent, "generate_plan", fake_generate)

    revised = agent.revise_goal_draft(draft, "拆成两段，3个月", NOW, TZ)

    assert revised is not None and revised.source == "llm"
    assert captured["instruction"] == "拆成两段，3个月"
    assert captured["current_steps"]


def test_llm_generated_plan_is_stored_for_custom_goal(session, user, monkeypatch):
    from goalcal.domain import planner

    brief = planner.GoalBrief(
        title="复习IBA6104",
        raw="复习IBA6104",
        deadline=NOW.date() + timedelta(days=7),
        complexity="simple",
    )
    outline = ["范围", "练习", "模拟"]
    plan = [
        {
            "seq": 1,
            "milestone": "范围",
            "title": "整理 IBA6104 考试范围和往年题",
            "outcome": "资料清单",
            "est_min": 25,
            "energy": 0,
            "kind": "admin",
        },
        {
            "seq": 2,
            "milestone": "范围",
            "title": "限时做一套往年题并整理错题",
            "outcome": "错题清单",
            "est_min": 60,
            "energy": 2,
            "kind": "deep",
        },
    ]
    monkeypatch.setattr(agent, "generate_outline_plan", lambda *args, **kwargs: (outline, plan))

    goal, source = agent.create_goal_from_brief(session, user.id, brief, NOW, TZ)

    assert source == "llm"
    assert goal.template_key == "llm"
    assert goal_svc.outline_phases(goal) == outline
    assert [step.title for step in goal.steps] == [
        "整理 IBA6104 考试范围和往年题",
        "限时做一套往年题并整理错题",
    ]


def test_regenerate_goal_plan_replaces_old_template_steps(session, user, monkeypatch):
    goal = goal_svc.create_goal(
        session, user.id, "复习IBA6104", deadline=NOW.date() + timedelta(days=7)
    )
    assert goal.template_key == "exam"

    plan = [
        {
            "seq": 1,
            "milestone": "范围",
            "title": "整理 IBA6104 的考纲和往年题",
            "outcome": "资料清单",
            "est_min": 30,
            "energy": 0,
            "kind": "admin",
        }
    ]
    monkeypatch.setattr(agent, "generate_plan", lambda *args, **kwargs: plan)

    count = agent.regenerate_goal_plan(session, goal, NOW, TZ)

    assert count == 1
    assert goal.template_key == "llm"
    assert [step.title for step in goal.steps] == ["整理 IBA6104 的考纲和往年题"]


def test_propose_goal_plan_is_preview_only_and_passes_feedback(
    session, user, monkeypatch
):
    goal = goal_svc.create_goal(
        session, user.id, "复习IBA6104", deadline=NOW.date() + timedelta(days=7)
    )
    original = [step.title for step in goal.steps]
    captured = {}

    def fake_generate(*args, **kwargs):
        captured.update(kwargs)
        return [
            {
                "seq": 1,
                "milestone": "范围",
                "title": "先整理考纲",
                "outcome": "考纲清单",
                "est_min": 20,
                "energy": 0,
                "kind": "admin",
            }
        ]

    monkeypatch.setattr(agent, "generate_plan", fake_generate)

    plan = agent.propose_goal_plan(session, goal, NOW, TZ, instruction="更细一点")

    assert plan and plan[0]["title"] == "先整理考纲"
    assert captured["instruction"] == "更细一点"
    assert captured["current_steps"]
    assert [step.title for step in goal.steps] == original


def test_goal_brief_keeps_waiting_when_answer_is_vague(session, user):
    """模糊回答不能无声采用默认期限并开始生成计划。"""
    brief = planner.assess("学会弹钢琴", None, "我要学会弹钢琴", today=NOW.date())
    assert not planner.ready(brief)
    reply = agent.continue_goal(session, user.id, brief, "随便", NOW, TZ)

    assert reply.goal_draft is None
    assert reply.goal_brief is not None
    reply = agent.continue_goal(session, user.id, reply.goal_brief, "use 90 days", NOW, TZ)
    assert reply.goal_draft.brief.assumptions
    reply = agent.confirm_goal_draft(session, user.id, reply.goal_draft, True, NOW, TZ)
    goal = goal_svc.active_goals(session, user.id)[0]
    assert len(goal.steps) == 2
    assert goal.deadline == NOW.date() + timedelta(days=90)


def test_legacy_goal_clarification_can_be_cancelled(session, user):
    brief = planner.assess("学会弹钢琴", None, "我要学会弹钢琴", today=NOW.date())
    reply = agent.continue_goal(session, user.id, brief, "先不建了", NOW, TZ)

    assert "no goal created" in reply.text
    assert goal_svc.active_goals(session, user.id) == []


def test_new_command_interrupts_goal_clarification(session, user):
    assert agent.looks_like_new_command('delete "track my goal"', NOW)
    assert agent.looks_like_new_command("Add a meeting tomorrow 10am", NOW)
    assert not agent.looks_like_new_command("零基础，3 个月", NOW)


def test_proposals_do_not_overlap_existing_events(session, user):
    """回归：曾经把本地时间直接当 UTC 查忙碌块，导致排期撞上已有日程。"""
    busy_start = datetime(2026, 10, 21, 10, 0, tzinfo=ZoneInfo(TZ))
    _event(session, user, "Class: GenAI", busy_start, 120)
    goal = goal_svc.create_goal(
        session, user.id, "Publish my first short video", deadline=date(2026, 11, 20)
    )
    step = sorted(goal.steps, key=lambda s: s.seq)[0]

    candidates = agent._propose(session, user.id, step, NOW, TZ, goal.deadline, days=1)
    assert candidates
    for c in candidates:
        assert not (c.start < busy_start + timedelta(hours=2) and c.end > busy_start)


def test_accept_books_block_and_step_reads_as_scheduled(session, user):
    reply = agent.handle(
        session, user.id, "I want to publish my first short video in 30 days", NOW, TZ
    )
    reply = agent.confirm_goal_draft(session, user.id, reply.goal_draft, True, NOW, TZ)
    chosen = reply.candidates[0]
    message = agent.accept(session, user.id, reply.step_id, chosen, NOW, TZ)

    step = session.get(Step, reply.step_id)
    # scheduled 不落库：步骤仍是 todo，由未来目标块推导出 scheduled
    assert step.status == "todo"
    ev = session.scalar(select(Event).where(Event.source == "proposal"))
    assert ev.kind == "block"
    assert ev.step_id == step.id
    assert ev.start_utc == as_utc_naive(chosen.start, TZ)
    assert goal_svc.step_display_status(step, has_future_block=True) == "scheduled"
    assert "Booked" in message


def test_state_is_logged(session, user):
    reply = agent.handle(session, user.id, "I'm tired", NOW, TZ)
    assert reply.action.state == "tired"
    log = session.scalar(select(StateLog))
    assert log.label == "tired" and log.energy == "low"


def test_partial_progress_does_not_fake_completion(session, user):
    goal = goal_svc.create_goal(session, user.id, "Learn guitar")
    step = sorted(goal.steps, key=lambda s: s.seq)[0]
    goal_svc.complete_step(session, step, minutes=10, verdict="partial", raw_text="started")

    assert step.status == "todo"
    assert step.completed_at is None
    assert step.minutes_spent == 10
    assert goal_svc.progress(goal) == 0

    checkin = session.scalar(select(Checkin))
    assert checkin.verdict == "partial" and checkin.minutes == 10
    assert goal_svc.next_step(goal).id == step.id


def _book_first_step(session, user, now=NOW):
    reply = agent.handle(
        session, user.id, "I want to publish my first short video in 30 days", now, TZ
    )
    reply = agent.confirm_goal_draft(session, user.id, reply.goal_draft, True, now, TZ)
    agent.accept(session, user.id, reply.step_id, reply.candidates[0], NOW, TZ)
    return reply.step_id


def test_accept_twice_replaces_block_instead_of_stacking(session, user):
    reply = agent.handle(
        session, user.id, "I want to publish my first short video in 30 days", NOW, TZ
    )
    reply = agent.confirm_goal_draft(session, user.id, reply.goal_draft, True, NOW, TZ)
    agent.accept(session, user.id, reply.step_id, reply.candidates[0], NOW, TZ)
    agent.accept(session, user.id, reply.step_id, reply.candidates[1], NOW, TZ)

    blocks = list(session.scalars(select(Event).where(Event.step_id == reply.step_id)))
    assert len(blocks) == 1
    step = session.get(Step, reply.step_id)
    assert goal_svc.step_display_status(step, has_future_block=bool(blocks)) == "scheduled"


def test_completing_a_step_cancels_its_block(session, user):
    step_id = _book_first_step(session, user)
    reply = agent.handle(session, user.id, "I finished Write one sentence today", NOW, TZ)

    assert session.get(Step, step_id).status == "done"
    assert session.scalars(select(Event).where(Event.step_id == step_id)).all() == []
    assert "done" in reply.text.lower()


def test_completing_a_step_keeps_due_block_as_done_record(session, user):
    step_id = _book_first_step(session, user)
    step = session.get(Step, step_id)
    future = session.scalar(select(Event).where(Event.step_id == step_id))
    past = cal.create_event(
        session, user.id, f"Video: {step.title}",
        NOW - timedelta(hours=3), NOW - timedelta(hours=2),
        kind="block", source="proposal", step_id=step_id, tz=TZ,
    )

    agent.handle(session, user.id, "I finished Write one sentence today", NOW, TZ)

    ev = session.get(Event, past.id)
    assert ev is not None
    assert ev.status == "done"
    assert ev.resolved_at == as_utc_naive(NOW, TZ)
    assert session.get(Event, future.id) is None
    assert session.get(Step, step_id).status == "done"


def test_deleting_a_block_sends_the_step_back_to_todo(session, user):
    step_id = _book_first_step(session, user)
    ev = session.scalar(select(Event).where(Event.step_id == step_id))
    cal.delete_event(session, ev)
    step = session.get(Step, step_id)
    assert goal_svc.step_display_status(step, has_future_block=False) == "todo"


def test_deleting_one_of_two_blocks_keeps_step_scheduled(session, user):
    step_id = _book_first_step(session, user)
    goal = goal_svc.active_goals(session, user.id)[0]
    step = session.get(Step, step_id)
    extra = _event(
        session, user, f"{goal.title}: {step.title}", NOW + timedelta(days=3, hours=2), 45
    )
    extra.kind = "block"
    extra.step_id = step_id
    session.flush()

    existing = session.scalars(select(Event).where(Event.step_id == step_id)).all()
    cal.delete_event(session, existing[0])
    remaining = session.scalars(select(Event).where(Event.step_id == step_id)).all()
    step = session.get(Step, step_id)
    assert goal_svc.step_display_status(step, has_future_block=bool(remaining)) == "scheduled"


def test_propose_for_step_skips_done_steps(session, user):
    goal = goal_svc.create_goal(session, user.id, "Learn guitar")
    step = sorted(goal.steps, key=lambda s: s.seq)[0]
    goal_svc.complete_step(session, step)

    candidates, returned = agent.propose_for_step(session, user.id, step.id, NOW, TZ)
    assert candidates == []
    assert returned is not None and returned.status == "done"


def test_unknown_chat_gets_a_clarification_not_a_wall_of_help(session, user):
    reply = agent.handle(session, user.id, "hmm maybe later", NOW, TZ)
    assert reply.text == agent.CLARIFY


def test_explicit_help_returns_the_full_menu(session, user):
    reply = agent.handle(session, user.id, "help", NOW, TZ)
    assert reply.text == agent.HELP


def test_chat_can_schedule_the_next_goal_step(session, user):
    goal_svc.create_goal(
        session, user.id, "Publish my first short video", deadline=date(2026, 11, 20)
    )
    reply = agent.handle(session, user.id, "帮我安排下一步", NOW, TZ)

    assert reply.action.intent == "schedule_step"
    assert reply.step_id is not None
    assert 1 <= len(reply.candidates) <= 6
    assert "时段" in reply.text


def test_delete_goal_by_title(session, user):
    goal = goal_svc.create_goal(session, user.id, "track my goal")
    step = sorted(goal.steps, key=lambda s: s.seq)[0]
    _event(
        session, user, f"{goal.title}: {step.title}", NOW + timedelta(days=1), 45,
        kind="block",
    ).step_id = step.id
    session.flush()

    reply = agent.handle(session, user.id, 'delete "track my goal"', NOW, TZ)

    assert reply.action.intent == "delete_goal"
    assert "Deleted goal" in reply.text
    assert goal.status == "archived"
    assert goal_svc.active_goals(session, user.id) == []
    assert session.scalars(select(Event).where(Event.step_id == step.id)).all() == []


def test_delete_goal_with_multiple_goals_asks_which_one(session, user):
    goal_svc.create_goal(session, user.id, "Learn guitar")
    goal_svc.create_goal(session, user.id, "Run a half marathon")

    reply = agent.handle(session, user.id, "delete my goal", NOW, TZ)

    assert "Which goal" in reply.text
    assert len(goal_svc.active_goals(session, user.id)) == 2


def test_delete_missing_goal_is_human_readable(session, user):
    goal_svc.create_goal(session, user.id, "Learn guitar")
    reply = agent.handle(session, user.id, 'delete "track my goal"', NOW, TZ)
    assert "couldn't find a goal" in reply.text


# ------------------------------------------------------------ P0-3 回归：进度汇报


def _goal_with_named_steps(session, user, titles):
    defs = [
        {
            "seq": i + 1,
            "milestone": "M",
            "title": t,
            "outcome": "",
            "est_min": 30,
            "energy": 1,
            "kind": "deep",
        }
        for i, t in enumerate(titles)
    ]
    return goal_svc.create_goal(
        session,
        user.id,
        "My project",
        deadline=NOW.date() + timedelta(days=30),
        step_defs=defs,
        template_key="llm",
    )


def test_progress_marks_the_named_step_not_the_first(session, user):
    goal = _goal_with_named_steps(session, user, ["Write the script", "Make the storyboard"])
    reply = agent.handle(session, user.id, "I finished the storyboard", NOW, TZ)

    steps = {s.title: s for s in goal.steps}
    assert steps["Make the storyboard"].status == "done"
    assert steps["Write the script"].status == "todo"
    assert "storyboard" in reply.text


def test_negated_progress_marks_nothing_done(session, user):
    goal = _goal_with_named_steps(session, user, ["Write the script"])
    agent.handle(session, user.id, "I did not finish the script", NOW, TZ)

    assert goal.steps[0].status == "todo"
    assert goal.steps[0].completed_at is None


# ------------------------------------------------------------ 置信度阈值：先追问再执行


def test_low_confidence_move_asks_for_confirmation(session, user):
    ev = _event(session, user, "Team meeting", NOW + timedelta(days=1))
    reply = agent.handle(session, user.id, "Move the team meeting", NOW, TZ)

    assert reply.action.intent == "move_event"
    assert "confirm" in reply.text
    assert session.get(Event, ev.id).start_utc == ev.start_utc


def test_low_confidence_delete_asks_for_confirmation(session, user, monkeypatch):
    from goalcal.llm.action import Action

    ev = _event(session, user, "Dentist", NOW + timedelta(days=2))
    low = Action(intent="delete_event", target="Dentist", confidence=0.4, raw="maybe delete dentist")
    monkeypatch.setattr(agent, "parse", lambda *args, **kwargs: (low, "test"))

    reply = agent.handle(session, user.id, "maybe delete dentist", NOW, TZ)

    assert "confirm" in reply.text
    assert session.get(Event, ev.id) is not None


def test_confident_delete_still_executes(session, user):
    _event(session, user, "Dentist appointment", NOW + timedelta(days=2))
    reply = agent.handle(session, user.id, "Cancel the dentist appointment", NOW, TZ)

    assert reply.action.confidence >= agent.CONFIRM_THRESHOLD
    assert "Deleted" in reply.text
    assert session.scalar(select(Event)) is None


# ------------------------------------------------------------ 排期窗口与批量排


def _goal_with_sized_steps(session, user, sizes, title="My project"):
    defs = [
        {
            "seq": i + 1,
            "milestone": "M",
            "title": t,
            "outcome": "",
            "est_min": minutes,
            "energy": 1,
            "kind": "deep",
        }
        for i, (t, minutes) in enumerate(sizes)
    ]
    return goal_svc.create_goal(
        session,
        user.id,
        title,
        deadline=NOW.date() + timedelta(days=30),
        step_defs=defs,
        template_key="llm",
    )


def test_schedule_step_with_window_proposes_inside_window(session, user):
    _goal_with_named_steps(session, user, ["写脚本", "画分镜"])
    reply = agent.handle(session, user.id, "安排写脚本到今天下午", NOW, TZ)

    assert reply.action.intent == "schedule_step"
    assert reply.candidates
    for c in reply.candidates:
        assert c.start.date() == NOW.date()
        assert 12 <= c.start.hour and c.end.hour <= 18


def test_schedule_step_full_window_falls_back_with_note(session, user):
    _goal_with_named_steps(session, user, ["写脚本"])
    _event(
        session, user, "Long meeting",
        datetime(2026, 10, 21, 11, 30, tzinfo=ZoneInfo(TZ)), 420,  # 11:30–18:30 占满下午
    )
    reply = agent.handle(session, user.id, "安排写脚本到今天下午", NOW, TZ)

    assert "no free slot" in reply.text
    assert reply.candidates  # 回退到默认 5 天范围


def test_schedule_step_with_explicit_time_books_directly(session, user):
    goal = _goal_with_named_steps(session, user, ["写脚本", "画分镜"])
    reply = agent.handle(session, user.id, "把写脚本安排在今天下午3点", NOW, TZ)

    assert reply.action.intent == "schedule_step"
    assert reply.candidates == []
    assert "已将" in reply.text and "写入日历" in reply.text
    ev = session.scalar(select(Event).where(Event.kind == "block"))
    assert ev is not None and ev.source == "proposal"
    local = as_local(ev.start_utc, TZ)
    assert (local.hour, local.minute) == (15, 0)
    step = next(s for s in goal.steps if s.title == "写脚本")
    assert ev.step_id == step.id
    assert int((ev.end_utc - ev.start_utc).total_seconds() // 60) == step.est_min


def test_propose_for_step_honors_window(session, user):
    goal = _goal_with_named_steps(session, user, ["写脚本"])
    step = goal.steps[0]
    window = (
        datetime(2026, 10, 22, 8, 0, tzinfo=ZoneInfo(TZ)),
        datetime(2026, 10, 22, 12, 0, tzinfo=ZoneInfo(TZ)),
    )
    candidates, returned = agent.propose_for_step(session, user.id, step.id, NOW, TZ, window=window)
    assert returned is not None
    assert candidates
    assert all(c.start.date() == date(2026, 10, 22) and c.end.hour <= 12 for c in candidates)


def test_schedule_all_packs_steps_with_buffers(session, user):
    _goal_with_sized_steps(
        session, user,
        [("Write the script", 30), ("Make the storyboard", 30), ("Record the video", 45)],
    )
    reply = agent.handle(session, user.id, "今天下午有空，把任务都排了", NOW, TZ)

    assert reply.action.intent == "schedule_all"
    assert reply.batch is not None and len(reply.batch) == 3
    zone = ZoneInfo(TZ)
    assert reply.batch[0]["start"] == datetime(2026, 10, 21, 12, 0, tzinfo=zone)
    # 块间 10 分钟 buffer
    assert reply.batch[1]["start"] == datetime(2026, 10, 21, 12, 40, tzinfo=zone)
    assert reply.batch[2]["start"] == datetime(2026, 10, 21, 13, 20, tzinfo=zone)
    assert "Book them all" in reply.text


def test_schedule_all_stops_at_the_daily_cap(session, user):
    _goal_with_sized_steps(session, user, [("A", 45), ("B", 45), ("C", 45), ("D", 45)])
    reply = agent.handle(session, user.id, "今天下午有空，把任务都排了", NOW, TZ)

    assert len(reply.batch) == 2  # 45+45=90，再加 45 超过 120 分钟上限即停
    assert "Didn't fit" in reply.text
    assert "“C”" in reply.text and "“D”" in reply.text


def test_schedule_all_packs_across_goals_in_seq_order(session, user):
    g1 = _goal_with_sized_steps(session, user, [("G1 first", 30), ("G1 second", 30)], "Goal one")
    g2 = _goal_with_sized_steps(session, user, [("G2 first", 30)], "Goal two")
    g2.created_at = g1.created_at + timedelta(seconds=1)
    session.flush()

    reply = agent.handle(session, user.id, "schedule everything tomorrow morning", NOW, TZ)

    assert reply.batch is not None
    assert [b["title"] for b in reply.batch] == ["G1 first", "G1 second", "G2 first"]
    assert all(b["start"].date() == NOW.date() + timedelta(days=1) for b in reply.batch)


def test_resolve_step_without_target_prefers_urgent_then_pinned(session, user):
    g1 = _goal_with_sized_steps(session, user, [("G1 step", 30)], "Goal one")
    g2 = _goal_with_sized_steps(session, user, [("G2 step", 30)], "Goal two")
    g2.created_at = g1.created_at + timedelta(seconds=1)
    g2.deadline = NOW.date() - timedelta(days=1)  # 逾期 → 紧急度最高
    session.flush()

    goal, step, error = agent._resolve_step(goal_svc.active_goals(session, user.id), None)
    assert error is None
    assert goal.id == g2.id and step.title == "G2 step"

    goal_svc.set_pinned(session, g1, True)
    session.flush()
    goal, step, error = agent._resolve_step(goal_svc.active_goals(session, user.id), None)
    assert error is None
    assert goal.id == g1.id and step.title == "G1 step"


def test_schedule_all_skips_already_scheduled_steps(session, user):
    goal = _goal_with_sized_steps(session, user, [("A", 30), ("B", 30)])
    step_a = sorted(goal.steps, key=lambda s: s.seq)[0]
    ev = _event(session, user, "My project: A", NOW + timedelta(hours=4), 30, kind="block")
    ev.step_id = step_a.id
    session.flush()

    reply = agent.handle(session, user.id, "把任务都排了", NOW, TZ)

    assert reply.batch is not None and len(reply.batch) == 1
    assert reply.batch[0]["title"] == "B"
    assert "Already scheduled" in reply.text


def test_schedule_all_full_window_reports_nothing_fit(session, user):
    _goal_with_sized_steps(session, user, [("A", 60)])
    _event(
        session, user, "All afternoon",
        datetime(2026, 10, 21, 11, 30, tzinfo=ZoneInfo(TZ)), 420,
    )
    reply = agent.handle(session, user.id, "今天下午有空，把任务都排了", NOW, TZ)

    assert reply.batch is None
    assert "Couldn't fit anything" in reply.text


def test_accept_batch_books_every_block_and_metrics_once(session, user):
    import json

    from goalcal.models import Interaction

    _goal_with_sized_steps(session, user, [("A", 30), ("B", 30), ("C", 30)])
    reply = agent.handle(session, user.id, "今天下午有空，把任务都排了", NOW, TZ)
    msg = agent.accept_batch(session, user.id, reply.batch, NOW, TZ)

    assert "Booked 3 block(s)" in msg
    events = list(session.scalars(select(Event).where(Event.kind == "block")))
    assert len(events) == 3
    assert {ev.step_id for ev in events} == {b["step_id"] for b in reply.batch}
    assert all(ev.source == "proposal" for ev in events)
    logs = list(session.scalars(select(Interaction).where(Interaction.kind == "proposal_accepted")))
    assert len(logs) == 1
    assert json.loads(logs[0].payload)["batch"] == 3

