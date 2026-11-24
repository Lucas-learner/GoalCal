"""Bidirectional calendar sync engine: loop-free pull merge, outbox push, token persistence."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import db
from ..crypto import CredentialError, decrypt_secrets, encrypt_secrets
from ..models import (
    CalendarConnection,
    CalendarSource,
    Event,
    EventSyncLink,
    Goal,
    Step,
    SyncOutbox,
    User,
)
from ..timeutil import now_utc
from ..web.models import HabitOccurrence, WebExecution
from .calendar_sync import (
    record_source_error,
    release_source,
    reserved_habit_spans,
    try_acquire_source,
)
from .ics_import import ImportResult, _delete_synced_event
from .sync_providers import (
    CalDavAdapter,
    GoogleCalendarAdapter,
    OutlookCalendarAdapter,
    PullResult,
    PushResult,
    RemoteEvent,
    SyncAuthError,
    SyncProviderError,
)

UTC = timezone.utc
_LOG = logging.getLogger("goalcal.bidirectional_sync")
_BACKOFF_SECONDS = (15, 60, 300, 900, 3600)


def _as_utc_naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo is None else dt.astimezone(UTC).replace(tzinfo=None)


def _utc_iso_z(dt: datetime) -> str:
    aware = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return aware.isoformat().replace("+00:00", "Z")


def canonical_event_hash(
    title: str,
    start_utc: datetime,
    end_utc: datetime,
    status: str,
    all_day: bool = False,
) -> str:
    raw = json.dumps(
        [
            title.strip(),
            int(_as_utc_naive(start_utc).replace(tzinfo=UTC).timestamp()),
            int(_as_utc_naive(end_utc).replace(tzinfo=UTC).timestamp()),
            status,
            bool(all_day),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def is_exportable_goalcal_event(event: Event) -> bool:
    if event.sync_source_id is not None or event.source in {"import", "sync"}:
        return False
    return event.kind in {"block", "habit", "external"}


def build_push_payload(session: Session, user: User, event: Event, *, base_url: str | None = None) -> dict:
    effective_status = event.status
    summary = event.title
    desc_lines: list[str] = []

    if event.kind == "block":
        step = session.get(Step, event.step_id) if event.step_id is not None else None
        goal = session.get(Goal, step.goal_id) if step is not None else None
        if goal is not None:
            desc_lines.append(f"Goal: {goal.title}")
        if step is not None and step.milestone:
            desc_lines.append(f"Phase: {step.milestone}")
        desc_lines.append(f"Status: {effective_status}")
    elif event.kind == "habit":
        occ = session.scalar(
            select(HabitOccurrence).where(
                HabitOccurrence.user_id == user.id,
                HabitOccurrence.event_id == event.id,
            )
        )
        if occ is not None:
            effective_status = "planned" if occ.status == "pending" else occ.status
        desc_lines.append("Type: Habit")
        desc_lines.append(f"Status: {effective_status}")
    else:
        desc_lines.append("Type: Calendar event")

    if effective_status == "done" and not summary.startswith("✓ "):
        summary = f"✓ {summary}"

    if base_url:
        desc_lines.append(f"GoalCal: {base_url.rstrip('/')}/app/calendar")

    return {
        "event_id": event.id,
        "title": summary,
        "description": "\n".join(desc_lines),
        "start_utc": _utc_iso_z(event.start_utc),
        "end_utc": _utc_iso_z(event.end_utc),
        "all_day": bool(event.all_day),
        "content_hash": canonical_event_hash(
            summary,
            event.start_utc,
            event.end_utc,
            "active",
            bool(event.all_day),
        ),
    }


def enqueue_event_sync(
    session: Session,
    user_id: int,
    event: Event,
    *,
    action: str | None = None,
    now_naive: datetime,
    suppress_source_id: int | None = None,
) -> int:
    """Queue outbox push tasks for all enabled bidirectional GoalCal calendars of a user."""
    if not is_exportable_goalcal_event(event):
        return 0
    user = session.get(User, user_id)
    if user is None or user.disabled:
        return 0

    resolved_action = action or ("delete" if event.status == "skipped" else "upsert")
    sources = list(
        session.scalars(
            select(CalendarSource).where(
                CalendarSource.user_id == user_id,
                CalendarSource.enabled.is_(True),
                CalendarSource.writable.is_(True),
                CalendarSource.is_goalcal_calendar.is_(True),
                CalendarSource.connection_id.is_not(None),
            )
        )
    )
    if not sources:
        return 0

    payload = build_push_payload(session, user, event) if resolved_action == "upsert" else {}
    queued = 0
    for src in sources:
        if suppress_source_id is not None and src.id == suppress_source_id:
            continue
        link = session.scalar(
            select(EventSyncLink).where(
                EventSyncLink.source_id == src.id,
                EventSyncLink.event_id == event.id,
            )
        )
        if resolved_action == "delete" and link is None:
            continue
        if (
            resolved_action == "upsert"
            and link is not None
            and link.sync_state == "synced"
            and link.last_synced_hash == payload.get("content_hash")
        ):
            continue

        remote_uid = link.remote_uid if link else ""
        remote_href = link.remote_href if link else ""
        item_payload = {**payload, "remote_uid": remote_uid, "remote_href": remote_href}
        existing_pending = session.scalar(
            select(SyncOutbox).where(
                SyncOutbox.source_id == src.id,
                SyncOutbox.event_id == event.id,
                SyncOutbox.status == "pending",
            )
        )
        if existing_pending is not None:
            existing_pending.action = resolved_action
            existing_pending.remote_uid = remote_uid
            existing_pending.remote_href = remote_href
            existing_pending.payload_json = json.dumps(item_payload, ensure_ascii=False)
            existing_pending.next_attempt_at = now_naive
            existing_pending.attempts = 0
            existing_pending.last_error = ""
            existing_pending.updated_at = now_naive
        else:
            session.add(
                SyncOutbox(
                    user_id=user_id,
                    source_id=src.id,
                    event_id=event.id,
                    remote_uid=remote_uid,
                    remote_href=remote_href,
                    action=resolved_action,
                    payload_json=json.dumps(item_payload, ensure_ascii=False),
                    attempts=0,
                    next_attempt_at=now_naive,
                    status="pending",
                    created_at=now_naive,
                    updated_at=now_naive,
                )
            )
        queued += 1
    session.flush()
    return queued


def backfill_goalcal_source(session: Session, user_id: int, source: CalendarSource, *, now_naive: datetime) -> int:
    """Queue existing eligible GoalCal events into a newly connected GoalCal bidirectional calendar."""
    if not (source.enabled and source.writable and source.is_goalcal_calendar and source.connection_id):
        return 0
    user = session.get(User, user_id)
    if user is None:
        return 0
    win_start = now_naive - timedelta(days=7)
    win_end = now_naive + timedelta(days=180)
    events = list(
        session.scalars(
            select(Event)
            .where(
                Event.user_id == user_id,
                Event.status != "skipped",
                Event.end_utc >= win_start,
                Event.start_utc <= win_end,
            )
            .order_by(Event.start_utc, Event.id)
        )
    )
    count = 0
    for ev in events:
        if not is_exportable_goalcal_event(ev):
            continue
        payload = build_push_payload(session, user, ev)
        existing = session.scalar(
            select(SyncOutbox).where(
                SyncOutbox.source_id == source.id,
                SyncOutbox.event_id == ev.id,
                SyncOutbox.status == "pending",
            )
        )
        if existing is not None:
            continue
        session.add(
            SyncOutbox(
                user_id=user_id,
                source_id=source.id,
                event_id=ev.id,
                remote_uid="",
                remote_href="",
                action="upsert",
                payload_json=json.dumps(payload, ensure_ascii=False),
                attempts=0,
                next_attempt_at=now_naive,
                status="pending",
                created_at=now_naive,
                updated_at=now_naive,
            )
        )
        count += 1
    session.flush()
    return count


def _find_link(session: Session, source_id: int, remote_uid: str, remote_href: str = "") -> EventSyncLink | None:
    if remote_uid:
        row = session.scalar(
            select(EventSyncLink).where(
                EventSyncLink.source_id == source_id,
                EventSyncLink.remote_uid == remote_uid,
            )
        )
        if row is not None:
            return row
    if remote_href:
        return session.scalar(
            select(EventSyncLink).where(
                EventSyncLink.source_id == source_id,
                EventSyncLink.remote_href == remote_href,
            )
        )
    return None


def _is_event_running(session: Session, event_id: int) -> bool:
    return (
        session.scalar(
            select(WebExecution.id).where(
                WebExecution.event_id == event_id,
                WebExecution.status == "running",
            )
        )
        is not None
    )


def apply_native_pull(
    session: Session,
    user_id: int,
    tz: str,
    source: CalendarSource,
    pull_result: PullResult,
    *,
    now: datetime,
    slot_allowed=None,
) -> ImportResult:
    """Merge native pull events for either an external calendar or the bidirectional GoalCal calendar."""
    now_naive = _as_utc_naive(now)
    result = ImportResult()

    if source.is_goalcal_calendar:
        for rev in pull_result.events:
            link = _find_link(session, source.id, rev.remote_uid, rev.remote_href)
            local_ev: Event | None = session.get(Event, link.event_id) if link else None
            if local_ev is None and rev.goalcal_event_id is not None:
                candidate = session.get(Event, rev.goalcal_event_id)
                if candidate is not None and candidate.user_id == user_id and candidate.sync_source_id is None:
                    local_ev = candidate
                    if link is None and rev.remote_uid:
                        link = EventSyncLink(
                            user_id=user_id,
                            event_id=candidate.id,
                            source_id=source.id,
                            remote_uid=rev.remote_uid,
                            remote_href=rev.remote_href,
                            remote_etag=rev.remote_etag,
                            last_synced_hash="",
                            sync_state="synced",
                        )
                        session.add(link)
                        session.flush()
            if local_ev is None or local_ev.user_id != user_id:
                continue

            if _is_event_running(session, local_ev.id):
                result.skipped += 1
                continue

            if rev.cancelled:
                if local_ev.status in ("planned", "moved") and local_ev.kind != "habit":
                    local_ev.status = "skipped"
                    local_ev.resolved_at = now_naive
                    enqueue_event_sync(
                        session,
                        user_id,
                        local_ev,
                        action="delete",
                        now_naive=now_naive,
                        suppress_source_id=source.id,
                    )
                    if link is not None:
                        link.sync_state = "deleted"
                        link.last_synced_hash = canonical_event_hash(
                            local_ev.title,
                            local_ev.start_utc,
                            local_ev.end_utc,
                            "cancelled",
                            local_ev.all_day,
                        )
                    result.removed += 1
                continue

            remote_hash = canonical_event_hash(rev.title, rev.start_utc, rev.end_utc, "active", rev.all_day)
            if link is not None and link.last_synced_hash == remote_hash:
                if rev.remote_etag:
                    link.remote_etag = rev.remote_etag[:255]
                continue

            if local_ev.status in ("planned", "moved") and not rev.all_day:
                changed = local_ev.start_utc != rev.start_utc or local_ev.end_utc != rev.end_utc
                if changed:
                    local_ev.start_utc = rev.start_utc
                    local_ev.end_utc = rev.end_utc
                    local_ev.status = "moved"
                    result.updated += 1
                    # Keep hash matching what we received so our own push won't bounce back.
                    user = session.get(User, user_id)
                    if link is not None:
                        link.last_synced_hash = (
                            canonical_event_hash(
                                build_push_payload(session, user, local_ev)["title"],
                                local_ev.start_utc,
                                local_ev.end_utc,
                                "active",
                                local_ev.all_day,
                            )
                            if user is not None
                            else remote_hash
                        )
                        link.remote_etag = rev.remote_etag[:255]
                        link.remote_href = rev.remote_href or link.remote_href
                        link.sync_state = "synced"
                    enqueue_event_sync(
                        session,
                        user_id,
                        local_ev,
                        action="upsert",
                        now_naive=now_naive,
                        suppress_source_id=source.id,
                    )
                elif link is not None:
                    link.last_synced_hash = remote_hash
                    link.remote_etag = rev.remote_etag[:255]
        source.last_synced_at = now_naive
        source.updated_at = now_naive
        source.last_status = "ok"
        source.last_error = ""
        source.last_added = result.added
        source.last_updated = result.updated
        source.last_removed = result.removed
        source.last_skipped = result.skipped
        if pull_result.sync_token:
            source.sync_token = pull_result.sync_token[:500]
        session.flush()
        return result

    # External inbound calendar (read into local kind="external", source="sync")
    seen_uids: set[str] = set()
    for rev in pull_result.events:
        link = _find_link(session, source.id, rev.remote_uid, rev.remote_href)
        if rev.cancelled:
            if link is not None:
                ev = session.get(Event, link.event_id)
                if ev is not None and ev.sync_source_id == source.id:
                    _delete_synced_event(session, ev)
                    result.removed += 1
            elif rev.remote_uid:
                ev = session.scalar(
                    select(Event).where(
                        Event.user_id == user_id,
                        Event.sync_source_id == source.id,
                        Event.uid == rev.remote_uid,
                    )
                )
                if ev is not None:
                    _delete_synced_event(session, ev)
                    result.removed += 1
            continue

        if rev.all_day and source.all_day_mode == "ignore":
            continue

        blocks_habit = source.blocks_scheduling and not (rev.all_day and source.all_day_mode == "show")
        if blocks_habit and slot_allowed is not None and not slot_allowed(rev.start_utc, rev.end_utc):
            result.skipped += 1
            continue

        seen_uids.add(rev.remote_uid)
        ev = session.get(Event, link.event_id) if link else None
        if ev is None and rev.remote_uid:
            ev = session.scalar(
                select(Event).where(
                    Event.user_id == user_id,
                    Event.sync_source_id == source.id,
                    Event.uid == rev.remote_uid,
                )
            )
        end_utc = rev.end_utc if rev.end_utc > rev.start_utc else rev.start_utc + timedelta(minutes=60)
        new_hash = canonical_event_hash(rev.title, rev.start_utc, end_utc, "active", rev.all_day)
        if ev is not None:
            ev.title = rev.title[:200]
            ev.start_utc = rev.start_utc
            ev.end_utc = end_utc
            ev.all_day = rev.all_day
            if ev.status == "skipped":
                ev.status = "planned"
            result.updated += 1
        else:
            ev = Event(
                user_id=user_id,
                title=rev.title[:200],
                start_utc=rev.start_utc,
                end_utc=end_utc,
                kind="external",
                source="sync",
                uid=rev.remote_uid[:200] if rev.remote_uid else None,
                sync_source_id=source.id,
                all_day=rev.all_day,
            )
            session.add(ev)
            session.flush()
            result.added += 1

        if link is None and rev.remote_uid:
            session.add(
                EventSyncLink(
                    user_id=user_id,
                    event_id=ev.id,
                    source_id=source.id,
                    remote_uid=rev.remote_uid[:255],
                    remote_href=rev.remote_href[:1000],
                    remote_etag=rev.remote_etag[:255],
                    last_synced_hash=new_hash,
                    sync_state="synced",
                )
            )
        elif link is not None:
            link.remote_href = (rev.remote_href or link.remote_href)[:1000]
            link.remote_etag = rev.remote_etag[:255]
            link.last_synced_hash = new_hash
            link.sync_state = "synced"

    if pull_result.full_sync:
        existing_rows = list(
            session.scalars(
                select(Event).where(
                    Event.user_id == user_id,
                    Event.sync_source_id == source.id,
                    Event.end_utc >= now_naive,
                )
            )
        )
        for ev in existing_rows:
            if ev.uid and ev.uid not in seen_uids:
                _delete_synced_event(session, ev)
                result.removed += 1

    source.last_synced_at = now_naive
    source.updated_at = now_naive
    source.last_status = "ok"
    source.last_error = ""
    source.last_added = result.added
    source.last_updated = result.updated
    source.last_removed = result.removed
    source.last_skipped = result.skipped
    if pull_result.sync_token:
        source.sync_token = pull_result.sync_token[:500]
    session.flush()
    return result


def pull_native_for_source(
    provider: str,
    *,
    conn_server_url: str,
    conn_username: str,
    secrets_dict: dict,
    source_url: str,
    remote_calendar_id: str,
    sync_token: str | None,
    all_day_mode: str,
    tz: str,
    now_naive: datetime,
) -> tuple[PullResult, dict | None, datetime | None]:
    """Perform network pull + OAuth refresh outside SQLite write locks."""
    updated_secrets: dict | None = None
    updated_expires: datetime | None = None
    active_secrets = secrets_dict

    if provider == "caldav":
        password = str(active_secrets.get("password") or "")
        pull_res = CalDavAdapter.pull(
            source_url or remote_calendar_id,
            conn_username,
            password,
            tz=tz,
            now_naive=now_naive,
            sync_token=sync_token,
            all_day_mode=all_day_mode or "ignore",
        )
        return pull_res, None, None

    if provider == "google":
        active_secrets, new_exp, mutated = GoogleCalendarAdapter.ensure_fresh_token(active_secrets)
        if mutated:
            updated_secrets, updated_expires = active_secrets, new_exp
        pull_res = GoogleCalendarAdapter.pull(
            remote_calendar_id,
            active_secrets,
            tz=tz,
            now_naive=now_naive,
            sync_token=sync_token,
            all_day_mode=all_day_mode or "ignore",
        )
        return pull_res, updated_secrets, updated_expires

    if provider == "outlook":
        active_secrets, new_exp, mutated = OutlookCalendarAdapter.ensure_fresh_token(active_secrets)
        if mutated:
            updated_secrets, updated_expires = active_secrets, new_exp
        pull_res = OutlookCalendarAdapter.pull(
            remote_calendar_id,
            active_secrets,
            tz=tz,
            now_naive=now_naive,
            sync_token=sync_token,
            all_day_mode=all_day_mode or "ignore",
        )
        return pull_res, updated_secrets, updated_expires

    raise SyncProviderError(f"Unsupported native provider: {provider}")


def sync_native_source_once(source_id: int, *, now: datetime | None = None) -> ImportResult | None:
    """Sync one native CalendarSource with network calls outside SQLite write locks."""
    if not try_acquire_source(source_id):
        return None
    now_aware = (now or now_utc()).astimezone(UTC) if (now and now.tzinfo) else (now or now_utc()).replace(tzinfo=UTC)
    now_naive = now_aware.replace(tzinfo=None)
    try:
        session = db.new_session()
        try:
            source = session.get(CalendarSource, source_id)
            if source is None or not source.enabled or not source.connection_id:
                return None
            conn = session.get(CalendarConnection, source.connection_id)
            user = session.get(User, source.user_id)
            if conn is None or user is None or user.disabled or conn.status == "reauth_required":
                return None
            provider = source.provider
            conn_id = conn.id
            conn_server_url = conn.server_url
            conn_username = conn.username
            secrets_dict = decrypt_secrets(conn.encrypted_secrets)
            source_url = source.url
            remote_cal_id = source.remote_calendar_id
            sync_token = source.sync_token
            all_day_mode = source.all_day_mode or "ignore"
            user_id = user.id
            tz = user.tz or "Asia/Shanghai"
        except CredentialError as exc:
            _mark_connection_error(source_id=source_id, message=str(exc), reauth=True, now_aware=now_aware)
            return None
        finally:
            session.close()

        pull_res: PullResult | None = None
        updated_secrets: dict | None = None
        updated_expires: datetime | None = None
        err_msg: str | None = None
        auth_failed = False
        try:
            pull_res, updated_secrets, updated_expires = pull_native_for_source(
                provider,
                conn_server_url=conn_server_url,
                conn_username=conn_username,
                secrets_dict=secrets_dict,
                source_url=source_url,
                remote_calendar_id=remote_cal_id,
                sync_token=sync_token,
                all_day_mode=all_day_mode,
                tz=tz,
                now_naive=now_naive,
            )
        except SyncAuthError as exc:
            auth_failed = True
            err_msg = str(exc)
        except (SyncProviderError, ValueError) as exc:
            err_msg = str(exc)
        except Exception as exc:
            err_msg = f"Unexpected sync error ({type(exc).__name__})"

        with db.get_engine().connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            ws = Session(bind=connection, expire_on_commit=False, join_transaction_mode="rollback_only")
            try:
                src = ws.get(CalendarSource, source_id)
                conn_row = ws.get(CalendarConnection, conn_id)
                if src is None:
                    connection.rollback()
                    return None
                if conn_row is not None and updated_secrets is not None:
                    conn_row.encrypted_secrets = encrypt_secrets(updated_secrets)
                    conn_row.token_expires_at = updated_expires
                    conn_row.status = "active"
                    conn_row.last_error = ""
                if err_msg is not None or pull_res is None:
                    record_source_error(src, err_msg or "Native calendar sync failed", now=now_aware)
                    if auth_failed and conn_row is not None:
                        conn_row.status = "reauth_required"
                        conn_row.last_error = (err_msg or "Authorization expired")[:500]
                    ws.flush()
                    connection.commit()
                    return None

                if conn_row is not None and conn_row.status != "active":
                    conn_row.status = "active"
                    conn_row.last_error = ""
                reserved = reserved_habit_spans(ws, user_id, now_naive)
                imported = apply_native_pull(
                    ws,
                    user_id,
                    tz,
                    src,
                    pull_res,
                    now=now_aware,
                    slot_allowed=lambda s, e, r=reserved: not any(s < hi and e > lo for lo, hi in r),
                )
                ws.flush()
                connection.commit()
                return imported
            except BaseException:
                connection.rollback()
                _LOG.warning("native_sync_commit_failed source_id=%s", source_id, exc_info=True)
                return None
            finally:
                ws.close()
    finally:
        release_source(source_id)


def _mark_connection_error(*, source_id: int, message: str, reauth: bool, now_aware: datetime) -> None:
    with db.get_engine().connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        ws = Session(bind=connection, expire_on_commit=False, join_transaction_mode="rollback_only")
        try:
            src = ws.get(CalendarSource, source_id)
            if src is not None:
                record_source_error(src, message, now=now_aware)
                if src.connection_id:
                    conn = ws.get(CalendarConnection, src.connection_id)
                    if conn is not None and reauth:
                        conn.status = "reauth_required"
                        conn.last_error = message[:500]
            ws.flush()
            connection.commit()
        finally:
            ws.close()


def _push_item(
    provider: str,
    *,
    conn_username: str,
    secrets_dict: dict,
    source_url: str,
    remote_calendar_id: str,
    action: str,
    payload: dict,
    remote_uid: str,
    remote_href: str,
) -> tuple[PushResult | None, dict | None, datetime | None]:
    active_secrets = secrets_dict
    updated_secrets: dict | None = None
    updated_expires: datetime | None = None

    if provider == "caldav":
        password = str(active_secrets.get("password") or "")
        if action == "delete":
            if remote_uid or remote_href:
                CalDavAdapter.push_delete(source_url or remote_calendar_id, conn_username, password, remote_uid, remote_href)
            return None, None, None
        return CalDavAdapter.push_upsert(source_url or remote_calendar_id, conn_username, password, payload), None, None

    if provider == "google":
        active_secrets, new_exp, mutated = GoogleCalendarAdapter.ensure_fresh_token(active_secrets)
        if mutated:
            updated_secrets, updated_expires = active_secrets, new_exp
        if action == "delete":
            if remote_uid:
                GoogleCalendarAdapter.push_delete(remote_calendar_id, active_secrets, remote_uid)
            return None, updated_secrets, updated_expires
        return GoogleCalendarAdapter.push_upsert(remote_calendar_id, active_secrets, payload), updated_secrets, updated_expires

    if provider == "outlook":
        active_secrets, new_exp, mutated = OutlookCalendarAdapter.ensure_fresh_token(active_secrets)
        if mutated:
            updated_secrets, updated_expires = active_secrets, new_exp
        if action == "delete":
            if remote_uid:
                OutlookCalendarAdapter.push_delete(remote_calendar_id, active_secrets, remote_uid)
            return None, updated_secrets, updated_expires
        return OutlookCalendarAdapter.push_upsert(remote_calendar_id, active_secrets, payload), updated_secrets, updated_expires

    raise SyncProviderError(f"Unsupported native push provider: {provider}")


def process_outbox_once(*, now: datetime | None = None, batch_size: int = 20) -> int:
    """Drain pending SyncOutbox items with network calls strictly outside SQLite write locks."""
    now_aware = (now or now_utc()).astimezone(UTC) if (now and now.tzinfo) else (now or now_utc()).replace(tzinfo=UTC)
    now_naive = now_aware.replace(tzinfo=None)

    jobs: list[dict] = []
    session = db.new_session()
    try:
        rows = session.execute(
            select(SyncOutbox, CalendarSource, CalendarConnection, User)
            .join(CalendarSource, SyncOutbox.source_id == CalendarSource.id)
            .join(CalendarConnection, CalendarSource.connection_id == CalendarConnection.id)
            .join(User, SyncOutbox.user_id == User.id)
            .where(
                SyncOutbox.status == "pending",
                SyncOutbox.next_attempt_at <= now_naive,
                CalendarSource.enabled.is_(True),
                CalendarSource.writable.is_(True),
                CalendarConnection.status == "active",
                User.disabled.is_(False),
            )
            .order_by(SyncOutbox.next_attempt_at, SyncOutbox.id)
            .limit(batch_size)
        ).all()
        for item, src, conn, _user in rows:
            try:
                secrets_dict = decrypt_secrets(conn.encrypted_secrets)
            except CredentialError:
                continue
            jobs.append({
                "outbox_id": item.id,
                "user_id": item.user_id,
                "source_id": src.id,
                "connection_id": conn.id,
                "provider": src.provider,
                "conn_username": conn.username,
                "secrets_dict": secrets_dict,
                "source_url": src.url,
                "remote_calendar_id": src.remote_calendar_id,
                "action": item.action,
                "event_id": item.event_id,
                "remote_uid": item.remote_uid,
                "remote_href": item.remote_href,
                "payload": json.loads(item.payload_json or "{}"),
            })
    finally:
        session.close()

    processed = 0
    for job in jobs:
        push_res: PushResult | None = None
        updated_secrets: dict | None = None
        updated_expires: datetime | None = None
        err_msg: str | None = None
        auth_failed = False
        try:
            push_res, updated_secrets, updated_expires = _push_item(
                job["provider"],
                conn_username=job["conn_username"],
                secrets_dict=job["secrets_dict"],
                source_url=job["source_url"],
                remote_calendar_id=job["remote_calendar_id"],
                action=job["action"],
                payload=job["payload"],
                remote_uid=job["remote_uid"],
                remote_href=job["remote_href"],
            )
        except SyncAuthError as exc:
            auth_failed = True
            err_msg = str(exc)
        except (SyncProviderError, ValueError) as exc:
            err_msg = str(exc)
        except Exception as exc:
            err_msg = f"Unexpected outbox push error ({type(exc).__name__})"

        with db.get_engine().connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            ws = Session(bind=connection, expire_on_commit=False, join_transaction_mode="rollback_only")
            try:
                outbox_row = ws.get(SyncOutbox, job["outbox_id"])
                conn_row = ws.get(CalendarConnection, job["connection_id"])
                if outbox_row is None or outbox_row.status != "pending":
                    connection.rollback()
                    continue
                if conn_row is not None and updated_secrets is not None:
                    conn_row.encrypted_secrets = encrypt_secrets(updated_secrets)
                    conn_row.token_expires_at = updated_expires
                    conn_row.status = "active"
                    conn_row.last_error = ""

                if err_msg is not None:
                    outbox_row.attempts += 1
                    outbox_row.last_error = err_msg[:500]
                    outbox_row.updated_at = now_naive
                    if auth_failed and conn_row is not None:
                        conn_row.status = "reauth_required"
                        conn_row.last_error = err_msg[:500]
                    if outbox_row.attempts >= outbox_row.max_attempts:
                        outbox_row.status = "dead"
                    else:
                        delay = _BACKOFF_SECONDS[min(outbox_row.attempts - 1, len(_BACKOFF_SECONDS) - 1)]
                        outbox_row.next_attempt_at = now_naive + timedelta(seconds=delay)
                    ws.flush()
                    connection.commit()
                    continue

                outbox_row.status = "done"
                outbox_row.last_error = ""
                outbox_row.updated_at = now_naive

                if job["event_id"] is not None:
                    link = ws.scalar(
                        select(EventSyncLink).where(
                            EventSyncLink.source_id == job["source_id"],
                            EventSyncLink.event_id == job["event_id"],
                        )
                    )
                    if job["action"] == "delete":
                        if link is not None:
                            ws.delete(link)
                    elif push_res is not None:
                        content_hash = str(job["payload"].get("content_hash") or "")
                        if link is None:
                            ws.add(
                                EventSyncLink(
                                    user_id=job["user_id"],
                                    event_id=job["event_id"],
                                    source_id=job["source_id"],
                                    remote_uid=push_res.remote_uid[:255],
                                    remote_href=push_res.remote_href[:1000],
                                    remote_etag=push_res.remote_etag[:255],
                                    last_synced_hash=content_hash,
                                    last_pushed_at=now_naive,
                                    sync_state="synced",
                                )
                            )
                        else:
                            link.remote_uid = push_res.remote_uid[:255] or link.remote_uid
                            link.remote_href = (push_res.remote_href or link.remote_href)[:1000]
                            link.remote_etag = push_res.remote_etag[:255]
                            link.last_synced_hash = content_hash
                            link.last_pushed_at = now_naive
                            link.sync_state = "synced"
                ws.flush()
                connection.commit()
                processed += 1
            except BaseException:
                connection.rollback()
                _LOG.warning("outbox_commit_failed outbox_id=%s", job["outbox_id"], exc_info=True)
            finally:
                ws.close()
    return processed


__all__ = [
    "apply_native_pull",
    "backfill_goalcal_source",
    "build_push_payload",
    "canonical_event_hash",
    "enqueue_event_sync",
    "is_exportable_goalcal_event",
    "process_outbox_once",
    "pull_native_for_source",
    "sync_native_source_once",
]
