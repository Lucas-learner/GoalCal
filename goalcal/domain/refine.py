"""滚动细化的最小版本：把卡住的步骤拆出更小的下一步（阶段 5 会扩展成完整版）。

LLM 可用时拆成 2–3 个小步骤；不可用时按规则补一个 5 分钟启动动作。
原步骤保留不动，新步骤插在它的 seq 前面。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy.orm import Session

from ..config import settings
from ..models import Goal, Step

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
        log.warning("LLM 细化失败(%s)，回退规则拆解", exc)
        return None
    return defs[:3] if len(defs) >= 2 else None


def _rule_defs(step: Step) -> list[dict[str, Any]]:
    defs: list[dict[str, Any]] = [
        {"title": f"5 分钟启动：{step.title}的第一步", "est_min": 5, "energy": 0}
    ]
    if step.est_min > 30:
        defs.append(
            {
                "title": f"{step.title}（先完成一半）",
                "est_min": step.est_min // 2,
                "energy": step.energy,
            }
        )
    return defs


def refine_next_steps(session: Session, goal: Goal, step: Step) -> list[Step]:
    """在卡住的步骤前面插入更小的下一步，返回新步骤（第一个是启动动作）。"""
    defs = _llm_defs(goal, step) or _rule_defs(step)
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
        session.add(new_step)
        created.append(new_step)
    session.flush()
    return created
