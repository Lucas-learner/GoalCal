from pathlib import Path

from sqlalchemy import select
from streamlit.testing.v1 import AppTest

from goalcal.db import session_scope, init_db
from goalcal import auth
from goalcal.models import User
import uuid
import pytest
from goalcal.models import Event, Goal

APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture(autouse=True)
def _ui_account(monkeypatch):
    monkeypatch.setenv("GOALCAL_UI_TEST_ACCOUNT", "ui_" + uuid.uuid4().hex[:12])


def _app() -> AppTest:
    import os
    account = os.environ["GOALCAL_UI_TEST_ACCOUNT"]
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception, at.exception
    init_db()
    with session_scope() as s:
        user = s.scalar(select(User).where(User.username == account))
        if user is None:
            from datetime import timedelta
            from goalcal.domain import goals, calendar_service as cal, refine
            from goalcal.timeutil import local_now
            user = User(name="UI test", username=account, tz="Asia/Shanghai",
                        password_hash=auth.credentials(account, "test-password-long", "Asia/Shanghai")[1])
            s.add(user); s.flush()
            now = local_now("Asia/Shanghai")
            goal = goals.create_goal(s, user.id, "Publish my first short video")
            goals.complete_step(s, goal.steps[0], raw_text="explicit test fixture")
            refine.refine_upcoming(s, goal)
            cal.create_event(s, user.id, "Test appointment", now+timedelta(days=1),
                             now+timedelta(days=1,hours=1), tz="Asia/Shanghai")
        username = user.username
    next(t for t in at.text_input if t.key == "login_name").set_value(username)
    next(t for t in at.text_input if t.key == "login_password").set_value("test-password-long")
    next(b for b in at.button if b.label == "Log in").click().run()
    assert not at.exception, at.exception
    assert at.chat_input, "The explicit UI test account must be logged in"
    return at


def _goals(at):
    return at.switch_page("routes/goals.py").run()


def _calendar(at):
    return at.switch_page("routes/calendar.py").run()


def _settings(at):
    return at.switch_page("routes/settings.py").run()


def _chat(at):
    at._page_hash = next(
        page_hash for page_hash, info in at._registered_pages.items()
        if info.get("page_name") == "Chat"
    )
    return at.run()


def test_app_boots_to_chat_without_three_column_workbench():
    at = _app()
    assert at.title[0].value == "GoalCal"
    assert at.chat_input
    assert not any(b.label == "Find time" for b in at.button)


def test_chat_turn_creates_a_goal():
    at = _app()
    at.chat_input[0].set_value("I want to publish my first short video in 30 days").run()
    assert not at.exception, at.exception
    assert at.session_state["goal_draft"] is not None
    with session_scope() as s:
        assert len(list(s.scalars(select(Goal).where(Goal.user_id == at.session_state["user_id"])))) == 1
    next(b for b in at.button if b.label == "Create & schedule").click().run()
    assert not at.exception, at.exception
    assert any("Goal set" in m.value for m in at.markdown)
    assert any(b.label == "Book" for b in at.button)


def test_goal_card_exposes_actions():
    at = _goals(_app())
    labels = {button.label for button in at.button}
    assert "Find time" in labels
    assert "Complete" in labels


def test_find_time_returns_to_chat_with_inline_candidate_slots():
    at = _goals(_app())
    next(b for b in at.button if b.label == "Find time").click().run()
    assert not at.exception, at.exception
    assert at.chat_input
    assert any(b.label == "Book" for b in at.button)
    assert not any("Choose a time" in item.value for item in at.toast)
    assert not at.get("dialog")


def test_candidate_chips_refilter_without_error():
    at = _goals(_app())
    next(b for b in at.button if b.label == "Find time").click().run()
    at = _chat(at)
    sel = next(s for s in at.selectbox if s.key.startswith("filter_sel_"))
    sel.set_value(2).run()
    assert not at.exception, at.exception
    assert any(b.label == "Book" for b in at.button)


