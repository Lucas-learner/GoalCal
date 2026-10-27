from pathlib import Path
from datetime import timedelta

from sqlalchemy import func, select
from streamlit.testing.v1 import AppTest

from goalcal.db import session_scope, init_db
from goalcal import auth
from goalcal.models import User
import uuid
import pytest
from goalcal.models import Conversation, Event, Goal

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


def _empty_app() -> AppTest:
    """注册一个没有任何目标/日程/历史的全新账号并登录。"""
    username = "ui_" + uuid.uuid4().hex[:12]
    invitation = auth.invite()
    at = AppTest.from_file(APP, default_timeout=30).run()
    next(t for t in at.text_input if t.key == "register_name").set_value(username)
    next(t for t in at.text_input if t.label == "Password (at least 12 characters)").set_value("new-user-password-42")
    next(t for t in at.text_input if t.label == "Invitation code").set_value(invitation)
    next(b for b in at.button if b.label == "Register").click().run()
    next(t for t in at.text_input if t.key == "login_name").set_value(username)
    next(t for t in at.text_input if t.key == "login_password").set_value("new-user-password-42")
    next(b for b in at.button if b.label == "Log in").click().run()
    assert not at.exception
    assert at.chat_input
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
    assert not at.title  # 主区不再显示产品名
    assert any(m.value == "**GoalCal**" for m in at.markdown)  # 侧栏顶部产品名
    assert at.chat_input
    assert not any(b.label == "Find time" for b in at.button)


def test_empty_home_keeps_three_layers_without_data():
    at = _empty_app()
    assert not at.title
    assert any(m.value == "What do you want to make time for?" for m in at.markdown)
    labels = [b.label for b in at.button if (b.key or "").startswith("home_suggestion_")]
    assert labels == ["Start a new goal", "Add an event", "See what I can do"]
    assert not any(b.key == "home_suggestion_goal" for b in at.button)  # 无目标排期按钮
    at.chat_input[0].set_value("hmm").run()
    assert not at.exception, at.exception
    # 首条消息进入消息流，三层首页消失
    assert not any((b.key or "").startswith("home_suggestion_") for b in at.button)
    assert any(m.value for m in at.markdown)


def test_home_shows_slogan_and_contextual_suggestions():
    at = _app()
    assert not at.title
    assert any(m.value == "What do you want to make time for?" for m in at.markdown)
    goal_btn = next(b for b in at.button if b.key == "home_suggestion_goal")
    assert goal_btn.label == "Find time for my goal"
    event_btn = next(b for b in at.button if b.key == "home_suggestion_event")
    assert event_btn.label == "Review my calendar"
    assert any(b.key == "home_suggestion_new" for b in at.button)
    # 旧首页元素不再出现：Focus 切换器、进度/Next step/Next event 摘要、静态指引
    assert not at.selectbox
    captions = {c.value for c in at.caption}
    assert not any(c.startswith("Next step:") for c in captions)
    assert not any(c.startswith("Next event:") for c in captions)
    assert not any(c.startswith("Keep going") for c in captions)
    assert not any(c.startswith("Start something") for c in captions)
    assert not any("days left" in c for c in captions)


def test_home_goal_suggestion_asks_which_goal_then_schedules_the_answer():
    from datetime import date
    from goalcal.domain import goals as goal_svc
    at = _app()
    with session_scope() as s:
        urgent = goal_svc.create_goal(s, at.session_state["user_id"], "Urgent goal", deadline=date(2026, 10, 29))
        goal_svc.create_goal(s, at.session_state["user_id"], "Later goal", deadline=date(2027, 1, 31))
        expected_step_id = goal_svc.next_step(urgent).id
    at.run()
    assert not at.exception, at.exception
    btn = next(b for b in at.button if b.key == "home_suggestion_goal")
    assert btn.label == "Find time for my goal"
    btn.click().run()
    assert not at.exception, at.exception
    assert at.session_state["pending"] is None
    assert at.session_state["messages"][-1]["content"] == (
        "Which goal should I schedule? Name the goal or its next step."
    )

    at.chat_input[0].set_value("Urgent goal").run()
    assert not at.exception, at.exception
    pending = at.session_state["pending"]
    assert pending is not None
    assert pending["step_id"] == expected_step_id
    assert any(b.label == "Book" for b in at.button)
    next(b for b in at.button if b.label == "Dismiss").click().run()
    assert not at.exception, at.exception
    assert at.session_state["pending"] is None


