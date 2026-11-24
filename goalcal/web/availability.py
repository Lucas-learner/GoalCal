"""Minute-precise weekly scheduling constraints, shared by old and new UIs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ..i18n import pick


def minute(value: str, *, end: bool = False, lang: str = "zh") -> int:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise ValueError(pick(lang, "时间必须为 HH:MM", "Time must be in HH:MM format"))
    if not value[:2].isdigit() or not value[3:].isdigit():
        raise ValueError(pick(lang, "时间必须为 HH:MM", "Time must be in HH:MM format"))
    h, m = int(value[:2]), int(value[3:])
    if h == 24 and m == 0 and end:
        return 1440
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(pick(lang, "时间超出范围", "Time is out of range"))
    return h * 60 + m


def validate_availability(value: dict, *, lang: str = "zh") -> dict:
    if not isinstance(value, dict):
        raise ValueError(pick(lang, "可用时间必须是对象", "Availability must be an object"))
    out = {}
    for name in ("available_windows", "protected_windows", "preferred_windows"):
        windows = value.get(name)
        if not isinstance(windows, list) or len(windows) > 100:
            raise ValueError(pick(lang, f"{name} 必须是至多 100 个时段的列表", f"{name} must be a list of at most 100 windows"))
        clean = []
        for w in windows:
            if not isinstance(w, dict):
                raise ValueError(pick(lang, "时段必须是对象", "Each time window must be an object"))
            days = w.get("days")
            if (not isinstance(days, list) or not days or len(days) > 7
                    or any(type(d) is not int or not 0 <= d <= 6 for d in days)
                    or len(set(days)) != len(days)):
                raise ValueError(pick(lang, "时段必须指定 0 至 6 的星期", "Days must list unique weekday indices from 0 to 6"))
            if minute(w.get("start"), lang=lang) >= minute(w.get("end"), end=True, lang=lang):
                raise ValueError(pick(lang, "时段结束必须晚于开始", "Window end must be later than start"))
            clean.append({"days": sorted(days), "start": w["start"], "end": w["end"]})
        if name == "available_windows" and not clean:
            raise ValueError(pick(lang, "至少保留 1 个可用时段", "Keep at least 1 available window"))
        out[name] = clean
    for name, low, high in (("buffer_minutes", 0, 120), ("max_goal_minutes_per_day", 1, 1440)):
        n = value.get(name)
        if type(n) is not int or not low <= n <= high:
            raise ValueError(pick(lang, f"{name} 必须为 {low} 至 {high} 的整数", f"{name} must be an integer between {low} and {high}"))
        out[name] = n
    return out


def intervals(day: datetime, windows: list[dict]) -> list[tuple[datetime, datetime]]:
    midnight = day.replace(hour=0, minute=0, second=0, microsecond=0)
    ranges = sorted((midnight + timedelta(minutes=minute(w["start"])),
                     midnight + timedelta(minutes=minute(w["end"], end=True)))
                    for w in windows if day.weekday() in w["days"])
    merged = []
    for a, b in ranges:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged


def allows(start: datetime, end: datetime, config: dict) -> bool:
    """Windows are half-open; adjacent available windows form one interval."""
    if start.tzinfo is not None and end.tzinfo is not None:
        if end.astimezone(timezone.utc) <= start.astimezone(timezone.utc):
            return False
    elif end <= start:
        return False
    if (start.tzinfo is not None and end.tzinfo is not None
            and start.utcoffset() != (end.astimezone(timezone.utc) - timedelta(microseconds=1)).astimezone(start.tzinfo).utcoffset()):
        # Crossing a clock change may revisit wall minutes or skip them. Check
        # each encountered minute rather than comparing folded local endpoints.
        lo, hi = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        points = [lo, hi - timedelta(microseconds=1)]
        cursor = lo.replace(second=0, microsecond=0) + timedelta(minutes=1)
        while cursor < hi:
            points.append(cursor)
            cursor += timedelta(minutes=1)
        for point in points:
            local = point.astimezone(start.tzinfo)
            if (local.date() != start.date()
                    or not any(a <= local < b for a, b in intervals(local, config["available_windows"]))
                    or any(a <= local < b for a, b in intervals(local, config["protected_windows"]))):
                return False
        return True
    return (any(a <= start and end <= b for a, b in intervals(start, config["available_windows"]))
            and not any(start < b and end > a for a, b in intervals(start, config["protected_windows"])))


def preferred(start: datetime, end: datetime, config: dict) -> bool:
    return any(a <= start and end <= b for a, b in intervals(start, config["preferred_windows"]))