def test_schedule_all_shows_batch_plan_and_books():
    at = _app()
    at.chat_input[0].set_value("今天下午有空，把任务都排了").run()
    assert not at.exception, at.exception
    assert any("Batch plan" in m.value for m in at.markdown)
    next(b for b in at.button if b.label == "Book all").click().run()
    assert not at.exception, at.exception
    assert any("Booked" in m.value for m in at.markdown)
    assert not any(t.value.startswith("Booked") for t in at.toast)


def test_calendar_delete_requires_confirmation():
    at = _calendar(_app())
    with session_scope() as s:
        event_id = s.scalar(select(Event.id).where(Event.user_id == at.session_state["user_id"]).order_by(Event.id))
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
    next(b for b in at.button if b.label == "Save goal").click().run()
    at.chat_input[0].set_value('delete "track my goal"').run()
    assert not at.exception, at.exception
    assert any("Deleted goal" in m.value for m in at.markdown)
    with session_scope() as s:
        goal = s.scalar(select(Goal).where(Goal.title.contains("track my goal")))
        assert goal is not None and goal.status == "archived"


def test_add_custom_step_from_goal_card():
    at = _goals(_app())
    next(t for t in at.text_input if t.label == "Title" and t.placeholder == "e.g. Record the intro").set_value("Practice chord changes")
    next(b for b in at.button if b.label == "Add step").click().run()
    assert not at.exception, at.exception
    assert any(t.value == "Added step “Practice chord changes”." for t in at.toast)
    assert any("Practice chord changes" in m.value for m in at.markdown)


def test_new_goal_button_returns_to_chat_instead_of_duplicate_form():
    at = _goals(_app())
    next(b for b in at.button if b.label == "New goal").click().run()
    assert not at.exception, at.exception
    assert at.chat_input
    assert not any(t.label == "Goal title" for t in at.text_input)


def test_piano_goal_clarifies_then_builds_complex_plan():
    at = _app()
    at.chat_input[0].set_value("我要学会弹钢琴").run()
    assert not at.exception, at.exception
    assert any("complex" in m.value and "confirm" in m.value for m in at.markdown)
    with session_scope() as s:
        assert s.scalar(select(Goal).where(Goal.title == "学会弹钢琴")) is None
    next(b for b in at.button if b.label == "3 months").click().run()
    assert not at.exception, at.exception
    assert at.session_state["goal_draft"] is not None
    next(b for b in at.button if b.label == "Save goal").click().run()
    assert not at.exception, at.exception
    assert any("Goal set" in m.value for m in at.markdown)
    assert at.session_state["goal_brief"] is None
    with session_scope() as s:
        goal = s.scalar(select(Goal).where(Goal.title == "学会弹钢琴"))
        assert goal is not None and goal.template_key == "piano"
        assert len(goal.steps) == 2


def test_goal_replan_preview_is_applied_only_after_confirmation():
    at = _goals(_app())
    with session_scope() as s:
        goal = s.scalar(select(Goal).where(Goal.status == "active", Goal.user_id == at.session_state["user_id"]).order_by(Goal.id))
        goal_id = goal.id
    at.session_state["replan_draft"] = {
        "goal_id": goal_id,
        "instruction": "test preview",
        "plan": [{"seq": 1, "milestone": "Preview", "title": "Preview-only step", "outcome": "nothing yet", "est_min": 20, "energy": 0, "kind": "admin"}],
    }
    at.run()
    assert not at.exception, at.exception
    assert any("Proposed revised plan" in m.value for m in at.markdown)
    with session_scope() as s:
        goal = s.get(Goal, goal_id)
        assert all(step.title != "Preview-only step" for step in goal.steps)
    for checkbox in at.checkbox:
        if checkbox.key == f"confirm_replan_{goal_id}":
            checkbox.check().run()
            break
    next(b for b in at.button if b.label == "Apply revised plan").click().run()
    assert not at.exception, at.exception
    assert any(t.value.startswith("Applied the revised plan") for t in at.toast)
    with session_scope() as s:
        goal = s.get(Goal, goal_id)
        assert [step.title for step in goal.steps] == ["Preview-only step"]