@pytest.mark.parametrize("answer", ["tomorrow 3pm", "明天下午3点", "改到明天下午3点"])
def test_pending_exact_clock_books_the_current_step(answer):
    at = _app()
    next(b for b in at.button if b.key == "home_suggestion_goal").click().run()
    step_id = at.session_state["pending"]["step_id"]

    at.chat_input[0].set_value(answer).run()

    assert not at.exception, at.exception
    assert at.session_state["pending"] is None
    with session_scope() as s:
        blocks = list(s.scalars(select(Event).where(Event.step_id == step_id)))
        assert len(blocks) == 1
        assert blocks[0].kind == "block"
        assert not s.scalar(select(Event).where(
            Event.user_id == at.session_state["user_id"], Event.title == "Untitled"
        ))


def test_pending_past_clock_keeps_candidates_and_does_not_write():
    at = _app()
    next(b for b in at.button if b.key == "home_suggestion_goal").click().run()
    step_id = at.session_state["pending"]["step_id"]

    at.chat_input[0].set_value("today 12:01am").run()

    assert at.session_state["pending"] is not None
    assert "already passed" in at.session_state["messages"][-1]["content"]
    with session_scope() as s:
        assert not s.scalar(select(Event).where(Event.step_id == step_id))


def test_pending_explicit_new_event_is_not_captured_as_step_time():
    at = _app()
    next(b for b in at.button if b.key == "home_suggestion_goal").click().run()
    step_id = at.session_state["pending"]["step_id"]

    at.chat_input[0].set_value("Add a meeting tomorrow 3pm").run()

    with session_scope() as s:
        event = s.scalar(select(Event).where(
            Event.user_id == at.session_state["user_id"], Event.title.ilike("%meeting%")
        ))
        assert event is not None and event.step_id is None
        assert not s.scalar(select(Event).where(Event.step_id == step_id))


def test_home_advances_to_the_next_unscheduled_step_after_booking():
    at = _app()
    next(b for b in at.button if b.key == "home_suggestion_goal").click().run()
    first_step_id = at.session_state["pending"]["step_id"]
    with session_scope() as s:
        goal = s.scalar(select(Goal).where(Goal.user_id == at.session_state["user_id"]))
        second_step_id = next(step.id for step in sorted(goal.steps, key=lambda item: item.seq)
                              if step.status == "todo" and step.id != first_step_id)
    next(b for b in at.button if b.label == "Book").click().run()
    next(b for b in at.button if b.label == "New conversation").click().run()

    next(b for b in at.button if b.key == "home_suggestion_goal").click().run()
    assert at.session_state["pending"]["step_id"] == second_step_id


def test_home_hides_find_when_every_todo_step_has_a_future_block():
    from goalcal.domain import calendar_service as cal
    from goalcal.timeutil import local_now

    at = _app()
    with session_scope() as s:
        goals = list(s.scalars(select(Goal).where(Goal.user_id == at.session_state["user_id"])))
        now = local_now("Asia/Shanghai")
        for goal in goals:
            for index, step in enumerate(goal.steps):
                if step.status == "todo":
                    start = now + timedelta(days=index + 1)
                    cal.create_event(s, at.session_state["user_id"], "Scheduled", start,
                                     start + timedelta(minutes=step.est_min), kind="block",
                                     step_id=step.id, tz="Asia/Shanghai")
    at.run()
    assert not any(b.key == "home_suggestion_goal" for b in at.button)


def test_pending_weekend_text_refilters_candidates():
    at = _app()
    next(b for b in at.button if b.key == "home_suggestion_goal").click().run()

    at.chat_input[0].set_value("this weekend").run()

    pending = at.session_state["pending"]
    assert pending is not None and pending["candidates"]
    assert all(candidate.start.weekday() >= 5 for candidate in pending["candidates"])
    assert "Updated the suggested times" in at.session_state["messages"][-1]["content"]


