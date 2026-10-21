from __future__ import annotations

import difflib
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Event
from ..timeutil import as_local, as_utc_naive
from .scheduler import BusyBlock


def create_event(
    session: Session,
    user_id: int,
    title: str,
    start: datetime,
    end: datetime,
    *,
    kind: str = "external",
    source: str = "chat",
    step_id: int | None = None,
    tz: str = "Asia/Shanghai",
) -> Event:
    ev = Event(
        user_id=user_id,
        title=title.strip()[:200],
        start_utc=as_utc_naive(start, tz),
        end_utc=as_utc_naive(end, tz),
        kind=kind,
        source=source,
        step_id=step_id,
    )
    session.add(ev)
    session.flush()
    return ev


def events_between(
    session: Session, user_id: int, start_utc: datetime, end_utc: datetime
) -> list[Event]:
    return list(
        session.scalars(
            select(Event)
            .where(
                Event.user_id == user_id,
                Event.end_utc > start_utc,
                Event.start_utc < end_utc,
            )
            .order_by(Event.start_utc)
        )
    )


def find_event(session: Session, user_id: int, hint: str) -> Event | None:
    """按标题模糊找事件，给 Move/Delete 用。"""
    events = list(session.scalars(select(Event).where(Event.user_id == user_id)))
    if not events:
        return None
    hint_l = hint.lower().strip()
    if not hint_l:
        return None
    for ev in events:
        if hint_l in ev.title.lower() or ev.title.lower() in hint_l:
            return ev
    titles = [ev.title.lower() for ev in events]
    match = difflib.get_close_matches(hint_l, titles, n=1, cutoff=0.5)
    if match:
        return events[titles.index(match[0])]
    return None


def move_event(session: Session, event: Event, new_start: datetime, tz: str) -> Event:
    duration = event.end_utc - event.start_utc
    event.start_utc = as_utc_naive(new_start, tz)
    event.end_utc = event.start_utc + duration
    event.status = "moved"
    session.flush()
    return event


def to_fullcalendar(events: list[Event], tz: str) -> list[dict]:
    out = []
    for ev in events:
        start = as_local(ev.start_utc, tz)
        end = as_local(ev.end_utc, tz)
        out.append(
            {
                "id": str(ev.id),
                "title": ev.title,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "color": {"external": "#94a3b8", "block": "#0f766e", "buffer": "#e2e8f0"}.get(
                    ev.kind, "#94a3b8"
                ),
                "extendedProps": {"kind": ev.kind, "step_id": ev.step_id, "source": ev.source},
            }
        )
    return out


def busy_blocks(
    session: Session, user_id: int, start_utc: datetime, end_utc: datetime, tz: str
) -> list[BusyBlock]:
    blocks = []
    for ev in events_between(session, user_id, start_utc, end_utc):
        blocks.append(
            BusyBlock(
                start=as_local(ev.start_utc, tz),
                end=as_local(ev.end_utc, tz),
                kind=ev.kind,
            )
        )
    return blocks
