"""滚动细化。

refine_next_steps：把卡住的一个步骤拆成更小的下一步（插在原步骤前）。
refine_upcoming：剩余步骤不够时，按阶段大纲追加下一批 2–3 步（LLM 优先，
模板目标回退到模板顺序的后续步骤）。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..data.goal_templates import TEMPLATES, first_batch, full_steps
from ..models import Checkin, Goal, Step
from .goals import outline_phases

log = logging.getLogger(__name__)

REFINE_SYSTEM = """You are GoalCal's step refinement planner.
The user is stuck on one step of their goal. Break THAT step into 2-3 smaller,
immediately doable next actions. Return ONE JSON object:
{"steps":[{"title":"...","est_min":15,"energy":0}]}

Rules:
1. The first step must be a 5-minute starter action that lowers the barrier to begin.
2. Each step is one observable action of 5-30 minutes. Do not put dates or clock times in steps.
3. Use the same language as the goal/step titles. Output only JSON, no markdown.
"""


def _llm_defs(goal: Goal, step: Step) -> list[dict[str, Any]] | None:
    """让 LLM 把当前步骤拆成 2–3 个更小的下一步；失败返回 None。"""
    if not settings.llm_ready:
        return None
    from ..llm.client import LLMClient

    payload = {
        "goal_title": goal.title,
        "stuck_step": step.title,
        "stuck_step_est_min": step.est_min,
    }
    try:
        data = LLMClient().complete_json(
            REFINE_SYSTEM, json.dumps(payload, ensure_ascii=False), temperature=0.0
        )
        defs = [
            {
                "title": str(s.get("title", "")).strip()[:200],
                "est_min": max(5, min(int(s.get("est_min", 15)), 60)),
                "energy": max(0, min(int(s.get("energy", 0)), 2)),
            }
            for s in data.get("steps") or []
        ]
        defs = [d for d in defs if d["title"]]
    except Exception as exc:  # noqa: BLE001 - 细化失败必须能回退
        log.warning("LLM fallback or validation failure")
        return None
    return defs[:3] if len(defs) >= 2 else None


def _rule_defs(step: Step) -> list[dict[str, Any]]:
    defs: list[dict[str, Any]] = [
        {"title": f"5-minute starter: the first step of “{step.title}”", "est_min": 5, "energy": 0}
    ]
    if step.est_min > 30:
        defs.append(
            {
                "title": f"{step.title} (first half)",
                "est_min": step.est_min // 2,
                "energy": step.energy,
            }
        )
    return defs


def refine_next_steps(session: Session, goal: Goal, step: Step, *, use_llm: bool = True) -> list[Step]:
    """在卡住的步骤前面插入更小的下一步，返回新步骤（第一个是启动动作）。

    use_llm=False（L1 失败后的降级路径）时只用确定性规则拆分，不构造 LLM 请求。
    """
    defs = (_llm_defs(goal, step) if use_llm else None) or _rule_defs(step)
    base = step.seq
    for s in goal.steps:
        if s.seq >= base:
            s.seq += len(defs)
    session.flush()

    created = []
    for i, d in enumerate(defs):
        new_step = Step(
            goal_id=goal.id,
            seq=base + i,
            milestone=step.milestone,
            kind=step.kind,
            **d,
        )
        goal.steps.append(new_step)  # append 走关系集合，已加载的 goal.steps 不会变陈旧
        created.append(new_step)
    session.flush()
    return created


# ------------------------------------------------------------------ 滚动续批

UPCOMING_SYSTEM = """You are GoalCal's rolling planner.
The goal has a phase outline; earlier steps are done and the user needs the next
batch. Plan the NEXT 2-3 concrete steps, continuing the current phase or opening
the next one. Return ONE JSON object:
{"steps":[{"milestone":"...","title":"...","est_min":30,"energy":1,"kind":"deep"}]}

Rules:
1. milestone must be one of the given outline phases.
2. Each step is one observable action of 5-60 minutes; the first step must be
   doable immediately (lower the barrier if the user was stuck or short on time).
3. Calibrate with the history: if actual minutes exceeded estimates, or steps were
   missed for no_time/tired, keep the new steps small; if it was easy, keep pace.