def test_home_review_suggestion_runs_a_real_upcoming_query():
    at = _app()
    btn = next(b for b in at.button if b.key == "home_suggestion_event")
    assert btn.label == "Review my calendar"
    btn.click().run()
    assert not at.exception, at.exception
    assert at.session_state["has_user_history"] is True
    assert not any((b.key or "").startswith("home_suggestion_") for b in at.button)
    assert any("Test appointment" in m.value for m in at.markdown)


def test_home_new_goal_suggestion_asks_before_creating_anything():
    at = _empty_app()
    next(b for b in at.button if b.key == "home_suggestion_new").click().run()
    assert not at.exception, at.exception
    assert at.session_state["goal_intake"] is True
    assert at.session_state["goal_draft"] is None
    assert not any(m["role"] == "user" for m in at.session_state["messages"])
    assert not any((b.key or "").startswith("home_suggestion_") for b in at.button)

    at.chat_input[0].set_value("Learn guitar in 30 days").run()
    assert not at.exception, at.exception
    assert at.session_state["goal_intake"] is None
    draft = at.session_state["goal_draft"]
    assert draft is not None
    assert draft.brief.title == "Learn guitar"
    assert not any("short video" in m["content"] for m in at.session_state["messages"])


def test_home_suggestions_pure_function():
    from datetime import datetime
    from goalcal.ui import chat_page

    full = {
        "goal_title": "Learn guitar",
        "next_step_id": 7,
        "next_step_minutes": 30,
        "next_event_title": "Learn tennis",
        "next_event_start": datetime(2026, 10, 28, 16, 0),
    }
    rows = chat_page.home_suggestions(full)
    assert [r["key"] for r in rows] == ["home_suggestion_goal", "home_suggestion_event", "home_suggestion_new"]
    assert rows[0]["action"] == "run_turn" and rows[0]["text"] == "Find time for my goal"
    assert rows[0]["label"] == "Find time for my goal"
    assert rows[1]["action"] == "run_turn" and rows[1]["text"] == "What's on this week?"
    assert rows[1]["label"] == "Review my calendar"
    assert rows[2]["action"] == "begin_goal"

    # 无日程：省略日程行；无未排期步骤：绝不出现排期按钮
    no_event = {**full, "next_event_title": None, "next_event_start": None}
    assert [r["key"] for r in chat_page.home_suggestions(no_event)] == ["home_suggestion_goal", "home_suggestion_new"]
    no_step = {**no_event, "next_step_id": None}
    assert [r["key"] for r in chat_page.home_suggestions(no_step)] == ["home_suggestion_new", "home_suggestion_event", "home_suggestion_help"]

    # 空账号：同样三层结构，Create a goal / Add an event / 产品能力
    empty = {**no_step, "goal_title": None}
    rows = chat_page.home_suggestions(empty)
    assert [r["key"] for r in rows] == ["home_suggestion_new", "home_suggestion_event", "home_suggestion_help"]
    assert rows[0]["action"] == "begin_goal"
    assert rows[1]["text"] == "Add a meeting tomorrow 10am"
    assert rows[2]["text"] == "What can you do?"


def test_message_markdown_preserves_visible_line_breaks():
    from goalcal.ui.chat import message_markdown

    assert message_markdown("Today:\n• First\n• Second") == "Today:  \n• First  \n• Second"
    assert message_markdown("Paragraph one\n\nParagraph two") == "Paragraph one  \n  \nParagraph two"


def test_chat_scrolls_after_a_turn_but_reload_resets_without_scroll(monkeypatch):
    from goalcal.ui import chat as chat_ui

    calls = []
    monkeypatch.setattr(chat_ui, "scroll_to_bottom", lambda: calls.append("scroll"))
    at = _app()
    assert calls == []

    at.chat_input[0].set_value("hmm").run()
    assert not at.exception, at.exception
    assert calls == ["scroll"]

    # 整页重进（新浏览器会话）：会话重置回指引首页，不因旧历史滚动
    at.session_state["conversation_loaded"] = False
    at.run()
    assert not at.exception, at.exception
    assert calls == ["scroll"]
    assert [m["content"] for m in at.session_state["messages"]] == [
        "Hi — tell me a goal, or add something to your calendar."
    ]


