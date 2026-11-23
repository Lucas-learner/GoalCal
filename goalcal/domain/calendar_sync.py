"""Calendar synchronization domain: outbound .ics feed + pluggable inbound providers.

Phase 1 implements:
- Read-only outbound .ics subscription feeds (GoalCal -> Apple / Google / Outlook / etc.)
- Periodic & on-demand inbound ICS URL subscriptions via safe_fetch (External -> GoalCal)

The CalendarSource + SyncProvider abstraction is intentionally provider-agnostic so
future native bidirectional providers (Google OAuth, Apple CalDAV, Microsoft Graph)
plug into the same persistence, scheduling, and UI layers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import logging
import secrets
import threading
from typing import Protocol

from icalendar import Calendar, Event as ICalEvent, vDuration
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import db, memory
from ..models import CalendarFeed, CalendarSource, Event, Goal, Step, User
from ..safe_fetch import FetchError, FetchResponse, fetch_ics_conditional, normalize_ics_url
from ..timeutil import now_utc
from ..web.models import HabitOccurrence
from .ics_import import ImportResult, delete_source_events, import_ics

UTC = timezone.utc
MAX_SOURCES_PER_USER = 10
MIN_SYNC_INTERVAL_MINUTES = 15
MAX_SYNC_INTERVAL_MINUTES = 1440
DEFAULT_SYNC_INTERVAL_MINUTES = 30
FEED_PAST_DAYS = 30
FEED_FUTURE_DAYS = 180
FEED_TOUCH_INTERVAL_SECONDS = 300

_LOG = logging.getLogger("goalcal.calendar_sync")
_IN_FLIGHT_LOCK = threading.Lock()
_IN_FLIGHT_SOURCES: set[int] = set()


class SyncProvider(Protocol):
    name: str

    def normalize_url(self, url: str) -> str: ...

    def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResponse: ...


class IcsUrlProvider:
    name = "ics_url"

    def normalize_url(self, url: str) -> str:
        return normalize_ics_url(url)

    def fetch(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResponse:
        return fetch_ics_conditional(
            url,
            etag=etag,
            last_modified=last_modified,
            allow_webcal=True,
        )


PROVIDERS: dict[str, SyncProvider] = {
    "ics_url": IcsUrlProvider(),
}


def get_provider(name: str) -> SyncProvider:
    provider = PROVIDERS.get(name)
    if provider is None:
        raise ValueError(f"Unsupported calendar sync provider: {name}")
    return provider


def new_feed_token() -> str:
    return secrets.token_urlsafe(32)


def _as_utc_aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _as_utc_naive(dt: datetime) -> datetime:
    return _as_utc_aware(dt).replace(tzinfo=None)


def _status_label(status: str) -> str:
    return {
        "planned": "Planned",
        "moved": "Rescheduled",
        "done": "Done",
        "partial": "Partial",
        "missed": "Missed",
    }.get(status, status)


def build_ics_feed(
    session: Session,
    user: User,
    feed: CalendarFeed,
    *,
    now: datetime | None = None,
    base_url: str | None = None,
) -> tuple[bytes, str]:
    """Render a deterministic RFC 5545 VCALENDAR + ETag for a user's outbound feed."""
    now_naive = _as_utc_naive(now or now_utc())
    window_start = now_naive - timedelta(days=FEED_PAST_DAYS)
    window_end = now_naive + timedelta(days=FEED_FUTURE_DAYS)

    cal = Calendar()
    cal.add("version", "2.0")
    cal.add("prodid", "-//GoalCal//Calendar Sync//EN")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", "GoalCal")
    cal.add("x-wr-timezone", user.tz or "Asia/Shanghai")
    cal.add("refresh-interval", vDuration(timedelta(minutes=15)), parameters={"VALUE": "DURATION"})
    cal.add("x-published-ttl", vDuration(timedelta(minutes=15)))

    rows = list(
        session.scalars(
            select(Event)
            .where(
                Event.user_id == user.id,
                Event.end_utc >= window_start,
                Event.start_utc <= window_end,
            )
            .order_by(Event.start_utc, Event.id)
        )
    )

    eligible: list[Event] = []
    for ev in rows:
        if ev.status == "skipped":
            continue
        # Never re-export imported or synced external events (prevents sync loops).
        if ev.sync_source_id is not None or ev.source in {"import", "sync"}:
            continue
        if ev.kind == "block" and not feed.include_blocks:
            continue
        if ev.kind == "habit" and not feed.include_habits:
            continue
        if ev.kind == "external" and not feed.include_manual:
            continue
        if ev.kind not in {"block", "habit", "external"}:
            continue
        eligible.append(ev)

    step_ids = {ev.step_id for ev in eligible if ev.step_id is not None}
    steps_by_id: dict[int, Step] = {}
    goals_by_id: dict[int, Goal] = {}
    if step_ids:
        steps = list(session.scalars(select(Step).where(Step.id.in_(step_ids))))
        steps_by_id = {s.id: s for s in steps}
        goal_ids = {s.goal_id for s in steps}
        if goal_ids:
            goals = list(session.scalars(select(Goal).where(Goal.id.in_(goal_ids))))
            goals_by_id = {g.id: g for g in goals}

    habit_event_ids = {ev.id for ev in eligible if ev.kind == "habit"}
    occ_by_event_id: dict[int, HabitOccurrence] = {}
    if habit_event_ids:
        occs = list(
            session.scalars(
                select(HabitOccurrence).where(
                    HabitOccurrence.user_id == user.id,
                    HabitOccurrence.event_id.in_(habit_event_ids),
                )
            )
        )
        occ_by_event_id = {occ.event_id: occ for occ in occs if occ.event_id is not None}

    app_url = f"{base_url.rstrip('/')}/app/calendar" if base_url else None

    for ev in eligible:
        vevent = ICalEvent()
        vevent.add("uid", f"goalcal-event-{ev.id}@goalcal.app")
        stamp = _as_utc_aware(ev.resolved_at or ev.created_at or ev.start_utc)
        vevent.add("dtstamp", stamp)
        vevent.add("last-modified", stamp)
        vevent.add("dtstart", _as_utc_aware(ev.start_utc))
        vevent.add("dtend", _as_utc_aware(ev.end_utc))
        vevent.add("status", "CONFIRMED")

        effective_status = ev.status
        category = "Event"
        summary = ev.title
        desc_lines: list[str] = []

        if ev.kind == "block":
            category = "Block"
            step = steps_by_id.get(ev.step_id) if ev.step_id is not None else None
            goal = goals_by_id.get(step.goal_id) if step is not None else None
            if goal is not None:
                desc_lines.append(f"Goal: {goal.title}")
            if step is not None and step.milestone:
                desc_lines.append(f"Phase: {step.milestone}")
            desc_lines.append(f"Status: {_status_label(effective_status)}")
        elif ev.kind == "habit":
            category = "Habit"
            occ = occ_by_event_id.get(ev.id)
            if occ is not None:
                effective_status = "planned" if occ.status == "pending" else occ.status
            desc_lines.append("Type: Habit")
            desc_lines.append(f"Status: {_status_label(effective_status)}")
        else:
            desc_lines.append("Type: Calendar event")

        if effective_status == "done" and not summary.startswith("✓ "):
            summary = f"✓ {summary}"

        if app_url:
            desc_lines.append(f"GoalCal: {app_url}")

        vevent.add("summary", summary)
        if desc_lines:
            vevent.add("description", "\n".join(desc_lines))
        vevent.add("categories", ["GoalCal", category])
        cal.add_component(vevent)

    raw = cal.to_ical()
    etag = f'"{hashlib.sha256(raw).hexdigest()[:24]}"'
    return raw, etag


