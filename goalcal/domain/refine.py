"""滚动细化。

refine_next_steps：把卡住的一个步骤拆成更小的下一步（插在原步骤前）。
refine_upcoming：剩余步骤不够时，按阶段大纲追加下一批 2–3 步（LLM 优先，
模板目标回退到模板顺序的后续步骤）。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..data.goal_templates import TEMPLATES, first_batch, full_steps
from ..i18n import normalize_lang
from ..llm.router import plan_client
from ..models import Checkin, Goal, Step
from .goals import outline_phases

log = logging.getLogger(__name__)


def _plan_llm_client():
    """refine 走 plan 层客户端（与 llm/plan.py 一致）：只配 LLM_API_KEY_PLAN 也能用。"""
    if not settings.llm_enabled:
        return None
    client = plan_client()
    return client if getattr(client, "ready", True) else None

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
    client = _plan_llm_client()
    if client is None:
        return None

    payload = {
        "goal_title": goal.title,
        "stuck_step": step.title,
        "stuck_step_est_min": step.est_min,
    }
    try:
        data = client.complete_json(
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
{"steps":[{"milestone":"...","title":"...","outcome":"...","est_min":30,"energy":1,"kind":"deep"}]}

Rules:
1. milestone must be one of the given outline phases.
2. Each step is one observable action of 5-60 minutes; the first step must be
   doable immediately (lower the barrier if the user was stuck or short on time).
   Strictly keep every step <= 60 minutes: never emit steps exceeding 60 minutes.
   If training or preparation requires extended volume, break it into structured
   blocks (e.g., warmup + main drill, or progressive intervals) rather than a
   single monolithic step over 60 minutes.
3. Calibrate BOTH ways with the history: if actual minutes exceeded estimates, or
   steps were missed for no_time/tired, keep the new steps small; if steps were
   done easily and on time, raise the duration, intensity, or scope.
4. Budget the calendar with days_left: give each remaining phase a share of the
   remaining days, and when the current phase has consumed its share, open the
   next phase and fold unfinished work into it. Phases whose outline name carries
   a hard target (a peak distance, volume, or score) take priority: never open a
   wind-down or wrap-up phase before that target is reached; when days are short,
   compress earlier phases instead of sacrificing the target. If phase_stall is
   present in the payload, the named phase has dragged on — open the next phase now.
   CRITICAL EVENT TIMING: As long as days_left > 0, the target deadline and actual
   event (e.g. race, exam, launch) are strictly in the future. Completing earlier
   steps ahead of schedule does NOT advance the event date. NEVER emit event-day
   execution or post-event wrap-up / recovery steps (e.g. 赛后恢复, 考后复盘) while
   days_left > 0; instead, schedule readiness drills, tapering, or maintenance steps.
5. Repetition is allowed, standing still is not: a repeated drill must state what
   changes this time (more distance/duration/intensity, a higher quality bar, or
   an explicit consolidation purpose). Never emit a step essentially identical
   to an existing one.
6. Readability & format rules:
   - title: clear verb-object phrase, 8-26 Chinese characters (or <=14 English words). Put ONLY the core action in title; NO parenthetical explanations, NO nested quotes, and NO multi-clause "after A, do B (note C)" sentences.
   - outcome: one crisp verifiable result in 10-24 Chinese characters (or <=16 English words); NO slash lists in parentheses.
7. Output language MUST follow target_language ("Simplified Chinese" -> Chinese titles and outcomes; "English" -> English titles and outcomes). When target_language is "auto", match the goal title's language. Output only JSON, no markdown.
"""


def _phase_stats(
    goal: Goal, outline: list[str], completing: Step | None = None
) -> list[dict[str, Any]]:
    """各阶段的已拆分进度，供模型按剩余天数分配阶段预算。"""
    stats = []
    for name in outline:
        steps = [s for s in goal.steps if s.milestone == name]
        stats.append(
            {
                "name": name,
                "done_steps": sum(1 for s in steps if s.status == "done" or s is completing),
                "todo_steps": sum(1 for s in steps if s.status == "todo" and s is not completing),
            }
        )
    return stats