def _seed_history(at, count):
    from goalcal import conversation as conv

    uid = at.session_state["user_id"]
    with session_scope() as s:
        conv.save_state(s, uid, [
            {"role": "user" if i % 2 else "assistant", "content": f"history-{i:02d}",
             "client_id": f"seed-{i:02d}"}
            for i in range(count)
        ], {})


def _history_markdowns(at):
    return [m.value for m in at.markdown if m.value.startswith("history-")]


def test_reload_wipes_history_and_hides_load_earlier(monkeypatch):
    """重开页面不再分页恢复历史：旧消息真实删除，只剩重置后的欢迎语。"""
    from goalcal.ui import chat as chat_ui
    from goalcal.models import Message

    bottoms = []
    monkeypatch.setattr(chat_ui, "scroll_to_bottom", lambda: bottoms.append("bottom"))

    at = _app()
    _seed_history(at, 31)
    at.session_state["conversation_loaded"] = False
    at.run()
    assert not at.exception, at.exception
    assert _history_markdowns(at) == []
    assert not any(b.key == "load_earlier" for b in at.button)
    assert bottoms == []
    with session_scope() as s:
        remaining = s.scalar(
            select(func.count(Message.id)).where(
                Message.conversation_id == select(Conversation.id).where(
                    Conversation.user_id == at.session_state["user_id"]
                ).scalar_subquery()
            )
        )
    assert remaining == 1  # 旧历史真实删除，仅重置后的欢迎消息


def test_new_conversation_clears_chat_state_but_keeps_goals_and_calendar():
    from goalcal.models import Message
    from goalcal.ui.controller import STATE_KEYS

    at = _app()
    uid = at.session_state["user_id"]
    sidebar_items = [(node.type, getattr(node, "label", None))
                     for node in at.sidebar.children.values()]
    assert sidebar_items.index(("button", "New conversation")) < sidebar_items.index(
        ("page_link", "Chat")
    )
    with session_scope() as s:
        goals_before = s.scalar(select(func.count(Goal.id)).where(Goal.user_id == uid))
        events_before = s.scalar(select(func.count(Event.id)).where(Event.user_id == uid))

    at.chat_input[0].set_value("I want to learn guitar").run()
    assert not at.exception, at.exception
    assert at.session_state["goal_brief"] is not None
    next(b for b in at.button if b.label == "New conversation").click().run()
    assert not at.exception, at.exception
    assert [m["content"] for m in at.session_state["messages"]] == [
        "Hi — tell me a goal, or add something to your calendar."
    ]
    assert at.session_state["has_user_history"] is False
    assert at.session_state["chat_has_more"] is False
    assert all(at.session_state[key] is None for key in STATE_KEYS if key != "dialog_context")
    assert at.session_state["dialog_context"].kind == "idle"
    assert at.session_state["dialog_context"].offers == []
    assert any(b.label == "New conversation" for b in at.sidebar.button)
    assert not any(b.label in {"Clear conversation", "Keep conversation"}
                   for b in at.sidebar.button)

    with session_scope() as s:
        conversation_id = s.scalar(select(Conversation.id).where(Conversation.user_id == uid))
        assert s.scalar(select(func.count(Message.id)).where(
            Message.conversation_id == conversation_id)) == 1
        assert s.scalar(select(func.count(Goal.id)).where(Goal.user_id == uid)) == goals_before
        assert s.scalar(select(func.count(Event.id)).where(Event.user_id == uid)) == events_before


def test_load_earlier_after_in_session_truncation():
    """会话内累计超过一页后消息被截断（没有数据库 id），Load earlier 不能是死按钮。"""
    at = _app()
    for i in range(16):
        at.chat_input[0].set_value(f"msg {i:02d}").run()
        assert not at.exception, at.exception
    assert len(at.session_state["messages"]) == 30
    assert not any(m.get("id") for m in at.session_state["messages"])

    next(b for b in at.button if b.key == "load_earlier").click().run()
    assert not at.exception, at.exception
    # 从数据库恢复锚点并 prepend 了更早的消息（最早的 "msg 00" 回来了）
    assert any(m["content"] == "msg 00" for m in at.session_state["messages"])
    assert at.session_state["chat_has_more"] is False


