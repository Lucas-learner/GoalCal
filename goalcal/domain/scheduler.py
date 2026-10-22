"""状态感知排期引擎（确定性，无 LLM）。

这是本项目的技术核心，也是评测里“可解释 vs 单 prompt 拍脑袋”的对比对象。
所有函数都是纯函数：输入日程 + 状态 + 要求，输出带分数的候选时段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime
    kind: str = "external"  # external | block | buffer

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass(frozen=True)
class BusyBlock:
    start: datetime
    end: datetime
    kind: str = "external"

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass(frozen=True)
class Requirement:
    est_min: int = 45
    energy: int = 1  # 0 低 / 1 中 / 2 高
    kind: str = "deep"  # deep | admin | physical | social | research
    min_chunk: int = 20


@dataclass
class Preferences:
    day_start_hour: int = 8
    day_end_hour: int = 22
    max_goal_minutes_per_day: int = 120
    buffer_minutes: int = 10
    lunch: tuple[int, int] = (12, 13)
    preferred_windows: tuple[tuple[int, int], ...] = ((9, 12), (14, 18), (20, 22))
    granularity: int = 15


@dataclass
class Candidate:
    start: datetime
    end: datetime
    score: float
    reasons: list[str] = field(default_factory=list)

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    def to_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "score": round(self.score, 3),
            "reasons": self.reasons,
        }


# 默认精力曲线：起床后两小时最好，午饭后低谷，晚上有第二春
ENERGY_CURVE: dict[int, int] = {
    7: 1,
    8: 2,
    9: 2,
    10: 2,
    11: 2,
    12: 1,
    13: 0,
    14: 1,
    15: 1,
    16: 1,
    17: 1,
    18: 1,
    19: 1,
    20: 2,
    21: 2,
    22: 1,
}

# 不同任务类型对时段/精力的小偏好（±0.08 量级，不喧宾夺主）
KIND_TIME_BONUS: dict[str, tuple[int, int]] = {
    "deep": (9, 12),
    "research": (14, 18),
    "admin": (14, 18),
    "physical": (7, 10),
    "social": (18, 21),
}


def energy_at(dt: datetime) -> int:
    return ENERGY_CURVE.get(dt.hour, 1)


def _ceil_to(dt: datetime, granularity: int) -> datetime:
    """向上取整到最近的刻钟，避免把候选放在“过去”。"""
    discard = dt.minute % granularity
    base = dt.replace(second=0, microsecond=0)
    return base + timedelta(minutes=(granularity - discard) % granularity)


# ---------------------------------------------------------------- 空闲计算


def merge_busy(blocks: list[BusyBlock]) -> list[BusyBlock]:
    if not blocks:
        return []
    ordered = sorted(blocks, key=lambda b: b.start)
    merged = [ordered[0]]
    for b in ordered[1:]:
        last = merged[-1]
        if b.start <= last.end:
            merged[-1] = BusyBlock(last.start, max(last.end, b.end), last.kind)
        else:
            merged.append(b)
    return merged


def free_intervals(
    busy: list[BusyBlock],
    window_start: datetime,
    window_end: datetime,
    buffer_minutes: int = 10,
    min_minutes: int = 20,
) -> list[Interval]:
    """在窗口内挖掉忙碌块（两侧各留 buffer），返回可用空档。"""
    padded = [
        BusyBlock(b.start - timedelta(minutes=buffer_minutes),
                  b.end + timedelta(minutes=buffer_minutes),
                  b.kind)
        for b in merge_busy(busy)
    ]
    cursor = window_start
    gaps: list[Interval] = []
    for b in padded:
        if b.end <= window_start or b.start >= window_end:
            continue
        if b.start > cursor:
            gap_end = min(b.start, window_end)
            if (gap_end - cursor).total_seconds() / 60 >= min_minutes:
                gaps.append(Interval(cursor, gap_end))
        cursor = max(cursor, b.end)
    if (window_end - cursor).total_seconds() / 60 >= min_minutes:
        gaps.append(Interval(cursor, window_end))
    return gaps


def _aligned_starts(interval: Interval, est_min: int, gran: int, limit: int = 200) -> list[datetime]:
    """在空档里按刻钟生成起点。不能只取前几个，否则会漏掉下午/晚上。

    空档起点可能是 11:10（忙碌块结束 + buffer），也要向上对齐到 11:15。
    """
    starts: list[datetime] = []
    step = timedelta(minutes=gran)
    cur = _ceil_to(interval.start, gran)
    while cur + timedelta(minutes=est_min) <= interval.end and len(starts) < limit:
        starts.append(cur)
        cur += step
    return starts


# ---------------------------------------------------------------- 打分


def _score(
    start: datetime,
    end: datetime,
    req: Requirement,
    prefs: Preferences,
    goal_minutes_that_day: int,
    days_from_now: int,
    deadline: date | None,
    today: date,
    adjacent_before: bool,
) -> tuple[float, list[str]]:
    score = 0.5
    reasons: list[str] = []

    # 1) 精力匹配：不能在高耗任务上安排低谷时段
    slot_energy = energy_at(start)
    diff = req.energy - slot_energy
    if diff > 0:
        score -= 0.18 * diff
    elif req.energy >= 1 and slot_energy >= req.energy:
        score += 0.12
        if req.energy == 2 and 9 <= start.hour <= 11:
            reasons.append("上午 9–11 点是你精力最好的整块时间，适合深度任务")
        elif req.energy <= 1:
            reasons.append("任务强度不高，低谷时段也能推进")
    if slot_energy == 0 and req.energy >= 1:
        score -= 0.15
        reasons.append("午饭后精力低谷，建议只做低强度任务")

    # 2) 用户偏好时段
    in_pref = any(a <= start.hour < b for a, b in prefs.preferred_windows)
    if in_pref:
        score += 0.10

    # 3) 任务类型与时段匹配
    lo, hi = KIND_TIME_BONUS.get(req.kind, (0, 24))
    if lo <= start.hour < hi:
        score += 0.08
        reasons.append(_kind_reason(req.kind, start.hour))

    # 4) 越早越好（避免拖到截止日前）
    score -= min(days_from_now, 7) * 0.035

    # 5) 截止压力
    if deadline:
        days_left = (deadline - today).days
        if days_left <= 7:
            score += 0.10
            reasons.append(f"距离截止还有 {days_left} 天，建议尽早推进")
        elif days_left <= 14:
            score += 0.05

    # 6) 当日过载
    if goal_minutes_that_day + req.est_min > prefs.max_goal_minutes_per_day:
        score -= 0.45
        reasons.append("当天目标时间已接近上限，不建议再加块")

    # 7) 切换成本 / 缓冲
    if adjacent_before:
        score -= 0.07
        reasons.append("紧接在另一个日程之后，已自动留出缓冲")

    # 8) 午饭保护
    if prefs.lunch[0] <= start.hour < prefs.lunch[1]:
        score -= 0.12

    # 9) 时长匹配度
    if req.est_min + 15 <= (end - start).total_seconds() / 60 <= req.est_min * 1.5:
        score += 0.05

    if not reasons:
        if in_pref:
            reasons.append("落在你通常状态较好的时段")
        else:
            reasons.append("这是附近唯一能放下的完整时间块")

    return max(0.0, min(1.0, score)), reasons


def _kind_reason(kind: str, hour: int) -> str:
    return {
        "deep": "这类需要连续思考的任务放在上午更稳",
        "research": "下午适合查资料、看参考，不需要太多意志力",
        "admin": "行政类小任务放在下午，不占用你的高精力时段",
        "physical": "早上做身体类任务，后面不容易被别的事挤掉",
        "social": "傍晚安排沟通/协作类任务更自然",
    }.get(kind, "时段与任务类型匹配")


# ---------------------------------------------------------------- 对外主入口


def propose(
    req: Requirement,
    now: datetime,
    busy: list[BusyBlock],
    prefs: Preferences | None = None,
    days: int = 5,
    top_n: int = 3,
    deadline: date | None = None,
) -> list[Candidate]:
    """在 now 之后的 days 天内，给出 top_n 个候选时段（按分数降序）。"""
    prefs = prefs or Preferences()
    today = now.date()
    pool: list[Candidate] = []

    for offset in range(days):
        day = today + timedelta(days=offset)
        window_start = datetime.combine(
            day, time(hour=prefs.day_start_hour), tzinfo=now.tzinfo
        )
        window_end = datetime.combine(
            day, time(hour=prefs.day_end_hour), tzinfo=now.tzinfo
        )
        if offset == 0:
            window_start = max(window_start, _ceil_to(now, prefs.granularity))
        if window_start >= window_end:
            continue

        day_busy = [b for b in busy if b.end > window_start and b.start < window_end]
        goal_minutes = sum(
            b.minutes for b in day_busy
            if b.kind == "block" and b.start >= window_start and b.start < window_end
        )
        gaps = free_intervals(
            day_busy, window_start, window_end,
            buffer_minutes=prefs.buffer_minutes, min_minutes=req.min_chunk,
        )
        for gap in gaps:
            for start in _aligned_starts(gap, req.est_min, prefs.granularity):
                end = start + timedelta(minutes=req.est_min)
                adjacent = any(
                    abs((start - b.end).total_seconds()) <= 20 * 60 for b in day_busy
                )
                score, reasons = _score(
                    start, end, req, prefs, goal_minutes, offset, deadline, today, adjacent
                )
                pool.append(Candidate(start=start, end=end, score=score, reasons=reasons))

    pool.sort(key=lambda c: (-c.score, c.start))
    # 同一小时内只保留一个，避免三个候选其实是同一个空档
    picked: list[Candidate] = []
    for c in pool:
        if all(abs((c.start - p.start).total_seconds()) >= 3600 for p in picked):
            picked.append(c)
        if len(picked) >= top_n:
            break
    return picked


def describe(candidates: list[Candidate]) -> str:
    if not candidates:
        return "这几天没有足够长的空档。要不要把这一步拆小一点，或者延长截止日？"
    lines = []
    for i, c in enumerate(candidates, 1):
        lines.append(
            f"{i}. {c.start:%m/%d %a %H:%M}–{c.end:%H:%M}（{c.minutes} 分钟，匹配度 {c.score:.0%}）"
            f"：{c.reasons[0]}"
        )
    return "\n".join(lines)