def test_unauthenticated_and_logout_clear_state():
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.chat_input
    at = _app()
    at.session_state["replan_draft"] = {"goal_id": -1, "plan": []}
    at.session_state["messages"] = [{"role": "user", "content": "private"}]
    next(b for b in at.button if b.label == "Log out").click().run()
    assert not at.exception
    assert not at.chat_input
    assert "messages" not in at.session_state
    assert "replan_draft" not in at.session_state


def test_registration_login_starts_empty_and_disable_exits():
    username = "ui_" + uuid.uuid4().hex[:12]
    invitation = auth.invite()
    at = AppTest.from_file(APP, default_timeout=30).run()
    next(t for t in at.text_input if t.key == "register_name").set_value(username)
    next(t for t in at.text_input if t.label == "Password (at least 12 characters)").set_value("new-user-password-42")
    next(t for t in at.text_input if t.label == "Invitation code").set_value(invitation)
    next(b for b in at.button if b.label == "Register").click().run()
    assert not at.exception
    next(t for t in at.text_input if t.key == "login_name").set_value(username)
    next(t for t in at.text_input if t.key == "login_password").set_value("new-user-password-42")
    next(b for b in at.button if b.label == "Log in").click().run()
    assert not at.exception
    assert at.chat_input
    uid = at.session_state["user_id"]
    with session_scope() as s:
        assert list(s.scalars(select(Goal).where(Goal.user_id == uid))) == []
        assert list(s.scalars(select(Event).where(Event.user_id == uid))) == []
    auth.manage(username, disabled=True)
    at.run()
    assert not at.exception and not at.chat_input
    assert "auth_token" not in at.session_state


def test_trial_feedback_form_saves_only_to_current_account():
    import json
    from goalcal.models import Interaction
    at = _settings(_app())
    uid = at.session_state["user_id"]
    next(t for t in at.text_area if t.label == "What happened? What would you like us to improve?").set_value("Test feedback: calendar drag needs a clearer result")
    next(b for b in at.button if b.label == "Send feedback").click().run()
    assert not at.exception
    assert any("Feedback saved" in item.value for item in at.success)
    with session_scope() as s:
        row = s.scalar(select(Interaction).where(Interaction.kind == "user_feedback", Interaction.user_id == uid).order_by(Interaction.id.desc()))
        assert json.loads(row.payload)["message"] == "Test feedback: calendar drag needs a clearer result"


def test_history_learning_can_be_disabled_from_settings():
    import json
    from goalcal import memory
    at = _settings(_app())
    uid = at.session_state["user_id"]
    next(c for c in at.checkbox if c.label == "Learn preferred windows from goal history").uncheck()
    next(b for b in at.button if b.label == "Save preferences").click().run()

    assert not at.exception, at.exception
    with session_scope() as s:
        rows = memory.list_preferences(s, uid)
    row = next(row for row in rows if row.key == memory.LEARNING_ENABLED_KEY)
    assert json.loads(row.value) is False


def _dismiss_dialog_from_frontend(at, title):
    from streamlit.proto.WidgetStates_pb2 import WidgetState
    dialog = next(d for d in at.get("dialog") if d.proto.dialog.title == title)
    assert dialog.proto.dialog.id, "Dismissal must have a server callback"
    states = at._tree.get_widget_states()
    states.widgets.append(WidgetState(id=dialog.proto.dialog.id, trigger_value=True))
    at._run(states)