def fetch_source_content(
    provider_name: str,
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
) -> FetchResponse:
    """Run network fetch for a sync source (must be called OUTSIDE SQLite write locks)."""
    provider = get_provider(provider_name)
    return provider.fetch(url, etag=etag, last_modified=last_modified)


def apply_source_sync(
    session: Session,
    user_id: int,
    tz: str,
    source: CalendarSource,
    fetched: FetchResponse,
    *,
    now: datetime,
    slot_allowed=None,
) -> ImportResult:
    """Apply fetched calendar payload into SQLite for a single CalendarSource."""
    now_naive = _as_utc_naive(now)
    source.last_synced_at = now_naive
    source.updated_at = now_naive
    if fetched.not_modified:
        source.last_status = "ok"
        source.last_error = ""
        if fetched.etag is not None:
            source.etag = fetched.etag[:255]
        if fetched.last_modified is not None:
            source.last_modified = fetched.last_modified[:128]
        session.flush()
        return ImportResult()

    assert fetched.content is not None
    result = import_ics(
        session,
        user_id,
        fetched.content,
        tz,
        now=now_naive,
        slot_allowed=slot_allowed,
        sync_source_id=source.id,
        prune_missing=True,
    )
    source.last_status = "ok"
    source.last_error = ""
    source.last_added = result.added
    source.last_updated = result.updated
    source.last_removed = result.removed
    source.last_skipped = result.skipped
    source.etag = fetched.etag[:255] if fetched.etag else None
    source.last_modified = fetched.last_modified[:128] if fetched.last_modified else None
    session.flush()
    return result


