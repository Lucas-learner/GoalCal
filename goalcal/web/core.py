"""Tenant-aware web operations. The caller owns transaction, auth and retries.

Legacy records use content versions so changes made through the original UI are
also detected. Writes never commit, delete events, or expose conversation state.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import inspect, or_, select

from .. import memory, metrics
from ..data.goal_templates import first_batch, full_steps, milestones, pick_template
from ..domain import goals as goal_service, recovery, scheduler, weekly_planner
from ..domain.calendar_sync import detect_sync_conflicts, event_blocks_scheduling
from ..i18n import format_when, normalize_lang, pick
from ..models import (
    CalendarConnection, CalendarFeed, CalendarSource, Checkin, Event, Goal,
    Interaction, Step, User, RescheduleLog,
)
from .availability import allows, validate_availability
from .models import Habit, HabitOccurrence, WebDraft, WebExecution, WebOperation, WebProposal, WebWeeklyPlan
from .transport_models import WebThread

UTC = timezone.utc
OPEN = ("planned", "moved")


class ApiError(ValueError):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()[:32]


def version_token(row) -> str:
    """Opaque version including legacy-only fields that may change elsewhere."""
    if isinstance(row, (WebDraft, WebExecution, WebProposal, WebWeeklyPlan)):
        return str(row.version)
    values = {c.key: getattr(row, c.key) for c in inspect(type(row)).columns}
    if isinstance(row, CalendarFeed):
        # Background feed polling updates last_accessed_at; do not cause false 409 conflicts on settings edits.
        values.pop("last_accessed_at", None)
    if isinstance(row, CalendarSource):
        for col in (
            "last_synced_at",
            "last_status",
            "last_error",
            "last_added",
            "last_updated",
            "last_removed",
            "last_skipped",
            "etag",
            "last_modified",
            "sync_token",
            "webhook_channel_id",
            "webhook_resource_id",
            "webhook_secret",
            "webhook_expires_at",
            "updated_at",
        ):
            values.pop(col, None)
    if isinstance(row, CalendarConnection):
        for col in ("token_expires_at", "encrypted_secrets", "updated_at"):
            values.pop(col, None)
    if isinstance(row, Goal):
        # A goal is an aggregate: a legacy step change also updates its DTO version.
        values["steps"] = [(s.id, version_token(s)) for s in sorted(row.steps, key=lambda s: (s.seq, s.id))]
    return _hash(values)


def parse_datetime(value, *, lang: str = "zh") -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ApiError("validation", pick(lang, "请输入带时区的 ISO 日期时间", "Please enter an ISO datetime with a time zone"), 422)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone required")
        return parsed.astimezone(UTC)
    except (ValueError, OverflowError) as exc:
        raise ApiError("validation", pick(lang, "请输入带时区的 ISO 日期时间", "Please enter an ISO datetime with a time zone"), 422) from exc


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _naive(value: datetime) -> datetime:
    return _utc(value).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat().replace("+00:00", "Z") if value is not None else None


def _text(value, field=None, limit=200, empty=False, *, lang: str = "zh") -> str:
    label = field or pick(lang, "标题", "Title")
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ApiError("validation", pick(lang, f"{label}需要 1 至 {limit} 个字符", f"{label} must be 1 to {limit} characters"), 422)
    return value.strip()


def _integer(value, lo, hi, field, *, lang: str = "zh") -> int:
    if type(value) is not int or not lo <= value <= hi:
        raise ApiError("validation", pick(lang, f"{field}需要 {lo} 至 {hi} 的整数", f"{field} must be an integer between {lo} and {hi}"), 422)
    return value


def _deadline(value, *, lang: str = "zh") -> date | None:
    if value is None or value == "":
        return None
    try:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError()
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ApiError("validation", pick(lang, "截止日期格式应为 YYYY-MM-DD", "Deadline must use YYYY-MM-DD format"), 422) from exc


class Service:
    def __init__(self, session, user_id: int, tz: str, now: datetime, lang: str = "zh"):
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone aware")
        self.session, self.user_id, self.tz = session, user_id, tz
        self.lang = normalize_lang(lang) or "zh"
        self.zone = ZoneInfo(tz)
        self.now = now.astimezone(UTC)
        self.now_naive = _naive(now)

    def _t(self, zh: str, en: str) -> str:
        return pick(self.lang, zh, en)

    @property
    def habits(self):
        from .habits import HabitService
        return HabitService(self)

    @property
    def calendar_sync(self):
        from .calendar_sync import CalendarSyncService
        return CalendarSyncService(self)

    @property
    def reminders(self):
        from .reminders import ReminderService
        return ReminderService(self)

    def _get(self, model, id):
        row = self.session.get(model, id)
        if row is None or (hasattr(row, "user_id") and row.user_id != self.user_id):
            raise ApiError("not_found", self._t("记录不存在", "Record not found"), 404)
        if isinstance(row, Step):
            self._get(Goal, row.goal_id)
        return row

    def check_version(self, row, body):
        if not isinstance(body, dict) or not isinstance(body.get("version"), str):
            raise ApiError("validation", self._t("需要记录版本", "Record version is required"), 422)
        if body["version"] != version_token(row):
            raise ApiError("version_conflict", self._t("内容已更新，请刷新后重试", "This item was updated elsewhere. Refresh and retry."))

    def serialize_step(self, step: Step) -> dict:
        scheduled = next(iter(self._open_step_events(step.id)), None)
        running = self._running()
        active_exec = running if running and running.step_id == step.id else None
        return {"id": step.id, "goal_id": step.goal_id, "title": step.title,
                "outcome": step.outcome, "est_min": step.est_min, "status": step.status,
                "milestone": step.milestone, "version": version_token(step),
                "scheduled_event": self.serialize_event(scheduled) if scheduled else None,
                "active_execution": self.serialize_execution(active_exec) if active_exec else None}

    def serialize_goal(self, goal: Goal) -> dict:
        return {"id": goal.id, "title": goal.title, "outcome": goal.outcome,
                "deadline": goal.deadline.isoformat() if goal.deadline else None,
                "status": goal.status, "pinned": goal.pinned, "version": version_token(goal),
                "progress": goal_service.progress(goal), "outline": goal_service.outline_phases(goal),
                "steps": [self.serialize_step(s) for s in sorted(goal.steps, key=lambda s: (s.seq, s.id))]}

    def _sources_by_id(self) -> dict[int, CalendarSource]:
        cached = getattr(self, "_cached_sources_by_id", None)
        if cached is None:
            cached = {
                s.id: s
                for s in self.session.scalars(select(CalendarSource).where(CalendarSource.user_id == self.user_id))
            }
            self._cached_sources_by_id = cached
        return cached

    def _blocks_scheduling(self, event: Event) -> bool:
        return event_blocks_scheduling(event, self._sources_by_id())

    def serialize_event(self, event: Event) -> dict:
        step = self._get(Step, event.step_id) if event.step_id is not None else None
        goal = self._get(Goal, step.goal_id) if step else None
        src = self._sources_by_id().get(event.sync_source_id) if event.sync_source_id is not None else None
        blocks_scheduling = self._blocks_scheduling(event)
        title = event.title
        if event.kind == "block" and goal is not None:
            for sep in (" · ", ": ", "："):
                prefix = f"{goal.title}{sep}"
                if title.startswith(prefix) and len(title) > len(prefix):
                    title = title[len(prefix):]
                    break
        return {"id": event.id, "title": title, "start": _iso(event.start_utc),
                "end": _iso(event.end_utc), "kind": event.kind, "status": event.status,
                "source": event.source, "sync_source_id": event.sync_source_id,
                "all_day": bool(event.all_day),
                "sync_source_name": src.name if src else None,
                "sync_source_color": src.color if src else None,
                "blocks_scheduling": blocks_scheduling,
                "step_id": event.step_id, "step_version": version_token(step) if step else None,
                "goal_id": goal.id if goal else None,
                "goal_title": goal.title if goal else None, "version": version_token(event)}

    def serialize_execution(self, execution: WebExecution) -> dict:
        if execution.habit_occurrence_id is not None:
            occ = self._get(HabitOccurrence, execution.habit_occurrence_id)
            habit = self._get(Habit, occ.habit_id)
            event = self._get(Event, execution.event_id)
            return {"id": execution.id, "event_id": execution.event_id, "step_id": None,
                    "habit_id": habit.id, "habit_occurrence_id": occ.id,
                    "goal_title": self._t("习惯", "Habit"), "step_title": event.title,
                    "started_at": _iso(execution.started_at),
                    "expected_end_at": _iso(execution.expected_end_at),
                    "status": execution.status, "version": version_token(execution)}
        step = self._get(Step, execution.step_id)
        goal = self._get(Goal, step.goal_id)
        return {"id": execution.id, "event_id": execution.event_id, "step_id": step.id,
                "habit_id": None, "habit_occurrence_id": None,
                "goal_title": goal.title, "step_title": step.title,
                "started_at": _iso(execution.started_at),
                "expected_end_at": _iso(execution.expected_end_at),
                "status": execution.status, "version": version_token(execution)}

    def serialize_proposal(self, proposal: WebProposal) -> dict:
        payload = json.loads(proposal.payload_json)
        step = self._get(Step, proposal.step_id)
        goal = self._get(Goal, proposal.goal_id)
        status = proposal.status
        if status == "open" and proposal.expires_at <= self.now_naive:
            status = "expired"
        return {"id": proposal.id, "version": version_token(proposal), "status": status,
                "goal_id": goal.id, "goal_title": goal.title, "step_id": step.id,
                "step_title": step.title, "expires_at": _iso(proposal.expires_at),
                "candidates": payload["candidates"]}

    def _events(self):
        return list(self.session.scalars(select(Event).where(Event.user_id == self.user_id)))

    def _executions(self):
        return list(self.session.scalars(select(WebExecution).where(WebExecution.user_id == self.user_id)))

    def _running(self):
        return next((r for r in self._executions() if r.status == "running"), None)

    def _ensure_not_running(self, *, event_id=None, step_id=None, goal_id=None, habit_occurrence_id=None, habit_id=None):
        for execution in self._executions():
            if execution.status != "running":
                continue
            if ((event_id is not None and execution.event_id == event_id)
                    or (step_id is not None and execution.step_id == step_id)
                    or (habit_occurrence_id is not None and execution.habit_occurrence_id == habit_occurrence_id)
                    or (habit_id is not None and execution.habit_occurrence_id is not None
                        and self._get(HabitOccurrence, execution.habit_occurrence_id).habit_id == habit_id)
                    or (goal_id is not None and execution.step_id is not None
                        and self._get(Step, execution.step_id).goal_id == goal_id)):
                raise ApiError("execution_active", self._t("请先完成当前执行的签到", "Please check in on the running session first"))

    def _active_step(self, step: Step):
        goal = self._get(Goal, step.goal_id)
        if goal.status != "active" or step.status != "todo":
            raise ApiError("invalid_state", self._t("只有进行中目标的待办步骤可以执行或排期", "Only todo steps in an active goal can be started or scheduled"))
        return goal

    def _open_step_events(self, step_id):
        return [e for e in self._events() if e.step_id == step_id and e.kind == "block" and e.status in OPEN]

    def _prefs(self):
        return memory.get_scheduler_preferences(self.session, self.user_id, now=self.now_naive)

    def _availability(self):
        prefs = self._prefs()
        if prefs.web_availability is not None:
            return prefs.web_availability
        def window(start, end):
            return [{"days": list(range(7)), "start": f"{start:02}:00", "end": f"{end:02}:00"}]
        return {"available_windows": window(prefs.day_start_hour, prefs.day_end_hour),
                "protected_windows": window(*prefs.lunch),
                "preferred_windows": [w for a, b in prefs.preferred_windows for w in window(a, b)],
                "buffer_minutes": prefs.buffer_minutes,
                "max_goal_minutes_per_day": prefs.max_goal_minutes_per_day}

    def _when(self, start, end):
        a, b = _utc(start).astimezone(self.zone), _utc(end).astimezone(self.zone)
        return format_when(a, b, self.lang)

    def _validate_interval(self, start, end):
        if end <= start or end - start > timedelta(days=7):
            raise ApiError("validation", self._t("结束须晚于开始，单个日程不能超过七天", "End time must be after start time and span at most 7 days"), 422)

    def _slot(self, start, end, *, ignore=(), step=None, goal=None, goal_block=True, habit_block=False, strict=True):
        self._validate_interval(start, end)
        start, end = _utc(start), _utc(end)
        config = self._availability()
        local_start, local_end = start.astimezone(self.zone), end.astimezone(self.zone)
        if goal_block or habit_block:
            if start < self.now:
                raise ApiError("invalid_time", self._t("执行时间不能在过去", "Execution time cannot be in the past"))
            if strict and not allows(local_start, local_end, config):
                raise ApiError("outside_availability", self._t("该时间不在可用时段，或与保护时段重叠", "That time is outside your available windows or overlaps a protected window"))
            if goal is None and step is not None:
                goal = self._active_step(step)
            if strict and goal and goal.deadline and local_end > datetime.combine(goal.deadline + timedelta(days=1), time(), self.zone):
                raise ApiError("deadline", self._t("该时间超过目标截止日期", "That time passes the goal deadline"))
        if not strict:
            return
        buffer = timedelta(minutes=config["buffer_minutes"])
        occupied = [
            e for e in self._events()
            if e.id not in ignore and e.status not in ("skipped", "missed") and self._blocks_scheduling(e)
        ]
        def overlaps(e):
            pad = buffer if goal_block or habit_block or e.kind == "habit" else timedelta(0)
            return start < _utc(e.end_utc) + pad and end > _utc(e.start_utc) - pad
        if any(overlaps(e) for e in occupied):
            raise ApiError("schedule_conflict", self._t("该时段与已有日程冲突", "That time conflicts with an existing event"))
        if goal_block:
            day_start = datetime.combine(local_start.date(), time(), self.zone).astimezone(UTC)
            day_end = datetime.combine(local_start.date() + timedelta(days=1), time(), self.zone).astimezone(UTC)
            minutes = sum(max(0, (min(_utc(e.end_utc), day_end) - max(_utc(e.start_utc), day_start)).total_seconds()) / 60
                          for e in occupied if e.kind == "block" and _utc(e.end_utc) > day_start and _utc(e.start_utc) < day_end)
            if minutes + (end - start).total_seconds() / 60 > config["max_goal_minutes_per_day"]:
                raise ApiError("daily_limit", self._t("该日任务时长已达上限", "Daily task limit reached for that day"))

    def _slot_override_note(self, start, end, *, ignore=(), check_availability=True) -> str:
        start, end = _utc(start), _utc(end)
        config = self._availability()
        local_start, local_end = start.astimezone(self.zone), end.astimezone(self.zone)
        outside = check_availability and not allows(local_start, local_end, config)
        overlaps = any(
            start < _utc(e.end_utc) and end > _utc(e.start_utc)
            for e in self._events()
            if e.id not in ignore and e.status not in ("skipped", "missed") and self._blocks_scheduling(e)
        )
        if outside and overlaps:
            return self._t("（不在可用时段且与已有日程重合）", " (outside available hours & overlaps existing event)")
        if outside:
            return self._t("（不在可用时段）", " (outside available hours)")
        if overlaps:
            return self._t("（与已有日程重合）", " (overlaps existing event)")
        return ""

    def _ui_pref(self, key: str, allowed: tuple[str, ...]) -> str:
        prefs = memory.list_preferences(self.session, self.user_id, scope="ui", now=self.now_naive)
        row = next((p for p in prefs if p.key == key), None)
        if not row:
            return ""
        try:
            val = json.loads(row.value)
        except ValueError:
            return ""
        return val if val in allowed else ""

    def _save_ui_pref(self, key: str, value: str, allowed: tuple[str, ...], err_zh: str, err_en: str) -> None:
        if value not in ("", *allowed):
            raise ApiError("validation", self._t(err_zh, err_en), 422)
        if value:
            memory.set_preference(self.session, self.user_id, key, value, scope="ui")
        else:
            memory.delete_preference(self.session, self.user_id, key, scope="ui")

    def me(self):
        user = self._get(User, self.user_id)
        return {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "timezone": user.tz,
            "lang": self._ui_pref("lang", ("zh", "en")),
            "theme": self._ui_pref("theme", ("light", "dark")),
            "calendar_palette": self._ui_pref("calendar_palette", ("calm", "mono", "vivid")),
        }

    def update_me(self, body):
        if "lang" in body:
            self._save_ui_pref("lang", body["lang"], ("zh", "en"), "语言选项无效", "Invalid language option")
        if "theme" in body:
            self._save_ui_pref("theme", body["theme"], ("light", "dark"), "外观选项无效", "Invalid appearance option")
        if "calendar_palette" in body:
            self._save_ui_pref(
                "calendar_palette",
                body["calendar_palette"],
                ("calm", "mono", "vivid"),
                "日历配色选项无效",
                "Invalid calendar palette option",
            )
        self.session.flush()
        return self.me()

    def goals(self, include_archived: bool = False):
        stmt = select(Goal).where(Goal.user_id == self.user_id)
        if not include_archived:
            stmt = stmt.where(Goal.status != "archived")
        rows = self.session.scalars(stmt)
        return {"goals": [self.serialize_goal(g) for g in sorted(rows, key=lambda g: (not g.pinned, g.id))]}

    def goal(self, id, include_archived: bool = False):
        goal = self._get(Goal, id)
        if goal.status == "archived" and not include_archived:
            raise ApiError("not_found", self._t("目标不存在或已归档", "Goal not found or already archived"), 404)
        return self.serialize_goal(goal)

    def today(self):
        day = self.now.astimezone(self.zone).date()
        start = datetime.combine(day, time(), self.zone)
        end = datetime.combine(day + timedelta(days=1), time(), self.zone)
        events = self.events(_iso(start), _iso(end))["events"]
        active = list(self.session.scalars(select(Goal).where(Goal.user_id == self.user_id, Goal.status == "active")))
        upcoming = sorted(
            [e for e in self._events() if e.kind == "block" and e.status in OPEN and _utc(e.end_utc) > self.now],
            key=lambda e: e.start_utc,
        )
        valid_upcoming = []
        for ev in upcoming:
            if ev.step_id is not None:
                step = self._get(Step, ev.step_id)
                if (step.status == "todo" and step.goal.status == "active"
                        and goal_service.next_step(step.goal).id == step.id):
                    valid_upcoming.append((ev, step))

        chosen = None
        # 1) Prefer blocks scheduled for today (before tomorrow local midnight).
        for ev, step in valid_upcoming:
            if _utc(ev.start_utc) < end:
                chosen = {"goal": self.serialize_goal(step.goal), "step": self.serialize_step(step), "event": self.serialize_event(ev)}
                break

        # 2) Otherwise prioritize active goals (pinned -> deadline -> id) with an unscheduled next step.
        if chosen is None:
            scheduled_step_ids = {step.id for _, step in valid_upcoming}
            for goal in sorted(active, key=lambda g: (not g.pinned, g.deadline or date.max, g.id)):
                step = goal_service.next_step(goal)
                if step and step.id not in scheduled_step_ids:
                    chosen = {"goal": self.serialize_goal(goal), "step": self.serialize_step(step), "event": None}
                    break

        # 3) Fallback to earliest future block (tomorrow or later) so Today remains informative.
        if chosen is None and valid_upcoming:
            ev, step = valid_upcoming[0]
            chosen = {"goal": self.serialize_goal(step.goal), "step": self.serialize_step(step), "event": self.serialize_event(ev)}
        running = self._running()
        recovery_events = [e for e in recovery.unresolved_blocks(self.session, self.user_id, self.now_naive)
                           if not any(x.event_id == e.id and x.status == "running" for x in self._executions())]
        habits = self.habits.range(day, day, view="today")
        timed = [item for item in habits["items"] if item["status"] == "pending" and item["end"]
                 and parse_datetime(item["end"]) > self.now]
        dated = [item for item in habits["items"] if item["status"] == "pending" and not item["start"]]
        goal_today = chosen and chosen["event"] and parse_datetime(chosen["event"]["start"]) < end
        habit_next = next(iter(timed or dated), None)
        if goal_today and (not habit_next or not habit_next["start"]
                           or parse_datetime(chosen["event"]["start"]) <= parse_datetime(habit_next["start"])):
            habit_next = None
        return {"date": day.isoformat(), "timezone": self.tz, "next": chosen,
                "execution": self.serialize_execution(running) if running else None,
                "recovery": [self.serialize_event(e) for e in recovery_events], "events": events,
                "habits": habits, "habit_next": habit_next,
                "sync_conflicts": detect_sync_conflicts(self.session, self.user_id, self.now_naive)}

    def _step_defs(self, values):
        if not isinstance(values, list) or not 1 <= len(values) <= 100:
            raise ApiError("validation", self._t("需要 1 至 100 个步骤", "Expected 1 to 100 steps"), 422)
        out = []
        for value in values:
            if not isinstance(value, dict):
                raise ApiError("validation", self._t("步骤格式错误", "Invalid step format"), 422)
            kind = value.get("kind", "deep")
            if kind not in ("deep", "admin", "physical", "social", "research"):
                raise ApiError("validation", self._t("步骤类型无效", "Invalid step kind"), 422)
            out.append({"title": _text(value.get("title"), lang=self.lang),
                        "outcome": _text(value.get("outcome", ""), self._t("成果", "Outcome"), 4000, empty=True, lang=self.lang),
                        "est_min": _integer(value.get("est_min", 45), 5, 480, self._t("预计分钟", "Estimated minutes"), lang=self.lang),
                        "energy": _integer(value.get("energy", 1), 0, 2, self._t("精力", "Energy"), lang=self.lang), "kind": kind,
                        "milestone": _text(value.get("milestone", ""), self._t("阶段", "Phase"), 120, empty=True, lang=self.lang)})
        return out

    def draft(self, body):
        title = _text(body.get("title"), lang=self.lang)
        deadline = _deadline(body.get("deadline"), lang=self.lang)
        if deadline and deadline < self.now.astimezone(self.zone).date():
            raise ApiError("validation", self._t("截止日期不能在过去", "Deadline cannot be in the past"), 422)
        prepared = body.get("_prepared")
        if prepared is not None:
            if not isinstance(prepared, dict):
                raise ApiError("validation", self._t("生成的草稿格式错误", "Generated draft format is invalid"), 422)
            definitions = self._step_defs(prepared.get("steps"))
            outline = prepared.get("outline", [])
            source = _text(prepared.get("source", "llm"), self._t("草稿来源", "Draft source"), 200, lang=self.lang)
        else:
            key = pick_template(title)
            definitions = self._step_defs(first_batch(full_steps(key, lang=self.lang)))
            outline = milestones(key, lang=self.lang)
            source = "template_fallback: deterministic goal template (LLM unavailable)"
        if (not isinstance(outline, list) or len(outline) > 100
                or any(not isinstance(x, str) or not x.strip() or len(x) > 120 for x in outline)):
            raise ApiError("validation", self._t("阶段大纲格式错误", "Phase outline format is invalid"), 422)
        payload = {"title": title, "deadline": deadline.isoformat() if deadline else None,
                   "outline": outline, "steps": definitions, "source": source}
        draft = WebDraft(id=uuid4().hex, user_id=self.user_id, payload_json=_json(payload), version=1)
        self.session.add(draft)
        self.session.flush()
        return {"id": draft.id, **payload, "version": version_token(draft)}

    def create_goal(self, body):
        draft = self._get(WebDraft, body.get("draft_id"))
        self.check_version(draft, body)
        if draft.goal_id is not None:
            raise ApiError("draft_used", self._t("该草稿已保存", "This draft has already been saved"))
        title = _text(body.get("title"), lang=self.lang)
        deadline = _deadline(body.get("deadline"), lang=self.lang)
        if deadline and deadline < self.now.astimezone(self.zone).date():
            raise ApiError("validation", self._t("截止日期不能在过去", "Deadline cannot be in the past"), 422)
        definitions = self._step_defs(body.get("steps"))
        payload = json.loads(draft.payload_json)
        goal = goal_service.create_goal(self.session, self.user_id, title, deadline=deadline,
                                       with_template=False, step_defs=[dict(s, seq=i) for i, s in enumerate(definitions)],
                                       template_key=(pick_template(payload["title"]) if definitions == payload.get("steps")
                                                     and payload.get("source") != "llm" else "web_outline"),
                                       now=self.now_naive)
        goal.deadline = deadline
        goal.draft_id = draft.id
        goal.outline = _json(payload.get("outline", []))
        # Edited milestones must remain reachable even if the outline was edited away.
        if {s["milestone"] for s in definitions} - set(payload.get("outline", [])):
            goal.outline = _json(list(dict.fromkeys(s["milestone"] for s in definitions if s["milestone"])))
        draft.goal_id = goal.id
        draft.version += 1
        self.session.flush()
        return {"message": self._t(f"已创建目标「{goal.title}」", f'Created goal "{goal.title}"'), "goal": self.serialize_goal(goal)}

    def _auto_refill(self, goal: Goal, body: dict) -> str:
        """Auto-generate the next outline phase once <= 1 todo step remains."""
        todo_count = sum(1 for s in goal.steps if s.status == "todo")
        if goal.status != "active" or todo_count > 1:
            return ""
        remaining = set(goal_service.outline_phases(goal)) - {s.milestone for s in goal.steps}
        if not remaining:
            return ""
        from ..agent import apply_refill, plan_refill
        defs = body.get("_refill_defs")
        if defs is None:
            defs = plan_refill(self.session, self.user_id, goal, self.now_naive, use_llm=False, lang=self.lang)
        if not defs:
            return ""
        message = apply_refill(self.session, self.user_id, goal, self.now_naive, defs)
        self.session.flush()
        if not message:
            return ""
        next_phase = defs[0].get("milestone", "") if defs else ""
        if next_phase:
            return self._t(f"；已自动生成「{next_phase}」阶段步骤", f'; added next phase "{next_phase}"')
        return self._t("；已自动生成下一阶段步骤", "; added next-phase steps")

    def refill(self, goal_id, body):
        """Continue a completed starter batch through the existing rolling planner."""
        from ..agent import apply_refill, plan_refill

        goal = self._get(Goal, goal_id)
        self.check_version(goal, body)
        if goal.status != "active" or any(s.status == "todo" for s in goal.steps):
            raise ApiError("invalid_state", self._t("请先完成当前批次，再生成下一批步骤", "Complete all pending steps in this batch before generating the next one"))
        self._ensure_not_running(goal_id=goal.id)
        remaining = set(goal_service.outline_phases(goal)) - {s.milestone for s in goal.steps}
        if not remaining:
            raise ApiError("no_remaining_phases", self._t("所有阶段均已有步骤", "All outline phases already have steps"))
        defs = body.get("_refill_defs")
        used_llm = defs is not None
        if defs is None:
            defs = plan_refill(self.session, self.user_id, goal, self.now_naive, use_llm=False, lang=self.lang)
        if not defs:
            raise ApiError("no_remaining_phases", self._t("没有可生成的后续步骤", "No further steps available to generate"))
        message = apply_refill(self.session, self.user_id, goal, self.now_naive, defs)
        self.session.flush()
        if not message:
            raise ApiError("no_remaining_phases", self._t("没有可生成的后续步骤", "No further steps available to generate"))
        source_note = self._t("AI 拆解", "AI tailored") if used_llm else self._t("模板与阶段大纲", "template & outline")
        return {
            "message": self._t(f"已为「{goal.title}」生成下一批步骤（{source_note}）", f'Generated next batch for "{goal.title}" ({source_note})'),
            "goal": self.serialize_goal(goal),
        }

    def _soft_cancel(self, events):
        for event in events:
            event.status = "skipped"
            event.resolved_at = self.now_naive

    def update_goal(self, id, body):
        goal = self._get(Goal, id)
        self.check_version(goal, body)
        title = _text(body["title"], lang=self.lang) if "title" in body else goal.title
        status = body.get("status", goal.status)
        if status not in ("active", "paused", "done", "archived"):
            raise ApiError("validation", self._t("目标状态无效", "Invalid goal status"), 422)
        if "pinned" in body and type(body["pinned"]) is not bool:
            raise ApiError("validation", self._t("pinned 必须为布尔值", "pinned must be a boolean"), 422)
        if status != "active":
            self._ensure_not_running(goal_id=goal.id)
            self._soft_cancel([e for e in self._events() if e.step_id in {s.id for s in goal.steps}
                               and e.status in OPEN and _utc(e.start_utc) >= self.now])
        if title != goal.title:
            for event in self._events():
                if event.status in OPEN and event.step_id in {step.id for step in goal.steps}:
                    step = self._get(Step, event.step_id)
                    event.title = step.title[:200]
        was_archived = goal.status == "archived"
        goal.title, goal.status = title, status
        if "deadline" in body:
            raw_deadline = body["deadline"].strip()
            if raw_deadline == "":
                goal.deadline = None
            else:
                try:
                    new_deadline = date.fromisoformat(raw_deadline)
                except ValueError:
                    raise ApiError("validation", self._t("截止日期格式无效", "Invalid deadline date"), 422) from None
                if new_deadline < self.now.astimezone(self.zone).date():
                    raise ApiError("invalid_deadline", self._t("截止日期不能在过去", "Deadline cannot be in the past"), 422)
                goal.deadline = new_deadline
        if "pinned" in body:
            goal.pinned = body["pinned"]
        if status == "archived":
            goal.pinned = False
            for proposal in self.session.scalars(select(WebProposal).where(
                    WebProposal.user_id == self.user_id, WebProposal.goal_id == goal.id, WebProposal.status == "open")):
                proposal.status = "superseded"
                proposal.version += 1
            for thread in self.session.scalars(select(WebThread).where(
                    WebThread.user_id == self.user_id, WebThread.focus_goal_id == goal.id)):
                thread.focus_goal_id = None
        self.session.flush()
        if status == "archived":
            msg = self._t(
                f"「{goal.title}」已归档至 设置 → 已归档",
                f'"{goal.title}" archived to Settings → Archived',
            )
        elif was_archived and status == "active":
            msg = self._t(
                f"「{goal.title}」已恢复至目标",
                f'"{goal.title}" restored to Goals',
            )
        else:
            msg = self._t(f"已更新目标「{goal.title}」", f'Updated goal "{goal.title}"')
        return {"message": msg, "goal": self.serialize_goal(goal)}

    def replan_goal(self, goal_id, body, *, update_thread: bool = True):
        goal = self._get(Goal, goal_id)
        self.check_version(goal, body)
        if goal.status != "active":
            raise ApiError("invalid_state", self._t("只有进行中的目标可以重新规划", "Only active goals can be replanned"))
        has_steps = body.get("steps") is not None
        has_deadline = "deadline" in body
        if not has_steps and not has_deadline:
            raise ApiError("validation", self._t("没有需要更新的规划内容", "No plan changes to apply"), 422)
        if has_steps:
            self._ensure_not_running(goal_id=goal.id)
        definitions = self._step_defs(body.get("steps")) if has_steps else None
        if has_deadline:
            raw_deadline = body["deadline"].strip()
            if raw_deadline == "":
                goal.deadline = None
            else:
                try:
                    new_deadline = date.fromisoformat(raw_deadline)
                except ValueError:
                    raise ApiError("validation", self._t("截止日期格式无效", "Invalid deadline date"), 422) from None
                if new_deadline < self.now.astimezone(self.zone).date():
                    raise ApiError("invalid_deadline", self._t("截止日期不能在过去", "Deadline cannot be in the past"), 422)
                goal.deadline = new_deadline
        if definitions is not None:
            todo_steps = [s for s in goal.steps if s.status == "todo"]
            todo_ids = [s.id for s in todo_steps]
            if todo_ids:
                for proposal in self.session.scalars(select(WebProposal).where(
                        WebProposal.user_id == self.user_id, WebProposal.step_id.in_(todo_ids))):
                    self.session.delete(proposal)
                open_future = [e for e in self._events() if e.step_id in todo_ids and e.status in OPEN and _utc(e.start_utc) >= self.now]
                self._soft_cancel(open_future)
                self.session.flush()
                referenced = set()
                for model, col in ((Event, Event.step_id), (Checkin, Checkin.step_id),
                                   (WebExecution, WebExecution.step_id), (Interaction, Interaction.step_id)):
                    for sid in self.session.scalars(select(col).where(col.in_(todo_ids))):
                        if sid is not None:
                            referenced.add(sid)
                for step in todo_steps:
                    if step.id in referenced:
                        step.status = "dropped"
                    else:
                        self.session.delete(step)
                self.session.flush()
            self.session.expire(goal, ["steps"])
            next_seq = max((s.seq for s in goal.steps), default=-1) + 1
            for offset, d in enumerate(definitions):
                self.session.add(Step(
                    goal_id=goal.id,
                    seq=next_seq + offset,
                    title=d["title"],
                    est_min=d["est_min"],
                    milestone=d["milestone"],
                    energy=d["energy"],
                    status="todo",
                ))
            done_milestones = [s.milestone for s in sorted(goal.steps, key=lambda x: x.seq) if s.status == "done" and s.milestone]
            new_outline = body.get("outline") or []
            step_milestones = [d["milestone"] for d in definitions if d.get("milestone")]
            merged_outline = list(dict.fromkeys([*done_milestones, *[x.strip() for x in new_outline if isinstance(x, str) and x.strip()], *step_milestones]))
            if merged_outline:
                goal.outline = _json(merged_outline)
        if definitions is not None:
            summary_zh = f"已重新规划「{goal.title}」，共 {len(definitions)} 个待办步骤"
            summary_en = f'Replanned "{goal.title}" with {len(definitions)} todo steps'
            if has_deadline:
                if goal.deadline:
                    summary_zh += f"（截止 {goal.deadline.isoformat()}）"
                    summary_en += f" (due {goal.deadline.isoformat()})"
                else:
                    summary_zh += "（已取消截止日期）"
                    summary_en += " (deadline removed)"
        elif goal.deadline:
            summary_zh = f"已将「{goal.title}」的截止日期更新为 {goal.deadline.isoformat()}"
            summary_en = f'Updated deadline for "{goal.title}" to {goal.deadline.isoformat()}'
        else:
            summary_zh = f"已取消「{goal.title}」的截止日期"
            summary_en = f'Removed deadline for "{goal.title}"'
        msg = self._t(summary_zh, summary_en)
        # Clear pending replan card in assistant thread once applied via HTTP button.
        if update_thread:
            thread = self.session.scalar(select(WebThread).where(WebThread.user_id == self.user_id))
            if thread:
                messages = json.loads(thread.messages_json or "[]")
                if messages and (messages[-1].get("replan_goal") or messages[-1].get("replan_prompt_goal_id")):
                    messages[-1].pop("replan_goal", None)
                    messages[-1].pop("replan_prompt_goal_id", None)
                    messages[-1].pop("replan_context", None)
                    messages.append({
                        "id": uuid4().hex,
                        "role": "assistant",
                        "content": msg + self._t("。", "."),
                        "ts": self.now.isoformat(),
                    })
                    thread.messages_json = json.dumps(messages[-40:], ensure_ascii=False)
                    thread.version += 1
        self.session.flush()
        self.session.expire(goal, ["steps"])
        return {
            "message": msg,
            "goal": self.serialize_goal(goal),
        }

    def delete_goal(self, id, body):
        goal = self._get(Goal, id)
        self.check_version(goal, body)
        self._ensure_not_running(goal_id=goal.id)
        title = goal.title
        step_ids = [s.id for s in goal.steps]
        event_ids = [e.id for e in self._events() if e.step_id in step_ids]
        proposal_ids = [p.id for p in self.session.scalars(select(WebProposal).where(
            WebProposal.user_id == self.user_id, WebProposal.goal_id == goal.id))]
        for thread in self.session.scalars(select(WebThread).where(WebThread.user_id == self.user_id)):
            if thread.focus_goal_id == goal.id:
                thread.focus_goal_id = None
            if thread.proposal_id in proposal_ids:
                thread.proposal_id = None
        for draft in self.session.scalars(select(WebDraft).where(
                WebDraft.user_id == self.user_id, WebDraft.goal_id == goal.id)):
            draft.goal_id = None
        interaction_filters = [Interaction.goal_id == goal.id]
        if step_ids:
            interaction_filters.append(Interaction.step_id.in_(step_ids))
        if event_ids:
            interaction_filters.append(Interaction.event_id.in_(event_ids))
        for item in self.session.scalars(select(Interaction).where(
                Interaction.user_id == self.user_id, or_(*interaction_filters))):
            if item.goal_id == goal.id:
                item.goal_id = None
            if item.step_id in step_ids:
                item.step_id = None
            if item.event_id in event_ids:
                item.event_id = None
        self.session.flush()
        exec_filters = []
        if step_ids:
            exec_filters.append(WebExecution.step_id.in_(step_ids))
        if event_ids:
            exec_filters.append(WebExecution.event_id.in_(event_ids))
        if exec_filters:
            for execution in self.session.scalars(select(WebExecution).where(
                    WebExecution.user_id == self.user_id, or_(*exec_filters))):
                self.session.delete(execution)
        for proposal in self.session.scalars(select(WebProposal).where(
                WebProposal.user_id == self.user_id, WebProposal.goal_id == goal.id)):
            self.session.delete(proposal)
        if event_ids:
            for log in self.session.scalars(select(RescheduleLog).where(
                    or_(RescheduleLog.event_id.in_(event_ids), RescheduleLog.new_event_id.in_(event_ids)))):
                self.session.delete(log)
        checkin_filters = []
        if step_ids:
            checkin_filters.append(Checkin.step_id.in_(step_ids))
        if event_ids:
            checkin_filters.append(Checkin.event_id.in_(event_ids))
        if checkin_filters:
            for checkin in self.session.scalars(select(Checkin).where(or_(*checkin_filters))):
                self.session.delete(checkin)
        if event_ids:
            for event in self.session.scalars(select(Event).where(
                    Event.user_id == self.user_id, Event.id.in_(event_ids))):
                self.session.delete(event)
        self.session.flush()
        self.supersede_weekly_plans()
        self.session.delete(goal)
        self.session.flush()
        return {"message": self._t(f"已删除目标「{title}」", f'Deleted goal "{title}"')}

    def update_step(self, id, body):
        step = self._get(Step, id)
        self.check_version(step, body)
        title = _text(body["title"], lang=self.lang) if "title" in body else step.title
        est = _integer(body["est_min"], 5, 480, self._t("预计分钟", "Estimated minutes"), lang=self.lang) if "est_min" in body else step.est_min
        status = body.get("status", step.status)
        if status not in ("todo", "done", "dropped"):
            raise ApiError("validation", self._t("步骤状态无效", "Invalid step status"), 422)
        if step.goal.status in ("paused", "archived") and status != step.status:
            raise ApiError("invalid_state", self._t("请先恢复目标", "Resume this goal first"))
        self._ensure_not_running(step_id=step.id)
        if title != step.title:
            for event in self._open_step_events(step.id):
                event.title = title[:200]
        step.title, step.est_min = title, est
        refill_note = ""
        if status != step.status:
            if status == "done":
                goal_service.complete_step(self.session, step, now=self.now_naive)
            else:
                step.status, step.completed_at = status, None
                if status == "todo" and step.goal.status == "done":
                    step.goal.status = "active"
            if status in ("done", "dropped"):
                self._soft_cancel([e for e in self._open_step_events(step.id) if _utc(e.start_utc) >= self.now])
            if status == "done":
                refill_note = self._auto_refill(step.goal, body)
        self.session.flush()
        return {
            "message": self._t(f"已更新「{step.goal.title}」的步骤{refill_note}", f'Updated step in "{step.goal.title}"{refill_note}'),
            "goal": self.serialize_goal(step.goal),
        }

    def events(self, start, end):
        start, end = parse_datetime(start, lang=self.lang), parse_datetime(end, lang=self.lang)
        if end <= start or end - start > timedelta(days=93):
            raise ApiError("validation", self._t("日历查询范围须为 1 至 93 天", "Calendar query range must be 1 to 93 days"), 422)
        habits = self.habits.range(start.astimezone(self.zone).date(),
                                   (end - timedelta(microseconds=1)).astimezone(self.zone).date(), view="calendar")
        occurrences = {i["event_id"]: i for i in habits["items"] if i["event_id"]}
        events = []
        for event in self._events():
            if _utc(event.end_utc) <= start or _utc(event.start_utc) >= end:
                continue
            if event.kind != "habit":
                events.append(self.serialize_event(event))
            elif event.id in occurrences:
                occurrence = occurrences[event.id]
                status = "planned" if occurrence["status"] == "pending" else occurrence["status"]
                events.append({**self.serialize_event(event), "status": status, "habit": occurrence})
        return {
            "events": sorted(events, key=lambda event: (event["start"], event["id"])),
            "habits": habits,
            "sync_conflicts": detect_sync_conflicts(self.session, self.user_id, self.now_naive),
        }

    def create_event(self, body):
        title = _text(body.get("title"), lang=self.lang)
        start, end = parse_datetime(body.get("start"), lang=self.lang), parse_datetime(body.get("end"), lang=self.lang)
        self._slot(start, end, goal_block=False)
        event = Event(user_id=self.user_id, title=title, start_utc=_naive(start), end_utc=_naive(end), kind="external", status="planned", source="web")
        self.session.add(event)
        self.session.flush()
        return {"message": self._t(f"已添加「{title}」：{self._when(start, end)}", f'Added "{title}": {self._when(start, end)}'), "event": self.serialize_event(event),
                "operation_id": self._operation([(event, None)])}

    def _editable_event(self, id, body):
        event = self._get(Event, id)
        self.check_version(event, body)
        if event.kind == "habit":
            raise ApiError("invalid_state", self._t("请从习惯详情调整这次安排", "Edit this session from its habit details"))
        if event.status not in OPEN:
            raise ApiError("invalid_state", self._t("该日程已处理", "This event has already been resolved"))
        self._ensure_not_running(event_id=event.id)
        return event

    def _event_snapshot(self, event):
        return {"id": event.id, "start": _iso(event.start_utc), "end": _iso(event.end_utc),
                "status": event.status, "resolved_at": _iso(event.resolved_at)}

    def _operation(self, changes):
        self.session.flush()
        row = WebOperation(id=uuid4().hex, user_id=self.user_id, payload_json=_json([
            {"before": before, "after": self._event_snapshot(event), "version": version_token(event)}
            for event, before in changes]), undone=False)
        self.session.add(row)
        self.session.flush()
        return row.id

    def move_event(self, id, body):
        event = self._editable_event(id, body)
        start, end = parse_datetime(body.get("start"), lang=self.lang), parse_datetime(body.get("end"), lang=self.lang)
        step = self._get(Step, event.step_id) if event.step_id else None
        goal = self._active_step(step) if step else None
        self._slot(start, end, ignore=(event.id,), step=step, goal=goal, goal_block=event.kind == "block", strict=False)
        note = self._slot_override_note(start, end, ignore=(event.id,), check_availability=event.kind == "block")
        before = self._event_snapshot(event)
        event.start_utc, event.end_utc, event.status = _naive(start), _naive(end), "moved"
        operation = self._operation([(event, before)])
        return {"message": self._t(f"已调整「{event.title}」：{self._when(start, end)}", f'Rescheduled "{event.title}": {self._when(start, end)}') + note, "event": self.serialize_event(event), "operation_id": operation}

    def cancel_event(self, id, body):
        event = self._editable_event(id, body)
        before = self._event_snapshot(event)
        self._soft_cancel([event])
        operation = self._operation([(event, before)])
        return {"message": self._t(f"已取消「{event.title}」：{self._when(event.start_utc, event.end_utc)}", f'Cancelled "{event.title}": {self._when(event.start_utc, event.end_utc)}'), "event": self.serialize_event(event), "operation_id": operation}

    def supersede_proposals(self):
        for row in self.session.scalars(select(WebProposal).where(WebProposal.user_id == self.user_id, WebProposal.status == "open")):
            row.status, row.version = "superseded", row.version + 1
        self.session.flush()

    def supersede_weekly_plans(self):
        for row in self.session.scalars(select(WebWeeklyPlan).where(WebWeeklyPlan.user_id == self.user_id, WebWeeklyPlan.status == "open")):
            row.status, row.version = "superseded", row.version + 1
        self.session.flush()

    def serialize_weekly_plan(self, plan: WebWeeklyPlan) -> dict:
        payload = json.loads(plan.payload_json)
        status = plan.status
        if status == "open" and plan.expires_at <= self.now_naive:
            status = "expired"
        return {
            "id": plan.id,
            "version": version_token(plan),
            "status": status,
            "scope": payload["scope"],
            "strategy": payload["strategy"],
            "keep_existing_blocks": payload["keep_existing_blocks"],
            "week_start": payload["week_start"],
            "week_end": payload["week_end"],
            "window_start": payload["window_start"],
            "window_end": payload["window_end"],
            "expires_at": _iso(plan.expires_at),
            "available_goals": payload["available_goals"],
            "capacity": payload["capacity"],
            "goal_summaries": payload["goal_summaries"],
            "items": payload["items"],
            "unscheduled": payload["unscheduled"],
        }

    def create_weekly_plan(self, body):
        scope = body.get("scope", "auto")
        strategy = body.get("strategy", "balanced")
        keep_existing_blocks = bool(body.get("keep_existing_blocks", True))
        requested_goal_ids = body.get("goal_ids")

        now_local = self.now.astimezone(self.zone)
        resolved_scope, week_start, week_end, win_start_local, win_end_local = weekly_planner.resolve_week_window(now_local, scope)
        win_start_utc = win_start_local.astimezone(UTC)
        win_end_utc = win_end_local.astimezone(UTC)

        active_goals = sorted(
            self.session.scalars(select(Goal).where(Goal.user_id == self.user_id, Goal.status == "active")),
            key=lambda g: (not g.pinned, g.deadline or date.max, g.id),
        )
        available_goals = []
        eligible_goals: list[Goal] = []
        for g in active_goals:
            todo_steps = [s for s in g.steps if s.status == "todo"]
            if not todo_steps:
                continue
            available_goals.append({
                "goal_id": g.id,
                "title": g.title,
                "pinned": bool(g.pinned),
                "deadline": g.deadline.isoformat() if g.deadline else None,
                "todo_count": len(todo_steps),
            })
            eligible_goals.append(g)

        selected_id_set = set(requested_goal_ids) if requested_goal_ids is not None else {g.id for g in eligible_goals}
        selected_goals = [g for g in eligible_goals if g.id in selected_id_set]

        running = self._running()
        running_event_id = running.event_id if running else None
        all_events = self._events()
        recent_cutoff = self.now_naive - timedelta(days=7)

        recent_step_ids = {
            c.step_id
            for c in self.session.scalars(select(Checkin).where(Checkin.ts >= recent_cutoff))
        }
        recent_event_step_ids = {
            e.step_id for e in all_events
            if e.step_id is not None and e.status in ("done", "partial")
            and ((e.resolved_at and e.resolved_at >= recent_cutoff) or e.end_utc >= recent_cutoff)
        }

        replaceable_event_ids: set[int] = set()
        replaceable_event_versions: dict[str, str] = {}
        goal_inputs: list[weekly_planner.WeeklyGoalInput] = []
        step_versions: dict[str, str] = {}
        goal_versions: dict[str, str] = {}

        open_events_by_step: dict[int, list[Event]] = {}
        for ev in all_events:
            if ev.step_id is not None and ev.kind == "block" and ev.status in OPEN:
                open_events_by_step.setdefault(ev.step_id, []).append(ev)

        selected_step_ids: set[int] = set()
        for g in selected_goals:
            goal_versions[str(g.id)] = version_token(g)
            todo_steps = sorted((s for s in g.steps if s.status == "todo"), key=lambda s: (s.seq, s.id))
            all_step_ids = {s.id for s in g.steps}
            recent_activity = any(
                (s.completed_at is not None and s.completed_at >= recent_cutoff)
                or s.id in recent_step_ids
                or s.id in recent_event_step_ids
                for s in g.steps
            )
            step_inputs: list[weekly_planner.WeeklyStepInput] = []
            for s in todo_steps:
                step_open = open_events_by_step.get(s.id, [])
                replaces_id: int | None = None
                if step_open:
                    if keep_existing_blocks:
                        continue
                    can_replace = all(
                        ev.id != running_event_id
                        and _utc(ev.start_utc) >= max(self.now, win_start_utc)
                        and _utc(ev.end_utc) <= win_end_utc
                        for ev in step_open
                    )
                    if not can_replace:
                        continue
                    for ev in step_open:
                        replaceable_event_ids.add(ev.id)
                        replaceable_event_versions[str(ev.id)] = version_token(ev)
                    replaces_id = step_open[0].id
                step_versions[str(s.id)] = version_token(s)
                selected_step_ids.add(s.id)
                step_inputs.append(
                    weekly_planner.WeeklyStepInput(
                        step_id=s.id,
                        seq=s.seq,
                        title=s.title,
                        est_min=s.est_min,
                        energy=s.energy,
                        kind=s.kind,
                        milestone=s.milestone,
                        replaces_event_id=replaces_id,
                    )
                )

            existing_win_min = 0
            for ev in all_events:
                if (
                    ev.id not in replaceable_event_ids
                    and ev.step_id in all_step_ids
                    and ev.kind == "block"
                    and ev.status not in ("skipped", "missed")
                    and self._blocks_scheduling(ev)
                ):
                    es, ee = _utc(ev.start_utc), _utc(ev.end_utc)
                    if ee > win_start_utc and es < win_end_utc:
                        existing_win_min += int(round((min(ee, win_end_utc) - max(es, win_start_utc)).total_seconds() / 60.0))

            goal_inputs.append(
                weekly_planner.WeeklyGoalInput(
                    goal_id=g.id,
                    title=g.title,
                    pinned=bool(g.pinned),
                    deadline=g.deadline,
                    total_todo_minutes=sum(s.est_min for s in todo_steps),
                    recent_activity=recent_activity,
                    existing_window_minutes=existing_win_min,
                    steps=tuple(step_inputs),
                )
            )

        avail_cfg = self._availability()
        prefs = self._prefs()
        prefs.web_availability = avail_cfg
        busy = [
            scheduler.BusyBlock(_utc(e.start_utc).astimezone(self.zone), _utc(e.end_utc).astimezone(self.zone), e.kind)
            for e in all_events
            if e.id not in replaceable_event_ids
            and e.status not in ("skipped", "missed")
            and self._blocks_scheduling(e)
        ]

        plan_res = weekly_planner.plan_week(
            goal_inputs,
            now_local,
            (win_start_local, win_end_local),
            busy,
            prefs,
            scope=resolved_scope,
            week_start=week_start,
            week_end=week_end,
            strategy=strategy,
            keep_existing_blocks=keep_existing_blocks,
            lang=self.lang,
        )

        self.supersede_weekly_plans()
        payload = {
            "scope": plan_res.scope,
            "strategy": plan_res.strategy,
            "keep_existing_blocks": plan_res.keep_existing_blocks,
            "week_start": plan_res.week_start.isoformat(),
            "week_end": plan_res.week_end.isoformat(),
            "window_start": _iso(win_start_utc),
            "window_end": _iso(win_end_utc),
            "preferences": _hash(avail_cfg),
            "step_versions": step_versions,
            "goal_versions": goal_versions,
            "replaceable_event_versions": replaceable_event_versions,
            "available_goals": available_goals,
            "selected_goal_ids": [g.id for g in selected_goals],
            "capacity": {
                "free_minutes": plan_res.capacity_free_minutes,
                "weekly_limit_minutes": plan_res.capacity_weekly_limit_minutes,
                "existing_minutes": plan_res.capacity_existing_minutes,
                "planned_minutes": plan_res.capacity_planned_minutes,
            },
            "goal_summaries": [
                {
                    "goal_id": gs.goal_id,
                    "goal_title": gs.goal_title,
                    "pinned": gs.pinned,
                    "deadline": gs.deadline.isoformat() if gs.deadline else None,
                    "weight": gs.weight,
                    "existing_minutes": gs.existing_minutes,
                    "planned_minutes": gs.planned_minutes,
                    "planned_count": gs.planned_count,
                    "total_todo_minutes": gs.total_todo_minutes,
                }
                for gs in plan_res.goal_summaries
            ],
            "items": [
                {
                    "goal_id": item.goal_id,
                    "goal_title": item.goal_title,
                    "step_id": item.step_id,
                    "step_title": item.step_title,
                    "milestone": item.milestone,
                    "est_min": item.est_min,
                    "start": _iso(item.start),
                    "end": _iso(item.end),
                    "score": item.score,
                    "reasons": item.reasons,
                    "replaces_event_id": item.replaces_event_id,
                }
                for item in plan_res.items
            ],
            "unscheduled": [
                {
                    "goal_id": u.goal_id,
                    "goal_title": u.goal_title,
                    "step_id": u.step_id,
                    "step_title": u.step_title,
                    "est_min": u.est_min,
                    "reason_code": u.reason_code,
                    "reason": u.reason,
                }
                for u in plan_res.unscheduled
            ],
        }
        row = WebWeeklyPlan(
            id=uuid4().hex,
            user_id=self.user_id,
            status="open",
            version=1,
            expires_at=self.now_naive + timedelta(minutes=30),
            payload_json=_json(payload),
        )
        self.session.add(row)
        self.session.flush()
        return self.serialize_weekly_plan(row)

    def accept_weekly_plan(self, plan_id: str, body):
        plan = self._get(WebWeeklyPlan, plan_id)
        self.check_version(plan, body)
        if plan.status != "open" or plan.expires_at <= self.now_naive:
            raise ApiError("stale_proposal", self._t("该周排期草案已失效，请重新生成", "This weekly plan has expired; please generate a new one"))
        payload = json.loads(plan.payload_json)
        if payload["preferences"] != _hash(self._availability()):
            raise ApiError("stale_proposal", self._t("可用时间偏好已更新，请重新生成周排期", "Availability preferences changed; please regenerate the weekly plan"))

        all_items = payload.get("items", [])
        if not all_items:
            raise ApiError("validation", self._t("该草案没有可采纳的时段", "No planned blocks available to accept"), 422)

        requested_ids = body.get("step_ids")
        if requested_ids is not None:
            by_step = {item["step_id"]: item for item in all_items}
            dedup_ids = list(dict.fromkeys(requested_ids))
            if not dedup_ids or any(sid not in by_step for sid in dedup_ids):
                raise ApiError("validation", self._t("选择的步骤不在该排期草案中", "Selected steps must belong to this weekly plan"), 422)
            selected_items = [by_step[sid] for sid in dedup_ids]
        else:
            selected_items = list(all_items)

        step_versions = payload.get("step_versions", {})
        goal_versions = payload.get("goal_versions", {})
        replaceable_versions = payload.get("replaceable_event_versions", {})

        resolved_entries = []
        old_events_by_id: dict[int, Event] = {}
        for item in selected_items:
            step = self._get(Step, item["step_id"])
            goal = self._active_step(step)
            self._ensure_not_running(step_id=step.id)
            if (step_versions.get(str(step.id)) != version_token(step)
                    or goal_versions.get(str(goal.id)) != version_token(goal)):
                raise ApiError("stale_proposal", self._t("目标或步骤已更新，请重新生成周排期", "Goal or step changed; please regenerate the weekly plan"))
            open_evts = self._open_step_events(step.id)
            for old_ev in open_evts:
                if str(old_ev.id) in replaceable_versions and replaceable_versions[str(old_ev.id)] != version_token(old_ev):
                    raise ApiError("stale_proposal", self._t("已有日程已变更，请重新生成周排期", "Existing schedule changed; please regenerate the weekly plan"))
                old_events_by_id[old_ev.id] = old_ev
            start = parse_datetime(item["start"], lang=self.lang)
            end = parse_datetime(item["end"], lang=self.lang)
            resolved_entries.append((item, step, goal, start, end))

        ignore_ids = set(old_events_by_id.keys())
        config = self._availability()
        buffer = timedelta(minutes=config["buffer_minutes"])

        for _, step, goal, start, end in resolved_entries:
            self._slot(start, end, ignore=ignore_ids, step=step, goal=goal, strict=True)

        for i, (_, _, _, s1, e1) in enumerate(resolved_entries):
            for _, _, _, s2, e2 in resolved_entries[i + 1:]:
                if s1 < e2 + buffer and e1 > s2 - buffer:
                    raise ApiError("schedule_conflict", self._t("所选排期块之间存在时间或缓冲冲突", "Selected blocks conflict with each other"))

        existing_blocks = [
            e for e in self._events()
            if e.id not in ignore_ids
            and e.kind == "block"
            and e.status not in ("skipped", "missed")
            and self._blocks_scheduling(e)
        ]
        affected_days = {start.astimezone(self.zone).date() for _, _, _, start, _ in resolved_entries}
        for day in affected_days:
            lo = datetime.combine(day, time(), self.zone).astimezone(UTC)
            hi = datetime.combine(day + timedelta(days=1), time(), self.zone).astimezone(UTC)
            spans = [(_utc(e.start_utc), _utc(e.end_utc)) for e in existing_blocks]
            spans += [(s, e) for _, _, _, s, e in resolved_entries]
            day_minutes = sum(
                max(0.0, (min(ee, hi) - max(ss, lo)).total_seconds() / 60.0)
                for ss, ee in spans if ee > lo and ss < hi
            )
            if day_minutes > config["max_goal_minutes_per_day"]:
                raise ApiError("daily_limit", self._t("该日任务时长已达上限", "Daily task limit reached for that day"))

        old_events = sorted(old_events_by_id.values(), key=lambda e: e.id)
        changes = [(e, self._event_snapshot(e)) for e in old_events]
        self._soft_cancel(old_events)

        created_events: list[Event] = []
        for _, step, goal, start, end in resolved_entries:
            ev = Event(
                user_id=self.user_id,
                title=step.title[:200],
                start_utc=_naive(start),
                end_utc=_naive(end),
                kind="block",
                status="planned",
                source="proposal",
                step_id=step.id,
            )
            self.session.add(ev)
            created_events.append(ev)
        self.session.flush()
        for ev in created_events:
            changes.append((ev, None))

        plan.status = "accepted"
        plan.version += 1
        self.supersede_proposals()
        operation = self._operation(changes)
        for ev, (_, step, goal, _, _) in zip(created_events, resolved_entries):
            metrics.record(
                self.session,
                self.user_id,
                metrics.PROPOSAL_ACCEPTED,
                self.now_naive,
                goal_id=goal.id,
                step_id=step.id,
                event_id=ev.id,
                payload={"source": "weekly_plan", "plan_id": plan.id},
            )
        count = len(created_events)
        return {
            "message": self._t(
                f"已采纳本周平衡排期，共安排 {count} 个时间块",
                f"Accepted weekly plan with {count} block(s)",
            ),
            "accepted_count": count,
            "events": [self.serialize_event(ev) for ev in created_events],
            "operation_id": operation,
        }

    def propose(self, step_id, body):
        step = self._get(Step, step_id)
        goal = self._active_step(step)
        self._ensure_not_running(step_id=step.id)
        start = parse_datetime(body["window_start"], lang=self.lang) if body.get("window_start") is not None else self.now
        end = parse_datetime(body["window_end"], lang=self.lang) if body.get("window_end") is not None else start + timedelta(days=7)
        if end <= start or end - start > timedelta(days=93):
            raise ApiError("validation", self._t("候选查询范围须为 1 至 93 天", "Proposal search window must be 1 to 93 days"), 422)
        start = max(start, self.now)
        if goal.deadline:
            end = min(end, datetime.combine(goal.deadline + timedelta(days=1), time(), self.zone).astimezone(UTC))
        ignore = {e.id for e in self._open_step_events(step.id)}
        prefs = self._prefs()
        # Web proposals use the same materialized constraints as all later writes.
        prefs.web_availability = self._availability()
        busy = [scheduler.BusyBlock(_utc(e.start_utc).astimezone(self.zone), _utc(e.end_utc).astimezone(self.zone), e.kind)
                for e in self._events()
                if e.id not in ignore and e.status not in ("skipped", "missed") and self._blocks_scheduling(e)]
        candidates = scheduler.propose(scheduler.Requirement(est_min=step.est_min, energy=step.energy, kind=step.kind, min_chunk=min(20, step.est_min)),
                                       self.now.astimezone(self.zone), busy, prefs=prefs, top_n=6, deadline=goal.deadline,
                                       window=(start.astimezone(self.zone), end.astimezone(self.zone)), lang=self.lang) if end > start else []
        candidates.sort(key=lambda c: c.start)
        self.supersede_proposals()
        row = WebProposal(id=uuid4().hex, user_id=self.user_id, step_id=step.id, goal_id=goal.id,
                          status="open", version=1, expires_at=self.now_naive + timedelta(minutes=30),
                          payload_json=_json({"step_version": version_token(step), "goal_version": version_token(goal),
                                              "preferences": _hash(self._availability()),
                                              "candidates": [{"start": _iso(c.start), "end": _iso(c.end), "reasons": c.reasons,
                                                              "score": round(c.score, 3)} for c in candidates]}))
        self.session.add(row)
        self.session.flush()
        if candidates:
            metrics.record(self.session, self.user_id, metrics.PROPOSAL_SHOWN, self.now_naive,
                           goal_id=goal.id, step_id=step.id, payload={"count": len(candidates), "source": "web"})
        return self.serialize_proposal(row)

    def schedule_step(self, step_id, body):
        step = self._get(Step, step_id)
        self.check_version(step, body)
        goal = self._active_step(step)
        self._ensure_not_running(step_id=step.id)
        start = parse_datetime(body.get("start"), lang=self.lang)
        if start <= self.now:
            raise ApiError("invalid_time", self._t("请选择将来的开始时间", "Please choose a future start time"), 422)
        end = start + timedelta(minutes=step.est_min)
        old = self._open_step_events(step.id)
        ignore_ids = {e.id for e in old}
        self._slot(start, end, ignore=ignore_ids, step=step, goal=goal, strict=False)
        note = self._slot_override_note(start, end, ignore=ignore_ids)
        changes = [(e, self._event_snapshot(e)) for e in old]
        self._soft_cancel(old)
        event = Event(user_id=self.user_id, title=step.title[:200],
                      start_utc=_naive(start), end_utc=_naive(end), kind="block",
                      status="planned", source="web", step_id=step.id)
        self.session.add(event)
        self.session.flush()
        changes.append((event, None))
        self.supersede_proposals()
        operation = self._operation(changes)
        return {"message": self._t(f"已安排「{goal.title}」：{step.title}，{self._when(start, end)}", f'Scheduled "{goal.title}" · {step.title}, {self._when(start, end)}') + note,
                "event": self.serialize_event(event), "goal": self.serialize_goal(goal),
                "operation_id": operation}

    def accept(self, proposal_id, body):
        proposal = self._get(WebProposal, proposal_id)
        self.check_version(proposal, body)
        if proposal.status != "open" or proposal.expires_at <= self.now_naive:
            raise ApiError("stale_proposal", self._t("该候选已失效，请重新排期", "This proposal has expired; please generate new slots"))
        payload = json.loads(proposal.payload_json)
        step = self._get(Step, proposal.step_id)
        goal = self._active_step(step)
        self._ensure_not_running(step_id=step.id)
        if (payload["step_version"] != version_token(step) or payload["goal_version"] != version_token(goal)
                or payload["preferences"] != _hash(self._availability())):
            raise ApiError("stale_proposal", self._t("目标或偏好已更新，请重新排期", "Goal or availability changed; please generate new slots"))
        index = _integer(body.get("index"), 0, max(0, len(payload["candidates"]) - 1), self._t("候选序号", "Candidate index"), lang=self.lang)
        if not payload["candidates"]:
            raise ApiError("validation", self._t("没有可接受的候选", "No candidate slots available to accept"), 422)
        candidate = payload["candidates"][index]
        start, end = parse_datetime(candidate["start"], lang=self.lang), parse_datetime(candidate["end"], lang=self.lang)
        old = self._open_step_events(step.id)
        self._slot(start, end, ignore={e.id for e in old}, step=step, goal=goal)
        changes = [(e, self._event_snapshot(e)) for e in old]
        self._soft_cancel(old)
        event = Event(user_id=self.user_id, title=step.title[:200], start_utc=_naive(start),
                      end_utc=_naive(end), kind="block", status="planned", source="proposal", step_id=step.id)
        self.session.add(event)
        self.session.flush()
        changes.append((event, None))
        recoverable = [old_event for old_event in self._events()
                       if old_event.step_id == step.id and old_event.status in ("missed", "partial")
                       and old_event.resolved_at is not None and old_event.end_utc <= event.start_utc]
        if recoverable:
            previous = max(recoverable, key=lambda e: (e.resolved_at, e.id))
            self.session.add(RescheduleLog(event_id=previous.id, new_event_id=event.id,
                             ts=self.now_naive, moved_to_utc=event.start_utc, reason="web_recovery", auto=False))
        proposal.status, proposal.version = "accepted", proposal.version + 1
        operation = self._operation(changes)
        metrics.record(self.session, self.user_id, metrics.PROPOSAL_ACCEPTED, self.now_naive,
                       goal_id=goal.id, step_id=step.id, event_id=event.id, payload={"source": "web"})
        return {"message": self._t(f"已安排「{goal.title}」：{step.title}，{self._when(start, end)}", f'Scheduled "{goal.title}" · {step.title}, {self._when(start, end)}'), "event": self.serialize_event(event),
                "proposal": self.serialize_proposal(proposal), "operation_id": operation}

    def undo(self, operation_id):
        operation = self._get(WebOperation, operation_id)
        if operation.undone:
            raise ApiError("already_undone", self._t("该操作已撤销", "This operation has already been undone"))
        changes = json.loads(operation.payload_json)
        if isinstance(changes, dict) and "habit_id" in changes:
            return self.habits.undo(operation, changes)
        ids = {c["after"]["id"] for c in changes}
        rows = []
        for change in changes:
            event = self._get(Event, change["after"]["id"])
            self._ensure_not_running(event_id=event.id)
            if version_token(event) != change["version"]:
                raise ApiError("undo_conflict", self._t("日程已被修改，无法撤销", "Event changed since then and cannot be undone"))
            before = change["before"]
            if before is not None and before["status"] in OPEN:
                step = self._get(Step, event.step_id) if event.step_id else None
                goal = self._active_step(step) if step else None
                # Restoring overdue history is valid, but still checks interval conflicts.
                if event.kind == "block" and parse_datetime(before["start"], lang=self.lang) >= self.now:
                    self._slot(parse_datetime(before["start"], lang=self.lang), parse_datetime(before["end"], lang=self.lang), ignore=ids, step=step, goal=goal)
                else:
                    self._slot(parse_datetime(before["start"], lang=self.lang), parse_datetime(before["end"], lang=self.lang), ignore=ids, goal_block=False)
            rows.append((event, before))
        # Validate the complete restored schedule, including cumulative daily load.
        restored = [(e, b) for e, b in rows if b is not None and b["status"] in OPEN]
        config = self._availability()
        for i, (event, before) in enumerate(restored):
            start, end = parse_datetime(before["start"], lang=self.lang), parse_datetime(before["end"], lang=self.lang)
            for other, other_before in restored[i + 1:]:
                pad = timedelta(minutes=config["buffer_minutes"] if "block" in (event.kind, other.kind) else 0)
                if start < parse_datetime(other_before["end"], lang=self.lang) + pad and end > parse_datetime(other_before["start"], lang=self.lang) - pad:
                    raise ApiError("undo_conflict", self._t("恢复的日程相互冲突", "Restored events conflict with each other"))
        existing = [
            e for e in self._events()
            if e.id not in ids and e.kind == "block" and e.status not in ("skipped", "missed")
            and self._blocks_scheduling(e)
        ]
        for event, before in restored:
            if event.kind != "block" or parse_datetime(before["start"], lang=self.lang) < self.now:
                continue
            day = parse_datetime(before["start"], lang=self.lang).astimezone(self.zone).date()
            lo = datetime.combine(day, time(), self.zone).astimezone(UTC)
            hi = datetime.combine(day + timedelta(days=1), time(), self.zone).astimezone(UTC)
            spans = [(_utc(e.start_utc), _utc(e.end_utc)) for e in existing]
            spans += [(parse_datetime(b["start"], lang=self.lang), parse_datetime(b["end"], lang=self.lang)) for e, b in restored if e.kind == "block"]
            minutes = sum(max(0, (min(end, hi) - max(start, lo)).total_seconds()) / 60 for start, end in spans if end > lo and start < hi)
            if minutes > config["max_goal_minutes_per_day"]:
                raise ApiError("daily_limit", self._t("恢复日程将超出每日任务时长上限", "Restoring would exceed your daily task limit"))
        for event, before in rows:
            if before is None:
                self._soft_cancel([event])
                for log in self.session.scalars(select(RescheduleLog).where(RescheduleLog.new_event_id == event.id)):
                    log.new_event_id = None
            else:
                event.start_utc, event.end_utc = _naive(parse_datetime(before["start"], lang=self.lang)), _naive(parse_datetime(before["end"], lang=self.lang))
                event.status = before["status"]
                event.resolved_at = _naive(parse_datetime(before["resolved_at"], lang=self.lang)) if before["resolved_at"] else None
        operation.undone = True
        self.session.flush()
        return {"message": self._t("已撤销操作", "Undone"), "event": self.serialize_event(rows[-1][0])}

    def _start(self, step, event=None):
        goal = self._active_step(step)
        if self._running():
            raise ApiError("execution_active", self._t("已有正在进行的执行", "Another session is already running"))
        if goal_service.next_step(goal).id != step.id:
            raise ApiError("invalid_state", self._t("请先完成这个目标的前一步", "Complete the previous step in this goal first"))
        if event is None and self._open_step_events(step.id):
            raise ApiError("already_scheduled", self._t("该步骤已有日程，请从日程开始", "This step is already scheduled; start from its event"))
        duration = (_utc(event.end_utc) - _utc(event.start_utc)) if event else timedelta(minutes=step.est_min)
        end = self.now + duration
        ignore_ids = (event.id,) if event else ()
        self._slot(self.now, end, ignore=ignore_ids, step=step, goal=goal, strict=False)
        note = self._slot_override_note(self.now, end, ignore=ignore_ids)
        if event is None:
            event = Event(user_id=self.user_id, title=step.title[:200], kind="block", source="web", status="planned", step_id=step.id)
            self.session.add(event)
        event.start_utc, event.end_utc = self.now_naive, _naive(end)
        self.session.flush()
        execution = WebExecution(id=uuid4().hex, user_id=self.user_id, event_id=event.id, step_id=step.id,
                                 status="running", started_at=self.now_naive, expected_end_at=_naive(end), version=1)
        self.session.add(execution)
        self.session.flush()
        return {"message": self._t(f"开始「{goal.title}」：{step.title}，{self._when(self.now, end)}", f'Started "{goal.title}" · {step.title}, {self._when(self.now, end)}') + note, "event": self.serialize_event(event),
                "execution": self.serialize_execution(execution)}

    def start_event(self, id, body):
        event = self._get(Event, id)
        self.check_version(event, body)
        if event.kind != "block" or event.status not in OPEN or event.step_id is None:
            raise ApiError("invalid_state", self._t("只有未处理的目标块可以开始", "Only open goal blocks can be started"))
        if any(x.event_id == event.id for x in self._executions()):
            raise ApiError("already_started", self._t("该日程已开始过", "This event has already been started"))
        return self._start(self._get(Step, event.step_id), event)

    def start_step(self, id, body):
        step = self._get(Step, id)
        self.check_version(step, body)
        return self._start(step)

    def checkin_execution(self, id, body):
        execution = self._get(WebExecution, id)
        self.check_version(execution, body)
        if execution.status != "running":
            raise ApiError("already_resolved", self._t("该执行已签到", "This session has already been checked in"))
        outcome = body.get("outcome")
        if outcome not in ("done", "extend", "adjust"):
            raise ApiError("validation", self._t("执行签到结果无效", "Invalid execution check-in outcome"), 422)
        event = self._get(Event, execution.event_id)
        if execution.habit_occurrence_id is not None:
            occ = self._get(HabitOccurrence, execution.habit_occurrence_id)
            if (event.status not in OPEN or occ.event_id != event.id
                    or event.start_utc != execution.started_at or event.end_utc != execution.expected_end_at):
                raise ApiError("invalid_state", self._t("执行对应的日程已被修改，请刷新后处理", "The scheduled event for this session changed; refresh and retry"))
            if outcome == "extend":
                end = _utc(execution.expected_end_at) + timedelta(minutes=15)
                if end <= self.now:
                    raise ApiError("invalid_time", self._t("执行已超时，请调整或完成后重新开始", "Session is past due; wrap up or adjust and restart"))
                actual_now = self.now
                try:
                    self.now = _utc(event.start_utc)
                    self._slot(self.now, end, ignore=(event.id,), goal_block=False, habit_block=True, strict=False)
                finally:
                    self.now = actual_now
                execution.expected_end_at, event.end_utc = _naive(end), _naive(end)
            else:
                if self.now_naive < event.end_utc:
                    event.end_utc = max(event.start_utc + timedelta(seconds=1), self.now_naive)
                if outcome == "done":
                    event.status, event.resolved_at = "done", self.now_naive
                    occ.status, occ.completed_at = "done", self.now_naive
                    execution.status = "completed"
                else:
                    event.status, event.resolved_at = "skipped", self.now_naive
                    occ.event_id = None
                    execution.status = "adjusted"
                occ.revision += 1
                execution.resolved_at = self.now_naive
            execution.version += 1
            self.session.flush()
            return {"message": self._t(f"已更新习惯「{event.title}」的执行", f'Updated session for habit "{event.title}"'),
                    "execution": self.serialize_execution(execution), "event": self.serialize_event(event)}
        step = self._get(Step, execution.step_id)
        goal = self._active_step(step)
        if (event.status not in OPEN or event.step_id != execution.step_id
                or event.start_utc != execution.started_at or event.end_utc != execution.expected_end_at):
            raise ApiError("invalid_state", self._t("执行对应的日程已被修改，请刷新后处理", "The scheduled event for this session changed; refresh and retry"))
        if outcome == "extend":
            end = _utc(execution.expected_end_at) + timedelta(minutes=15)
            # Keep the original interval, but allow its already elapsed portion.
            if end <= self.now:
                raise ApiError("invalid_time", self._t("执行已超时，请调整或完成后重新开始", "Session is past due; wrap up or adjust and restart"))
            # Revalidate whole interval with original start by temporarily evaluating at that instant.
            actual_now = self.now
            try:
                self.now = _utc(event.start_utc)
                self._slot(self.now, end, ignore=(event.id,), step=step, goal=goal, strict=False)
            finally:
                self.now = actual_now
            execution.expected_end_at, event.end_utc = _naive(end), _naive(end)
        else:
            minutes = max(0, int((self.now_naive - execution.started_at).total_seconds() // 60))
            previous_checkin = self.session.info.get("web_execution_checkin")
            self.session.info["web_execution_checkin"] = execution.id
            try:
                recovery.resolve_block(self.session, event, "done" if outcome == "done" else "partial", self.now_naive, minutes=minutes)
            finally:
                if previous_checkin is None:
                    self.session.info.pop("web_execution_checkin", None)
                else:
                    self.session.info["web_execution_checkin"] = previous_checkin
            # Early completion releases the unspent part of this calendar block.
            # Keep expected_end_at as the execution's original expectation.
            if self.now_naive < event.end_utc:
                event.end_utc = max(event.start_utc + timedelta(seconds=1), self.now_naive)
            execution.status = "completed" if outcome == "done" else "adjusted"
            execution.resolved_at = self.now_naive
            if outcome == "done":
                self._soft_cancel([e for e in self._open_step_events(step.id) if e.id != event.id and _utc(e.start_utc) >= self.now])
        execution.version += 1
        self.session.flush()
        refill_note = self._auto_refill(goal, body) if outcome == "done" else ""
        return {"message": self._t(f"已更新「{goal.title}」的执行{refill_note}", f'Updated session for "{goal.title}"{refill_note}'), "execution": self.serialize_execution(execution),
                "event": self.serialize_event(event), "goal": self.serialize_goal(goal)}

    def checkin_event(self, id, body):
        event = self._get(Event, id)
        self.check_version(event, body)
        outcome = body.get("outcome")
        if outcome not in ("done", "missed", "partial"):
            raise ApiError("validation", self._t("日程签到结果无效", "Invalid event check-in outcome"), 422)
        if event.kind != "block" or event.status not in OPEN or _utc(event.end_utc) >= self.now:
            raise ApiError("invalid_state", self._t("只有已结束且未签到的目标块可以签到", "Only ended, unresolved goal blocks can be checked in"))
        self._ensure_not_running(event_id=event.id, step_id=event.step_id)
        if any(x.event_id == event.id for x in self._executions()):
            raise ApiError("already_started", self._t("请从执行记录签到", "Please check in from the active execution record"))
        step = self._get(Step, event.step_id) if event.step_id else None
        if step:
            self._active_step(step)
        recovery.resolve_block(self.session, event, outcome, self.now_naive)
        if step and outcome == "done":
            self._soft_cancel([e for e in self._open_step_events(step.id) if _utc(e.start_utc) >= self.now])
        self.session.flush()
        refill_note = self._auto_refill(step.goal, body) if step and outcome == "done" else ""
        result = {"message": self._t(f"已记录签到{refill_note}", f"Check-in recorded{refill_note}"), "event": self.serialize_event(event)}
        if step:
            result["goal"] = self.serialize_goal(step.goal)
        return result

    def preferences(self):
        data = self._availability()
        return {**data, "version": _hash(data)}

    def save_preferences(self, body):
        if not isinstance(body.get("version"), str):
            raise ApiError("validation", self._t("需要偏好版本", "Preferences version is required"), 422)
        if body["version"] != self.preferences()["version"]:
            raise ApiError("version_conflict", self._t("偏好已更新，请刷新后重试", "Preferences were updated elsewhere. Refresh and retry."))
        try:
            data = validate_availability(body, lang=self.lang)
        except ValueError as exc:
            raise ApiError("validation", str(exc), 422) from exc
        memory.set_preference(self.session, self.user_id, "web_availability", data, scope="scheduler")
        self.session.flush()
        return self.preferences()