@pytest.mark.parametrize("callback,title,close_mode", [
    ("eventClick", "Event details", "frontend"),
    ("dateClick", "New event", "frontend"),
    ("eventClick", "Event details", "button"),
    ("dateClick", "New event", "button"),
])
def test_calendar_dismiss_does_not_reopen_on_other_buttons(callback, title, close_mode, monkeypatch):
    from goalcal.ui import calendar_view
    values = {}; renders = []
    def render(*args, **kwargs):
        key = kwargs.get("key", "goalcal_calendar")
        renders.append(kwargs)
        return values.get(key, {})
    monkeypatch.setattr(calendar_view, "render", render)
    at = _calendar(_app())
    with session_scope() as s:
        event_id = s.scalar(select(Event.id).where(Event.user_id == at.session_state["user_id"]))
    view = {"type": "timeGridDay", "currentStart": "2026-10-24T00:00:00Z"}
    payload = {"callback": callback, callback: {"view": view}}
    if callback == "eventClick":
        payload[callback]["event"] = {"id": str(event_id)}
    else:
        payload[callback].update(date="2026-10-24T09:00:00Z", allDay=False)
    old_key = renders[-1].get("key", "goalcal_calendar")
    values[old_key] = payload
    at.run()
    assert not at.exception
    assert any(d.proto.dialog.title == title for d in at.get("dialog"))
    if close_mode == "frontend":
        _dismiss_dialog_from_frontend(at, title)
    else:
        label = "Close" if callback == "eventClick" else "Cancel"
        next(b for b in at.button if b.label == label).click().run()
    assert not at.exception
    assert not at.get("dialog")
    assert not at.session_state.get("selected_event_id")
    assert not at.session_state.get("new_event_start")
    at = _goals(at)
    next(b for b in at.button if b.label == "Complete").click().run()
    assert not at.exception and not at.get("dialog")
    at = _calendar(at)
    new_key = renders[-1].get("key", "goalcal_calendar")
    assert new_key != old_key
    assert renders[-1]["initial_view"] == "timeGridDay"
    assert renders[-1]["initial_date"] == "2026-10-24"
    values[new_key] = payload
    at.run()
    assert not at.exception
    assert any(d.proto.dialog.title == title for d in at.get("dialog"))


def test_inline_slots_dismiss_does_not_resurface_on_other_pages():
    at = _goals(_app())
    next(b for b in at.button if b.label == "Find time").click().run()
    assert any(b.label == "Book" for b in at.button)
    at = _chat(at)
    next(b for b in at.button if b.label == "Dismiss").click().run()
    assert not at.exception
    assert at.session_state["pending"] is None
    at = _goals(at)
    next(b for b in at.button if b.label == "Complete").click().run()
    assert not at.exception and not at.get("dialog")
    assert at.session_state["pending"] is None


def test_settings_save_preferences_for_scheduler():
    from goalcal import memory

    at = _settings(_app())
    next(t for t in at.number_input if t.label == "Day starts").set_value(20)
    next(t for t in at.number_input if t.label == "Day ends").set_value(22)
    next(b for b in at.button if b.label == "Save preferences").click().run()
    assert not at.exception, at.exception
    assert any("Preferences saved" in item.value for item in at.success)
    with session_scope() as s:
        prefs = memory.get_scheduler_preferences(s, at.session_state["user_id"])
        assert prefs.day_start_hour == 20
        assert prefs.day_end_hour == 22
        rows = memory.list_preferences(s, at.session_state["user_id"])
        assert any(row.key == "day_start_hour" and row.scope == "global" and row.source == "explicit" for row in rows)


def test_returning_user_restores_conversation():
    title = "Persistence meeting " + uuid.uuid4().hex[:8]
    at = _app()
    at.chat_input[0].set_value(f"Add {title} tomorrow 10am").run()
    assert not at.exception, at.exception

    returning = _app()
    assert not returning.exception, returning.exception
    assert any(title in m.value for m in returning.markdown)


def test_calendar_dates_set_loads_the_new_visible_range(monkeypatch):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from goalcal.domain import calendar_service as cal
    from goalcal.ui import calendar_view

    at = _app()
    uid = at.session_state["user_id"]
    start = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=45)
    with session_scope() as s:
        cal.create_event(s, uid, "Far future event", start, start + timedelta(hours=1), tz="Asia/Shanghai")
    rendered = []
    payload = {
        "callback": "datesSet",
        "datesSet": {
            "startStr": (start - timedelta(days=1)).date().isoformat(),
            "endStr": (start + timedelta(days=6)).date().isoformat(),
            "view": {"type": "timeGridWeek", "currentStart": start.date().isoformat()},
        },
    }

    def render(events, **kwargs):
        rendered.append(events)
        return payload if len(rendered) == 1 else {}

    monkeypatch.setattr(calendar_view, "render", render)
    at = _calendar(at)
    assert not at.exception, at.exception
    assert len(rendered) >= 2
    assert any(event["title"] == "Far future event" for event in rendered[-1])