def _phase_stall(goal: Goal, outline: list[str]) -> dict[str, Any] | None:
    """最近 4 步全停在同一阶段、且后面还有没拆步骤的阶段 → 返回停滞提示。"""
    steps = sorted(goal.steps, key=lambda s: s.seq)
    recent = [s.milestone for s in steps[-4:]]
    if len(recent) < 4 or len(set(recent)) != 1:
        return None
    current = recent[0]
    if current not in outline or outline.index(current) >= len(outline) - 1:
        return None
    stepped = {s.milestone for s in goal.steps}
    if all(name in stepped for name in outline[outline.index(current) + 1:]):
        return None
    return {"phase": current, "batches": 2}


def _miss_reasons(session: Session, goal: Goal) -> list[str]:
    rows = session.scalars(
        select(Checkin.reason)
        .join(Step, Checkin.step_id == Step.id)
        .where(Step.goal_id == goal.id, Checkin.reason.is_not(None))
    ).all()
    return sorted({r for r in rows if r})


def _upcoming_llm_defs(
    session: Session,
    goal: Goal,
    today: date | None = None,
    completing: Step | None = None,
    lang: str | None = None,
) -> list[dict[str, Any]] | None:
    """让 LLM 按大纲 + 完成历史 + 错过原因 + 剩余天数生成下一批；失败返回 None。"""
    client = _plan_llm_client()
    if client is None:
        return None

    today = today or date.today()
    outline = outline_phases(goal)
    resolved_lang = normalize_lang(lang)
    target_language = (
        "Simplified Chinese" if resolved_lang == "zh"
        else "English" if resolved_lang == "en"
        else "auto"
    )
    done_steps = [
        {"title": s.title, "est_min": s.est_min, "actual_min": s.minutes_spent or s.est_min}
        for s in goal.steps
        if s.status == "done" or s is completing
    ]
    payload = {
        "goal_title": goal.title,
        "target_language": target_language,
        "outline": outline,
        "days_left": (goal.deadline - today).days if goal.deadline else None,
        "phases": _phase_stats(goal, outline, completing=completing),
        "phase_stall": _phase_stall(goal, outline),
        "done_steps": done_steps,
        "remaining_steps": [s.title for s in goal.steps if s.status == "todo" and s is not completing],
        "miss_reasons": _miss_reasons(session, goal),
    }
    try:
        data = client.complete_json(
            UPCOMING_SYSTEM, json.dumps(payload, ensure_ascii=False), temperature=0.0
        )
        defs = []
        days_left = (goal.deadline - today).days if goal.deadline else None
        _POST_EVENT_RE = re.compile(
            r"(赛后|考后|活动后|比赛复盘|赛后恢复|赛后首次|post-?race|post-?exam|post-?event|after the race)",
            re.I,
        )
        for s in data.get("steps") or []:
            title = str(s.get("title", "")).strip()[:200]
            if not title:
                continue
            # 边界校验 1：截止日前（days_left > 0），实际活动尚未发生，拒绝赛后/考后恢复步骤
            if days_left is not None and days_left > 0 and _POST_EVENT_RE.search(title):
                log.warning(
                    "Step %r emitted post-event recovery before deadline (days_left=%d); rejecting batch",
                    title,
                    days_left,
                )
                return None
            try:
                est = int(s.get("est_min", 30))
            except (TypeError, ValueError):
                est = 30
            # 边界校验 2：超出产品单步时长约束（<=60分钟）时拒绝并回退，不单纯缩小估时数字
            if est > 60:
                log.warning(
                    "Step %r est_min %d exceeds 60m ceiling; rejecting batch rather than shrinking estimate",
                    title,
                    est,
                )
                return None
            defs.append(
                {
                    "milestone": str(s.get("milestone", "")).strip()[:120],
                    "title": title,
                    "outcome": str(s.get("outcome", "")).strip()[:500],
                    "est_min": max(5, est),
                    "energy": max(0, min(int(s.get("energy", 1)), 2)),
                    "kind": str(s.get("kind", "deep")),
                }
            )
    except Exception as exc:  # noqa: BLE001 - 细化失败必须能回退
        log.warning("LLM fallback or validation failure")
        return None
    return defs[:3] if defs else None


