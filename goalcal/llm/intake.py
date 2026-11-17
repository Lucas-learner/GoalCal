"""Pre-draft goal intake: title distillation & boundary-aware calibration.

Runs read-only before plan generation:
1. Distills conversational user descriptions into a clean, actionable goal title.
2. Fast-paths clear & feasible goals directly to draft generation.
3. Triggers a single concise calibration step only when there is a real blocker
   (scope vs. time mismatch, vague target, or missing critical baseline), while
   respecting immovable external dates and keeping the final decision with the user.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from ..config import settings
from ..domain import planner
from ..i18n import normalize_lang, pick
from .router import router

log = logging.getLogger(__name__)

CONFLICT_KINDS = ("none", "scope_vs_time", "vague_target", "missing_baseline")

INTAKE_SYSTEM = """You are GoalCal's goal intake & feasibility calibrator.
Today is {now} ({tz}).

The user just typed a free-form goal description (and optionally picked a target deadline).
Your job is NOT to write steps yet. Instead, output ONE JSON object:
{{
  "needs_calibration": false,
  "conflict_kind": "none",
  "suggested_title": "concise goal title",
  "suggested_outcome": "short verifiable deliverable",
  "reason": "",
  "questions": []
}}

When needs_calibration is true, include 1-3 targeted questions with 2-3 crisp options each:
{{
  "needs_calibration": true,
  "conflict_kind": "scope_vs_time | vague_target | missing_baseline",
  "suggested_title": "concise goal title",
  "suggested_outcome": "short verifiable deliverable",
  "reason": "One calm, neutral sentence explaining the missing baseline or time/scope tension.",
  "questions": [
    {{
      "id": "baseline",
      "prompt": "Short question about current starting level or progress",
      "options": [
        {{
          "id": "opt1",
          "label": "Short selectable answer",
          "suggested_title": null,
          "suggested_deadline": null,
          "adjusted_scope": null
        }}
      ]
    }}
  ]
}}

Rules across ALL domains (exams, software builds, creative projects, skills, work deliverables, fitness, etc.):
1. ALWAYS produce a clean, scannable suggested_title (4-18 Chinese chars or 2-7 English words).
   Strip conversational framing ("我想", "能", "会", "目前只有", "I want to", "right now I...") and background excuses,
   while preserving key numbers/specs that define the target (e.g. "完成全程马拉松", "雅思 7.5 冲刺备考", "SaaS MVP 上线", "整理两年报销发票").
2. Keep needs_calibration=false (fast path) whenever the goal is clear and plausible within days_left
   and weekly_available_hours. Do NOT over-interrogate straightforward tasks or reasonable plans.
3. CRITICAL — never assume starting level/progress if omitted:
   If the user describes an ambitious, quantitative, exam, multi-part project, or endurance goal WITHOUT stating
   their current level or progress (e.g. "一个月后跑全马" without current distance, "下个月考雅思7.5" without current score,
   "两周上线SaaS" without current progress), set needs_calibration=true and include a question (`id: "baseline"`)
   FIRST asking their current starting point/progress before jumping to conclusions! Wait to judge feasibility neutrally
   ("取决于目前基础…") rather than assuming they are a beginner.
4. Set needs_calibration=true when there is a genuine blocker:
   - missing_baseline: Critical starting level or existing progress is omitted and changes step 1 & feasibility.
   - scope_vs_time: Stated starting baseline/scope vs days_left or weekly_available_hours is visibly tight.
   - vague_target: Open-ended wish with no observable finish line AND no baseline (e.g. "学好钢琴", "improve coding").
5. Respect boundaries & user agency:
   - Never lecture, scold, or act as a doctor/authority. State time/scope trade-offs neutrally in 1 sentence.
   - When deadline tightness or baseline is involved, include `id: "deadline_type"` with options `id: "fixed"` (FIXED external event/exam/launch) and `id: "flexible"` (FLEXIBLE target), AND a direction question offering:
     (a) a realistic narrowed/staged scope if tight or fixed (fill suggested_title + adjusted_scope, suggested_deadline=null),
     (b) an extended realistic horizon if flexible (fill suggested_deadline as YYYY-MM-DD > today + suggested_title).
     Do NOT include a "keep original as-is" option inside questions (the UI already provides a dedicated override button).
   - Keep JSON compact (max 3 questions, 2-3 concise options each).
   - All text MUST follow target_language ("Simplified Chinese" -> Chinese; "English" -> English).
   - Output ONLY valid JSON, no markdown.