def test_settings_reject_inverted_day_bounds():
    at = _settings(_app())
    next(t for t in at.number_input if t.label == "Day starts").set_value(23)
    next(t for t in at.number_input if t.label == "Day ends").set_value(1)
    next(b for b in at.button if b.label == "Save preferences").click().run()
    assert not at.exception, at.exception
    assert any("must be earlier" in item.value for item in at.error)
    assert not any("Preferences saved" in item.value for item in at.success)


def test_calendar_event_change_preserves_resized_duration(monkeypatch):
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from goalcal.domain import calendar_service as cal
    from goalcal.ui import calendar_view

    at = _app()
    uid = at.session_state["user_id"]
    start = datetime.now(ZoneInfo("Asia/Shanghai")) + timedelta(days=1)
    with session_scope() as s:
        event = cal.create_event(s, uid, "Resize me", start, start + timedelta(hours=1), tz="Asia/Shanghai")
        event_id = event.id
    moved_start = start + timedelta(hours=2)
    payload = {
        "callback": "eventChange",
        "eventChange": {
            "event": {
                "id": str(event_id),
                "start": moved_start.isoformat(),
                "end": (moved_start + timedelta(hours=3)).isoformat(),
            },
            "view": {"type": "timeGridWeek", "currentStart": start.date().isoformat()},
        },
    }
    monkeypatch.setattr(calendar_view, "render", lambda *args, **kwargs: payload)
    at = _calendar(at)
    assert not at.exception, at.exception
    with session_scope() as s:
        event = s.get(Event, event_id)
        assert event.end_utc - event.start_utc == timedelta(hours=3)
        assert cal.as_local(event.start_utc, "Asia/Shanghai").hour == moved_start.hour


def _goal_count(at):
    with session_scope() as s:
        return len(list(s.scalars(select(Goal).where(Goal.user_id == at.session_state["user_id"]))))


def test_goal_preview_restores_then_cancel_writes_nothing():
    at = _app()
    before = _goal_count(at)
    at.chat_input[0].set_value("I want to publish my first short video in 30 days").run()
    draft_id = at.session_state["goal_draft"].draft_id
    returning = _app()
    assert returning.session_state["goal_draft"].draft_id == draft_id
    assert any(b.label == "Save goal" for b in returning.button)
    next(b for b in returning.button if b.label == "Cancel").click().run()
    assert not returning.exception, returning.exception
    assert returning.session_state["goal_draft"] is None
    assert _goal_count(returning) == before
    assert _app().session_state["goal_draft"] is None


def test_save_goal_by_text_clears_draft_and_does_not_book():
    at = _app()
    before = _goal_count(at)
    at.chat_input[0].set_value("I want to publish my first short video in 30 days").run()
    at.chat_input[0].set_value("仅保存").run()
    assert not at.exception, at.exception
    assert at.session_state["goal_draft"] is None
    assert at.session_state["pending"] is None
    assert _goal_count(at) == before + 1
    at.run()
    assert _goal_count(at) == before + 1


@pytest.mark.parametrize("selection", ["第二个", "2", "second", "button"])
def test_candidate_text_and_button_book_the_same_slot(selection):
    at = _goals(_app())
    next(b for b in at.button if b.label == "Find time").click().run()
    at = _chat(at)
    pending = at.session_state["pending"]
    candidate = pending["candidates"][1]
    step_id = pending["step_id"]
    if selection == "button":
        [b for b in at.button if b.label == "Book"][1].click().run()
    else:
        at.chat_input[0].set_value(selection).run()
    assert not at.exception, at.exception
    assert at.session_state["pending"] is None
    assert sum("Booked" in item.value for item in at.markdown) == 1
    assert not any("Booked" in item.value for item in at.toast)
    from goalcal.timeutil import as_utc_naive
    with session_scope() as s:
        blocks = list(s.scalars(select(Event).where(Event.step_id == step_id)))
        assert len(blocks) == 1
        assert blocks[0].start_utc == as_utc_naive(candidate.start, "Asia/Shanghai")
    at = _goals(at)
    assert any(button.label == "Reschedule" for button in at.button)


