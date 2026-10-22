import json
from datetime import date
from types import SimpleNamespace

from goalcal.data.goal_templates import build_steps, pick_template
from goalcal.llm import plan as plan_mod

def _exam_steps():
    return {
        "steps": [
            {"seq": 1, "milestone": "范围", "title": "整理 IBA6104 考试范围和往年题", "outcome": "资料清单", "est_min": 25, "energy": 0, "kind": "admin"},
            {"seq": 2, "milestone": "诊断", "title": "按主题标记会 / 不熟 / 不会", "outcome": "诊断表", "est_min": 30, "energy": 1, "kind": "deep"},
            {"seq": 3, "milestone": "复习", "title": "复习最弱的 2 个主题", "outcome": "能解释核心概念", "est_min": 40, "energy": 2, "kind": "deep"},
            {"seq": 4, "milestone": "练习", "title": "限时做一套往年题", "outcome": "一套答案", "est_min": 60, "energy": 2, "kind": "deep"},
            {"seq": 5, "milestone": "错题", "title": "整理错题并回补知识点", "outcome": "错题清单", "est_min": 40, "energy": 2, "kind": "deep"},
            {"seq": 6, "milestone": "模拟", "title": "限时模拟并做最终查漏", "outcome": "最终检查清单", "est_min": 60, "energy": 2, "kind": "deep"},
        ]
    }


class _FakeClient:
    def __init__(self, payload):
        self.payload = payload

    def complete_json(self, system, user, temperature=0.0):
        return self.payload


def test_exam_template_is_used_as_fallback():
    assert pick_template("复习IBA6104") == "exam"
    key, steps = build_steps("复习IBA6104", "simple")
    assert key == "exam"
    assert len(steps) == 6
    titles = " ".join(step["title"] for step in steps)
    assert "错题" in titles or "往年题" in titles
    assert "最粗糙的第一版" not in titles


def test_llm_plan_is_used_when_available(monkeypatch):
    monkeypatch.setattr(plan_mod, "settings", SimpleNamespace(llm_ready=True))
    monkeypatch.setattr(plan_mod, "LLMClient", lambda: _FakeClient(_exam_steps()))

    steps = plan_mod.generate_plan(
        "复习IBA6104",
        date(2026, 10, 29),
        today=date(2026, 10, 22),
        complexity="simple",
    )

    assert steps is not None
    assert len(steps) == 6
    assert steps[0]["seq"] == 1
    assert "IBA6104" in steps[0]["title"]


def test_invalid_llm_plan_falls_back(monkeypatch):
    monkeypatch.setattr(plan_mod, "settings", SimpleNamespace(llm_ready=True))
    monkeypatch.setattr(
        plan_mod, "LLMClient", lambda: _FakeClient({"steps": [{"title": ""}]})
    )

    assert (
        plan_mod.generate_plan(
            "复习IBA6104", date(2026, 10, 29), today=date(2026, 10, 22)
        )
        is None
    )


def test_plan_is_none_when_llm_disabled(monkeypatch):
    monkeypatch.setattr(plan_mod, "settings", SimpleNamespace(llm_ready=False))
    assert (
        plan_mod.generate_plan(
            "复习IBA6104", date(2026, 10, 29), today=date(2026, 10, 22)
        )
        is None
    )


def test_too_many_steps_are_fitted_to_complexity(monkeypatch):
    payload = {"steps": _exam_steps()["steps"] * 2}
    monkeypatch.setattr(plan_mod, "settings", SimpleNamespace(llm_ready=True))
    monkeypatch.setattr(plan_mod, "LLMClient", lambda: _FakeClient(payload))

    steps = plan_mod.generate_plan(
        "复习IBA6104",
        date(2026, 10, 29),
        today=date(2026, 10, 22),
        complexity="simple",
    )
    assert steps is not None
    assert len(steps) == 6


def test_revision_request_and_current_steps_reach_prompt(monkeypatch):
    captured = {}

    class _CapturingClient:
        def complete_json(self, system, user, temperature=0.0):
            captured["temperature"] = temperature
            captured["payload"] = json.loads(user)
            return _exam_steps()

    monkeypatch.setattr(plan_mod, "settings", SimpleNamespace(llm_ready=True))
    monkeypatch.setattr(plan_mod, "LLMClient", lambda: _CapturingClient())

    plan_mod.generate_plan(
        "复习IBA6104",
        date(2026, 10, 29),
        today=date(2026, 10, 22),
        complexity="simple",
        instruction="更细一点，重点放在做题",
        current_steps=["1. 整理范围 (20m)"],
    )

    assert captured["temperature"] == 0.0
    assert captured["payload"]["revision_request"] == "更细一点，重点放在做题"
    assert captured["payload"]["current_steps"] == ["1. 整理范围 (20m)"]
