from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select

from goalcal import agent
from goalcal.domain import calendar_service as cal
from goalcal.domain import goals as goal_svc
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


def test_create_goal_returns_steps_and_candidates(session, user):
    reply = agent.handle(
        session, user.id, "I want to publish my first short video in 30 days", NOW, TZ
    )
    assert reply.action.intent == "create_goal"
    assert reply.goal_id is not None and reply.step_id is not None
    goal = goal_svc.active_goals(session, user.id)[0]
    assert len(goal.steps) == 2  # 滚动拆解：只落首批步骤
    assert goal_svc.outline_phases(goal)
    assert 1 <= len(reply.candidates) <= 3
    assert all(c.reasons for c in reply.candidates)


def test_piano_goal_asks_before_creating(session, user):
    reply = agent.handle(session, user.id, "我要学会弹钢琴", NOW, TZ)

    assert reply.action.intent == "create_goal"
    assert reply.goal_brief is not None
    assert "confirm" in reply.text
    assert goal_svc.active_goals(session, user.id) == []


def test_piano_goal_is_created_after_clarification(session, user):
    first = agent.handle(session, user.id, "我要学会弹钢琴", NOW, TZ)
    reply = agent.continue_goal(
        session, user.id, first.goal_brief,
        "零基础，3 个月，能弹一首简单曲子", NOW, TZ,
    )

    assert reply.goal_brief is None
    assert "Goal set" in reply.text
    goal = goal_svc.active_goals(session, user.id)[0]
    assert goal.template_key == "piano"
    assert len(goal.steps) == 2
    assert goal_svc.outline_phases(goal) == ["准备与基线", "手型与音阶", "读谱与节奏", "第一首曲子", "巩固与下一步"]
    assert goal.deadline == NOW.date() + timedelta(days=90)
    assert 1 <= len(reply.candidates) <= 3


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


def test_piano_goal_defaults_when_answer_is_vague(session, user):
    first = agent.handle(session, user.id, "我要学会弹钢琴", NOW, TZ)
    reply = agent.continue_goal(session, user.id, first.goal_brief, "随便", NOW, TZ)

    assert "Assumptions" in reply.text
    goal = goal_svc.active_goals(session, user.id)[0]
    assert len(goal.steps) == 2
    assert goal.deadline == NOW.date() + timedelta(days=90)


def test_goal_clarification_can_be_cancelled(session, user):
    first = agent.handle(session, user.id, "我要学会弹钢琴", NOW, TZ)
    reply = agent.continue_goal(session, user.id, first.goal_brief, "先不建了", NOW, TZ)

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
    agent.accept(session, user.id, reply.step_id, reply.candidates[0], NOW, TZ)
    return reply.step_id


def test_accept_twice_replaces_block_instead_of_stacking(session, user):
    reply = agent.handle(
        session, user.id, "I want to publish my first short video in 30 days", NOW, TZ
    )
    agent.accept(session, user.id, reply.step_id, reply.candidates[0], NOW, TZ)
    agent.accept(session, user.id, reply.step_id, reply.candidates[1], NOW, TZ)

    blocks = list(session.scalars(select(Event).where(Event.step_id == reply.step_id)))
    assert len(blocks) == 1
    step = session.get(Step, reply.step_id)
    assert goal_svc.step_display_status(step, has_future_block=bool(blocks)) == "scheduled"


def test_completing_a_step_cancels_its_block(session, user):
    step_id = _book_first_step(session, user)
    reply = agent.handle(session, user.id, "I finished the script today", NOW, TZ)

    assert session.get(Step, step_id).status == "done"
    assert session.scalars(select(Event).where(Event.step_id == step_id)).all() == []
    assert "done" in reply.text.lower()


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
    assert 1 <= len(reply.candidates) <= 3
    assert "slots" in reply.text.lower()


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

