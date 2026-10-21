from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app.py")


def test_app_boots_without_exception():
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception, at.exception
    assert at.title[0].value.startswith("📆 GoalCal")


def test_chat_turn_creates_a_goal():
    at = AppTest.from_file(APP, default_timeout=30).run()
    at.chat_input[0].set_value("I want to publish my first short video in 30 days").run()
    assert not at.exception, at.exception
    joined = " ".join(m.value for m in at.markdown)
    assert "Goal set" in joined
