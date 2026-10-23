"""确定性的自然语言日期解析。

为什么不用 dateparser 直接上：
    dateparser 对 "Wednesday 10am" 这类“星期 + 时间”会给出跨年或丢时间的结果。
    本项目 90% 的解析准确率指标，靠的就是这一层可控的规则。

策略：
    1. 先用本模块的规则抓日期/时间（覆盖 95% 的真实说法）
    2. 抓不到再退回 dateparser
    3. LLM 只负责判断 intent，不负责算日期
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

CN_WEEKDAYS = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6,
}

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

PERIOD_HOURS = {
    "morning": 9, "上午": 9, "早上": 9, "早晨": 9,
    "noon": 12, "midday": 12, "中午": 12,
    "afternoon": 14, "下午": 14,
    "evening": 19, "晚上": 19, "傍晚": 19,
    "night": 20, "tonight": 20,
}

# 时间窗口（半开区间 [lo, hi)）：排期建议用，打分逻辑不变
WINDOW_HOURS = {
    "morning": (8, 12), "上午": (8, 12), "早上": (8, 12), "早晨": (8, 12),
    "afternoon": (12, 18), "下午": (12, 18),
    "evening": (18, 23), "晚上": (18, 23), "傍晚": (18, 23),
    "night": (18, 23),
}

_CN_WINDOW_DAY = {"今天": 0, "今日": 0, "明天": 1, "后天": 2}
_EN_WINDOW_DAY = {"today": 0, "this": 0, "tomorrow": 1, "day after tomorrow": 2}

# 时段词后面紧跟数字（"下午3点"/"tonight at 9"）是明确时间点，不算窗口
_CN_WINDOW = re.compile(
    r"(今天|今日|明天|后天)?\s*(上午|早上|早晨|下午|晚上|傍晚)(?!\s*\d)"
)
_CN_NIGHT = re.compile(r"(今晚|明晚)(?!\s*\d)")
_EN_WINDOW = re.compile(
    r"\b(?:(today|tomorrow|this|day after tomorrow)\s+)?(morning|afternoon|evening|night)"
    r"(?!\s*(?:at\s*)?\d)\b|\b(tonight)(?!\s*(?:at\s*)?\d)",
    re.I,
)


def extract_window(text: str, now: datetime) -> tuple[datetime, datetime] | None:
    """抽取"今天下午"/"tomorrow morning"/"tonight"这类时间窗口，返回本地时间半开区间。"""
    tz = now.tzinfo

    m = _CN_NIGHT.search(text)
    if m:
        offset = 0 if m.group(1) == "今晚" else 1
        day = (now + timedelta(days=offset)).date()
        return _dt(day, 18, 0, tz), _dt(day, 23, 0, tz)

    m = _CN_WINDOW.search(text)
    if m:
        offset = _CN_WINDOW_DAY.get(m.group(1) or "今天", 0)
        lo, hi = WINDOW_HOURS[m.group(2)]
        day = (now + timedelta(days=offset)).date()
        return _dt(day, lo, 0, tz), _dt(day, hi, 0, tz)

    m = _EN_WINDOW.search(text)
    if m:
        if m.group(3):  # tonight
            offset, lo, hi = 0, 18, 23
        else:
            offset = _EN_WINDOW_DAY[(m.group(1) or "today").lower()]
            lo, hi = WINDOW_HOURS[m.group(2).lower()]
        day = (now + timedelta(days=offset)).date()
        return _dt(day, lo, 0, tz), _dt(day, hi, 0, tz)
    return None


def _dt(day: date, hour: int, minute: int, tz) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=tz)


def extract_time(text: str) -> tuple[int, int] | None:
    """先找显式时间，再找时段词。"""
    m = re.search(r"\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text, re.I)
    if m:
        hour = int(m.group(1)) % 12
        if m.group(3).lower() == "pm":
            hour += 12
        return hour, int(m.group(2) or 0)

    m = re.search(r"\b(?:at\s+)?([01]?\d|2[0-3]):([0-5]\d)\b", text)
    if m:
        return int(m.group(1)), int(m.group(2))

    m = re.search(r"\b(?:at\s+)([01]?\d|2[0-3])\b", text, re.I)
    if m:
        return int(m.group(1)), 0

    m = re.search(r"(上午|下午|晚上|早上|中午|今晚|明晚)?\s*(\d{1,2})\s*点\s*(半|(\d{1,2})\s*分?)?", text)
    if m:
        hour = int(m.group(2))
        if m.group(1) in {"下午", "晚上", "今晚", "明晚"} and hour < 12:
            hour += 12
        minute = 30 if m.group(3) == "半" else int(m.group(4) or 0)
        return hour, minute

    m = re.search(r"\b(noon|midday|morning|afternoon|evening|night|tonight)\b|上午|下午|晚上|中午|早上",
                  text, re.I)
    if m:
        return PERIOD_HOURS.get(m.group(0).lower(), 9), 0
    return None


def _next_weekday(now: datetime, target: int, force_next: bool) -> date:
    delta = (target - now.weekday()) % 7
    if force_next and delta == 0:
        delta = 7
    return (now + timedelta(days=delta)).date()


def find_dates(text: str, now: datetime) -> list[tuple[str, datetime]]:
    """按出现顺序返回 (原文片段, 本地时间)。未给时间时默认 09:00。"""
    tz = now.tzinfo
    found: list[tuple[int, int, date]] = []

    def add(m: re.Match, d: date) -> None:
        if any(not (m.end() <= s or m.start() >= e) for s, e, _ in found):
            return
        found.append((m.start(), m.end(), d))

    # 1) day after tomorrow / 后天（必须早于 tomorrow 判断）
    #    中文词不能用 \b：汉字和数字都算 \w，"明天10点" 两边没有词边界
    for m in re.finditer(r"\bday\s+after\s+tomorrow\b|大后天|后天", text, re.I):
        add(m, (now + timedelta(days=3 if "大" in m.group(0) else 2)).date())

    # 2) today / tomorrow / tonight / 今天 / 明天
    for m in re.finditer(r"\btomorrow\b|今日|今天|明天|明晚|\btoday\b|\btonight\b|今晚", text, re.I):
        token = m.group(0).lower()
        if token in {"today", "今天", "今日"}:
            add(m, now.date())
        elif token in {"tonight", "今晚"}:
            add(m, now.date())
        else:
            add(m, (now + timedelta(days=1)).date())

    # 3) in N days / N 天后
    for m in re.finditer(r"\bin\s+(\d+)\s+days?\b|(\d+)\s*天[后以]?后", text, re.I):
        n = int(m.group(1) or m.group(2))
        add(m, (now + timedelta(days=n)).date())

    # 4) next week / 下周（"下周一" 交给下面的中文星期规则，别在这里吃掉）
    for m in re.finditer(r"\bnext\s+week\b|下周(?![一二三四五六日天])", text, re.I):
        add(m, (now + timedelta(days=7)).date())

    # 5) weekday（支持 next / this / coming）
    for m in re.finditer(
        r"\b(next|this|coming)?\s*(monday|mon|tuesday|tue|tues|wednesday|wed|thursday|thu|thur|thurs|"
        r"friday|fri|saturday|sat|sunday|sun)\b",
        text, re.I,
    ):
        force_next = (m.group(1) or "").lower() in {"next", "coming"}
        add(m, _next_weekday(now, WEEKDAYS[m.group(2).lower()], force_next))

    # 6) 中文星期：周五 / 星期五 / 礼拜五 / 下周五
    for m in re.finditer(r"(下+|这|本)?\s*(?:周|星期|礼拜)([一二三四五六日天])", text):
        target = CN_WEEKDAYS[m.group(2)]
        force_next = bool(m.group(1) and "下" in m.group(1))
        add(m, _next_weekday(now, target, force_next))

    # 7) 中文月日：10月29日 / 10月29号
    for m in re.finditer(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", text):
        month, day = int(m.group(1)), int(m.group(2))
        year = now.year + (1 if (month, day) < (now.month, now.day) else 0)
        try:
            add(m, date(year, month, day))
        except ValueError:
            pass

    # 8) Oct 28 / October 28th
    for m in re.finditer(
        r"\b(" + "|".join(MONTHS) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", text, re.I
    ):
        month = MONTHS[m.group(1).lower()]
        day = int(m.group(2))
        year = now.year + (1 if month < now.month else 0)
        try:
            add(m, date(year, month, day))
        except ValueError:
            pass

    # 9) 10/28 或 10-28
    for m in re.finditer(r"\b(\d{1,2})[/-](\d{1,2})\b", text):
        month, day = int(m.group(1)), int(m.group(2))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        year = now.year + (1 if month < now.month else 0)
        try:
            add(m, date(year, month, day))
        except ValueError:
            pass

    explicit = extract_time(text)
    ordered = sorted(found, key=lambda x: x[0])
    results: list[tuple[str, datetime]] = []
    for i, (s, e, d) in enumerate(ordered):
        is_last = i == len(ordered) - 1
        if explicit and is_last:
            hour, minute = explicit
        else:
            hour, minute = 9, 0
        results.append((text[s:e], _dt(d, hour, minute, tz)))
    return results