4. Use the same language as the goal title. Output only JSON, no markdown.
"""


def _miss_reasons(session: Session, goal: Goal) -> list[str]:
    rows = session.scalars(
        select(Checkin.reason)
        .join(Step, Checkin.step_id == Step.id)
        .where(Step.goal_id == goal.id, Checkin.reason.is_not(None))
    ).all()
    return sorted({r for r in rows if r})


def _upcoming_llm_defs(session: Session, goal: Goal) -> list[dict[str, Any]] | None:
    """让 LLM 按大纲 + 完成历史 + 错过原因生成下一批；失败返回 None。"""
    if not settings.llm_ready:
        return None
    from ..llm.client import LLMClient

    done_steps = [
        {"title": s.title, "est_min": s.est_min, "actual_min": s.minutes_spent}
        for s in goal.steps
        if s.status == "done"
    ]
    payload = {
        "goal_title": goal.title,
        "outline": outline_phases(goal),
        "done_steps": done_steps,
        "remaining_steps": [s.title for s in goal.steps if s.status == "todo"],
        "miss_reasons": _miss_reasons(session, goal),
    }
    try:
        data = LLMClient().complete_json(
            UPCOMING_SYSTEM, json.dumps(payload, ensure_ascii=False), temperature=0.0
        )
        defs = [
            {
                "milestone": str(s.get("milestone", "")).strip()[:120],
                "title": str(s.get("title", "")).strip()[:200],
                "est_min": max(5, min(int(s.get("est_min", 30)), 90)),
                "energy": max(0, min(int(s.get("energy", 1)), 2)),
                "kind": str(s.get("kind", "deep")),
            }
            for s in data.get("steps") or []
        ]
        defs = [d for d in defs if d["title"]]
    except Exception as exc:  # noqa: BLE001 - 细化失败必须能回退
        log.warning("LLM fallback or validation failure")
        return None
    return defs[:3] if defs else None


def _template_upcoming_defs(goal: Goal) -> list[dict[str, Any]]:
    """模板目标按模板顺序取下一批（模板步骤本来有序，天然适合滚动）。"""
    if goal.template_key not in TEMPLATES:
        # AI-created outlines still need a usable next phase when the provider
        # is unavailable, over quota, or this transaction already holds a write
        # lock. Instantiate each remaining phase once, without inventing detail.
        existing = {step.milestone for step in goal.steps}
        phase = next((name for name in outline_phases(goal) if name not in existing), None)
        if not phase:
            return []
        return [
            {"milestone": phase, "title": f"Define the next action for {phase}"[:200],
             "outcome": "Write one concrete action and its completion criterion",
             "est_min": 15, "energy": 1, "kind": "admin"},
            {"milestone": phase, "title": f"Complete a first action for {phase}"[:200],
             "outcome": "Record the result and what needs to happen next",
             "est_min": 30, "energy": 1, "kind": "deep"},
        ]
    full = full_steps(goal.template_key)
    used = {s.title for s in goal.steps}
    idx = 0
    while idx < len(full) and full[idx]["title"] in used:
        idx += 1
    return first_batch(full[idx:])


def _next_phase(goal: Goal) -> str:
    """下一批步骤默认归属的阶段：当前最后一步的阶段之后的那一个。"""
    outline = outline_phases(goal)
    steps = sorted(goal.steps, key=lambda s: s.seq)
    last_ms = steps[-1].milestone if steps else ""
    if outline:
        if last_ms in outline:
            return outline[min(outline.index(last_ms) + 1, len(outline) - 1)]
        return outline[0]
    return last_ms or "推进"


def refine_upcoming(session: Session, goal: Goal, *, use_llm: bool = True) -> list[Step]:
    """剩余步骤不足时追加下一批 2–3 步，返回新步骤（追加在末尾）。

    LLM 优先（大纲 + 已完成步骤及实际耗时 + 错过原因）；
    不可用且是模板目标时按模板顺序取后续；都没有则不补（返回 []）。
    use_llm=False（L1 失败后的降级路径）时跳过 LLM，直接走模板/大纲路径。
    """
    defs = (_upcoming_llm_defs(session, goal) if use_llm else None) or _template_upcoming_defs(goal)
    if not defs:
        return []

    outline = outline_phases(goal)
    fallback_ms = _next_phase(goal)
    base = max((s.seq for s in goal.steps), default=0) + 1
    created = []
    for i, d in enumerate(defs):
        milestone = d.get("milestone") or fallback_ms
        if outline and milestone not in outline:
            milestone = fallback_ms
        new_step = Step(
            goal_id=goal.id,
            seq=base + i,
            milestone=milestone,
            title=d["title"],
            outcome=d.get("outcome", ""),
            est_min=d["est_min"],
            energy=d["energy"],
            kind=d["kind"] if d["kind"] in {"deep", "admin", "physical", "social", "research"} else "deep",
        )
        goal.steps.append(new_step)
        created.append(new_step)
    session.flush()
    return created
