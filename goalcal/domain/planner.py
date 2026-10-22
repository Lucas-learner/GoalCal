"""目标拆解前的复杂度判断与澄清。

这里不查库、不调用 LLM：只把“目标说法”变成可确认的 brief，
再由 agent 调用 LLM 生成计划（失败才回退模板）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

SIMPLE_MARKERS = re.compile(
    r"整理|清理|预约|报名|买一?个|读一本|一次就好|简单|快速|小目标|"
    r"\bquick\b|\bsmall\b|\bone[- ]off\b",
    re.I,
)
COMPLEX_MARKERS = re.compile(
    r"钢琴|piano|语言|外语|编程|写代码|马拉松|创业|考级|考试|复习|备考|专业|精通|"
    r"从零|学会|掌握|发布|作品集|半年|一年|长期|"
    r"\bmaster\b|\bfluent\b|\bmarathon\b|\bfrom scratch\b",
    re.I,
)
SKILL_MARKERS = re.compile(
    r"学|learn|掌握|master|练|practice|弹|piano|钢琴|吉他|guitar|"
    r"语言|language|编程|coding|code|跑|run|游泳|swim|画|draw|"
    r"写|write|唱|sing",
    re.I,
)
EXAM_MARKERS = re.compile(
    r"复习|考试|exam|final|midterm|quiz|review|期末|期中|备考|刷题",
    re.I,
)
LEVEL_ZERO = re.compile(
    r"零基础|没基础|没有基础|从零|不会|新手|初学|刚开始|入门|"
    r"\bbeginner\b|\bfrom scratch\b|\bnew to\b",
    re.I,
)
LEVEL_SOME = re.compile(
    r"有基础|会一点|学过|有点基础|基础一般|以前练过|"
    r"\bintermediate\b|\bsome experience\b|\bplayed before\b",
    re.I,
)
LEVEL_ADVANCED = re.compile(
    r"熟练|中级|高级|专业|"
    r"\badvanced\b|\bprofessional\b|\bexperienced\b",
    re.I,
)
DURATION = re.compile(
    r"([半零一二两三四五六七八九十百\d]+(?:\.[\d]+)?)\s*"
    r"(天|日|周|星期|礼拜|个?月|年|days?|weeks?|months?|years?)",
    re.I,
)
CANCEL = re.compile(
    r"^(算了|先不|不要了|取消|不建了|never mind|forget it|cancel)\b|"
    r"先不建|不用了|算了",
    re.I,
)


@dataclass
class GoalBrief:
    title: str
    raw: str
    deadline: date | None = None
    level: str = ""
    target: str = ""
    complexity: str = "standard"
    round: int = 0
    assumptions: list[str] = field(default_factory=list)


def assess(
    title: str,
    deadline: date | None,
    raw: str,
    today: date | None = None,
) -> GoalBrief:
    title = normalize_title(title)
    today = today or date.today()
    if deadline is None:
        days = parse_duration_days(raw)
        if days is not None:
            deadline = today + timedelta(days=days)
    return GoalBrief(
        title=title,
        raw=raw,
        deadline=deadline,
        level=infer_level(raw) or infer_level(title),
        target=infer_target(raw),
        complexity=classify(title, deadline, raw, today=today),
    )


def normalize_title(title: str) -> str:
    title = re.sub(
        r"^\s*(我要|我想|我想要|希望|帮我|请|i want to|i'd like to|"
        r"i would like to|help me (to )?)\s*",
        "",
        title,
        flags=re.I,
    )
    return title.strip()[:120]


def is_skill(text: str) -> bool:
    return bool(SKILL_MARKERS.search(text or ""))


def is_exam(text: str) -> bool:
    return bool(EXAM_MARKERS.search(text or ""))


def classify(
    title: str,
    deadline: date | None,
    raw: str = "",
    today: date | None = None,
) -> str:
    text = f"{title} {raw}"
    if SIMPLE_MARKERS.search(text):
        return "simple"
    if deadline is not None:
        days = (deadline - (today or date.today())).days
        if days <= 14:
            return "simple"
        if days >= 90:
            return "complex"
    if COMPLEX_MARKERS.search(text) or is_skill(title):
        return "complex"
    return "standard"


def ready(brief: GoalBrief) -> bool:
    return brief.deadline is not None and (
        not is_skill(brief.title) or bool(brief.level)
    )


def clarification_text(brief: GoalBrief) -> str:
    missing_level = is_skill(brief.title) and not brief.level
    missing_deadline = brief.deadline is None
    label = complexity_label(brief.complexity)

    if missing_level and missing_deadline:
        return (
            f"“{brief.title}”我判断是一个**{label}目标**，不适合直接套固定 10 步。\n"
            "先确认两件事：\n"
            "① 你现在是什么水平？（零基础 / 有基础）\n"
            "② 想在多久内达到什么程度？（例如：3 个月 / 能弹一首简单曲子）\n\n"
            "回一句就行，例如：**零基础，3 个月，能弹一首简单曲子**。"
        )
    if missing_level:
        return (
            f"“{brief.title}”我会按**{label}目标**来拆。\n"
            "你现在的水平是？可以回：**零基础** / **有基础** / **先按零基础**。"
        )
    if is_exam(brief.title) and missing_deadline:
        return (
            f"“{brief.title}”先确认考试时间。\n"
            "可以回：**7 天后** / **14 天后** / **10 月 29 日**。"
        )
    return (
        f"“{brief.title}”我会按**{label}目标**来拆。\n"
        "想多久完成？可以回：**3 个月** / **6 个月** / **1 年**。"
    )


def quick_options(brief: GoalBrief) -> list[str]:
    missing_level = is_skill(brief.title) and not brief.level
    missing_deadline = brief.deadline is None
    if missing_level and missing_deadline:
        return [
            "零基础 · 3 个月 · 能弹一首简单曲子",
            "有基础 · 6 个月 · 能流畅弹奏",
            "先按 90 天入门计划",
            "先不建了",
        ]
    if missing_level:
        return ["零基础", "有基础", "先按零基础", "先不建了"]
    if is_exam(brief.title):
        return ["7 天后", "14 天后", "1 个月后", "先不建了"]
    return ["3 个月", "6 个月", "1 年", "先不建了"]


def cancellation_requested(text: str) -> bool:
    return bool(CANCEL.search((text or "").strip()))


def apply_answer(brief: GoalBrief, answer: str, today: date | None = None) -> GoalBrief:
    today = today or date.today()
    updated = replace(brief, assumptions=list(brief.assumptions), round=brief.round + 1)

    level = infer_level(answer)
    if level:
        updated.level = level
    target = infer_target(answer)
    if target:
        updated.target = target

    days = parse_duration_days(answer)
    if days is not None:
        updated.deadline = today + timedelta(days=days)
    else:
        explicit = parse_explicit_date(answer)
        if explicit is not None:
            updated.deadline = explicit
    updated.complexity = classify(updated.title, updated.deadline, updated.raw, today=today)
    return updated


def fill_defaults(brief: GoalBrief, today: date | None = None) -> GoalBrief:
    today = today or date.today()
    updated = replace(brief, assumptions=list(brief.assumptions))
    if updated.deadline is None:
        updated.deadline = today + timedelta(days=90)
        updated.assumptions.append("按 90 天的入门周期来拆")
    if is_skill(updated.title) and not updated.level:
        updated.level = "零基础"
        updated.assumptions.append("按零基础开始")
    if not updated.target:
        updated.target = default_target(updated.title)
        updated.assumptions.append(f"目标按“{updated.target}”理解")
    updated.complexity = classify(updated.title, updated.deadline, updated.raw, today=today)
    return updated


def default_target(title: str) -> str:
    text = title.lower()
    if any(k in text for k in ("复习", "考试", "exam", "final", "midterm", "quiz")):
        return "考试时能独立完成核心题型"
    if "钢琴" in text or "piano" in text:
        return "能完整弹一首简单曲子"
    if "视频" in text or "video" in text:
        return "发布第一条视频"
    if any(k in text for k in ("跑", "run", "健身", "fitness")):
        return "连续完成一个阶段的训练"
    if any(k in text for k in ("学", "learn", "语言", "programming", "编程")):
        return "能独立展示学到的技能"
    return "完成一次可展示的成果"


def infer_level(text: str) -> str:
    if LEVEL_ADVANCED.search(text or ""):
        return "熟练"
    if LEVEL_SOME.search(text or ""):
        return "有基础"
    if LEVEL_ZERO.search(text or ""):
        return "零基础"
    return ""


def infer_target(text: str) -> str:
    match = re.search(
        r"(?:达到|做到|目标是?|程度是?|能|可以)\s*([^，。；;\n]+)",
        text or "",
        flags=re.I,
    )
    if not match:
        return ""
    return match.group(1).strip(" ，。；;")[:120]


def parse_duration_days(text: str) -> int | None:
    match = DURATION.search(text or "")
    if not match:
        return None
    value, unit = match.groups()
    unit = unit.lower()
    number = chinese_number(value)
    if number is None or number <= 0:
        return None
    if unit == "天" or unit == "日":
        days = number
    elif unit in {"周", "星期", "礼拜"}:
        days = number * 7
    elif unit.endswith("月"):
        days = number * 30
    elif unit in {"年", "year", "years"}:
        days = number * 365
    elif unit in {"day", "days"}:
        days = number
    elif unit in {"week", "weeks"}:
        days = number * 7
    elif unit in {"month", "months"}:
        days = number * 30
    else:
        return None
    return int(days)


def parse_explicit_date(text: str) -> date | None:
    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text or "")
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def chinese_number(value: str) -> float | None:
    value = (value or "").strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    if value == "半":
        return 0.5
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        left, _, right = value.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    if value == "百":
        return 100
    total = 0
    for char in value:
        if char in digits:
            total = total * 10 + digits[char]
        else:
            return None
    return total


def complexity_label(complexity: str) -> str:
    return {"simple": "简单", "standard": "中等", "complex": "复杂"}.get(
        complexity, "中等"
    )
