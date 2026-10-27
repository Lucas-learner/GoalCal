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
    r"(天|日|周|星期|礼拜|个?月(?!\s*[零一二两三四五六七八九十\d]{1,2}\s*[日号])|年|days?|weeks?|months?|years?)",
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
        explicit = parse_explicit_date(raw, today)
        if explicit is not None:
            deadline = explicit
        else:
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
    """只在没有截止日时追问；水平先按默认值，第一次签到时顺带校正。"""
    return brief.deadline is not None


def clarification_text(brief: GoalBrief) -> str:
    if is_exam(brief.title):
        return (f'Before I make a plan for "{brief.title}", confirm the exam date or timeline. '
                'Choose 7 days, 1 month or 3 months, or type your own date or duration.')
    return (
        f'Before I make a plan for "{brief.title}", choose a timeline. '
        'Choose 7 days, 1 month or 3 months, or type your own date or duration. '
        'I will build the plan after that.'
    )


def quick_options(brief: GoalBrief) -> list[str]:
    return ["7 days", "1 month", "3 months", "Cancel"]


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

    explicit = parse_explicit_date(answer, today)
    if explicit is not None:
        updated.deadline = explicit
    else:
        days = parse_duration_days(answer)
        if days is not None:
            updated.deadline = today + timedelta(days=days)
    updated.complexity = classify(updated.title, updated.deadline, updated.raw, today=today)
    return updated


def fill_defaults(brief: GoalBrief, today: date | None = None) -> GoalBrief:
    today = today or date.today()
    updated = replace(brief, assumptions=list(brief.assumptions))
    if updated.deadline is None:
        updated.deadline = today + timedelta(days=90)
        updated.assumptions.append("Split over a 90-day starter horizon")
    if is_skill(updated.title) and not updated.level:
        updated.level = "beginner"
        updated.assumptions.append("Assuming beginner level")
    if not updated.target:
        updated.target = default_target(updated.title)
        updated.assumptions.append(f'Target read as "{updated.target}"')
    updated.complexity = classify(updated.title, updated.deadline, updated.raw, today=today)
    return updated


def default_target(title: str) -> str:
    text = title.lower()
    if any(k in text for k in ("复习", "考试", "exam", "final", "midterm", "quiz")):
        return "solve the core problem types independently in the exam"
    if "钢琴" in text or "piano" in text:
        return "play one simple piece start to finish"
    if "视频" in text or "video" in text:
        return "publish the first video"
    if any(k in text for k in ("跑", "run", "健身", "fitness")):
        return "complete one full training phase back to back"
    if any(k in text for k in ("学", "learn", "语言", "programming", "编程")):
        return "demo the skill independently"
    return "complete one presentable deliverable"


def infer_level(text: str) -> str:
    if LEVEL_ADVANCED.search(text or ""):
        return "advanced"
    if LEVEL_SOME.search(text or ""):
        return "some experience"
    if LEVEL_ZERO.search(text or ""):
        return "beginner"
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
    english_numbers = {"a": "1", "an": "1", "one": "1", "two": "2", "three": "3",
                       "four": "4", "five": "5", "six": "6", "seven": "7",
                       "eight": "8", "nine": "9", "ten": "10", "twelve": "12"}
    text = re.sub(
        r"\b(a|an|one|two|three|four|five|six|seven|eight|nine|ten|twelve)\s+"
        r"(?=(?:days?|weeks?|months?|years?)\b)",
        lambda match: english_numbers[match.group(1).lower()] + " ", text or "", flags=re.I,
    )
    if re.search(r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]", text or ""):
        # “10 月 29 日”是具体日期不是时长，交给 parse_explicit_date
        return None
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


def parse_explicit_date(text: str, today: date | None = None) -> date | None:
    match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text or "")
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    # 中文月日："10 月 29 日"；已过的日期滚到明年
    match = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", text or "")
    if match:
        today = today or date.today()
        month, day = int(match.group(1)), int(match.group(2))
        year = today.year + (1 if (month, day) < (today.month, today.day) else 0)
        try:
            return date(year, month, day)
        except ValueError:
            return None
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
    return {"simple": "simple", "standard": "standard", "complex": "complex"}.get(
        complexity, "standard"
    )
