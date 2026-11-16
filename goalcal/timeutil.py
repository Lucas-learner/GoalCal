"""时区约定

- 数据库里所有 datetime 一律存 **naive UTC**（换 Postgres 也不用改语义）。
- 只在三个边界做转换：用户输入 → UTC、DB → 展示、调度计算。
- 调度计算全部用 **带时区的本地时间**，避免跨境/夏令时把“周三上午”算错。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def tzinfo(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def utcnow_naive() -> datetime:
    """给 SQLAlchemy 的 default 用。"""
    return datetime.now(UTC).replace(tzinfo=None)


def local_now(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def as_utc_naive(dt: datetime, tz_name: str) -> datetime:
    """把任意 datetime 归一到 UTC naive。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt.astimezone(UTC).replace(tzinfo=None)


def as_local(dt_utc: datetime, tz_name: str) -> datetime:
    """DB 里的 UTC naive → 本地 aware。"""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=UTC)
    return dt_utc.astimezone(ZoneInfo(tz_name))


def day_bounds(day: date, tz_name: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(tz_name)
    start = datetime.combine(day, time(0, 0), tzinfo=tz)
    return start, start + timedelta(days=1)


def at(day: date, hour: int, minute: int, tz_name: str) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=ZoneInfo(tz_name))


def minutes_between(a: datetime, b: datetime) -> int:
    return int((b - a).total_seconds() // 60)