def test_sidebar_does_not_duplicate_chat_navigation_or_goal_status():
    at = _app()
    captions = {c.value for c in at.caption}
    assert not any(c.startswith("Goal:") for c in captions)
    assert not any(b.key == "sidebar_history" for b in at.button)

    at.chat_input[0].set_value("hmm").run()
    assert not at.exception, at.exception
    assert not any(b.key == "sidebar_history" for b in at.button)
    assert at.chat_input


def test_goal_draft_card_is_a_single_flat_panel():
    at = _app()
    at.chat_input[0].set_value("I want to publish my first short video in 30 days").run()
    assert not at.exception, at.exception
    captions = {c.value for c in at.caption}
    assert "Goal draft" in captions
    assert "GOAL DRAFT" not in captions
    labels = {b.label for b in at.button}
    assert {"Create & schedule", "Save goal", "Cancel"} <= labels


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
    assert at.session_state["goal_intake"] is True
    assert not any(t.label == "Goal title" for t in at.text_input)


def test_piano_goal_confirms_timeline_before_draft_and_creation():
    at = _app()
    at.chat_input[0].set_value("我要学会弹钢琴").run()
    assert not at.exception, at.exception
    assert at.session_state["goal_brief"] is not None
    assert at.session_state["goal_draft"] is None
    assert any("choose a timeline" in m.value for m in at.markdown)
    at.chat_input[0].set_value("1 month").run()
    assert not at.exception, at.exception
    assert at.session_state["goal_draft"] is not None
    with session_scope() as s:
        assert s.scalar(select(Goal).where(Goal.title == "学会弹钢琴")) is None
    next(b for b in at.button if b.label == "Save goal").click().run()
    assert not at.exception, at.exception
    assert any("Goal set" in m.value for m in at.markdown)
    assert at.session_state["goal_brief"] is None
    with session_scope() as s:
        goal = s.scalar(select(Goal).where(Goal.title == "学会弹钢琴"))
        assert goal is not None and goal.template_key == "piano"
        assert len(goal.steps) == 2
        from goalcal.timeutil import local_now
        assert goal.deadline == local_now("Asia/Shanghai").date() + timedelta(days=30)


def test_goal_timeline_shortcuts_cancel_and_custom_duration():
    at = _app()
    at.chat_input[0].set_value("I want to learn guitar").run()
    assert not at.exception, at.exception
    quick = {"7 days", "1 month", "3 months", "Cancel"}
    assert {b.label for b in at.button if b.label in quick} == quick
    assert not any(b.label in {"Use 90 days", "Never mind"} for b in at.button)

    next(b for b in at.button if b.label == "Cancel").click().run()
    assert not at.exception, at.exception
    assert at.session_state["goal_brief"] is None
    assert at.session_state["goal_draft"] is None

    at.chat_input[0].set_value("I want to learn guitar").run()
    at.chat_input[0].set_value("12 days").run()
    assert not at.exception, at.exception
    from goalcal.timeutil import local_now
    assert at.session_state["goal_draft"].deadline == (
        local_now("Asia/Shanghai").date() + timedelta(days=12)
    )


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
    assert not any(b.label == "Log out" for b in at.sidebar.button)
    assert all(node.type != "divider" for node in at.sidebar.children.values())
    at.session_state["replan_draft"] = {"goal_id": -1, "plan": []}
    at.session_state["messages"] = [{"role": "user", "content": "private"}]
    at.switch_page("routes/settings.py").run()
    assert not at.exception, at.exception
    assert any(h.value == "Account" for h in at.subheader)
    assert any(c.value == "Time zone: Asia/Shanghai" for c in at.caption)
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