"""


class IntakeOption(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    label: str = Field(min_length=1, max_length=120)
    suggested_title: str | None = Field(default=None, max_length=120)
    suggested_deadline: str | None = Field(default=None, max_length=16)
    adjusted_scope: str | None = Field(default=None, max_length=400)


class IntakeQuestion(BaseModel):
    id: str = Field(min_length=1, max_length=40)
    prompt: str = Field(min_length=1, max_length=200)
    options: list[IntakeOption] = Field(min_length=1, max_length=6)


class GoalIntakePayload(BaseModel):
    needs_calibration: bool = False
    conflict_kind: Literal["none", "scope_vs_time", "vague_target", "missing_baseline"] = "none"
    suggested_title: str = Field(min_length=1, max_length=120)
    suggested_outcome: str = Field(default="", max_length=240)
    reason: str = Field(default="", max_length=320)
    questions: list[IntakeQuestion] = Field(default_factory=list, max_length=4)

    @field_validator("suggested_outcome", "reason", mode="before")
    @classmethod
    def _coerce_none_str(cls, value: Any) -> str:
        return "" if value is None else str(value)

    @field_validator("questions", mode="before")
    @classmethod
    def _coerce_none_list(cls, value: Any) -> list[Any]:
        return [] if value is None else value


_LEAD_PHRASE = re.compile(
    r"^\s*(?:我?(?:想要?|我要|希望|打算|准备|计划|需要)(?:在[^，。；\n]{1,18}[内里前]?)?|"
    r"(?:帮我|请帮我|请)(?:安排|规划|拆解)?|"
    r"i\s+(?:want|would like|need|plan|hope)\s+to|"
    r"help\s+me\s+(?:to\s+)?)\s*",
    re.I,
)
_BG_CLAUSE = re.compile(
    r"^(?:但是?|不过|目前|现在|其实|当前|眼下|平时|之前|本来|currently|right now|at the moment|but|however)\b",
    re.I,
)
_FULL_SCOPE_MARKERS = re.compile(
    r"从零|零基础|完全不会|几乎不|还没定|全栈|完整|全部|整套|系统掌握|所有|"
    r"from scratch|beginner|complete\s+system|full[- ]featured|entire|all\s+features",
    re.I,
)
_HIGH_AMBITION_MARKERS = re.compile(
    r"雅思|托福|考研|考公|资格证|认证|论文|答辩|saas|上线|发布完整|独立开发|马拉松|半马|全马|精通|流利|转型|转行|"
    r"ielts|toefl|thesis|dissertation|saas|production app|marathon|fluent|master|career switch",
    re.I,
)
_VAGUE_SKILL_MARKERS = re.compile(
    r"^(?:我?(?:想|想要|要|打算|希望))?(?:学会|学好|掌握|提升|提高|练好|学一下|学习|"
    r"(?:learn|master|improve|get better at)\b)",
    re.I,
)


def extract_clean_title(description: str, *, lang: str = "zh") -> str:
    """Turn free-form user prose into a compact title without dropping core subject matter."""
    raw = re.sub(r"\s+", " ", (description or "").strip())
    if not raw:
        return pick(lang, "新目标", "New goal")

    clauses = [c.strip(" ，,。.;；:：!！?？") for c in re.split(r"[，,。.;；\n]+", raw) if c.strip()]
    core = clauses[0] if clauses else raw
    for candidate in clauses:
        if not _BG_CLAUSE.search(candidate):
            core = candidate
            break

    cleaned = _LEAD_PHRASE.sub("", core).strip(" ，,。.;；:：")
    cleaned = re.sub(
        r"^(?:在\s*)?[半零一二两三四五六七八九十百\d]+\s*(?:天|周|星期|礼拜|个?月|年)(?:之)?(?:内|后|前)\s*",
        "",
        cleaned,
    )
    cleaned = re.sub(r"^(?:可以|能够|能|会|去)\s*", "", cleaned)
    cleaned = re.sub(r"^(?:in|within|by)\s+\d+\s+(?:days?|weeks?|months?)\s+", "", cleaned, flags=re.I)
    cleaned = cleaned.strip() or planner.normalize_title(raw) or raw
    if len(cleaned) > 36:
        cleaned = cleaned[:36].rstrip()
    return cleaned


_BASELINE_PRESENT = re.compile(
    r"(?:目前|现在|当前|平时|已经|已有|考过|跑过|做过|写过|学过|零基础|没基础|从零|初学|新手|已有原型|写好|定题|"
    r"\bcurrently\b|\bright now\b|\balready\b|\bbeginner\b|\bfrom scratch\b|\bno experience\b|\bsome experience\b)",
    re.I,
)


def _baseline_question(text: str, lang: str) -> dict[str, Any]:
    low = text.lower()
    if any(k in low for k in ("跑", "马拉松", "半马", "全马", "marathon", "run", "swim", "游泳")):
        return {
            "id": "baseline",
            "prompt": pick(lang, "目前这部分的基础如何？", "What is your current starting level?"),
            "options": [
                {"id": "low", "label": pick(lang, "刚起步 / 目前单次 5km 以内", "Beginner / under 5km right now"), "suggested_title": None, "suggested_deadline": None, "adjusted_scope": pick(lang, "当前基础：刚起步（5km 以内）。", "Current baseline: beginner (under 5km).")},
                {"id": "mid", "label": pick(lang, "有一定基础 / 跑过 10–21km", "Intermediate / 10–21km base"), "suggested_title": None, "suggested_deadline": None, "adjusted_scope": pick(lang, "当前基础：具备 10–21km 耐力基础。", "Current baseline: 10–21km endurance base.")},
                {"id": "high", "label": pick(lang, "基础较扎实 / 已接近目标距离", "Experienced / near target distance"), "suggested_title": None, "suggested_deadline": None, "adjusted_scope": pick(lang, "当前基础：较扎实，重点做赛前巩固与保持。", "Current baseline: strong, focusing on final tune-up.")},
            ],
        }
    if any(k in low for k in ("雅思", "托福", "考研", "考公", "考试", "资格证", "ielts", "toefl", "exam")):
        return {
            "id": "baseline",
            "prompt": pick(lang, "目前模考或基础大概在什么位置？", "Where is your current level or mock score?"),
            "options": [
                {"id": "low", "label": pick(lang, "零基础 / 刚入门准备", "Starting fresh / beginner"), "suggested_title": None, "suggested_deadline": None, "adjusted_scope": pick(lang, "当前基础：零基础刚入门。", "Current baseline: starting fresh.")},
                {"id": "mid", "label": pick(lang, "有基础，距目标还差一小截", "Have basics, moderate gap to target"), "suggested_title": None, "suggested_deadline": None, "adjusted_scope": pick(lang, "当前基础：有基础，重点查漏补缺提分。", "Current baseline: solid basics, closing moderate gap.")},
                {"id": "high", "label": pick(lang, "已接近目标水平，主要冲刺模考", "Near target score, final sprint"), "suggested_title": None, "suggested_deadline": None, "adjusted_scope": pick(lang, "当前基础：已接近目标，聚焦限时模考冲刺。", "Current baseline: near target, focusing on timed practice.")},
            ],
        }
    if any(k in low for k in ("saas", "app", "上线", "开发", "论文", "thesis", "build", "launch", "project")):
        return {
            "id": "baseline",
            "prompt": pick(lang, "目前项目/文稿的进展到哪一步了？", "What is your current progress so far?"),
            "options": [
                {"id": "low", "label": pick(lang, "完全从零开始，还没动工", "Starting from zero"), "suggested_title": None, "suggested_deadline": None, "adjusted_scope": pick(lang, "当前进度：从零起步，尚未搭好框架。", "Current progress: starting from zero.")},
                {"id": "mid", "label": pick(lang, "已有大纲 / 原型或部分模块", "Have outline, prototype, or core part"), "suggested_title": None, "suggested_deadline": None, "adjusted_scope": pick(lang, "当前进度：已有原型或大纲，重点完成核心闭环。", "Current progress: outline/prototype ready.")},
                {"id": "high", "label": pick(lang, "主体已完成，主要差收尾测试", "Mostly built, polishing & wrapping up"), "suggested_title": None, "suggested_deadline": None, "adjusted_scope": pick(lang, "当前进度：主体已完成，聚焦收尾交付。", "Current progress: mostly complete, wrapping up.")},
            ],
        }
    return {
        "id": "baseline",
        "prompt": pick(lang, "目前这部分的基础或进度如何？", "What is your current level or progress?"),
        "options": [
            {"id": "low", "label": pick(lang, "刚开始 / 尚无基础", "Just starting / beginner"), "suggested_title": None, "suggested_deadline": None, "adjusted_scope": pick(lang, "当前基础：刚起步。", "Current baseline: beginner.")},
            {"id": "mid", "label": pick(lang, "有一定基础 / 已推进部分", "Some experience / partial progress"), "suggested_title": None, "suggested_deadline": None, "adjusted_scope": pick(lang, "当前基础：有一定基础或部分进度。", "Current baseline: intermediate / partial progress.")},
            {"id": "high", "label": pick(lang, "接近目标，主要做最后冲刺", "Close to target, final push"), "suggested_title": None, "suggested_deadline": None, "adjusted_scope": pick(lang, "当前基础：接近目标，以巩固冲刺为主。", "Current baseline: near target, final sprint.")},
        ],
    }


def _valid_future_iso(value: str | None, today: date) -> str | None:
    if not value:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed.isoformat() if parsed > today else None


def assess_intake_rules(
    description: str,
    deadline: date | None,
    *,
    today: date | None = None,
    weekly_available_minutes: int | None = None,
    lang: str = "zh",
) -> dict[str, Any]:
    """Deterministic fallback for pre-draft intake across general domains."""
    today = today or date.today()
    resolved_lang = normalize_lang(lang) or ("zh" if re.search(r"[\u4e00-\u9fff]", description) else "en")
    suggested_title = extract_clean_title(description, lang=resolved_lang)
    text = (description or "").strip()
    days_left = (deadline - today).days if deadline is not None else None

    # 1. Check scope vs time tension or missing baseline on ambitious goals across domains
    is_tight_horizon = days_left is not None and days_left <= 35
    is_very_tight = days_left is not None and days_left <= 14
    has_big_gap = bool(_FULL_SCOPE_MARKERS.search(text) and (_HIGH_AMBITION_MARKERS.search(text) or is_tight_horizon))
    has_explicit_baseline = bool(_BASELINE_PRESENT.search(text)) or bool(planner.infer_level(text))
    missing_baseline_for_ambitious = bool(_HIGH_AMBITION_MARKERS.search(text) and not has_explicit_baseline)
    feature_chunks = len(re.findall(r"[、,，+＋&/]|和|以及|\band\b", text, flags=re.I))
    heavy_bundle_in_short_window = is_very_tight and feature_chunks >= 2 and len(text) >= 18
    numeric_leap = re.search(r"(\d+(?:\.\d+)?)\s*(?:km|公里|小时|页|章|分).*?(\d+(?:\.\d+)?)\s*(?:km|公里|小时|页|章|分)?", text, flags=re.I)
    has_large_leap = False
    if numeric_leap:
        try:
            a, b = float(numeric_leap.group(1)), float(numeric_leap.group(2))
            has_large_leap = (a > 0 and b / max(a, 0.1) >= 4) or (b > 0 and a / max(b, 0.1) >= 4)
        except ValueError:
            has_large_leap = False

    low_weekly_capacity = weekly_available_minutes is not None and 0 < weekly_available_minutes <= 180 and _HIGH_AMBITION_MARKERS.search(text) and is_tight_horizon

    is_tight_scope = (is_tight_horizon and (has_big_gap or has_large_leap or heavy_bundle_in_short_window)) or low_weekly_capacity
    if is_tight_scope or missing_baseline_for_ambitious:
        longer_deadline = (today + timedelta(days=90)).isoformat()
        window_label = pick(resolved_lang, f" {days_left} 天", f"{days_left} days") if days_left is not None else pick(resolved_lang, "当前周期", "this timeline")
        conflict_kind = "scope_vs_time" if is_tight_scope else "missing_baseline"
        reason = (
            pick(
                resolved_lang,
                f"「{suggested_title}」在{window_label}内的可行性很大程度取决于你目前的基础，先确认起点和期限灵活性再安排更稳妥。",
                f'What fits for "{suggested_title}" in {window_label} depends heavily on your current baseline — clarifying your starting point and deadline flexibility helps build a realistic plan.',
            )
            if missing_baseline_for_ambitious
            else pick(
                resolved_lang,
                f"在{window_label}内完成「{suggested_title}」全部跨度可能偏紧，先确认时间性质和优先方向再出计划更稳妥。",
                f'Fitting the full scope of "{suggested_title}" into {window_label} looks tight; aligning deadline flexibility and scope first will make the plan realistic.',
            )
        )
        questions: list[dict[str, Any]] = []
        if not has_explicit_baseline:
            questions.append(_baseline_question(text, resolved_lang))
        questions.extend([
            {
                "id": "deadline_type",
                "prompt": pick(resolved_lang, "这个截止时间是什么性质？", "How fixed is this deadline?"),
                "options": [
                    {
                        "id": "fixed",
                        "label": pick(resolved_lang, "固定日程（比赛 / 考试 / 发布日）", "Fixed date (exam / launch / event)"),
                        "suggested_title": None,
                        "suggested_deadline": None,
                        "adjusted_scope": pick(resolved_lang, "截止日期为不可移动的固定外部事件，请优先围绕高性价比核心范围做安全安排。", "Deadline is a fixed external event; prioritize highest-impact core scope safely."),
                    },
                    {
                        "id": "flexible",
                        "label": pick(resolved_lang, "弹性期限（希望的参考时间）", "Flexible target timeline"),
                        "suggested_title": None,
                        "suggested_deadline": None,
                        "adjusted_scope": pick(resolved_lang, "时间线可适当调整以保证质量与节奏。", "Timeline can flex for sustainable pacing."),
                    },
                ],
            },
            {
                "id": "direction",
                "prompt": pick(resolved_lang, "希望如何设定本阶段目标？", "How should we scope this plan?"),
                "options": [
                    {
                        "id": "stage_scope",
                        "label": pick(resolved_lang, "日期不变，先锁定本阶段可达成核心目标", "Keep date, focus on an achievable core milestone"),
                        "suggested_title": pick(resolved_lang, f"{suggested_title}（阶段一）", f"{suggested_title} (Phase 1)"),
                        "suggested_deadline": None,
                        "adjusted_scope": pick(resolved_lang, "保留当前截止日，将本次范围收窄为当前最核心、可安全交付的阶段成果。", "Keep current deadline and narrow scope to a realistic first milestone."),
                    },
                    {
                        "id": "extend_horizon",
                        "label": pick(resolved_lang, "周期可顺延，按约 3 个月完整节奏推进", "Extend timeline (~3 months) for full scope"),
                        "suggested_title": suggested_title,
                        "suggested_deadline": longer_deadline,
                        "adjusted_scope": pick(resolved_lang, "延长周期至约 90 天，循序渐进完成目标。", "Extend horizon to ~90 days for gradual execution."),
                    },
                ],
            },
        ])
        return {
            "needs_calibration": True,
            "conflict_kind": conflict_kind,
            "suggested_title": suggested_title,
            "suggested_outcome": "",
            "reason": reason,
            "questions": questions,
            "source": "rules",
        }

    # 2. Check vague broad skill/ambition without outcome or baseline
    has_level = bool(planner.infer_level(text))
    has_concrete_quant = bool(re.search(r"\d+|首|篇|个|套|页|mvp|demo|ppt|视频|播客|发票|周报", text, flags=re.I))
    if _VAGUE_SKILL_MARKERS.search(text) and not has_level and not has_concrete_quant and len(text) <= 24:
        return {
            "needs_calibration": True,
            "conflict_kind": "vague_target",
            "suggested_title": suggested_title,
            "suggested_outcome": "",
            "reason": pick(
                resolved_lang,
                "补充一下当前基础与本阶段想看到的具体成果，生成的步骤会更贴合实际。",
                "A quick check on your starting level and target milestone will make the steps much more tailored.",
            ),
            "questions": [
                {
                    "id": "baseline",
                    "prompt": pick(resolved_lang, "目前这部分的基础如何？", "What is your current starting point?"),
                    "options": [
                        {
                            "id": "beginner",
                            "label": pick(resolved_lang, "零基础入门", "Complete beginner"),
                            "suggested_title": None,
                            "suggested_deadline": None,
                            "adjusted_scope": pick(resolved_lang, "当前基础：零基础入门。", "Starting level: beginner."),
                        },
                        {
                            "id": "some",
                            "label": pick(resolved_lang, "有一点基础 / 学过但生疏", "Some basics / rusty"),
                            "suggested_title": None,
                            "suggested_deadline": None,
                            "adjusted_scope": pick(resolved_lang, "当前基础：有一定基础，需恢复与巩固。", "Starting level: some basics / rusty."),
                        },
                        {
                            "id": "intermediate",
                            "label": pick(resolved_lang, "已有基础，想系统进阶", "Intermediate, aiming to advance"),
                            "suggested_title": None,
                            "suggested_deadline": None,
                            "adjusted_scope": pick(resolved_lang, "当前基础：已有较扎实基础，重点进阶实战。", "Starting level: intermediate."),
                        },
                    ],
                },
                {
                    "id": "milestone",
                    "prompt": pick(resolved_lang, "这一阶段做到什么程度就算完成？", "What concrete outcome defines success for this phase?"),
                    "options": [
                        {
                            "id": "first_output",
                            "label": pick(resolved_lang, "独立完成 1 个小作品 / 实操输出", "Ship one small solo output"),
                            "suggested_title": suggested_title,
                            "suggested_deadline": None,
                            "adjusted_scope": pick(resolved_lang, "目标成果：独立完成并交付一个具体小作品。", "Milestone: finish one concrete solo output."),
                        },
                        {
                            "id": "foundation_pass",
                            "label": pick(resolved_lang, "完成核心基础模块并做一次自测", "Master core basics + self-check"),
                            "suggested_title": suggested_title,
                            "suggested_deadline": None,
                            "adjusted_scope": pick(resolved_lang, "目标成果：掌握核心基础并通过一次实战检验。", "Milestone: cover core fundamentals and pass a practical self-test."),
                        },
                    ],
                },
            ],
            "source": "rules",
        }

    # 3. Fast path
    return {
        "needs_calibration": False,
        "conflict_kind": "none",
        "suggested_title": suggested_title,
        "suggested_outcome": "",
        "reason": "",
        "questions": [],
        "source": "rules",
    }


def evaluate_goal_intake(
    description: str,
    deadline: date | None,
    *,
    today: date | None = None,
    tz: str = "Asia/Shanghai",
    weekly_available_minutes: int | None = None,
    lang: str | None = None,
    use_llm: bool = True,
) -> dict[str, Any]:
    """Run goal intake via fast L1 LLM client when enabled, falling back to rules."""
    today = today or date.today()
    resolved_lang = normalize_lang(lang) or ("zh" if re.search(r"[\u4e00-\u9fff]", description) else "en")
    if not use_llm:
        return assess_intake_rules(
            description,
            deadline,
            today=today,
            weekly_available_minutes=weekly_available_minutes,
            lang=resolved_lang,
        )

    client = router.client("plan")
    if not settings.llm_enabled or not getattr(client, "ready", True):
        return assess_intake_rules(
            description,
            deadline,
            today=today,
            weekly_available_minutes=weekly_available_minutes,
            lang=resolved_lang,
        )

    target_language = "Simplified Chinese" if resolved_lang == "zh" else "English"
    payload = {
        "description": description.strip(),
        "deadline": deadline.isoformat() if deadline else None,
        "days_left": (deadline - today).days if deadline else None,
        "weekly_available_hours": round(weekly_available_minutes / 60, 1) if weekly_available_minutes else None,
        "target_language": target_language,
    }
    system = INTAKE_SYSTEM.format(now=today.isoformat(), tz=tz)
    try:
        raw_data = client.complete_json(system, json.dumps(payload, ensure_ascii=False), temperature=0.0)
        parsed = GoalIntakePayload.model_validate(raw_data)
    except (ValidationError, Exception) as exc:  # noqa: BLE001 - calibration must always degrade gracefully
        log.warning("LLM goal intake failed (%s); falling back to rules", type(exc).__name__)
        return assess_intake_rules(
            description,
            deadline,
            today=today,
            weekly_available_minutes=weekly_available_minutes,
            lang=resolved_lang,
        )

    questions_out = []
    if parsed.needs_calibration and parsed.questions:
        for q in parsed.questions[:3]:
            opts_out = []
            for o in q.options[:4]:
                opts_out.append({
                    "id": o.id,
                    "label": o.label,
                    "suggested_title": o.suggested_title.strip() if o.suggested_title else None,
                    "suggested_deadline": _valid_future_iso(o.suggested_deadline, today),
                    "adjusted_scope": o.adjusted_scope.strip() if o.adjusted_scope else None,
                })
            questions_out.append({"id": q.id, "prompt": q.prompt, "options": opts_out})

    return {
        "needs_calibration": bool(parsed.needs_calibration and questions_out),
        "conflict_kind": parsed.conflict_kind if (parsed.needs_calibration and questions_out) else "none",
        "suggested_title": parsed.suggested_title.strip()[:80] or extract_clean_title(description, lang=resolved_lang),
        "suggested_outcome": parsed.suggested_outcome.strip()[:200],
        "reason": parsed.reason.strip() if (parsed.needs_calibration and questions_out) else "",
        "questions": questions_out,
        "source": "llm",
    }