@pytest.mark.parametrize("window", ["改到明晚", "周四晚上"])
def test_pending_time_window_refilters_current_step(window, monkeypatch):
    from goalcal import agent
    from goalcal.domain.scheduler import Candidate
    from datetime import timedelta
    captured = []
    at = _goals(_app())
    next(b for b in at.button if b.label == "Find time").click().run()
    at = _chat(at)
    step_id = at.session_state["pending"]["step_id"]

    def propose(session, uid, sid, now, tz, *, window):
        captured.append((sid, window))
        return [Candidate(start=window[0], end=window[0] + timedelta(minutes=30), score=1, reasons=[])], None

    monkeypatch.setattr(agent, "propose_for_step", propose)
    at.chat_input[0].set_value(window).run()
    assert not at.exception, at.exception
    assert captured[0][0] == step_id
    assert captured[0][1][0].hour == 18
    if window == "周四晚上":
        assert captured[0][1][0].weekday() == 3
    else:
        from goalcal.timeutil import local_now
        from datetime import timedelta
        assert captured[0][1][0].date() == (local_now('Asia/Shanghai') + timedelta(days=1)).date()
    assert at.session_state["pending"]["step_id"] == step_id


def test_invalid_candidate_response_and_empty_window_preserve_suggestions(monkeypatch):
    from goalcal import agent
    at = _goals(_app())
    next(b for b in at.button if b.label == "Find time").click().run()
    at = _chat(at)
    original = at.session_state["pending"]
    for text in ["99", "hmm"]:
        at.chat_input[0].set_value(text).run()
        assert not at.exception, at.exception
        assert at.session_state["pending"] == original
    monkeypatch.setattr(agent, "propose_for_step", lambda *a, **k: ([], None))
    at.chat_input[0].set_value("改到明晚").run()
    assert not at.exception, at.exception
    assert at.session_state["pending"] == original


def test_batch_suggestions_restore_and_book():
    at = _app()
    at.chat_input[0].set_value("今天下午有空，把任务都排了").run()
    pending = at.session_state["pending"]
    assert pending["batch"]
    returning = _app()
    assert returning.session_state["pending"] == pending
    next(b for b in returning.button if b.label == "Book all").click().run()
    assert not returning.exception, returning.exception
    assert returning.session_state["pending"] is None


def test_goals_directory_selects_detail_and_archive_keeps_other_goal():
    from goalcal.domain import goals
    at = _app()
    with session_scope() as s:
        second = goals.create_goal(s, at.session_state["user_id"], "Second goal")
        second_id = second.id
    at = _goals(at)
    next(b for b in at.button if b.key == f"select_goal_{second_id}").click().run()
    assert not at.exception, at.exception
    assert at.session_state["selected_goal_id"] == second_id
    next(b for b in at.button if b.label == "Confirm archive goal").click().run()
    assert not at.exception, at.exception
    assert _goal_count(at) == 2
    with session_scope() as s:
        assert s.get(Goal, second_id).status == "archived"
    assert not any((b.key or "").startswith("select_goal_") for b in at.button)


def test_new_event_command_leaves_candidate_context_without_refilter(monkeypatch):
    from goalcal import agent
    at = _goals(_app())
    next(b for b in at.button if b.label == "Find time").click().run()
    at = _chat(at)
    def unexpected(*a, **k):
        raise AssertionError("An explicit event command must not refilter the step")
    monkeypatch.setattr(agent, "propose_for_step", unexpected)
    at.chat_input[0].set_value("Add review meeting tomorrow afternoon").run()
    assert not at.exception, at.exception
    assert at.session_state["pending"] is None
    with session_scope() as s:
        assert s.scalar(select(Event).where(Event.user_id == at.session_state["user_id"], Event.title == "review meeting")) is not None


def test_goal_revision_stays_preview_and_keeps_identity(monkeypatch):
    from copy import deepcopy
    from goalcal import agent
    at = _app()
    before = _goal_count(at)
    at.chat_input[0].set_value("I want to publish my first short video in 30 days").run()
    draft = at.session_state["goal_draft"]
    steps = deepcopy(draft.step_defs)
    steps[0]["title"] = "A smaller first step"
    monkeypatch.setattr(agent, "generate_plan", lambda *a, **k: steps)
    at.chat_input[0].set_value("Make each step smaller").run()
    assert not at.exception, at.exception
    assert at.session_state["goal_draft"].draft_id == draft.draft_id
    assert at.session_state["goal_draft"].step_defs[0]["title"] == "A smaller first step"
    assert _goal_count(at) == before
    assert any(b.label == "Save goal" for b in at.button)