def test_reload_starts_a_fresh_conversation():
    """整页重进不再恢复旧会话；日程等业务数据不受影响。"""
    title = "Persistence meeting " + uuid.uuid4().hex[:8]
    at = _app()
    at.chat_input[0].set_value(f"Add {title} tomorrow 10am").run()
    assert not at.exception, at.exception

    returning = _app()
    assert not returning.exception, returning.exception
    assert not any(title in m.value for m in returning.markdown)
    assert returning.session_state["has_user_history"] is False
    assert title in _event_titles(returning)


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


def test_goal_preview_cancel_writes_nothing():
    at = _app()
    before = _goal_count(at)
    at.chat_input[0].set_value("I want to publish my first short video in 30 days").run()
    assert at.session_state["goal_draft"] is not None
    assert any(b.label == "Save goal" for b in at.button)
    next(b for b in at.button if b.label == "Cancel").click().run()
    assert not at.exception, at.exception
    assert at.session_state["goal_draft"] is None
    assert _goal_count(at) == before


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


def _fake_extract_two_events(monkeypatch):
    from goalcal import agent
    from goalcal.llm.action import Action
    from datetime import timedelta

    def fake_extract(session, user_id, text, now, tz, *, client=None):
        day = (now + timedelta(days=1)).replace(second=0, microsecond=0)
        first = day.replace(hour=15, minute=30)
        second = day.replace(hour=20, minute=0)
        return [
            Action(intent="add_event", title="写脚本", start=first, end=first + timedelta(minutes=45), confidence=0.9),
            Action(intent="add_event", title="剪辑", start=second, end=second + timedelta(hours=1), confidence=0.85),
        ]

    monkeypatch.setattr(agent.event_plan, "extract_events", fake_extract)


def _event_titles(at):
    with session_scope() as s:
        return sorted(
            e.title for e in s.scalars(select(Event).where(Event.user_id == at.session_state["user_id"]))
        )


def test_event_draft_panel_confirm_writes_both(monkeypatch):
    _fake_extract_two_events(monkeypatch)
    at = _app()
    before = _event_titles(at)
    at.chat_input[0].set_value("帮我安排进日历").run()
    assert not at.exception, at.exception
    assert at.session_state["event_draft"] is not None
    assert "Calendar preview" in {c.value for c in at.caption}
    assert {"Add to calendar", "Cancel"} <= {b.label for b in at.button}
    assert not at.title  # 预览显示时不渲染首页状态区
    assert _event_titles(at) == before  # 预览不写库

    next(b for b in at.button if b.label == "Add to calendar").click().run()
    assert not at.exception, at.exception
    assert at.session_state["event_draft"] is None
    assert any("Added 2 event(s)" in m.value for m in at.markdown)
    assert _event_titles(at) == sorted(before + ["写脚本", "剪辑"])


def test_event_draft_cancel_writes_nothing(monkeypatch):
    _fake_extract_two_events(monkeypatch)
    at = _app()
    before = _event_titles(at)
    at.chat_input[0].set_value("帮我安排进日历").run()
    next(b for b in at.button if b.label == "Cancel").click().run()
    assert not at.exception, at.exception
    assert at.session_state["event_draft"] is None
    assert any("nothing was added" in m.value for m in at.markdown)
    assert _event_titles(at) == before
    assert _app().session_state["event_draft"] is None


def test_event_draft_new_command_exits_preview_without_writing(monkeypatch):
    _fake_extract_two_events(monkeypatch)
    at = _app()
    before = _event_titles(at)
    at.chat_input[0].set_value("帮我安排进日历").run()
    assert at.session_state["event_draft"] is not None
    at.chat_input[0].set_value("Add review meeting tomorrow 10am").run()
    assert not at.exception, at.exception
    assert at.session_state["event_draft"] is None
    titles = _event_titles(at)
    assert "review meeting" in titles
    assert "写脚本" not in titles and "剪辑" not in titles
    assert len(titles) == len(before) + 1


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
    booking_marker = "已将" if selection == "第二个" else "Booked"
    assert sum(booking_marker in item.value for item in at.markdown) == 1
    assert not any(booking_marker in item.value for item in at.toast)
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


