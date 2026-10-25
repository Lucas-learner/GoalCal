"""阶段 5：滚动式拆解。

建目标 = 阶段大纲 + 首批步骤；细化三个触发点（剩余步骤不足自动补、
卡住了、用户指令"拆细一点"）；进度按阶段计算；旧数据（无 outline）兼容。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import select

from goalcal import agent, metrics
from goalcal.domain import goals as goal_svc
from goalcal.domain import planner, refine
from goalcal.llm import plan as plan_mod
from goalcal.models import Checkin, Interaction, Step

TZ = "Asia/Shanghai"
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=ZoneInfo(TZ))  # Wednesday


def _goal_with_defs(session, user, defs, outline=None, template_key="llm"):
    step_defs = [
        {
            "seq": i + 1,
            "milestone": milestone,
            "title": title,
            "outcome": "",
            "est_min": est,
            "energy": 1,
            "kind": "deep",
        }
        for i, (milestone, title, est) in enumerate(defs)
    ]
    return goal_svc.create_goal(
        session, user.id, "My project",
        deadline=NOW.date() + timedelta(days=30),
        step_defs=step_defs, template_key=template_key, outline=outline,
    )


def _refined_rows(session):
    return list(
        session.scalars(
            select(Interaction).where(Interaction.kind == metrics.PLAN_REFINED)
        )
    )


# ------------------------------------------------------------ 建目标：大纲 + 首批


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload

    def complete_json(self, system, user, temperature=0.0):
        return self.payload


def _outline_payload():
    return {
        "outline": ["范围与诊断", "第一轮复习", "练习与错题", "模拟与查漏"],
        "steps": [
            {"milestone": "范围与诊断", "title": "收集考纲和往年题", "outcome": "资料清单", "est_min": 25, "energy": 0, "kind": "admin"},
            {"milestone": "范围与诊断", "title": "按主题标记会/不熟/不会", "outcome": "诊断表", "est_min": 30, "energy": 1, "kind": "deep"},
            {"milestone": "范围与诊断", "title": "多余的第一步", "outcome": "", "est_min": 30, "energy": 1, "kind": "deep"},
            {"milestone": "范围与诊断", "title": "多余的第二步", "outcome": "", "est_min": 30, "energy": 1, "kind": "deep"},
        ],
    }


def test_generate_outline_plan_returns_outline_and_first_batch(monkeypatch):
    monkeypatch.setattr(plan_mod, "settings", SimpleNamespace(llm_ready=True))
    monkeypatch.setattr(plan_mod, "LLMClient", lambda: _FakeClient(_outline_payload()))

    plan = plan_mod.generate_outline_plan(
        "复习IBA6104", NOW.date() + timedelta(days=7), today=NOW.date()
    )

    assert plan is not None
    outline, steps = plan
    assert outline == ["范围与诊断", "第一轮复习", "练习与错题", "模拟与查漏"]
    assert len(steps) == 3  # 首批最多 3 步
    assert all(s["milestone"] == "范围与诊断" for s in steps)
    assert [s["seq"] for s in steps] == [1, 2, 3]


def test_generate_outline_plan_rejects_short_outline(monkeypatch):
    monkeypatch.setattr(plan_mod, "settings", SimpleNamespace(llm_ready=True))
    payload = {"outline": ["唯一阶段"], "steps": [{"title": "做点什么"}]}
    monkeypatch.setattr(plan_mod, "LLMClient", lambda: _FakeClient(payload))

    assert plan_mod.generate_outline_plan("复习IBA6104", NOW.date(), today=NOW.date()) is None


def test_create_goal_with_llm_outline_stores_first_batch_only(session, user, monkeypatch):
    from datetime import date

    monkeypatch.setattr(plan_mod, "settings", SimpleNamespace(llm_ready=True))
    monkeypatch.setattr(plan_mod, "LLMClient", lambda: _FakeClient(_outline_payload()))

    brief = planner.GoalBrief(
        title="复习IBA6104", raw="复习IBA6104",
        deadline=date(2026, 10, 29), complexity="simple",
    )
    goal, source = agent.create_goal_from_brief(session, user.id, brief, NOW, TZ)

    assert source == "llm"
    assert goal_svc.outline_phases(goal) == ["范围与诊断", "第一轮复习", "练习与错题", "模拟与查漏"]
    assert len(goal.steps) == 3
    assert {s.milestone for s in goal.steps} == {"范围与诊断"}


def test_create_goal_template_fallback_also_rolls(session, user):
    from datetime import date

    brief = planner.GoalBrief(
        title="复习IBA6104", raw="复习IBA6104",
        deadline=date(2026, 10, 29), complexity="simple",
    )
    goal, source = agent.create_goal_from_brief(session, user.id, brief, NOW, TZ)

    assert source == "template"
    assert goal.template_key == "exam"
    assert goal_svc.outline_phases(goal) == ["Scope & diagnosis", "First-pass review", "Practice & error log", "Mock & gap check"]
    assert len(goal.steps) == 2
    assert {s.milestone for s in goal.steps} == {"Scope & diagnosis"}


# ------------------------------------------------------------ 自动细化：剩余步骤不足


def test_auto_refine_appends_next_batch_after_logging_progress(session, user):
    goal = goal_svc.create_goal(
        session, user.id, "Publish my first short video",
        deadline=NOW.date() + timedelta(days=30),
    )
    first = sorted(goal.steps, key=lambda s: s.seq)[0]

    reply = agent.handle(session, user.id, f"完成了{first.title}", NOW, TZ)

    assert reply.action.intent == "log_progress"
    session.refresh(goal)
    assert len(goal.steps) == 5  # 首批 2 步 + 模板续批 3 步（Script & storyboard）
    assert "Script & storyboard" in {s.milestone for s in goal.steps}
    assert "added" in reply.text
    rows = _refined_rows(session)
    assert len(rows) == 1
    assert json.loads(rows[0].payload)["auto"] is True


def test_auto_refine_does_nothing_while_two_steps_pending(session, user):
    goal = _goal_with_defs(
        session, user, [("M", "步骤一", 30), ("M", "步骤二", 30), ("M", "步骤三", 30)]
    )

    note = agent.auto_refine_if_low(session, user.id, goal, NOW)

    assert note == ""
    assert len(goal.steps) == 3
    assert _refined_rows(session) == []


def test_schedule_step_refines_when_no_pending_steps(session, user):
    goal = goal_svc.create_goal(
        session, user.id, "Publish my first short video",
        deadline=NOW.date() + timedelta(days=30),
    )
    for s in list(goal.steps):
        goal_svc.complete_step(session, s)
    session.refresh(goal)
    assert goal.status == "active"  # 大纲后续阶段还没拆，不能提前判完成
    assert goal_svc.next_step(goal) is None

    reply = agent.handle(session, user.id, "帮我安排下一步", NOW, TZ)

    # 没有待办步骤时自动补下一批（模板续批 3 步），并给出候选时段
    assert reply.step_id is not None
    assert 1 <= len(reply.candidates) <= 6
    assert "added" in reply.text
    session.refresh(goal)
    assert len(goal.steps) == 5
    rows = _refined_rows(session)
    assert len(rows) == 1 and json.loads(rows[0].payload)["auto"] is True


# ------------------------------------------------------------ 指令触发细化


def test_refine_command_splits_the_next_step(session, user):
    goal = _goal_with_defs(session, user, [("M", "写脚本", 60), ("M", "画分镜", 45)])
    step = sorted(goal.steps, key=lambda s: s.seq)[0]

    reply = agent.handle(session, user.id, "下一步拆细一点", NOW, TZ)

    assert reply.action.intent == "refine_steps"
    session.refresh(goal)
    assert len(goal.steps) == 4  # 规则降级：5 分钟启动 + 先完成一半
    starter = sorted(goal.steps, key=lambda s: s.seq)[0]
    assert starter.id != step.id and starter.est_min == 5
    assert session.get(Step, step.id).status == "todo"  # 原步骤保留
    assert "split" in reply.text
    rows = _refined_rows(session)
    assert len(rows) == 1
    assert rows[0].step_id == step.id
    assert json.loads(rows[0].payload)["auto"] is False


def test_refine_command_targets_the_named_step(session, user):
    goal = _goal_with_defs(session, user, [("M", "写脚本", 60), ("M", "画分镜", 45)])
    storyboard = sorted(goal.steps, key=lambda s: s.seq)[1]

    reply = agent.handle(session, user.id, "把画分镜拆细一点", NOW, TZ)

    assert reply.action.intent == "refine_steps"
    starter = sorted(goal.steps, key=lambda s: s.seq)[1]
    assert starter.est_min == 5 and starter.id != storyboard.id


# ------------------------------------------------------------ refine_upcoming：LLM 与模板


def test_refine_upcoming_llm_uses_history_and_reasons(session, user, monkeypatch):
    goal = _goal_with_defs(
        session, user, [("A", "已完成步骤", 30)], outline=["A", "B", "C"]
    )
    step = goal.steps[0]
    goal_svc.complete_step(session, step, minutes=50)
    session.add(Checkin(step_id=step.id, verdict="not_done", reason="no_time", minutes=0))
    session.flush()

    captured = {}

    class _CapturingClient:
        def complete_json(self, system, user, temperature=0.0):
            captured.update(json.loads(user))
            return {
                "steps": [
                    {"milestone": "B", "title": "下一阶段第一步", "est_min": 20, "energy": 1, "kind": "deep"},
                    {"milestone": "B", "title": "下一阶段第二步", "est_min": 30, "energy": 2, "kind": "research"},
                ]
            }

    monkeypatch.setattr(refine, "settings", SimpleNamespace(llm_ready=True))
    monkeypatch.setattr("goalcal.llm.client.LLMClient", lambda: _CapturingClient())

    created = refine.refine_upcoming(session, goal)

    assert [s.title for s in created] == ["下一阶段第一步", "下一阶段第二步"]
    assert {s.milestone for s in created} == {"B"}
    assert [s.seq for s in created] == [2, 3]
    assert captured["outline"] == ["A", "B", "C"]
    assert captured["done_steps"] == [
        {"title": "已完成步骤", "est_min": 30, "actual_min": 50}
    ]
    assert captured["miss_reasons"] == ["no_time"]


def test_refine_upcoming_falls_back_to_outline_phase_for_unknown_milestone(
    session, user, monkeypatch
):
    goal = _goal_with_defs(session, user, [("A", "已完成步骤", 30)], outline=["A", "B"])
    goal_svc.complete_step(session, goal.steps[0])
    payload = {"steps": [{"milestone": "不存在的阶段", "title": "新步骤", "est_min": 20, "energy": 1, "kind": "deep"}]}

    monkeypatch.setattr(refine, "settings", SimpleNamespace(llm_ready=True))
    monkeypatch.setattr("goalcal.llm.client.LLMClient", lambda: _FakeClient(payload))

    created = refine.refine_upcoming(session, goal)

    assert len(created) == 1
    assert created[0].milestone == "B"  # 大纲外的阶段名回落到下一阶段


def test_refine_upcoming_template_continuation(session, user):
    goal = goal_svc.create_goal(session, user.id, "Publish my first short video")
    for s in list(goal.steps):
        goal_svc.complete_step(session, s)

    created = refine.refine_upcoming(session, goal)

    assert len(created) == 3  # Script & storyboard 的 3 步
    assert {s.milestone for s in created} == {"Script & storyboard"}
    assert min(s.seq for s in created) == 3


def test_refine_upcoming_uses_outline_without_llm_and_template(session, user):
    goal = _goal_with_defs(session, user, [("M", "唯一步骤", 30)], outline=["M", "N"])
    goal_svc.complete_step(session, goal.steps[0])

    created = refine.refine_upcoming(session, goal)
    assert len(created) == 2
    assert all(step.milestone == "N" for step in created)
    assert refine.refine_upcoming(session, goal) == []


# ------------------------------------------------------------ 按阶段进度


def test_progress_counts_done_phases_plus_current_fraction(session, user):
    goal = _goal_with_defs(
        session, user,
        [("A", "a1", 30), ("A", "a2", 30), ("B", "b1", 30), ("B", "b2", 30)],
        outline=["A", "B", "C"],
    )
    steps = sorted(goal.steps, key=lambda s: s.seq)

    goal_svc.complete_step(session, steps[0])
    assert goal_svc.progress(goal) == round(0.5 / 3, 6)

    goal_svc.complete_step(session, steps[1])
    assert goal_svc.progress(goal) == round(1 / 3, 6)

    goal_svc.complete_step(session, steps[2])
    assert goal_svc.progress(goal) == round(1.5 / 3, 6)


def test_phases_without_steps_contribute_nothing(session, user):
    goal = _goal_with_defs(session, user, [("A", "a1", 30)], outline=["A", "B", "C"])
    goal_svc.complete_step(session, goal.steps[0])
    # A 阶段完成，B/C 还没拆出步骤 → 1/3
    assert goal_svc.progress(goal) == round(1 / 3, 6)
    stats = goal_svc.phase_status(goal)
    assert [p["state"] for p in stats] == ["done", "todo", "todo"]


# ------------------------------------------------------------ 旧数据兼容（无 outline）


def test_goal_without_outline_derives_phases_from_milestones(session, user):
    goal = _goal_with_defs(
        session, user, [("M1", "a1", 30), ("M1", "a2", 30), ("M2", "b1", 30)]
    )
    assert goal.outline == ""
    assert goal_svc.phases(goal) == ["M1", "M2"]

    steps = sorted(goal.steps, key=lambda s: s.seq)
    goal_svc.complete_step(session, steps[0])
    goal_svc.complete_step(session, steps[1])
    assert goal_svc.progress(goal) == 0.5

    goal_svc.complete_step(session, steps[2])
    session.refresh(goal)
    assert goal.status == "done"
    assert goal_svc.progress(goal) == 1.0
