"""LLM 目标拆解：输出结构化步骤，执行层负责校验和写库。

LLM 不直接改数据库；这里只返回可校验的 JSON，失败就返回 None，
由调用方回退到确定性模板。
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from pydantic import BaseModel, ValidationError, field_validator

from ..config import settings
from .client import LLMClient

log = logging.getLogger(__name__)

PLAN_PROMPT_VERSION = "plan-v2"
KINDS = {"deep", "admin", "physical", "social", "research"}
MAX_BY_COMPLEXITY = {"simple": 6, "standard": 9, "complex": 12}
MIN_BY_COMPLEXITY = {"simple": 4, "standard": 5, "complex": 7}

PLAN_SYSTEM = """You are GoalCal's goal decomposition planner.
Today is {now} ({tz}). Weekday: {weekday}.

Decompose the user's goal into concrete, schedulable steps. Return ONE JSON object:
{{"steps":[{{"seq":1,"milestone":"...","title":"...","outcome":"...","est_min":30,"energy":1,"kind":"deep"}}]}}

Rules:
1. Choose 4-12 steps based on the goal's complexity and the time available.
   - simple / short deadline: 4-6 steps
   - standard: 5-9 steps
   - complex / long horizon: 7-12 steps
2. Every step must be specific to THIS goal. Never reuse generic project wording such as
   "write a visible outcome", "find the biggest uncertainty", or "make a rough first version"
   unless the goal is actually a project.
3. One step = one observable action that can fit in 15-60 minutes. Do not put dates or
   clock times in steps; scheduling is a separate system.
4. Each step needs a milestone (a short phase name), an observable outcome, est_min,
   energy (0=low, 1=medium, 2=deep focus), and kind:
   deep | admin | physical | social | research.
5. For exam / course review goals, plan like a good tutor: collect scope and past papers,
   inventory topics, diagnose weak areas, review by topic, practice under time, review
   mistakes, then do a final recall/check.
6. For skill-learning goals, include baseline, core drills, guided practice, independent
   performance, feedback, and a small final output.
7. Use the same language as the goal title. Chinese goal -> Chinese steps.
8. seq must start at 1 and be contiguous. Output only JSON, no markdown.
9. If current_steps is provided, revise that plan instead of starting from scratch. Keep
   steps that still fit; change only what the revision_request asks for.
10. revision_request is a hard constraint. If it asks for shorter steps, fewer steps,
    more practice, a different focus, etc., apply it explicitly and do not ignore it.
11. Do not use placeholder labels such as "Topic A", "Part B", or "Step 1"; write
    descriptive titles instead.
"""


class PlanStep(BaseModel):
    seq: int = 0
    milestone: str
    title: str
    outcome: str = ""
    est_min: int = 45
    energy: int = 1
    kind: str = "deep"

    @field_validator("outcome", mode="before")
    @classmethod
    def _outcome(cls, value: Any) -> str:
        return "" if value is None else str(value)

    @field_validator("title")
    @classmethod
    def _title(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("must not be empty")
        return value[:200]

    @field_validator("milestone")
    @classmethod
    def _milestone(cls, value: str) -> str:
        value = (value or "").strip()
        if not value:
            raise ValueError("must not be empty")
        return value[:120]

    @field_validator("est_min")
    @classmethod
    def _minutes(cls, value: int) -> int:
        return max(15, min(int(value), 90))

    @field_validator("energy")
    @classmethod
    def _energy(cls, value: int) -> int:
        return max(0, min(int(value), 2))

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        value = (value or "deep").strip().lower()
        return value if value in KINDS else "deep"


class Plan(BaseModel):
    steps: list[PlanStep]


def generate_plan(
    title: str,
    deadline: date | None,
    *,
    today: date | None = None,
    level: str = "",
    target: str = "",
    complexity: str = "standard",
    raw: str = "",
    tz: str = "Asia/Shanghai",
    instruction: str = "",
    current_steps: list[str] | None = None,
) -> list[dict[str, Any]] | None:
    """调用 LLM 生成计划；未启用、失败或校验不通过时返回 None。

    instruction/current_steps 用于“有目的地修改计划”，而不是反复抽卡；
    相同输入下 temperature=0，尽量减少无意义漂移。
    """
    if not settings.llm_ready:
        return None

    today = today or date.today()
    days_left = (deadline - today).days if deadline else None
    payload = {
        "goal_title": title,
        "deadline": deadline.isoformat() if deadline else None,
        "days_left": days_left,
        "complexity": complexity,
        "current_level": level or "unspecified",
        "target_outcome": target or "unspecified",
        "original_user_text": raw,
        "current_steps": current_steps or None,
        "revision_request": instruction or None,
    }
    system = PLAN_SYSTEM.format(
        now=today.isoformat(), tz=tz, weekday=today.strftime("%A")
    )
    try:
        data = LLMClient().complete_json(
            system,
            json.dumps(payload, ensure_ascii=False),
            temperature=0.0,
        )
        plan = Plan.model_validate(data)
    except ValidationError as exc:
        log.warning("LLM 计划结构校验失败: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - 拆解失败必须能回退
        log.warning("LLM 计划生成失败(%s)，回退模板", exc)
        return None

    steps = _clean_steps(plan.steps, complexity)
    if steps is None:
        return None
    return steps


def _clean_steps(steps: list[PlanStep], complexity: str) -> list[dict[str, Any]] | None:
    minimum = MIN_BY_COMPLEXITY.get(complexity, 4)
    maximum = MAX_BY_COMPLEXITY.get(complexity, 12)
    if len(steps) < minimum:
        log.warning("LLM 计划步骤太少: %s < %s", len(steps), minimum)
        return None
    if len(steps) > maximum:
        steps = _fit_steps(steps, maximum)

    out = []
    for seq, step in enumerate(steps, 1):
        out.append(
            {
                "seq": seq,
                "milestone": step.milestone,
                "title": step.title,
                "outcome": step.outcome.strip()[:200],
                "est_min": step.est_min,
                "energy": step.energy,
                "kind": step.kind,
            }
        )
    return out


def _fit_steps(steps: list[PlanStep], maximum: int) -> list[PlanStep]:
    """保留首尾，均匀抽掉中间步骤，确保复杂度不会把计划撑爆。"""
    if maximum >= len(steps):
        return steps
    if maximum <= 1:
        return steps[:1]
    indexes = [round(i * (len(steps) - 1) / (maximum - 1)) for i in range(maximum)]
    indexes = sorted(set(indexes))
    return [steps[i] for i in indexes]
