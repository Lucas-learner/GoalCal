"""用户长期偏好记忆。

UserPreference 是通用键值记忆（JSON value + scope/source/confidence/evidence）；
白名单键与 scheduler.Preferences 字段一一对应，物化时做类型与范围校验，
过期行不生效，JSON 数组统一转成元组。没有显式时段偏好时，可从已解决的
目标块学习固定时段，但学习结果只存在于本次物化结果中，不写回数据库。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from .domain.scheduler import Preferences
from .models import Event, User, UserPreference
from .timeutil import as_local, utcnow_naive

DEFAULT_SCOPE = "global"
SCHEDULER_SCOPE = "scheduler"
_SOURCES = ("explicit", "derived", "system", "user", "agent")


class PreferenceError(ValueError):
    pass


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _as_int(name: str, lo: int, hi: int) -> Callable[[Any], int]:
    def convert(value: Any) -> int:
        if not _is_int(value) or not lo <= value <= hi:
            raise PreferenceError(f"{name} must be an integer in [{lo}, {hi}]")
        return value

    return convert


def _as_pair(name: str) -> Callable[[Any], tuple[int, int]]:
    def convert(value: Any) -> tuple[int, int]:
        if (
            not isinstance(value, (list, tuple))
            or len(value) != 2
            or not _is_int(value[0])
            or not _is_int(value[1])
            or not 0 <= value[0] < value[1] <= 24
        ):
            raise PreferenceError(f"{name} must be a [start, end] pair of hours in [0, 24]")
        return (value[0], value[1])

    return convert


def _as_windows(value: Any) -> tuple[tuple[int, int], ...]:
    if not isinstance(value, (list, tuple)):
        raise PreferenceError("preferred_windows must be a list of [start, end] hour pairs")
    return tuple(_as_pair("preferred_windows")(w) for w in value)


# scheduler.Preferences 字段白名单：key -> 校验/归一化函数
SCHEDULER_KEYS: dict[str, Callable[[Any], Any]] = {
    "day_start_hour": _as_int("day_start_hour", 0, 23),
    "day_end_hour": _as_int("day_end_hour", 1, 24),
    "max_goal_minutes_per_day": _as_int("max_goal_minutes_per_day", 1, 1440),
    "buffer_minutes": _as_int("buffer_minutes", 0, 120),
    "preferred_windows": _as_windows,
    "lunch": _as_pair("lunch"),
    "granularity": _as_int("granularity", 1, 120),
}


def _active(row: UserPreference, now: datetime) -> bool:
    return row.expires_at is None or row.expires_at > now


def _validate_meta(source: str, confidence: float, expires_at: datetime | None) -> None:
    if source not in _SOURCES:
        raise PreferenceError(f"source must be one of {_SOURCES}")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not 0.0 <= confidence <= 1.0:
        raise PreferenceError("confidence must be a number in [0, 1]")
    if expires_at is not None and not isinstance(expires_at, datetime):
        raise PreferenceError("expires_at must be a datetime or None")


def list_preferences(
    session: Session,
    user_id: int,
    *,
    scope: str | None = None,
    include_expired: bool = False,
    now: datetime | None = None,
) -> list[UserPreference]:
    now = now or utcnow_naive()
    stmt = select(UserPreference).where(UserPreference.user_id == user_id)
    if scope is not None:
        stmt = stmt.where(UserPreference.scope == scope)
    rows = list(session.scalars(stmt))
    if not include_expired:
        rows = [r for r in rows if _active(r, now)]
    return sorted(rows, key=lambda r: (r.scope, r.key))


def set_preference(
    session: Session,
    user_id: int,
    key: str,
    value: Any,
    *,
    scope: str = DEFAULT_SCOPE,
    source: str = "explicit",
    confidence: float = 1.0,
    evidence: str = "",
    expires_at: datetime | None = None,
) -> UserPreference:
    if not isinstance(key, str) or not key.strip():
        raise PreferenceError("key must be a non-empty string")
    key = key.strip()
    if not isinstance(scope, str) or not scope.strip():
        raise PreferenceError("scope must be a non-empty string")
    scope = scope.strip()
    _validate_meta(source, confidence, expires_at)
    if key in SCHEDULER_KEYS:
        value = SCHEDULER_KEYS[key](value)  # 白名单键：写入即校验并归一化
    try:
        payload = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise PreferenceError(f"value for {key!r} is not JSON-serializable") from exc
    row = session.execute(
        select(UserPreference).where(
            UserPreference.user_id == user_id,
            UserPreference.key == key,
            UserPreference.scope == scope,
        )
    ).scalar_one_or_none()
    if row is None:
        row = UserPreference(user_id=user_id, key=key, scope=scope)
        session.add(row)
    row.value = payload
    row.source = source
    row.confidence = float(confidence)
    row.evidence = evidence
    row.expires_at = expires_at
    row.updated_at = utcnow_naive()
    return row


def delete_preference(session: Session, user_id: int, key: str, *, scope: str = DEFAULT_SCOPE) -> bool:
    row = session.execute(
        select(UserPreference).where(
            UserPreference.user_id == user_id,
            UserPreference.key == key,
            UserPreference.scope == scope,
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    session.delete(row)
    return True


LEARNING_ENABLED_KEY = "learn_from_history"
_LEARNING_WINDOWS = ((6, 12), (12, 18), (18, 24))
_LEARNING_OUTCOME_WEIGHTS = {"done": 1.0, "partial": 0.5, "missed": 0.0}
_LEARNING_MIN_SAMPLES = 5
_LEARNING_MIN_SCORE = 3.0
_LEARNING_MIN_SHARE = 2.0 / 3.0


def preferred_window_insight(session: Session, user_id: int) -> dict[str, object] | None:
    user = session.get(User, user_id)
    if user is None:
        return None
    events = session.scalars(
        select(Event).where(
            Event.user_id == user_id,
            Event.kind == "block",
            Event.status.in_(tuple(_LEARNING_OUTCOME_WEIGHTS)),
        )
    )
    scores = dict.fromkeys(_LEARNING_WINDOWS, 0.0)
    sample_count = 0
    try:
        for event in events:
            local_hour = as_local(event.start_utc, user.tz).hour
            window = next((w for w in _LEARNING_WINDOWS if w[0] <= local_hour < w[1]), None)
            if window is None:
                continue
            sample_count += 1
            scores[window] += _LEARNING_OUTCOME_WEIGHTS[event.status]
    except (TypeError, ValueError, ZoneInfoNotFoundError):
        return None
    if sample_count < _LEARNING_MIN_SAMPLES:
        return None
    best_window, best_score = min(scores.items(), key=lambda item: (-item[1], item[0]))
    total_score = sum(scores.values())
    if (
        best_score < _LEARNING_MIN_SCORE
        or total_score <= 0
        or best_score / total_score < _LEARNING_MIN_SHARE
    ):
        return None
    return {"window": best_window, "samples": sample_count}


def get_scheduler_preferences(session: Session, user_id: int, *, now: datetime | None = None) -> Preferences:
    """物化排期偏好；有效显式设置优先，过期/非法行跳过，时段可只读学习。"""
    now = now or utcnow_naive()
    prefs = Preferences()
    rows = session.scalars(
        select(UserPreference).where(
            UserPreference.user_id == user_id,
            UserPreference.scope.in_((SCHEDULER_SCOPE, DEFAULT_SCOPE)),
        )
    )
    has_preferred_windows = False
    learning_enabled = True
    for row in sorted(rows, key=lambda item: item.scope == DEFAULT_SCOPE):
        if not _active(row, now):
            continue
        if row.scope == DEFAULT_SCOPE and row.key == LEARNING_ENABLED_KEY:
            try:
                learning_enabled = json.loads(row.value) is not False
            except (json.JSONDecodeError, TypeError):
                pass
            continue
        convert = SCHEDULER_KEYS.get(row.key)
        if convert is None:
            continue
        try:
            value = convert(json.loads(row.value))
        except (PreferenceError, json.JSONDecodeError, TypeError):
            continue
        setattr(prefs, row.key, value)
        if row.key == "preferred_windows":
            has_preferred_windows = True
    if learning_enabled and not has_preferred_windows:
        insight = preferred_window_insight(session, user_id)
        if insight is not None:
            prefs.preferred_windows = (insight["window"],)
    if prefs.day_end_hour <= prefs.day_start_hour:
        defaults = Preferences()
        prefs.day_start_hour = defaults.day_start_hour
        prefs.day_end_hour = defaults.day_end_hour
    return prefs