def record_source_error(source: CalendarSource, message: str, *, now: datetime) -> None:
    now_naive = _as_utc_naive(now)
    source.last_synced_at = now_naive
    source.updated_at = now_naive
    source.last_status = "error"
    source.last_error = (message or "Sync failed")[:500]


def reserved_habit_spans(session: Session, user_id: int, now_naive: datetime) -> list[tuple[datetime, datetime]]:
    prefs = memory.get_scheduler_preferences(session, user_id, now=now_naive)
    buffer_min = (
        prefs.web_availability.get("buffer_minutes", prefs.buffer_minutes)
        if prefs.web_availability is not None
        else prefs.buffer_minutes
    )
    pad = timedelta(minutes=int(buffer_min))
    events = session.scalars(
        select(Event).where(
            Event.user_id == user_id,
            Event.kind == "habit",
            Event.status.in_(("planned", "moved")),
            Event.end_utc > now_naive,
        )
    )
    return [(ev.start_utc - pad, ev.end_utc + pad) for ev in events]


def try_acquire_source(source_id: int) -> bool:
    with _IN_FLIGHT_LOCK:
        if source_id in _IN_FLIGHT_SOURCES:
            return False
        _IN_FLIGHT_SOURCES.add(source_id)
        return True


def release_source(source_id: int) -> None:
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT_SOURCES.discard(source_id)


