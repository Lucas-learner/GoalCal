from pathlib import Path

from sqlalchemy import select
from streamlit.testing.v1 import AppTest

from goalcal.db import session_scope
from goalcal.models import Event, Goal

APP = str(Path(__file__).resolve().parent.parent / "app.py")


def _app() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception, at.exception
    return at


def test_app_boots_without_exception():
    at = _app()
    assert at.title[0].value.startswith("📆 GoalCal")


def test_chat_turn_creates_a_goal():
    at = _app()
    at.chat_input[0].set_value("I want to publish my first short video in 30 days").run()
    assert not at.exception, at.exception
    joined = " ".join(m.value for m in at.markdown)
    assert "Goal set" in joined


def test_goal_card_exposes_actions():
    at = _app()
    labels = {button.label for button in at.button}
    assert "📅" in labels
    assert "✅" in labels


def test_find_time_shows_candidate_slots():
    at = _app()
    next(b for b in at.button if b.label == "📅").click().run()
    assert not at.exception, at.exception
    assert any("Suggested slots" in m.value for m in at.markdown)


def test_calendar_delete_requires_confirmation():
    at = _app()
    with session_scope() as s:
        event_id = s.scalar(select(Event.id).order_by(Event.id))

    at.session_state["selected_event_id"] = event_id
    at.run()
    assert not at.exception, at.exception
    next(b for b in at.button if b.label == "Delete event").click().run()
    assert any("cannot be undone" in w.value for w in at.warning)

    next(b for b in at.button if b.label == "Keep it").click().run()
    with session_scope() as s:
        assert s.get(Event, event_id) is not None


def test_chat_delete_goal_archives_the_goal():
    at = _app()
    at.chat_input[0].set_value("I want to track my goal in 30 days").run()
    at.chat_input[0].set_value('delete "track my goal"').run()
    assert not at.exception, at.exception

    assert any("Deleted goal" in m.value for m in at.markdown)
    with session_scope() as s:
        goal = s.scalar(select(Goal).where(Goal.title.contains("track my goal")))
        assert goal is not None and goal.status == "archived"


def test_add_custom_step_from_goal_card():
    at = _app()
    next(
        t for t in at.text_input
        if t.label == "Title" and t.placeholder == "e.g. Record the intro"
    ).set_value("Practice chord changes")
    next(b for b in at.button if b.label == "Add step").click().run()
    assert not at.exception, at.exception

    assert any(t.value == "Added step “Practice chord changes”." for t in at.toast)
    assert any("Practice chord changes" in m.value for m in at.markdown)


def test_create_blank_goal_from_ui():
    at = _app()
    next(t for t in at.text_input if t.label == "Goal title").set_value("My own plan")
    next(c for c in at.checkbox if "Auto-decompose" in c.label).uncheck()
    next(b for b in at.button if b.label == "Create goal").click().run()
    assert not at.exception, at.exception

    assert any(t.value == "Created blank goal “My own plan”." for t in at.toast)
    assert any("My own plan" in m.value for m in at.markdown)


def test_piano_goal_clarifies_then_builds_complex_plan():
    at = _app()
    at.chat_input[0].set_value("我要学会弹钢琴").run()
    assert not at.exception, at.exception
    assert any("complex" in m.value and "confirm" in m.value for m in at.markdown)
    with session_scope() as s:
        assert s.scalar(select(Goal).where(Goal.title == "学会弹钢琴")) is None

    next(b for b in at.button if b.label == "3 months").click().run()
    assert not at.exception, at.exception
    assert any("Goal set" in m.value for m in at.markdown)
    assert at.session_state["goal_brief"] is None
    with session_scope() as s:
        goal = s.scalar(select(Goal).where(Goal.title == "学会弹钢琴"))
        assert goal is not None and goal.template_key == "piano"
        assert len(goal.steps) == 2  # 滚动拆解：只落首批步骤


def test_goal_replan_preview_is_applied_only_after_confirmation():
    at = _app()
    with session_scope() as s:
        goal = s.scalar(select(Goal).where(Goal.status == "active").order_by(Goal.id))
        assert goal is not None
        goal_id = goal.id

    at.session_state["replan_draft"] = {
        "goal_id": goal_id,
        "instruction": "test preview",
        "plan": [
            {
                "seq": 1,
                "milestone": "Preview",
                "title": "Preview-only step",
                "outcome": "nothing yet",
                "est_min": 20,
                "energy": 0,
                "kind": "admin",
            }
        ],
    }
    at.run()
    assert not at.exception, at.exception
    assert any("Proposed revised plan" in m.value for m in at.markdown)

    with session_scope() as s:
        goal = s.get(Goal, goal_id)
        assert all(step.title != "Preview-only step" for step in goal.steps)

    next(b for b in at.button if b.label == "Apply revised plan").click().run()
    assert not at.exception, at.exception
    assert any(t.value.startswith("Applied the revised plan") for t in at.toast)

    with session_scope() as s:
        goal = s.get(Goal, goal_id)
        assert [step.title for step in goal.steps] == ["Preview-only step"]
