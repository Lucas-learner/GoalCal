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


def _app() -> AppTest:
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception, at.exception
    init_db()
    with session_scope() as s:
        user = s.scalar(select(User).where(User.username == "ui_test"))
        if user is None:
            from datetime import timedelta
            from goalcal.domain import goals, calendar_service as cal, refine
            from goalcal.timeutil import local_now
            user = User(name="UI test", username="ui_test", tz="Asia/Shanghai",
                        password_hash=auth.credentials("ui_test", "test-password-long", "Asia/Shanghai")[1])
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
    next(b for b in at.button if b.label == "登录").click().run()
    assert not at.exception, at.exception
    assert at.chat_input, "The explicit UI test account must be logged in"
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
    assert any(b.label == "Book" for b in at.button)


def test_candidate_chips_refilter_without_error():
    at = _app()
    next(b for b in at.button if b.label == "📅").click().run()
    assert not at.exception, at.exception
    sel = next(s for s in at.selectbox if s.key.startswith("filter_sel_"))
    sel.set_value(2).run()  # Tonight
    assert not at.exception, at.exception
    assert any(b.label == "Book" for b in at.button)


def test_schedule_all_shows_batch_plan_and_books():
    at = _app()
    at.chat_input[0].set_value("今天下午有空，把任务都排了").run()
    assert not at.exception, at.exception
    assert any("Batch plan" in m.value for m in at.markdown)

    next(b for b in at.button if b.label == "Book all").click().run()
    assert not at.exception, at.exception
    assert any(t.value.startswith("Booked") for t in at.toast)


def test_calendar_delete_requires_confirmation():
    at = _app()
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
        goal = s.scalar(select(Goal).where(Goal.status == "active", Goal.user_id == at.session_state["user_id"]).order_by(Goal.id))
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
    assert not any(b.label == "Reset demo data" for b in at.button)
    at = _app()
    at.session_state["replan_draft"] = {"goal_id": -1, "plan": []}
    at.session_state["messages"] = [{"role": "user", "content": "private"}]
    next(b for b in at.button if b.label == "退出登录").click().run()
    assert not at.exception
    assert not at.chat_input
    assert "messages" not in at.session_state
    assert "replan_draft" not in at.session_state


def test_registration_login_starts_empty_and_disable_exits():
    username = 'ui_' + uuid.uuid4().hex[:12]
    invitation = auth.invite()
    at = AppTest.from_file(APP, default_timeout=30).run()
    next(t for t in at.text_input if t.key == 'register_name').set_value(username)
    next(t for t in at.text_input if t.label == '密码（至少 12 个字符）').set_value('new-user-password-42')
    next(t for t in at.text_input if t.label == '邀请码').set_value(invitation)
    next(b for b in at.button if b.label == '注册').click().run()
    assert not at.exception
    next(t for t in at.text_input if t.key == 'login_name').set_value(username)
    next(t for t in at.text_input if t.key == 'login_password').set_value('new-user-password-42')
    next(b for b in at.button if b.label == '登录').click().run()
    assert not at.exception
    assert any('No active goal' in info.value for info in at.info)
    uid = at.session_state['user_id']
    with session_scope() as s:
        assert list(s.scalars(select(Goal).where(Goal.user_id == uid))) == []
        assert list(s.scalars(select(Event).where(Event.user_id == uid))) == []
    auth.manage(username, disabled=True)
    at.run()
    assert not at.exception and not at.chat_input
    assert 'auth_token' not in at.session_state


def test_trial_feedback_form_saves_only_to_current_account():
    import json
    from goalcal.models import Interaction
    at = _app()
    uid = at.session_state['user_id']
    next(t for t in at.text_area if t.label == '发生了什么？希望怎样改进？').set_value('Test feedback: calendar drag needs a clearer result')
    next(b for b in at.button if b.label == '提交反馈').click().run()
    assert not at.exception
    assert any('反馈已保存' in item.value for item in at.success)
    with session_scope() as s:
        row = s.scalar(select(Interaction).where(Interaction.kind == 'user_feedback', Interaction.user_id == uid).order_by(Interaction.id.desc()))
        assert json.loads(row.payload)['message'] == 'Test feedback: calendar drag needs a clearer result'


def _dismiss_dialog_from_frontend(at, title):
    """X, Escape and backdrop clicks all send the dialog's dismiss trigger."""
    from streamlit.proto.WidgetStates_pb2 import WidgetState
    dialog = next(d for d in at.get('dialog') if d.proto.dialog.title == title)
    assert dialog.proto.dialog.id, 'Dismissal must have a server callback'
    states = at._tree.get_widget_states()
    states.widgets.append(WidgetState(id=dialog.proto.dialog.id, trigger_value=True))
    at._run(states)


@pytest.mark.parametrize('callback,title,close_mode', [
    ('eventClick', 'Event details', 'frontend'),
    ('dateClick', 'New event', 'frontend'),
    ('eventClick', 'Event details', 'button'),
    ('dateClick', 'New event', 'button'),
])
def test_calendar_dismiss_does_not_reopen_on_other_buttons(callback, title, close_mode, monkeypatch):
    from goalcal.ui import calendar_view
    values = {}; renders = []
    def render(*args, **kwargs):
        key = kwargs.get('key', 'goalcal_calendar')
        renders.append(kwargs)
        return values.get(key, {})
    monkeypatch.setattr(calendar_view, 'render', render)
    at = _app()
    with session_scope() as s:
        event_id = s.scalar(select(Event.id).where(Event.user_id == at.session_state['user_id']))
    view = {'type': 'timeGridDay', 'currentStart': '2026-10-24T00:00:00Z'}
    payload = {'callback': callback, callback: {'view': view}}
    if callback == 'eventClick':
        payload[callback]['event'] = {'id': str(event_id)}
    else:
        payload[callback].update(date='2026-10-24T09:00:00Z', allDay=False)
    old_key = renders[-1].get('key', 'goalcal_calendar')
    values[old_key] = payload
    at.run()
    assert not at.exception
    assert any(d.proto.dialog.title == title for d in at.get('dialog'))
    if close_mode == 'frontend':
        _dismiss_dialog_from_frontend(at, title)
    else:
        label = 'Close' if callback == 'eventClick' else 'Cancel'
        next(b for b in at.button if b.label == label).click().run()
    assert not at.exception
    assert not at.get('dialog')
    assert not at.session_state.get('selected_event_id')
    assert not at.session_state.get('new_event_start')
    next(b for b in at.button if b.label == '✅').click().run()
    assert not at.exception
    assert not at.get('dialog')
    # A genuinely new click on the SAME event/time must still work.
    new_key = renders[-1].get('key', 'goalcal_calendar')
    assert new_key != old_key
    assert renders[-1]['initial_view'] == 'timeGridDay'
    assert renders[-1]['initial_date'] == '2026-10-24'
    values[new_key] = payload
    at.run()
    assert not at.exception
    assert any(d.proto.dialog.title == title for d in at.get('dialog'))


def test_suggested_slots_dismiss_does_not_resurface_on_other_buttons():
    at = _app()
    next(b for b in at.button if b.label == '📅').click().run()
    assert any(b.label == 'Book' for b in at.button)
    _dismiss_dialog_from_frontend(at, 'Suggested slots')
    assert not at.exception and not at.get('dialog')
    assert at.session_state['pending'] is None
    next(b for b in at.button if b.label == '✅').click().run()
    assert not at.exception and not at.get('dialog')