def sync_due_sources_once(*, now: datetime | None = None) -> int:
    """Periodic background tick: sync all due enabled sources without holding write locks during I/O."""
    now_aware = _as_utc_aware(now or now_utc())
    now_naive = now_aware.replace(tzinfo=None)

    candidates: list[tuple[int, int, str, str, str, str | None, str | None]] = []
    session = db.new_session()
    try:
        rows = session.execute(
            select(CalendarSource, User)
            .join(User, CalendarSource.user_id == User.id)
            .where(
                CalendarSource.enabled.is_(True),
                User.disabled.is_(False),
            )
        ).all()
        for source, user in rows:
            interval = max(MIN_SYNC_INTERVAL_MINUTES, int(source.sync_interval_minutes or DEFAULT_SYNC_INTERVAL_MINUTES))
            if source.last_synced_at is not None and (now_naive - source.last_synced_at) < timedelta(minutes=interval):
                continue
            candidates.append(
                (source.id, user.id, user.tz or "Asia/Shanghai", source.provider, source.url, source.etag, source.last_modified)
            )
    finally:
        session.close()

    synced = 0
    for source_id, user_id, tz, provider_name, url, etag, last_modified in candidates:
        if not try_acquire_source(source_id):
            continue
        try:
            fetched: FetchResponse | None = None
            err_msg: str | None = None
            try:
                fetched = fetch_source_content(provider_name, url, etag=etag, last_modified=last_modified)
            except (FetchError, ValueError) as exc:
                err_msg = str(exc)
            except Exception as exc:
                err_msg = f"Unexpected fetch error ({type(exc).__name__})"

            with db.get_engine().connect() as connection:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                ws = Session(bind=connection, expire_on_commit=False, join_transaction_mode="rollback_only")
                try:
                    source = ws.get(CalendarSource, source_id)
                    if source is None or not source.enabled:
                        connection.rollback()
                        continue
                    if err_msg is not None or fetched is None:
                        record_source_error(source, err_msg or "Calendar download failed", now=now_aware)
                    else:
                        reserved = reserved_habit_spans(ws, user_id, now_naive)
                        try:
                            apply_source_sync(
                                ws,
                                user_id,
                                tz,
                                source,
                                fetched,
                                now=now_aware,
                                slot_allowed=lambda s, e, r=reserved: not any(s < hi and e > lo for lo, hi in r),
                            )
                        except ValueError as exc:
                            record_source_error(source, str(exc), now=now_aware)
                    ws.flush()
                    connection.commit()
                    synced += 1
                except BaseException:
                    connection.rollback()
                    _LOG.warning("calendar_sync_commit_failed source_id=%s", source_id, exc_info=True)
                finally:
                    ws.close()
        finally:
            release_source(source_id)
    return synced


def serve_feed_by_token(
    token: str,
    *,
    if_none_match: str | None = None,
    base_url: str | None = None,
    now: datetime | None = None,
) -> tuple[int, bytes, str | None]:
    """Look up an active CalendarFeed by token and render its ICS bytes.

    Returns (status_code, body, etag). Status is 200, 304, or 404.
    """
    now_aware = _as_utc_aware(now or now_utc())
    now_naive = now_aware.replace(tzinfo=None)
    session = db.new_session()
    feed_id: int | None = None
    should_touch = False
    try:
        row = session.execute(
            select(CalendarFeed, User)
            .join(User, CalendarFeed.user_id == User.id)
            .where(
                CalendarFeed.token == token,
                CalendarFeed.enabled.is_(True),
                User.disabled.is_(False),
            )
        ).first()
        if row is None:
            return 404, b"", None
        feed, user = row
        feed_id = feed.id
        if feed.last_accessed_at is None or (now_naive - feed.last_accessed_at).total_seconds() >= FEED_TOUCH_INTERVAL_SECONDS:
            should_touch = True
        body, etag = build_ics_feed(session, user, feed, now=now_aware, base_url=base_url)
    finally:
        session.close()

    if should_touch and feed_id is not None:
        try:
            with db.get_engine().connect() as connection:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                ws = Session(bind=connection, expire_on_commit=False, join_transaction_mode="rollback_only")
                try:
                    f = ws.get(CalendarFeed, feed_id)
                    if f is not None:
                        f.last_accessed_at = now_naive
                        ws.flush()
                    connection.commit()
                except BaseException:
                    connection.rollback()
                finally:
                    ws.close()
        except Exception:
            pass  # Best-effort telemetry; never fail a feed response.

    if if_none_match and if_none_match.strip() == etag:
        return 304, b"", etag
    return 200, body, etag


__all__ = [
    "DEFAULT_SYNC_INTERVAL_MINUTES",
    "MAX_SOURCES_PER_USER",
    "MAX_SYNC_INTERVAL_MINUTES",
    "MIN_SYNC_INTERVAL_MINUTES",
    "PROVIDERS",
    "IcsUrlProvider",
    "SyncProvider",
    "apply_source_sync",
    "build_ics_feed",
    "delete_source_events",
    "fetch_source_content",
    "get_provider",
    "new_feed_token",
    "record_source_error",
    "release_source",
    "reserved_habit_spans",
    "serve_feed_by_token",
    "sync_due_sources_once",
    "try_acquire_source",
]
