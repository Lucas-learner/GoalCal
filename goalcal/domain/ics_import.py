"""ICS 只读导入：VEVENT → kind=external / source=import 的 Event。

- 按 (user_id, uid) 去重：重复导入更新 title/start/end，不新建；
- DAILY / WEEKLY RRULE 用 dateutil 展开未来 8 周，实例 uid 加 #N 后缀；
- 其他频率 / 全天事件 / 无 UID 的事件跳过并计入 skipped；
- 带 TZID 的时间转 UTC naive 存储；浮动时间按用户 tz 当本地时间；
- DTEND 缺失时退 DURATION，再退默认 60 分钟。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from collections.abc import Callable
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from icalendar import Calendar
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from ..models import Checkin, Event, EventSyncLink, Interaction, RescheduleLog, SyncOutbox
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
    removed: int = 0


def _to_utc_naive(value: datetime, tz: str) -> datetime:
    # 带 TZID 的转 UTC naive；浮动时间按用户时区当本地时间
    return as_utc_naive(value, tz)


def _event_window(component, tz: str, *, all_day_mode: str = "ignore") -> tuple[datetime, datetime, bool] | None:
    """返回 (UTC naive start, UTC naive end, is_all_day)；未启用全天导入或非法时间返回 None。"""
    start = component.decoded("DTSTART")
    if isinstance(start, date) and not isinstance(start, datetime):
        if all_day_mode not in {"show", "busy"}:
            return None
        zone = ZoneInfo(tz)
        end = component.decoded("DTEND", None)
        if isinstance(end, date) and not isinstance(end, datetime) and end > start:
            end_date = end
        else:
            duration = component.decoded("DURATION", None)
            days = max(1, duration.days) if isinstance(duration, timedelta) and duration.days > 0 else 1
            end_date = start + timedelta(days=days)
        local_start = datetime.combine(start, time(), zone)
        local_end = datetime.combine(end_date, time(), zone)
        return (
            local_start.astimezone(timezone.utc).replace(tzinfo=None),
            local_end.astimezone(timezone.utc).replace(tzinfo=None),
            True,
        )
    end = component.decoded("DTEND", None)
    if isinstance(end, date) and not isinstance(end, datetime):
        end = None
    if end is None:
        duration = component.decoded("DURATION", None)
        end = start + (duration if isinstance(duration, timedelta) else timedelta(minutes=DEFAULT_MINUTES))
    return _to_utc_naive(start, tz), _to_utc_naive(end, tz), False


def _exdates_utc(component, tz: str) -> set[datetime]:
    prop = component.get("EXDATE")
    if prop is None:
        return set()
    items = prop if isinstance(prop, list) else [prop]
    excluded: set[datetime] = set()
    zone = ZoneInfo(tz)
    for item in items:
        dts = getattr(item, "dts", None)
        if not dts:
            continue
        for entry in dts:
            dt = getattr(entry, "dt", None)
            if isinstance(dt, datetime):
                excluded.add(_to_utc_naive(dt, tz))
            elif isinstance(dt, date):
                excluded.add(datetime.combine(dt, time(), zone).astimezone(timezone.utc).replace(tzinfo=None))
    return excluded


def _expand(
    component,
    tz: str,
    now: datetime,
    budget: list[int] | None = None,
    *,
    all_day_mode: str = "ignore",
) -> list[tuple[str, datetime, datetime, bool]] | None:
    """展开成 (uid, start_utc, end_utc, all_day) 列表；无法处理（含跳过全天或已取消）返回 None。"""
    status = str(component.get("STATUS") or "").strip().upper()
    if status == "CANCELLED":
        return None
    window = _event_window(component, tz, all_day_mode=all_day_mode)
    if window is None:
        return None
    start_utc, end_utc, is_all_day = window
    duration = end_utc - start_utc
    base_uid = str(component.get("UID") or "").strip()
    if not base_uid:
        return None

    rule_prop = component.get("RRULE")
    if rule_prop is None:
        return [(base_uid, start_utc, end_utc, is_all_day)]
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
    zone = ZoneInfo(tz)
    rule_dtstart = datetime.combine(dtstart, time()) if isinstance(dtstart, date) and not isinstance(dtstart, datetime) else dtstart
    rule = rrulestr(f"RRULE:{rule_prop.to_ical().decode()}", dtstart=rule_dtstart)
    excluded = _exdates_utc(component, tz)
    horizon = now + timedelta(days=EXPAND_DAYS)
    out = []
    for index, occurrence in enumerate(rule, start=1):
        budget[0] += 1
        if budget[0] > MAX_SCAN:
            raise ValueError("Recurrence scan limit exceeded")
        if is_all_day:
            occ_local = datetime.combine(occurrence.date(), time(), zone)
            occ_utc = occ_local.astimezone(timezone.utc).replace(tzinfo=None)
        else:
            occ_utc = _to_utc_naive(occurrence, tz)
        if occ_utc > horizon:
            break
        if occ_utc in excluded:
            continue
        if occ_utc + duration < now:
            continue  # 已过去的实例不导入
        out.append((f"{base_uid}#{index}", occ_utc, occ_utc + duration, is_all_day))
    return out


def _delete_synced_event(session: Session, event: Event) -> None:
    session.execute(update(Checkin).where(Checkin.event_id == event.id).values(event_id=None))
    session.execute(update(Interaction).where(Interaction.event_id == event.id).values(event_id=None))
    session.execute(update(RescheduleLog).where(RescheduleLog.new_event_id == event.id).values(new_event_id=None))
    session.execute(delete(RescheduleLog).where(RescheduleLog.event_id == event.id))
    session.execute(delete(EventSyncLink).where(EventSyncLink.event_id == event.id))
    session.execute(update(SyncOutbox).where(SyncOutbox.event_id == event.id).values(event_id=None))
    session.delete(event)


def delete_source_events(session: Session, user_id: int, sync_source_id: int) -> int:
    """Safely remove all events imported by a specific CalendarSource."""
    events = list(
        session.scalars(
            select(Event).where(
                Event.user_id == user_id,
                Event.sync_source_id == sync_source_id,
            )
        )
    )
    for ev in events:
        _delete_synced_event(session, ev)
    session.flush()
    return len(events)


def _import_ics(
    session: Session,
    user_id: int,
    content: bytes,
    tz: str,
    now: datetime | None = None,
    slot_allowed: Callable[[datetime, datetime], bool] | None = None,
    sync_source_id: int | None = None,
    prune_missing: bool = False,
    all_day_mode: str = "ignore",
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
        expanded = _expand(component, tz, now, budget, all_day_mode=all_day_mode)
        count += len(expanded or [])
        if count > MAX_EVENTS:
            raise ValueError("Too many expanded events")
        prepared.append((component, expanded))
    if sync_source_id is not None:
        existing_query = select(Event).where(
            Event.user_id == user_id,
            Event.sync_source_id == sync_source_id,
            Event.uid.is_not(None),
        )
        event_source = "sync"
    else:
        existing_query = select(Event).where(
            Event.user_id == user_id,
            Event.source == "import",
            Event.sync_source_id.is_(None),
            Event.uid.is_not(None),
        )
        event_source = "import"
    existing = {ev.uid: ev for ev in session.scalars(existing_query)}
    seen_uids: set[str] = set()
    result = ImportResult()
    for component, expanded in prepared:
        if not expanded:
            status = str(component.get("STATUS") or "").strip().upper()
            if status != "CANCELLED":
                result.skipped += 1
            continue
        title = str(component.get("SUMMARY") or "Imported event").strip()[:200] or "Imported event"
        for uid, start_utc, end_utc, is_all_day in expanded:
            if end_utc <= start_utc:
                end_utc = start_utc + timedelta(minutes=DEFAULT_MINUTES)
            blocks_habit = not (is_all_day and all_day_mode == "show")
            if blocks_habit and slot_allowed is not None and not slot_allowed(start_utc, end_utc):
                result.skipped += 1
                continue
            seen_uids.add(uid)
            event = existing.get(uid)
            if event is not None:
                event.title = title
                event.start_utc = start_utc
                event.end_utc = end_utc
                event.all_day = is_all_day
                if sync_source_id is not None and event.status == "skipped":
                    event.status = "planned"
                result.updated += 1
            else:
                event = Event(
                    user_id=user_id,
                    title=title,
                    start_utc=start_utc,
                    end_utc=end_utc,
                    kind="external",
                    source=event_source,
                    uid=uid,
                    sync_source_id=sync_source_id,
                    all_day=is_all_day,
                )
                session.add(event)
                existing[uid] = event
                result.added += 1
    if prune_missing and sync_source_id is not None:
        for uid, ev in list(existing.items()):
            if uid not in seen_uids and ev.end_utc >= now:
                _delete_synced_event(session, ev)
                result.removed += 1
    session.flush()
    return result


def import_ics(
    session,
    user_id,
    content,
    tz,
    now=None,
    *,
    slot_allowed=None,
    sync_source_id=None,
    prune_missing=False,
    all_day_mode="ignore",
):
    # Savepoint keeps the entire import atomic even if a caller catches errors.
    with session.begin_nested():
        return _import_ics(
            session,
            user_id,
            content,
            tz,
            now,
            slot_allowed,
            sync_source_id=sync_source_id,
            prune_missing=prune_missing,
            all_day_mode=all_day_mode,
        )