@pytest.mark.parametrize('page_path', ['routes/calendar.py', 'routes/goals.py', 'routes/settings.py'])
def test_signed_out_deep_link_returns_login_without_rendering_page(page_path):
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception, at.exception
    at.switch_page(page_path).run()
    assert not at.exception, at.exception
    assert any(t.key == 'login_name' for t in at.text_input)
    assert any(b.label == 'Log in' for b in at.button)
    assert not at.chat_input


@pytest.mark.parametrize('page_path', ['routes/calendar.py', 'routes/goals.py', 'routes/settings.py'])
def test_one_login_from_subpage_restores_navigation(page_path, monkeypatch):
    signed_in = _app()
    with session_scope() as session:
        username = session.get(User, signed_in.session_state['user_id']).username
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.switch_page(page_path).run()
    calls = []
    login = auth.login
    def count_login(*args, **kwargs):
        calls.append(True)
        return login(*args, **kwargs)
    monkeypatch.setattr(auth, 'login', count_login)
    next(t for t in at.text_input if t.key == 'login_name').set_value(username)
    next(t for t in at.text_input if t.key == 'login_password').set_value('test-password-long')
    next(b for b in at.button if b.label == 'Log in').click().run()
    assert not at.exception, at.exception
    assert len(calls) == 1
    assert not any(t.key == 'login_name' for t in at.text_input)
    token = at.session_state['auth_token']
    at.run()
    assert at.session_state['auth_token'] == token
    assert not any(t.key == 'login_name' for t in at.text_input)
    assert at.session_state['tz'] == 'Asia/Shanghai'
    assert at.session_state['chat_page'] is not None
    at = _goals(at)
    next(b for b in at.button if b.label == 'Find time').click().run()
    assert not at.exception, at.exception
    assert at.chat_input


def test_calendar_component_is_rendered_across_pages_with_stable_key(monkeypatch):
    import streamlit as st
    from goalcal.ui import calendar_view
    rendered = []
    prefixes = []
    operations = []
    real_container = st.container
    real_html = st.html

    def container(*args, **kwargs):
        operations.append(('container', kwargs.get('key')))
        return real_container(*args, **kwargs)

    def html(*args, **kwargs):
        operations.append(('html', None))
        return real_html(*args, **kwargs)

    def capture(events, **kwargs):
        rendered.append((kwargs['key'], len(events)))
        prefixes.append(list(operations))
        operations.clear()
        return {}

    monkeypatch.setattr(st, 'container', container)
    monkeypatch.setattr(st, 'html', html)
    monkeypatch.setattr(calendar_view, 'render', capture)
    at = _app()
    assert at.chat_input
    after_chat = len(rendered)
    at = _goals(at)
    at = _calendar(at)
    at = _chat(at)
    assert not at.exception, at.exception
    assert len(rendered) >= after_chat + 3
    assert {key for key, _ in rendered} == {'goalcal_calendar_0'}
    assert all(('container', 'calendar_heading') in prefix for prefix in prefixes)
    assert all(prefix.count(('html', None)) == 1 for prefix in prefixes)


def test_calendar_shell_gets_new_event_before_opening_calendar(monkeypatch):
    from goalcal.ui import calendar_view
    title = 'Shell refresh ' + uuid.uuid4().hex[:8]
    seen = []
    real_render = calendar_view.render
    def capture(events, **kwargs):
        seen.append([event['title'] for event in events])
        return {}
    monkeypatch.setattr(calendar_view, 'render', capture)
    at = _app()
    at.chat_input[0].set_value(f'Add {title} tomorrow 10am').run()
    assert not at.exception, at.exception
    at = _calendar(at)
    assert any(title in names for names in seen)
    assert title in seen[-1]