def _template_upcoming_defs(goal: Goal, *, lang: str | None = None) -> list[dict[str, Any]]:
    """模板目标按模板顺序取下一批（模板步骤本来有序，天然适合滚动）。"""
    resolved = normalize_lang(lang) or "en"
    if goal.template_key not in TEMPLATES:
        # AI-created outlines still need a usable next phase when the provider
        # is unavailable, over quota, or this transaction already holds a write
        # lock. Instantiate each remaining phase once, without inventing detail.
        existing = {step.milestone for step in goal.steps}
        phase = next((name for name in outline_phases(goal) if name not in existing), None)
        if not phase:
            return []
        if resolved == "zh":
            return [
                {"milestone": phase, "title": f"明确「{phase}」的首个具体动作"[:200],
                 "outcome": "写下一条具体行动与完成标准",
                 "est_min": 15, "energy": 1, "kind": "admin"},
                {"milestone": phase, "title": f"完成「{phase}」的第一项推进"[:200],
                 "outcome": "记录完成结果与下一步要点",
                 "est_min": 30, "energy": 1, "kind": "deep"},
            ]
        return [
            {"milestone": phase, "title": f"Define the next action for {phase}"[:200],
             "outcome": "Write one concrete action and its completion criterion",
             "est_min": 15, "energy": 1, "kind": "admin"},
            {"milestone": phase, "title": f"Complete a first action for {phase}"[:200],
             "outcome": "Record the result and what needs to happen next",
             "est_min": 30, "energy": 1, "kind": "deep"},
        ]
    full = full_steps(goal.template_key, lang=resolved)
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


def _normalize_title(title: str) -> str:
    return re.sub(r"[\s\W_]+", "", title.casefold())


def _drop_duplicate_titles(
    defs: list[dict[str, Any]], existing_titles: list[str]
) -> list[dict[str, Any]]:
    """只丢与已有步骤（或本批已保留步骤）完全同名的标题。

    相似但不相同的标题不能丢：“3公里→4公里”是合法递进，“翻看记录”换个
    距离标记是恶性重复，字符串相似度区分不了——语义判断由 prompt rule 5 负责，
    这里只做零误伤的 exact 护栏。
    """
    seen = {_normalize_title(t) for t in existing_titles}
    kept = []
    for d in defs:
        norm = _normalize_title(d["title"])
        if norm and norm in seen:
            log.info("drop duplicate step title")
            continue
        seen.add(norm)
        kept.append(d)
    return kept


def plan_upcoming_defs(
    session: Session,
    goal: Goal,
    *,
    use_llm: bool = True,
    today: date | None = None,
    completing: Step | None = None,
    lang: str | None = None,
) -> list[dict[str, Any]]:
    """只读地算出下一批 2–3 步的定义（LLM 优先，模板/大纲兜底），不写库。

    完成步骤的路径必须先调它、再写库：flush 之后写事务守卫会拒绝 LLM 调用
    （见 llm/client.py 的 write_transaction 保护），非模板目标只剩占位兜底。
    """
    return (
        _upcoming_llm_defs(session, goal, today, completing=completing, lang=lang)
        if use_llm
        else None
    ) or _template_upcoming_defs(goal, lang=lang)


def refine_upcoming(
    session: Session,
    goal: Goal,
    *,
    use_llm: bool = True,
    defs: list[dict[str, Any]] | None = None,
    today: date | None = None,
    lang: str | None = None,
) -> list[Step]:
    """剩余步骤不足时追加下一批 2–3 步，返回新步骤（追加在末尾）。

    defs 预先规划好（写库前由 plan_upcoming_defs 算出）时直接应用；为 None 时
    现场规划。LLM 优先（大纲 + 已完成步骤及实际耗时 + 错过原因 + 剩余天数）；
    不可用且是模板目标时按模板顺序取后续；都没有则不补（返回 []）。
    use_llm=False（L1 失败后的降级路径）时跳过 LLM，直接走模板/大纲路径。
    与已有步骤近似重复的标题会被丢弃，不进库。
    """
    if defs is None:
        defs = plan_upcoming_defs(session, goal, use_llm=use_llm, today=today, lang=lang)
    defs = _drop_duplicate_titles(defs, [s.title for s in goal.steps])
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
