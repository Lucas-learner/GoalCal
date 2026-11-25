"""Proactive reminder preferences and native calendar alarm helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from icalendar import Alarm
from sqlalchemy.orm import Session

from .. import memory
from ..models import UserPreference

UTC = timezone.utc

REMINDERS_SCOPE = "reminders"
CALENDAR_ALARMS_KEY = "calendar_alarms"
EMAIL_DIGESTS_KEY = "email_digests"
MIN_ALARM_MINUTES = 0
MAX_ALARM_MINUTES = 1440

DEFAULT_CALENDAR_ALARMS: dict[str, Any] = {
    "enabled": True,
    "block_minutes": 10,
    "habit_minutes": 10,
    "manual_minutes": None,
}

DEFAULT_EMAIL_DIGESTS: dict[str, Any] = {
    "enabled": False,
    "morning_focus": False,
    "morning_time": "08:30",
    "evening_wrapup": True,
    "evening_time": "21:00",
    "weekly_habit_nudge": True,
    "goal_stall_nudge": True,
    "weekly_review": False,
    "weekly_review_time": "19:30",
    "quiet_start": "22:30",
    "quiet_end": "07:30",
    "daily_cap": 3,
}

_MINUTE_FIELDS = ("block_minutes", "habit_minutes", "manual_minutes")
_HHMM_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
_EMAIL_BOOL_FIELDS = (
    "enabled",
    "morning_focus",
    "evening_wrapup",
    "weekly_habit_nudge",
    "goal_stall_nudge",
    "weekly_review",
)
_EMAIL_TIME_FIELDS = (
    "morning_time",
    "evening_time",
    "weekly_review_time",
    "quiet_start",
    "quiet_end",
)


def _validate_minutes(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not (MIN_ALARM_MINUTES <= value <= MAX_ALARM_MINUTES):
        raise ValueError(f"{field} must be null or an integer between {MIN_ALARM_MINUTES} and {MAX_ALARM_MINUTES}")
    return value


def normalize_calendar_alarms(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("calendar_alarms must be an object")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    out: dict[str, Any] = {"enabled": enabled}
    for field in _MINUTE_FIELDS:
        out[field] = _validate_minutes(raw.get(field), field)
    return out


def get_calendar_alarms_with_row(
    session: Session,
    user_id: int,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], UserPreference | None]:
    prefs = memory.list_preferences(session, user_id, scope=REMINDERS_SCOPE, now=now)
    row = next((p for p in prefs if p.key == CALENDAR_ALARMS_KEY), None)
    if row is None:
        return dict(DEFAULT_CALENDAR_ALARMS), None
    try:
        parsed = json.loads(row.value)
        return normalize_calendar_alarms(parsed), row
    except (ValueError, TypeError):
        return dict(DEFAULT_CALENDAR_ALARMS), row


def get_calendar_alarms(
    session: Session,
    user_id: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    alarms, _ = get_calendar_alarms_with_row(session, user_id, now=now)
    return alarms


def set_calendar_alarms(
    session: Session,
    user_id: int,
    value: dict[str, Any],
) -> tuple[dict[str, Any], UserPreference]:
    normalized = normalize_calendar_alarms(value)
    row = memory.set_preference(
        session,
        user_id,
        CALENDAR_ALARMS_KEY,
        normalized,
        scope=REMINDERS_SCOPE,
        source="explicit",
    )
    return normalized, row


def _validate_hhmm(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _HHMM_RE.fullmatch(value.strip()):
        raise ValueError(f"{field} must use HH:MM 24-hour format")
    return value.strip()


def normalize_email_digests(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("email_digests must be an object")
    out: dict[str, Any] = {}
    for field in _EMAIL_BOOL_FIELDS:
        val = raw.get(field, DEFAULT_EMAIL_DIGESTS[field])
        if not isinstance(val, bool):
            raise ValueError(f"{field} must be a boolean")
        out[field] = val
    for field in _EMAIL_TIME_FIELDS:
        val = raw.get(field, DEFAULT_EMAIL_DIGESTS[field])
        out[field] = _validate_hhmm(val, field)
    cap = raw.get("daily_cap", DEFAULT_EMAIL_DIGESTS["daily_cap"])
    if isinstance(cap, bool) or not isinstance(cap, int) or not (1 <= cap <= 10):
        raise ValueError("daily_cap must be an integer between 1 and 10")
    out["daily_cap"] = cap
    return out


def get_email_digests_with_row(
    session: Session,
    user_id: int,
    *,
    now: datetime | None = None,
) -> tuple[dict[str, Any], UserPreference | None]:
    prefs = memory.list_preferences(session, user_id, scope=REMINDERS_SCOPE, now=now)
    row = next((p for p in prefs if p.key == EMAIL_DIGESTS_KEY), None)
    if row is None:
        return dict(DEFAULT_EMAIL_DIGESTS), None
    try:
        parsed = json.loads(row.value)
        return normalize_email_digests(parsed), row
    except (ValueError, TypeError):
        return dict(DEFAULT_EMAIL_DIGESTS), row


def get_email_digests(
    session: Session,
    user_id: int,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    digests, _ = get_email_digests_with_row(session, user_id, now=now)
    return digests


def set_email_digests(
    session: Session,
    user_id: int,
    value: dict[str, Any],
) -> tuple[dict[str, Any], UserPreference]:
    normalized = normalize_email_digests(value)
    row = memory.set_preference(
        session,
        user_id,
        EMAIL_DIGESTS_KEY,
        normalized,
        scope=REMINDERS_SCOPE,
        source="explicit",
    )
    return normalized, row


def resolve_event_alarm_minutes(
    alarms: dict[str, Any],
    *,
    kind: str,
    effective_status: str,
    all_day: bool = False,
) -> int | None:
    if not alarms.get("enabled") or all_day or effective_status not in ("planned", "moved"):
        return None
    field_map = {
        "block": "block_minutes",
        "habit": "habit_minutes",
        "external": "manual_minutes",
    }
    field = field_map.get(kind)
    if field is None:
        return None
    raw_minutes = alarms.get(field)
    if raw_minutes is None or isinstance(raw_minutes, bool) or not isinstance(raw_minutes, int):
        return None
    if not (MIN_ALARM_MINUTES <= raw_minutes <= MAX_ALARM_MINUTES):
        return None
    return raw_minutes


def format_alarm_description(summary: str, outcome: str | None = None) -> str:
    clean_summary = (summary or "GoalCal").strip() or "GoalCal"
    clean_outcome = (outcome or "").strip()
    if clean_outcome:
        return f"{clean_summary} — {clean_outcome[:120]}"[:200]
    return clean_summary[:200]


def public_app_base_url(base_url: str | None = None) -> str | None:
    raw = (base_url if base_url is not None else os.getenv("GOALCAL_PUBLIC_URL", "")).strip().rstrip("/")
    if not raw:
        raw = os.getenv("GOALCAL_PUBLIC_URL", "").strip().rstrip("/")
    if raw.startswith("https://") or raw.startswith("http://"):
        return raw
    return None


def event_deep_link(
    base_url: str | None,
    event_id: int,
    start_utc: datetime,
    tz: str | None,
) -> str | None:
    base = public_app_base_url(base_url)
    if not base:
        return None
    aware_utc = start_utc.replace(tzinfo=UTC) if start_utc.tzinfo is None else start_utc.astimezone(UTC)
    try:
        zone = ZoneInfo((tz or "").strip() or "Asia/Shanghai")
    except (ZoneInfoNotFoundError, ValueError):
        zone = UTC
    local_date = aware_utc.astimezone(zone).date().isoformat()
    return f"{base}/app/calendar?date={local_date}&event={event_id}"


def build_valarm(minutes: int, description: str) -> Alarm:
    alarm = Alarm()
    alarm.add("action", "DISPLAY")
    alarm.add("trigger", timedelta(minutes=-minutes) if minutes > 0 else timedelta(0))
    alarm.add("description", (description or "GoalCal").strip()[:200] or "GoalCal")
    return alarm


__all__ = [
    "CALENDAR_ALARMS_KEY",
    "DEFAULT_CALENDAR_ALARMS",
    "DEFAULT_EMAIL_DIGESTS",
    "EMAIL_DIGESTS_KEY",
    "MAX_ALARM_MINUTES",
    "MIN_ALARM_MINUTES",
    "REMINDERS_SCOPE",
    "build_valarm",
    "event_deep_link",
    "format_alarm_description",
    "get_calendar_alarms",
    "get_calendar_alarms_with_row",
    "get_email_digests",
    "get_email_digests_with_row",
    "normalize_calendar_alarms",
    "normalize_email_digests",
    "public_app_base_url",
    "resolve_event_alarm_minutes",
    "set_calendar_alarms",
    "set_email_digests",
]
