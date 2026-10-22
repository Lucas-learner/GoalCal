from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from goalcal.llm.rules import parse_with_rules

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=TZ)  # Wednesday


def test_goal_creation_with_relative_deadline():
    a = parse_with_rules("I want to publish my first short video in 30 days", NOW)
    assert a.intent == "create_goal"
    assert a.deadline.isoformat() == "2026-11-20"
    assert a.title == "publish my first short video"


def test_goal_title_strips_chinese_duration():
    a = parse_with_rules("我想在30天内学会弹钢琴", NOW)
    assert a.intent == "create_goal"
    assert a.title == "学会弹钢琴"
    assert a.deadline is not None


def test_goal_title_strips_chinese_date_adverbial():
    a = parse_with_rules("我要在10月29日前完成课程设计报告", NOW)
    assert a.intent == "create_goal"
    assert a.title == "完成课程设计报告"
    assert a.deadline.isoformat() == "2026-10-29"


def test_chinese_want_to_learn_goal():
    a = parse_with_rules("我要学会弹钢琴", NOW)
    assert a.intent == "create_goal"
    assert a.title == "学会弹钢琴"


def test_add_event_with_weekday_and_time():
    a = parse_with_rules("Add a meeting Wednesday 10am", NOW)
    assert a.intent == "add_event"
    assert a.start.weekday() == 2
    assert a.start.hour == 10
    assert "meeting" in (a.title or "").lower()


def test_add_event_with_duration():
    a = parse_with_rules("Block 90 minutes for writing tomorrow", NOW)
    assert a.intent == "add_event"
    assert a.duration_min == 90


def test_move_event_keeps_target():
    a = parse_with_rules("Move tomorrow's workout to Friday", NOW)
    assert a.intent == "move_event"
    assert "workout" in (a.target or "")
    assert a.start.weekday() == 4


def test_delete_event():
    a = parse_with_rules("Cancel the dentist appointment", NOW)
    assert a.intent == "delete_event"


def test_delete_goal_is_not_mistaken_for_create_goal():
    a = parse_with_rules('delete "track my goal"', NOW)
    assert a.intent == "delete_goal"
    assert a.target == "track my goal"


def test_delete_goal_chinese():
    a = parse_with_rules("删除目标 track my goal", NOW)
    assert a.intent == "delete_goal"
    assert a.target == "track my goal"


def test_delete_goal_unquoted_title_containing_goal():
    a = parse_with_rules("delete track my goal", NOW)
    assert a.intent == "delete_goal"
    assert a.target == "track my goal"


def test_delete_goal_without_name():
    a = parse_with_rules("delete my goal", NOW)
    assert a.intent == "delete_goal"
    assert a.target is None


def test_state_detection():
    assert parse_with_rules("I'm exhausted today", NOW).intent == "set_state"
    assert parse_with_rules("I'm tired", NOW).state == "tired"
    assert parse_with_rules("我有点累", NOW).state == "tired"


def test_progress_report():
    a = parse_with_rules("I finished the script today", NOW)
    assert a.intent == "log_progress"


def test_query():
    a = parse_with_rules("What do I have tomorrow?", NOW)
    assert a.intent == "query_events"


def test_schedule_next_step():
    a = parse_with_rules("帮我安排下一步", NOW)
    assert a.intent == "schedule_step"
    assert a.progress_target is None


def test_schedule_step_with_target():
    a = parse_with_rules("帮我找个时间写脚本", NOW)
    assert a.intent == "schedule_step"
    assert a.progress_target == "写脚本"


def test_unknown_falls_back_to_chat():
    assert parse_with_rules("hello there", NOW).intent == "chat"


# ------------------------------------------------------------ P0-2 回归：中文关键词误判


def test_bangmang_is_not_busy():
    a = parse_with_rules("帮忙加个会议明天10点", NOW)
    assert a.intent == "add_event"
    assert a.start.hour == 10
    assert "会议" in (a.title or "")


def test_jilei_is_not_tired():
    a = parse_with_rules("积累素材明天下午", NOW)
    assert a.intent == "add_event"
    assert a.start.hour == 14


def test_bangwo_anpai_is_add_not_query():
    a = parse_with_rules("帮我安排明天10点开会", NOW)
    assert a.intent == "add_event"
    assert a.start.hour == 10
    assert "开会" in (a.title or "")


def test_help_me_add_is_add_not_goal():
    a = parse_with_rules("help me add a meeting tomorrow 10am", NOW)
    assert a.intent == "add_event"
    assert a.start.hour == 10


def test_help_me_learn_is_still_a_goal():
    a = parse_with_rules("Help me learn guitar in 60 days", NOW)
    assert a.intent == "create_goal"
    assert a.deadline is not None


def test_bare_chinese_datetime_means_add():
    a = parse_with_rules("明天10点开会", NOW)
    assert a.intent == "add_event"
    assert (a.start.month, a.start.day, a.start.hour) == (10, 22, 10)
    assert a.title == "开会"


def test_chinese_state_phrases_still_work():
    assert parse_with_rules("我最近太忙了", NOW).state == "busy"
    assert parse_with_rules("没做，太忙了", NOW).state == "busy"
    assert parse_with_rules("完全没灵感", NOW).state == "uninspired"
    assert parse_with_rules("今天状态很好", NOW).state == "fresh"


def test_chinese_query_still_works():
    assert parse_with_rules("我明天有什么安排", NOW).intent == "query_events"
    assert parse_with_rules("我周五下午有空吗", NOW).intent == "query_events"


# ------------------------------------------------------------ 否定句不算完成


def test_negated_progress_is_not_logged():
    assert parse_with_rules("I did not finish the script", NOW).intent != "log_progress"
    assert parse_with_rules("I haven't done the reading yet", NOW).intent != "log_progress"
    assert parse_with_rules("脚本还没写完", NOW).intent != "log_progress"


def test_progress_target_is_cleaned_to_step_title():
    a = parse_with_rules("I finished the storyboard", NOW)
    assert a.intent == "log_progress"
    assert a.progress_target == "storyboard"


# ------------------------------------------------------------ 签到语境的短回答


def test_checkin_answer_outcomes():
    from goalcal.llm.rules import parse_checkin_answer

    a = parse_checkin_answer("写了一半", NOW)
    assert a.intent == "log_progress" and a.checkin_outcome == "partial"
    a = parse_checkin_answer("done", NOW)
    assert a.intent == "log_progress" and a.checkin_outcome == "done"
    a = parse_checkin_answer("没做，太忙了", NOW)
    assert a.intent == "set_state" and a.state == "busy" and a.checkin_outcome == "none"
    a = parse_checkin_answer("太累了", NOW)
    assert a.intent == "set_state" and a.state == "tired"


def test_checkin_new_command_returns_none():
    from goalcal.llm.rules import parse_checkin_answer

    assert parse_checkin_answer("帮我安排明天10点开会", NOW) is None
    assert parse_checkin_answer("明天10点帮我加个会", NOW) is None
    assert parse_checkin_answer("中午吃了吗", NOW) is None
