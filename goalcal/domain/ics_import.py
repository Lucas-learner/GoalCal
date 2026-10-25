"""ICS 只读导入：VEVENT → kind=external / source=import 的 Event。

- 按 (user_id, uid) 去重：重复导入更新 title/start/end，不新建；
- DAILY / WEEKLY RRULE 用 dateutil 展开未来 8 周，实例 uid 加 #N 后缀；
- 其他频率 / 全天事件 / 无 UID 的事件跳过并计入 skipped；
- 带 TZID 的时间转 UTC naive 存储；浮动时间按用户 tz 当本地时间；
- DTEND 缺失时退 DURATION，再退默认 60 分钟。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from dateutil.rrule import rrulestr
from icalendar import Calendar
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Event
from ..timeutil import as_utc_naive, now_utc

EXPAND_DAYS = 56  # RRULE 只展开未来 8 周
DEFAULT_MINUTES = 60
MAX_BYTES = 5 * 1024 * 1024
MAX_EVENTS = 5000
MAX_SCAN = 10000
_SUPPORTED_FREQS = {"DAILY", "WEEKLY"}


@dataclass
class ImportResult:
    added: int = 0
    updated: int = 0
    skipped: int = 0


def _to_utc_naive(value: datetime, tz: str) -> datetime:
    # 带 TZID 的转 UTC naive；浮动时间按用户时区当本地时间
    return as_utc_naive(value, tz)


def _event_window(component, tz: str) -> tuple[datetime, datetime] | None:
    """返回 (UTC naive start, UTC naive end)；全天事件或非法时间返回 None。"""
    start = component.decoded("DTSTART")
    if isinstance(start, date) and not isinstance(start, datetime):
        return None
    end = component.decoded("DTEND", None)
    if isinstance(end, date) and not isinstance(end, datetime):
        end = None
    if end is None:
        duration = component.decoded("DURATION", None)
        end = start + (duration if isinstance(duration, timedelta) else timedelta(minutes=DEFAULT_MINUTES))
    return _to_utc_naive(start, tz), _to_utc_naive(end, tz)


def _expand(component, tz: str, now: datetime, budget: list[int] | None = None) -> list[tuple[str, datetime, datetime]] | None:
    """展开成 (uid, start_utc, end_utc) 列表；无法处理（含全天事件）返回 None。"""
    window = _event_window(component, tz)
    if window is None:
        return None
    start_utc, end_utc = window
    duration = end_utc - start_utc
    base_uid = str(component.get("UID") or "").strip()
    if not base_uid:
        return None

    rule_prop = component.get("RRULE")
    if rule_prop is None:
        return [(base_uid, start_utc, end_utc)]
    freq = str(rule_prop.get("FREQ", [""])[0]).upper()
    if freq not in _SUPPORTED_FREQS:
        return None

    # dateutil 用原始（可能带时区的）DTSTART 展开，逐实例再转 UTC naive
    if set(rule_prop) - {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "WKST", "BYMONTHDAY", "BYMONTH"}:
        raise ValueError("Unsupported recurrence complexity")
    if int(rule_prop.get("INTERVAL", [1])[0]) < 1:
        raise ValueError("Invalid recurrence interval")
    budget = budget if budget is not None else [0]
    dtstart = component.decoded("DTSTART")
    rule = rrulestr(f"RRULE:{rule_prop.to_ical().decode()}", dtstart=dtstart)
    horizon = now + timedelta(days=EXPAND_DAYS)
    out = []
    for index, occurrence in enumerate(rule, start=1):
        budget[0] += 1
        if budget[0] > MAX_SCAN:
            raise ValueError("Recurrence scan limit exceeded")
        occ_utc = _to_utc_naive(occurrence, tz)
        if occ_utc > horizon:
            break
        if occ_utc + duration < now:
            continue  # 已过去的实例不导入
        out.append((f"{base_uid}#{index}", occ_utc, occ_utc + duration))
    return out


def _import_ics(
    session: Session,
    user_id: int,
    content: bytes,
    tz: str,
    now: datetime | None = None,
) -> ImportResult:
    now = now or now_utc()
    if len(content) > MAX_BYTES:
        raise ValueError("Calendar exceeds 5 MB")
    calendar = Calendar.from_ical(content)
    components = calendar.walk("VEVENT")
    if len(components) > MAX_EVENTS:
        raise ValueError("Too many events")
    budget = [0]
    prepared = []
    count = 0
    for component in components:
        expanded = _expand(component, tz, now, budget)
        count += len(expanded or [])
        if count > MAX_EVENTS:
            raise ValueError("Too many expanded events")
        prepared.append((component, expanded))
    existing = {
        ev.uid: ev
        for ev in session.scalars(
            select(Event).where(
                Event.user_id == user_id,
                Event.source == "import",
                Event.uid.is_not(None),
            )
        )
    }
    result = ImportResult()
    for component, expanded in prepared:
        if not expanded:
            result.skipped += 1
            continue
        title = str(component.get("SUMMARY") or "Imported event").strip()[:200] or "Imported event"
        for uid, start_utc, end_utc in expanded:
            if end_utc <= start_utc:
                end_utc = start_utc + timedelta(minutes=DEFAULT_MINUTES)
            event = existing.get(uid)
            if event is not None:
                event.title = title
                event.start_utc = start_utc
                event.end_utc = end_utc
                result.updated += 1
            else:
                event = Event(
                    user_id=user_id,
                    title=title,
                    start_utc=start_utc,
                    end_utc=end_utc,
                    kind="external",
                    source="import",
                    uid=uid,
                )
                session.add(event)
                existing[uid] = event
                result.added += 1
    session.flush()
    return result


def import_ics(session, user_id, content, tz, now=None):
    # Savepoint keeps the entire import atomic even if a caller catches errors.
    with session.begin_nested():
        return _import_ics(session, user_id, content, tz, now)
