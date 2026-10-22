from __future__ import annotations

import difflib
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Event
from ..timeutil import as_local, as_utc_naive, now_utc as _utcnow
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
    if end <= start:
        # LLM 偶尔会给出 end <= start；兜底成 1 小时，避免写入非法事件
        end = start + timedelta(minutes=60)
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


def _best_match(events: list[Event], now_utc: datetime | None) -> Event:
    """同名事件可能有多个：优先未来最近的一个，否则取最近发生的。"""
    if now_utc is not None:
        upcoming = [ev for ev in events if ev.start_utc >= now_utc]
        if upcoming:
            return min(upcoming, key=lambda ev: ev.start_utc)
    return max(events, key=lambda ev: ev.start_utc)


def find_event(
    session: Session,
    user_id: int,
    hint: str,
    now_utc: datetime | None = None,
) -> Event | None:
    """按标题模糊找事件，给 Move/Delete 用。

    now_utc 传入后优先匹配未来事件，避免“改到周五”改到几个月前的同名事件。
    """
    events = list(session.scalars(select(Event).where(Event.user_id == user_id)))
    if not events:
        return None
    hint_l = hint.lower().strip()
    if not hint_l:
        return None

    substring = [
        ev for ev in events if hint_l in ev.title.lower() or ev.title.lower() in hint_l
    ]
    if substring:
        return _best_match(substring, now_utc)

    by_title: dict[str, list[Event]] = {}
    for ev in events:
        by_title.setdefault(ev.title.lower(), []).append(ev)
    match = difflib.get_close_matches(hint_l, list(by_title), n=1, cutoff=0.5)
    if match:
        return _best_match(by_title[match[0]], now_utc)
    return None


def move_event(session: Session, event: Event, new_start: datetime, tz: str) -> Event:
    duration = event.end_utc - event.start_utc
    event.start_utc = as_utc_naive(new_start, tz)
    event.end_utc = event.start_utc + duration
    event.status = "moved"
    session.flush()
    return event


def delete_event(session: Session, event: Event) -> None:
    """硬删事件。scheduled 由未来目标块推导，步骤状态无需回写。"""
    session.delete(event)
    session.flush()


def cancel_future_blocks(
    session: Session, step_id: int, reference_utc: datetime | None = None
) -> int:
    """取消某步骤的未来目标块，返回删除数量。"""
    reference = reference_utc or _utcnow()
    events = list(
        session.scalars(
            select(Event).where(
                Event.step_id == step_id,
                Event.kind == "block",
                Event.start_utc >= reference,
            )
        )
    )
    for ev in events:
        delete_event(session, ev)
    return len(events)


def cancel_blocks(session: Session, step_id: int) -> int:
    """取消某步骤的所有目标块（用于重新排期，避免同一步骤叠加多个块）。"""
    events = list(
        session.scalars(
            select(Event).where(Event.step_id == step_id, Event.kind == "block")
        )
    )
    for ev in events:
        delete_event(session, ev)
    return len(events)


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