def test_batch_suggestions_book_all():
    at = _app()
    at.chat_input[0].set_value("今天下午有空，把任务都排了").run()
    pending = at.session_state["pending"]
    assert pending["batch"]
    next(b for b in at.button if b.label == "Book all").click().run()
    assert not at.exception, at.exception
    assert at.session_state["pending"] is None


def test_goals_directory_selects_detail_and_archive_keeps_other_goal():
    from goalcal.domain import goals
    at = _app()
    with session_scope() as s:
        second = goals.create_goal(s, at.session_state["user_id"], "Second goal")
        second_id = second.id
    at = _goals(at)
    assert len(at.progress) >= 2
    assert not any(c.value.startswith("Next ·") for c in at.caption)
    assert not any("steps completed" in c.value for c in at.caption)
    next(b for b in at.button if b.key == f"select_goal_{second_id}").click().run()
    assert not at.exception, at.exception
    assert at.session_state["selected_goal_id"] == second_id
    next(b for b in at.button if b.label == "Confirm archive goal").click().run()
    assert not at.exception, at.exception
    assert _goal_count(at) == 2
    with session_scope() as s:
        assert s.get(Goal, second_id).status == "archived"
    assert not any((b.key or "").startswith("select_goal_") for b in at.button)


def test_new_event_command_leaves_candidate_context_and_asks_for_clock(monkeypatch):
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
        assert s.scalar(select(Event).where(Event.user_id == at.session_state["user_id"], Event.title == "review meeting")) is None
    assert "When should I put" in at.session_state["messages"][-1]["content"]


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


def test_fresh_browser_session_resets_but_page_switch_keeps_history():
    """整页重进（新浏览器会话）清空会话回指引首页；应用内切页保留历史。"""
    from goalcal.models import Message

    username = "ui_" + uuid.uuid4().hex[:12]
    invitation = auth.invite()

    at = AppTest.from_file(APP, default_timeout=30).run()
    next(t for t in at.text_input if t.key == "register_name").set_value(username)
    next(t for t in at.text_input if t.label == "Password (at least 12 characters)").set_value("new-user-password-42")
    next(t for t in at.text_input if t.label == "Invitation code").set_value(invitation)
    next(b for b in at.button if b.label == "Register").click().run()
    next(t for t in at.text_input if t.key == "login_name").set_value(username)
    next(t for t in at.text_input if t.key == "login_password").set_value("new-user-password-42")
    next(b for b in at.button if b.label == "Log in").click().run()
    assert not at.exception, at.exception
    assert at.chat_input

    at.chat_input[0].set_value("hmm").run()
    assert not at.exception, at.exception
    assert at.session_state["has_user_history"] is True
    uid = at.session_state["user_id"]

    def _stored():
        with session_scope() as s:
            return s.scalar(
                select(func.count(Message.id)).where(
                    Message.conversation_id == select(Conversation.id).where(
                        Conversation.user_id == uid
                    ).scalar_subquery()
                )
            )

    assert _stored() >= 3  # 欢迎 + 用户消息 + 回复

    # 应用内切页（Goals → Chat）不清历史
    at = _goals(at)
    at = _chat(at)
    assert not at.exception, at.exception
    assert at.session_state["has_user_history"] is True
    assert any(m["content"] == "hmm" for m in at.session_state["messages"])

    # 整页重进（新 AppTest 实例 = 新浏览器会话）：历史消失，回到指引首页
    fresh = AppTest.from_file(APP, default_timeout=30).run()
    next(t for t in fresh.text_input if t.key == "login_name").set_value(username)
    next(t for t in fresh.text_input if t.key == "login_password").set_value("new-user-password-42")
    next(b for b in fresh.button if b.label == "Log in").click().run()
    assert not fresh.exception, fresh.exception
    assert fresh.session_state["has_user_history"] is False
    assert [m["content"] for m in fresh.session_state["messages"]] == [
        "Hi — tell me a goal, or add something to your calendar."
    ]
    assert any(b.key == "home_suggestion_new" for b in fresh.button)  # 空账号显示三层首页建议
    assert _stored() == 1  # DB 里旧消息真实删除，只剩重置后的欢迎消息
