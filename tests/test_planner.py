from datetime import date

from goalcal.data.goal_templates import build_steps, pick_template
from goalcal.domain import planner


def test_chinese_goal_is_assessed_as_complex_skill():
    brief = planner.assess("学会弹钢琴", None, "我要学会弹钢琴", today=date(2026, 10, 22))

    assert brief.complexity == "complex"
    assert brief.deadline is None
    assert brief.level == ""
    assert not planner.ready(brief)
    assert "确认" in planner.clarification_text(brief)


def test_goal_with_deadline_and_specific_outcome_is_ready():
    brief = planner.assess(
        "发布第一条视频",
        date(2026, 11, 20),
        "I want to publish my first short video in 30 days",
        today=date(2026, 10, 22),
    )
    assert planner.ready(brief)


def test_answers_fill_level_deadline_and_target():
    brief = planner.assess("学会弹钢琴", None, "我要学会弹钢琴", today=date(2026, 10, 22))
    brief = planner.apply_answer(
        brief, "零基础，3 个月，能弹一首简单曲子", today=date(2026, 10, 22)
    )

    assert brief.level == "零基础"
    assert brief.deadline == date(2027, 1, 20)
    assert brief.target == "弹一首简单曲子"
    assert planner.ready(brief)


def test_fill_defaults_after_vague_answer():
    brief = planner.assess("学会弹钢琴", None, "我要学会弹钢琴", today=date(2026, 10, 22))
    brief = planner.fill_defaults(
        planner.apply_answer(brief, "随便", today=date(2026, 10, 22)),
        today=date(2026, 10, 22),
    )

    assert brief.deadline == date(2027, 1, 20)
    assert brief.level == "零基础"
    assert brief.target
    assert brief.assumptions


def test_duration_parser_handles_chinese_and_english():
    assert planner.parse_duration_days("3 个月") == 90
    assert planner.parse_duration_days("三个月") == 90
    assert planner.parse_duration_days("半年") == 182
    assert planner.parse_duration_days("2 weeks") == 14
    assert planner.parse_duration_days("90 days") == 90


def test_month_day_is_not_a_duration():
    # 回归（P0-5）："10 月 29 日"曾被当成 "10 个月" → 截止日跑了 300 天
    assert planner.parse_duration_days("10 月 29 日") is None
    assert planner.parse_duration_days("10月29号") is None
    today = date(2026, 10, 21)
    assert planner.parse_explicit_date("10 月 29 日", today) == date(2026, 10, 29)
    assert planner.parse_explicit_date("3 月 5 日", today) == date(2027, 3, 5)


def test_apply_answer_prefers_explicit_month_day_over_duration():
    brief = planner.GoalBrief(title="复习IBA6104", raw="复习IBA6104")
    updated = planner.apply_answer(brief, "10 月 29 日", today=date(2026, 10, 21))
    assert updated.deadline == date(2026, 10, 29)


def test_assess_reads_chinese_month_day_deadline_from_raw():
    brief = planner.assess(
        "复习IBA6104", None, "10月29日前复习完IBA6104", today=date(2026, 10, 21)
    )
    assert brief.deadline == date(2026, 10, 29)


def test_piano_template_beats_generic_learn_keyword():
    assert pick_template("学会弹钢琴") == "piano"


def test_complexity_changes_step_count():
    _, simple = build_steps("学会弹钢琴", "simple")
    _, standard = build_steps("学会弹钢琴", "standard")
    _, complex_ = build_steps("学会弹钢琴", "complex")

    assert len(simple) == 6
    assert len(standard) == 8
    assert len(complex_) == 12
    assert [s["seq"] for s in simple] == list(range(1, 7))


def test_exam_goal_asks_for_exam_date_and_accepts_short_deadline():
    brief = planner.assess(
        "复习IBA6104", None, "复习IBA6104", today=date(2026, 10, 22)
    )

    assert not planner.ready(brief)
    assert "考试时间" in planner.clarification_text(brief)
    assert planner.quick_options(brief)[0] == "7 天后"

    brief = planner.apply_answer(brief, "7 天后", today=date(2026, 10, 22))
    assert brief.deadline == date(2026, 10, 29)
    assert planner.ready(brief)
    assert brief.complexity == "simple"
