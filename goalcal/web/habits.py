"""Habit rules, local-date records and their optional calendar reservations."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select

from ..models import Event, Goal
from .core import ApiError, UTC, _integer, _iso, _json, _naive, _text, _utc, parse_datetime
from .models import Habit, HabitOccurrence, HabitRule, WebExecution, WebOperation

DAY = timedelta(days=1)
HORIZON = timedelta(days=27)


def days(first, last):
    while first <= last:
        yield first
        first += DAY


def monday(day):
    return day - timedelta(days=day.weekday())


class HabitService:
    def __init__(self, web):
        self.web = web
        self.session = web.session
        self.uid = web.user_id
        self.now = web.now
        self.t = web._t

    def _habits(self):
        return list(self.session.scalars(select(Habit).where(Habit.user_id == self.uid).order_by(Habit.id)))

    def _rules(self, habit):
        return list(self.session.scalars(select(HabitRule).where(
            HabitRule.habit_id == habit.id, HabitRule.user_id == self.uid,
        ).order_by(HabitRule.effective_date)))

    def _records(self, habit, first, last):
        return {r.day: r for r in self.session.scalars(select(HabitOccurrence).where(
            HabitOccurrence.habit_id == habit.id, HabitOccurrence.user_id == self.uid,
            HabitOccurrence.day.between(first, last),
        ))}

    def _today(self, habit):
        return self.now.astimezone(ZoneInfo(habit.timezone)).date()

    def _date(self, value):
        try:
            day = date.fromisoformat(value)
            if day.isoformat() == value and 1900 <= day.year <= 9998:
                return day
        except (TypeError, ValueError):
            pass
        raise ApiError("validation", self.t("请选择有效日期", "Choose a valid date"), 422)

    def _config(self, body):
        mode = body.get("mode")
        weekdays = body.get("weekdays", [])
        clock = body.get("start_time")
        if mode not in ("fixed", "weekly"):
            raise ApiError("validation", self.t("请选择重复规则", "Choose a repeat rule"), 422)
        if mode == "fixed":
            if not weekdays or any(type(d) is not int or not 0 <= d <= 6 for d in weekdays):
                raise ApiError("validation", self.t("至少选择一天", "Select at least one day"), 422)
            if clock:
                try:
                    if len(clock) != 5 or time.fromisoformat(clock).isoformat(timespec="minutes") != clock:
                        raise ValueError()
                except ValueError:
                    raise ApiError("validation", self.t("请输入有效时间", "Enter a valid time"), 422) from None
        return {
            "title": _text(body.get("title"), lang=self.web.lang),
            "est_min": _integer(body.get("est_min"), 5, 480, self.t("预计分钟", "Estimated minutes"), lang=self.web.lang),
            "mode": mode, "weekdays": sorted(set(weekdays)) if mode == "fixed" else [],
            "start_time": (clock or None) if mode == "fixed" else None,
            "weekly_target": _integer(body.get("weekly_target"), 1, 7, self.t("每周次数", "Weekly target"),
                                      lang=self.web.lang) if mode == "weekly" else None,
        }

    @staticmethod
    def _rule(rules, day):
        return next((rule for rule in reversed(rules) if rule.effective_date <= day), None)

    @staticmethod
    def _active(habit, day):
        return day >= habit.start_date and not any(
            start <= day.isoformat() and (end is None or day.isoformat() < end)
            for start, end in json.loads(habit.inactive_periods)
        )

    def _eligible(self, habit, rules, day):
        rule = self._rule(rules, day)
        if rule is None or not self._active(habit, day):
            return None
        config = json.loads(rule.definition)
        return config if config["mode"] == "weekly" or day.weekday() in config["weekdays"] else None

    def _bounds(self, habit, day, config):
        zone = ZoneInfo(habit.timezone)
        local = datetime.combine(day, time.fromisoformat(config["start_time"]), zone)
        start = local.astimezone(UTC)
        end = start + timedelta(minutes=config["est_min"])
        if start.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
            raise ApiError("invalid_time", self.t("当地时间不存在，请另选时间", "This local time does not exist; choose another time"))
        self._same_day(habit, day, end)
        return start, end

    def _same_day(self, habit, day, end):
        if (end - timedelta(microseconds=1)).astimezone(ZoneInfo(habit.timezone)).date() != day:
            raise ApiError("invalid_time", self.t("习惯安排须在同一天内", "A habit slot must stay within its day"), 422)

    def _event(self, record):
        return self.session.get(Event, record.event_id) if record and record.event_id else None

    def _version(self, habit, day, row):
        event = self._event(row)
        # A record and its linked slot share one concurrency token.
        from .core import _hash, version_token
        return _hash([habit.revision, day, row.id if row else None, row.revision if row else None,
                      version_token(event) if event else None])

    def _new_record(self, habit, day, mode):
        row = HabitOccurrence(id=f"{habit.id}:{day}" if mode == "fixed" else uuid4().hex,
                              user_id=self.uid, habit_id=habit.id, day=day)
        self.session.add(row)
        self.session.flush()
        return row

    def _reservation(self, habit, row, config, start, end, *, strict=True):
        event = self._event(row)
        self._same_day(habit, row.day, end)
        self.web._slot(start, end, ignore=(event.id,) if event else (), goal_block=False, habit_block=True, strict=strict)
        if event is None:
            event = Event(user_id=self.uid, kind="habit", source="habit",
                          title=config["title"], start_utc=_naive(start), end_utc=_naive(end))
            self.session.add(event)
            self.session.flush()
            row.event_id = event.id
        event.title, event.status, event.resolved_at = config["title"], "planned", None
        event.start_utc, event.end_utc = _naive(start), _naive(end)
        row.reason = None

    def _withdraw(self, row, *, unlink=False):
        event = self._event(row)
        if event:
            event.status, event.resolved_at = "skipped", self.web.now_naive
        if unlink:
            row.event_id = None

    def needs_roll(self):
        for habit in self._habits():
            window = self._roll_window(habit)
            if window and self._has_timed_rule(self._rules(habit), *window):
                return True
        return False

    def _has_timed_rule(self, rules, first, last):
        current = self._rule(rules, first)
        relevant = [r for r in rules if first < r.effective_date <= last or r is current]
        return any(json.loads(rule.definition)["start_time"] for rule in relevant)

    def _roll_window(self, habit):
        if habit.status != "active":
            return None
        today = self._today(habit)
        first = max(today, habit.start_date, (habit.confirmed_through + DAY) if habit.confirmed_through else today)
        end = today + HORIZON
        return (first, end) if first <= end else None

    def roll(self, habits=None):
        for habit in self._habits() if habits is None else habits:
            window = self._roll_window(habit)
            if not window:
                continue
            rules = self._rules(habit)
            first, last = window
            records = self._records(habit, first, last)
            for day in days(first, last):
                config = self._eligible(habit, rules, day)
                if not config or not config["start_time"]:
                    continue
                row = records.get(day)
                if row and row.status != "cancelled":
                    continue
                row = row or self._new_record(habit, day, "fixed")
                row.status = "pending"
                try:
                    start, end = self._bounds(habit, day, config)
                    if start < self.now:
                        row.reason = "time_passed"
                    else:
                        self._reservation(habit, row, config, start, end)
                except ApiError as exc:
                    row.reason = exc.code
                row.revision += 1
            habit.confirmed_through = last
        self.session.flush()

    def _occurrence(self, habit, rules, day, row, *, offer=False):
        config = self._eligible(habit, rules, day)
        present = row is not None and row.status != "cancelled"
        if not present and (config is None or (config["mode"] == "weekly" and not offer)):
            return None
        config = config or json.loads(self._rule(rules, day).definition)
        status = row.status if present else "pending"
        today = self._today(habit)
        if status == "pending" and day < today:
            status = "missed"
        event = self._event(row) if present else None
        reason = row.reason if present else None
        if not present and config["start_time"] and day > today + HORIZON:
            reason = "unconfirmed"
        return {
            "id": row.id if row else f"{habit.id}:{day}", "habit_id": habit.id, "date": day.isoformat(),
            "title": config["title"], "mode": config["mode"], "est_min": config["est_min"],
            "timezone": habit.timezone, "today": today.isoformat(), "status": status,
            "can_record": config is not None and day <= today,
            "reason": reason, "event_id": event.id if event else None,
            "start": _iso(event.start_utc) if event else None, "end": _iso(event.end_utc) if event else None,
            "version": self._version(habit, day, row), "habit_version": str(habit.revision),
        }

    def _progress(self, habit, rules, week, records):
        today = self._today(habit)
        configs = {day: self._eligible(habit, rules, day) for day in days(week, week + 6 * DAY)}
        weekly = {d: c for d, c in configs.items() if c and c["mode"] == "weekly"}
        historical_rule = self._rule(rules, week + 6 * DAY)
        historical_config = json.loads(historical_rule.definition) if historical_rule else None
        done = sum(r.status == "done" for d, r in records.items() if monday(d) == week)
        handled = any(r.status in ("done", "skipped") for d, r in records.items() if monday(d) == week)
        if weekly or (historical_config and historical_config["mode"] == "weekly" and handled):
            config = next(iter(weekly.values()), historical_config)
            schedule_from = next((d.isoformat() for d in weekly if d >= today), None)
            return {"habit_id": habit.id, "title": config["title"], "est_min": config["est_min"],
                    "version": str(habit.revision), "timezone": habit.timezone,
                    "week": week.isoformat(), "end": (week + 6 * DAY).isoformat(),
                    "today": today.isoformat(), "done": done, "target": config["weekly_target"],
                    "partial": len(weekly) < 7, "mode": "weekly", "schedule_from": schedule_from}
        fixed = sum(bool(c and c["mode"] == "fixed") for c in configs.values())
        # Preserve completed or explicitly skipped dates when a pause begins midweek.
        for day, c in configs.items():
            row = records.get(day)
            if not c and row and row.status in ("done", "skipped"):
                fixed += 1
        if fixed and historical_config:
            return {"habit_id": habit.id, "title": historical_config["title"], "version": str(habit.revision),
                    "est_min": historical_config["est_min"],
                    "timezone": habit.timezone, "week": week.isoformat(), "end": (week + 6 * DAY).isoformat(),
                    "today": today.isoformat(), "done": done, "target": fixed,
                    "partial": False, "mode": "fixed", "schedule_from": None}
        return None

    def range(self, first, last, *, view):
        items, weeks = [], []
        for habit in self._habits():
            today = self._today(habit)
            rules = self._rules(habit)
            records = self._records(habit, monday(first), monday(last) + 6 * DAY)
            for day in days(first, last):
                item = self._occurrence(habit, rules, day, records.get(day))
                if item:
                    items.append(item)
            first_week, last_week = monday(first), monday(last)
            for offset in range((last_week - first_week).days // 7 + 1):
                week = first_week + 7 * offset * DAY
                progress = self._progress(habit, rules, week, records)
                if not progress or progress["mode"] != "weekly":
                    continue
                if view == "today":
                    offer = self._occurrence(habit, rules, today, records.get(today), offer=True)
                    if (offer and offer["mode"] == "weekly" and offer["status"] == "pending"
                            and (not records.get(today) or records[today].status == "cancelled")
                            and progress["done"] < progress["target"]):
                        weeks.append({**progress, "offer": offer})
                else:
                    weeks.append(progress)
        return {"items": sorted(items, key=lambda i: (i["date"], i["start"] or "", i["habit_id"])),
                "weeks": weeks}

    def list(self):
        ordered = sorted(self._habits(), key=lambda h: (not h.pinned, h.id))
        return {"habits": [self._summary(h) for h in ordered]}

    def _summary(self, habit):
        today = self._today(habit)
        rules = self._rules(habit)
        rule = self._rule(rules, today)
        latest = rules[-1]
        config = json.loads((rule or latest).definition)
        week = monday(today)
        records = self._records(habit, week, max(week + 6 * DAY, today + HORIZON))
        first = max(today, habit.start_date)
        next_item = None
        next_unscheduled = None
        next_week = None
        if habit.status == "active":
            for day in days(first, first + HORIZON):
                item = self._occurrence(habit, rules, day, records.get(day))
                if item and item["status"] == "pending":
                    if next_item is None:
                        next_item = item
                    if next_unscheduled is None and item["start"] is None:
                        next_unscheduled = item
                    if next_item is not None and next_unscheduled is not None:
                        break
            if next_unscheduled is None:
                week_records = self._records(habit, monday(first), monday(first + HORIZON) + 6 * DAY)
                w = monday(first)
                limit_week = monday(first + HORIZON)
                while w <= limit_week:
                    prog = self._progress(habit, rules, w, week_records)
                    if prog and prog["mode"] == "weekly":
                        planned_or_done = sum(
                            r.status == "done" or (r.status == "pending" and r.event_id is not None)
                            for d, r in week_records.items() if monday(d) == w
                        )
                        avail_day = next(
                            (d for d in days(w, w + 6 * DAY)
                             if d >= today and self._eligible(habit, rules, d)
                             and (not week_records.get(d) or week_records[d].status == "cancelled")),
                            None,
                        )
                        if planned_or_done < prog["target"] and avail_day is not None:
                            next_week = {**prog, "schedule_from": avail_day.isoformat()}
                            break
                    w += 7 * DAY
        goal = self.session.get(Goal, habit.goal_id) if habit.goal_id else None
        return {"id": habit.id, "status": habit.status, "pinned": bool(habit.pinned), "goal_id": habit.goal_id,
                "goal_title": goal.title if goal else None, "goal_status": goal.status if goal else None,
                "timezone": habit.timezone, "today": today.isoformat(), "start_date": habit.start_date.isoformat(),
                "version": str(habit.revision), "rule": config, "next": next_item,
                "next_unscheduled": next_unscheduled, "next_week": next_week,
                "progress": self._progress(habit, rules, week, records),
                "upcoming_rule": ({"date": latest.effective_date.isoformat(), "rule": json.loads(latest.definition)}
                                  if rule is not None and latest.id != rule.id else None)}

    def slots(self, habit_id, query):
        from ..domain import scheduler
        habit = self.web._get(Habit, habit_id)
        today = self._today(habit)
        start_day = self._date(query["start_date"]) if query.get("start_date") else today
        end_day = self._date(query["end_date"]) if query.get("end_date") else start_day
        if end_day < start_day:
            return {"candidates": []}
        start_day = max(today, start_day)
        end_day = min(end_day, start_day + HORIZON)
        if end_day < start_day:
            return {"candidates": []}
        period = query.get("period") or "all"
        if period not in ("all", "morning", "afternoon", "evening"):
            raise ApiError("validation", self.t("无效的时段筛选", "Invalid period filter"), 422)
        ignore_day = self._date(query["ignore_date"]) if query.get("ignore_date") else None
        rules = self._rules(habit)
        zone = ZoneInfo(habit.timezone)
        records = self._records(habit, start_day, end_day)
        eligible_days = {}
        for day in days(start_day, end_day):
            cfg = self._eligible(habit, rules, day)
            if not cfg:
                continue
            if cfg["mode"] == "weekly" and day != ignore_day:
                row = records.get(day)
                if row and row.status != "cancelled":
                    continue
            eligible_days[day] = cfg
        if not eligible_days:
            return {"candidates": []}
        default_min = next(iter(eligible_days.values()))["est_min"]
        est_min = _integer(int(query["minutes"]) if query.get("minutes") else default_min,
                           5, 480, self.t("分钟", "Minutes"), lang=self.web.lang)
        ignore = set()
        if ignore_day:
            ig_row = self._records(habit, ignore_day, ignore_day).get(ignore_day)
            if ig_row and ig_row.event_id:
                ignore.add(ig_row.event_id)
        win_start = max(self.now, datetime.combine(start_day, time(), zone).astimezone(UTC))
        win_end = datetime.combine(end_day + DAY, time(), zone).astimezone(UTC)
        if start_day == end_day and period != "all":
            p_bounds = {"morning": (6, 12), "afternoon": (12, 18), "evening": (18, 24)}[period]
            p_start = datetime.combine(start_day, time(p_bounds[0], 0), zone).astimezone(UTC)
            p_end = (datetime.combine(start_day + DAY, time(), zone) if p_bounds[1] == 24
                     else datetime.combine(start_day, time(p_bounds[1], 0), zone)).astimezone(UTC)
            win_start = max(win_start, p_start)
            win_end = min(win_end, p_end)
        if win_end <= win_start:
            return {"candidates": []}
        prefs = self.web._prefs()
        prefs.web_availability = self.web._availability()
        busy = [scheduler.BusyBlock(_utc(e.start_utc).astimezone(zone), _utc(e.end_utc).astimezone(zone), e.kind)
                for e in self.web._events()
                if e.id not in ignore and e.status not in ("skipped", "missed") and self.web._blocks_scheduling(e)]
        raw = scheduler.propose(
            scheduler.Requirement(est_min=est_min, energy=2, kind="habit", min_chunk=min(20, est_min)),
            self.now.astimezone(zone), busy, prefs=prefs, top_n=48 if period != "all" else 24,
            window=(win_start.astimezone(zone), win_end.astimezone(zone)), lang=self.web.lang,
        )
        valid = []
        for cand in sorted(raw, key=lambda c: c.start):
            local_start = cand.start.astimezone(zone)
            cand_day = local_start.date()
            if cand_day not in eligible_days:
                continue
            if (cand.end - timedelta(microseconds=1)).astimezone(zone).date() != cand_day:
                continue
            if period == "morning" and not (6 <= local_start.hour < 12):
                continue
            if period == "afternoon" and not (12 <= local_start.hour < 18):
                continue
            if period == "evening" and not (18 <= local_start.hour < 24):
                continue
            try:
                self.web._slot(cand.start.astimezone(UTC), cand.end.astimezone(UTC),
                               ignore=ignore, goal_block=False, habit_block=True)
            except ApiError:
                continue
            valid.append({
                "start": _iso(cand.start),
                "end": _iso(cand.end),
                "date": cand_day.isoformat(),
                "reasons": cand.reasons,
            })
            if len(valid) >= 6:
                break
        return {"candidates": valid}

    def detail(self, habit_id, query):
        habit = self.web._get(Habit, habit_id)
        summary = self._summary(habit)
        today = self._today(habit)
        rules = self._rules(habit)
        if query.get("date"):
            day = self._date(query["date"])
            row = self._records(habit, day, day).get(day)
            return {"occurrence": self._occurrence(habit, rules, day, row, offer=True)}
        last = self._date(query["through"]) if query.get("through") else today
        first = last - HORIZON
        records = self._records(habit, monday(first), monday(last) + 6 * DAY)
        history = [self._occurrence(habit, rules, day, records.get(day)) for day in days(first, last)]
        weeks = [self._progress(habit, rules, week, records)
                 for week in (monday(first) + 7 * i * DAY for i in range(5)) if week <= monday(last)]
        future = self._records(habit, today + DAY, today + HORIZON)
        week_start = monday(today)
        week_end = week_start + 6 * DAY
        week_records = self._records(habit, week_start, week_end)
        week_items = [self._occurrence(habit, rules, day, week_records.get(day))
                      for day in days(week_start, week_end)]
        progress = summary["progress"]
        if progress and progress["mode"] == "weekly" and progress["done"] < progress["target"]:
            offer = self._occurrence(habit, rules, today, week_records.get(today), offer=True)
            if offer and offer["status"] == "pending" and offer["can_record"]:
                progress["offer"] = offer
        return {**summary, "through": last.isoformat(), "week_items": [item for item in week_items if item],
                "history": [i for i in reversed(history) if i],
                "weeks": [p for p in reversed(weeks) if p],
                "scheduled": [self._occurrence(habit, rules, day, row) for day, row in sorted(future.items())
                              if row.status == "pending"]}

    def _version_check(self, expected, received):
        if expected != received:
            raise ApiError("version_conflict", self.t("内容已更新，请刷新后重试", "This item changed. Refresh and retry."))

    def _effective(self, habit, config, rules, today):
        current = self._rule(rules, today)
        if current is None:
            return habit.start_date
        current_mode = json.loads(current.definition)["mode"]
        return monday(today) + 7 * DAY if "weekly" in (current_mode, config["mode"]) else today + DAY

    def _goal_id(self, habit, body):
        goal_id = body.get("goal_id")
        if goal_id is not None:
            goal = self.web._get(Goal, goal_id)
            if goal.status != "active" and (habit is None or goal_id != habit.goal_id):
                raise ApiError("invalid_state", self.t("请选择进行中的目标", "Choose an active goal"))
        return goal_id

    def preview(self, body):
        habit_id = body.get("habit_id")
        habit = self.web._get(Habit, habit_id) if habit_id is not None else None
        config = self._config(body)
        self._goal_id(habit, body)
        today = self._today(habit) if habit else self.now.astimezone(self.web.zone).date()
        rules = self._rules(habit) if habit else []
        if rules and json.loads(rules[-1].definition) == config:
            effective = max(today, rules[-1].effective_date)
            return {"effective_date": effective.isoformat(), "week": monday(effective).isoformat(),
                    "weekly_target": config["weekly_target"], "slots": [], "replaces": []}
        effective = (self._effective(habit, config, rules, today) if habit
                     else self._date(body.get("start_date")))
        if not habit and effective < today:
            raise ApiError("validation", self.t("开始日期不能早于今天", "Start date cannot be before today"), 422)
        records = self._records(habit, effective, date(9998, 12, 31)) if habit else {}
        removed = [day.isoformat() for day, row in sorted(records.items()) if row.custom and row.status == "pending"]
        ignore = [row.event_id for row in records.values()
                  if row.status == "pending" and row.event_id is not None]
        slots = []
        if config["mode"] == "fixed":
            zone_owner = habit or Habit(timezone=self.web.tz)
            for day in days(effective, effective + HORIZON):
                if day.weekday() not in config["weekdays"]:
                    continue
                start = end = reason = None
                if config["start_time"]:
                    try:
                        start, end = self._bounds(zone_owner, day, config)
                        if start < self.now:
                            reason = "time_passed"
                        elif day > today + HORIZON:
                            reason = "unconfirmed"
                        else:
                            self.web._slot(start, end, ignore=ignore, goal_block=False, habit_block=True)
                    except ApiError as exc:
                        reason = exc.code
                slots.append({"date": day.isoformat(), "start": _iso(start), "end": _iso(end), "reason": reason})
                if len(slots) == 5:
                    break
        return {"effective_date": effective.isoformat(), "week": monday(effective).isoformat(),
                "weekly_target": config["weekly_target"], "slots": slots, "replaces": removed}

    def create(self, body):
        self.preview(body)
        habit = Habit(user_id=self.uid, goal_id=body.get("goal_id"), timezone=self.web.tz,
                      start_date=self._date(body.get("start_date")))
        self.session.add(habit)
        self.session.flush()
        self.session.add(HabitRule(habit_id=habit.id, user_id=self.uid, effective_date=habit.start_date,
                                  definition=_json(self._config(body))))
        self.session.flush()
        self.roll([habit])
        return {"habit": self._summary(habit), "message": self.t("已创建习惯", "Habit created")}

    def _clear_pending(self, habit, first):
        for row in self._records(habit, first, date(9998, 12, 31)).values():
            if row.status == "pending":
                self._withdraw(row, unlink=True)
                row.status, row.reason, row.custom = "cancelled", None, False
                row.revision += 1
        if habit.confirmed_through and habit.confirmed_through >= first:
            habit.confirmed_through = first - DAY

    def update(self, habit_id, body):
        habit = self.web._get(Habit, habit_id)
        self._version_check(str(habit.revision), body.get("version"))
        config = self._config(body)
        goal_id = self._goal_id(habit, body)
        today = self._today(habit)
        rules = self._rules(habit)
        if json.loads(rules[-1].definition) != config:
            effective = self._effective(habit, config, rules, today)
            if body.get("effective_date") and body["effective_date"] != effective.isoformat():
                raise ApiError("version_conflict", self.t("生效日期已变化，请重新预览", "The effective date changed. Preview again."))
            self._clear_pending(habit, effective)
            for rule in rules:
                if rule.effective_date >= effective:
                    self.session.delete(rule)
            self.session.flush()
            self.session.add(HabitRule(habit_id=habit.id, user_id=self.uid, effective_date=effective,
                                      definition=_json(config)))
        habit.goal_id = goal_id
        habit.revision += 1
        self.session.flush()
        self.roll([habit])
        return {"habit": self._summary(habit), "message": self.t("习惯已更新", "Habit updated")}

    def status(self, habit_id, body):
        habit = self.web._get(Habit, habit_id)
        self._version_check(str(habit.revision), body.get("version"))
        was_status = habit.status
        was_pinned = bool(habit.pinned)
        status = body.get("status", habit.status)
        pinned = body.get("pinned", habit.pinned)
        changed = False
        if habit.status != status:
            today = self._today(habit)
            periods = json.loads(habit.inactive_periods)
            if status == "active":
                if periods and periods[-1][0] == today.isoformat():
                    periods.pop()
                elif periods:
                    periods[-1][1] = today.isoformat()
            elif habit.status == "active":
                self.web._ensure_not_running(habit_id=habit.id)
                periods.append([today.isoformat(), None])
                self._clear_pending(habit, today)
            habit.inactive_periods = _json(periods)
            habit.status = status
            if status == "archived":
                pinned = False
            changed = True
        if bool(habit.pinned) != bool(pinned):
            habit.pinned = bool(pinned)
            changed = True
        if changed:
            habit.revision += 1
            self.session.flush()
            self.roll([habit])
        summary = self._summary(habit)
        title = summary["rule"]["title"]
        if status == "archived":
            msg = self.t(f"「{title}」已归档至 设置 → 已归档", f'"{title}" archived to Settings → Archived')
        elif was_status == "archived" and status == "active":
            msg = self.t(f"「{title}」已恢复至习惯", f'"{title}" restored to Habits')
        elif was_status != status and status == "paused":
            msg = self.t(f"已暂停习惯「{title}」", f'Paused habit "{title}"')
        elif was_status == "paused" and status == "active":
            msg = self.t(f"已恢复习惯「{title}」", f'Resumed habit "{title}"')
        elif bool(habit.pinned) != was_pinned:
            msg = (self.t(f"已置顶习惯「{title}」", f'Pinned habit "{title}"') if habit.pinned
                   else self.t(f"已取消置顶「{title}」", f'Unpinned "{title}"'))
        else:
            msg = self.t("习惯状态已更新", "Habit status updated")
        return {"habit": summary, "message": msg}

    def delete(self, habit_id, body):
        habit = self.web._get(Habit, habit_id)
        self._version_check(str(habit.revision), body.get("version"))
        if habit.status != "archived":
            raise ApiError("invalid_state", self.t("请先归档习惯后再删除", "Archive the habit before deleting it"))
        self.web._ensure_not_running(habit_id=habit.id)
        rules = self._rules(habit)
        title = json.loads(rules[-1].definition)["title"] if rules else f"#{habit.id}"
        occurrences = list(self.session.scalars(select(HabitOccurrence).where(
            HabitOccurrence.user_id == self.uid, HabitOccurrence.habit_id == habit.id)))
        occ_ids = [o.id for o in occurrences]
        event_ids = [o.event_id for o in occurrences if o.event_id is not None]
        if occ_ids:
            for execution in self.session.scalars(select(WebExecution).where(
                    WebExecution.user_id == self.uid, WebExecution.habit_occurrence_id.in_(occ_ids))):
                self.session.delete(execution)
        if event_ids:
            for execution in self.session.scalars(select(WebExecution).where(
                    WebExecution.user_id == self.uid, WebExecution.event_id.in_(event_ids))):
                self.session.delete(execution)
        for occ in occurrences:
            self.session.delete(occ)
        self.session.flush()
        if event_ids:
            for event in self.session.scalars(select(Event).where(
                    Event.user_id == self.uid, Event.id.in_(event_ids))):
                self.session.delete(event)
        for rule in rules:
            self.session.delete(rule)
        self.session.delete(habit)
        self.session.flush()
        return {"message": self.t(f"已删除习惯「{title}」", f'Deleted habit "{title}"')}

    def _snapshot(self, row):
        event = self._event(row)
        return {"date": row.day.isoformat(), "status": row.status, "prior_status": row.prior_status,
                "reason": row.reason, "custom": row.custom, "completed_at": _iso(row.completed_at),
                "event": self.web._event_snapshot(event) if event else None}

    def _save_undo(self, habit, row, before):
        self.session.flush()
        operation = WebOperation(id=uuid4().hex, user_id=self.uid, payload_json=_json({
            "habit_id": habit.id, "occurrence_id": row.id, "before": before,
            "version": self._version(habit, row.day, row),
        }))
        self.session.add(operation)
        return operation.id

    def _restore_slot(self, event):
        if _utc(event.start_utc) >= self.now:
            self.web._slot(_utc(event.start_utc), _utc(event.end_utc), ignore=(event.id,),
                           goal_block=False, habit_block=True)
        event.status, event.resolved_at = "planned", None

    def act(self, habit_id, body):
        habit = self.web._get(Habit, habit_id)
        day = self._date(body.get("date"))
        today = self._today(habit)
        rules = self._rules(habit)
        row = self._records(habit, day, day).get(day)
        action = body["action"]
        config = self._eligible(habit, rules, day)
        pending = row is None or row.status in ("pending", "cancelled")
        snapshot = self._snapshot(row) if row else None
        if body.get("version"):
            self._version_check(self._version(habit, day, row), body["version"])
        else:
            self._version_check(str(habit.revision), body.get("habit_version"))
            if row and row.status != "cancelled":
                self._version_check(self._version(habit, day, row), None)
        if row is not None and action != "start":
            self.web._ensure_not_running(event_id=row.event_id, habit_occurrence_id=row.id)
        allowed = config is not None
        if action == "restore":
            allowed = row is not None and row.status == "done"
        elif action == "done":
            allowed = allowed and day <= today and (pending or row.status == "skipped")
        elif action == "start":
            allowed = allowed and day == today and pending
        else:
            allowed = allowed and pending and day >= today
        if not allowed:
            raise ApiError("invalid_state", self.t("本次状态已变化，无法执行此操作", "This occurrence is no longer available for that action"))
        if action == "start":
            if self.web._running() is not None:
                raise ApiError("execution_active", self.t("已有进行中的执行，请先签到", "Another session is already running; check in first"))
            if row is None:
                row = self._new_record(habit, day, config["mode"])
            minutes = _integer(body.get("minutes", config["est_min"]), 5, 480, self.t("分钟", "Minutes"), lang=self.web.lang)
            end = self.now + timedelta(minutes=minutes)
            row.day, row.status, row.custom, row.reason = day, "pending", True, None
            ignore_ids = (row.event_id,) if row.event_id else ()
            self._reservation(habit, row, config, self.now, end, strict=False)
            note = self.web._slot_override_note(self.now, end, ignore=ignore_ids)
            row.revision += 1
            event = self._event(row)
            execution = WebExecution(
                id=uuid4().hex, user_id=self.uid, event_id=event.id, step_id=None,
                habit_occurrence_id=row.id, status="running",
                started_at=self.web.now_naive, expected_end_at=_naive(end), version=1,
            )
            self.session.add(execution)
            self.session.flush()
            return {
                "message": self.t(f"开始习惯「{config['title']}」，{self.web._when(self.now, end)}",
                                  f'Started habit "{config["title"]}", {self.web._when(self.now, end)}') + note,
                "event": self.web.serialize_event(event),
                "execution": self.web.serialize_execution(execution),
                "occurrence": self._occurrence(habit, rules, row.day, row),
            }
        if row is None:
            row = self._new_record(habit, day, config["mode"])
        event = self._event(row)
        note = ""
        if action == "schedule":
            start = parse_datetime(body["start"], lang=self.web.lang) if body.get("start") else None
            target = self._date(body["target_date"]) if body.get("target_date") else day
            if start and start.astimezone(ZoneInfo(habit.timezone)).date() != target:
                raise ApiError("invalid_time", self.t("所选时间需在安排日期内", "Choose a time on the selected date"), 422)
            if target < today or not self._eligible(habit, rules, target) or (target != day and (
                    config["mode"] != "weekly" or monday(target) != monday(day))):
                raise ApiError("invalid_time", self.t("固定习惯只能改当天，按周习惯只能在本周改期",
                                                      "Fixed habits stay on their date; weekly habits stay within their week"), 422)
            if target != day:
                other = self._records(habit, target, target).get(target)
                if other and other.status != "cancelled":
                    raise ApiError("duplicate_day", self.t("该日期已有一次安排或记录", "This date already has a session or record"))
                if other:
                    self.session.delete(other)
                    self.session.flush()
            row.day, row.status, row.custom, row.reason = target, "pending", True, None
            if start:
                minutes = _integer(body.get("minutes", config["est_min"]), 5, 480, self.t("分钟", "Minutes"),
                                   lang=self.web.lang)
                end = start + timedelta(minutes=minutes)
                ignore_ids = (row.event_id,) if row.event_id else ()
                self._reservation(habit, row, config, start, end, strict=False)
                note = self.web._slot_override_note(start, end, ignore=ignore_ids)
            else:
                self._withdraw(row, unlink=True)
        elif action == "cancel":
            if config["mode"] != "weekly" or not row.custom:
                raise ApiError("invalid_state", self.t("这次安排不可取消", "This session cannot be cancelled"))
            self._withdraw(row, unlink=True)
            row.status = "cancelled"
        elif action == "skip":
            self._withdraw(row)
            row.status = "skipped"
        elif action == "done":
            row.prior_status = ("cancelled" if config["mode"] == "weekly" and snapshot is None else row.status)
            row.status, row.completed_at = "done", self.web.now_naive
            if event and event.status != "skipped":
                if _utc(event.start_utc) > self.now:
                    self._withdraw(row)
                else:
                    event.status, event.resolved_at = "done", self.web.now_naive
        elif action == "restore":
            row.status, row.completed_at = row.prior_status or "pending", None
            row.prior_status = None
            if event and row.status == "pending" and day >= today and self._active(habit, day):
                self._restore_slot(event)
            elif day >= today and not self._active(habit, day) and row.status == "pending":
                self._withdraw(row, unlink=True)
                row.status = "cancelled"
        row.revision += 1
        operation_id = self._save_undo(habit, row, snapshot)
        messages = {
            "done": ("已完成本次", "Habit completed"), "skip": ("已跳过本次", "Session skipped"),
            "schedule": ("习惯安排已更新", "Habit schedule updated"), "cancel": ("已取消这次安排", "Session cancelled"),
            "restore": ("已撤销完成", "Completion undone"),
        }
        return {"message": self.t(*messages[action]) + note, "operation_id": operation_id,
                "occurrence": self._occurrence(habit, rules, row.day, row)}

    def undo(self, operation, payload):
        habit = self.web._get(Habit, payload["habit_id"])
        row = self.web._get(HabitOccurrence, payload["occurrence_id"])
        self._version_check(self._version(habit, row.day, row), payload["version"])
        before = payload["before"]
        if before:
            day = self._date(before["date"])
            if day != row.day and self._records(habit, day, day).get(day):
                raise ApiError("undo_conflict", self.t("原日期已有记录，无法撤销", "The original date is already in use"))
            old_event = before["event"]
            event = self._event(row)
            if event and (not old_event or event.id != old_event["id"]):
                self._withdraw(row, unlink=True)
            if old_event:
                event = self.web._get(Event, old_event["id"])
                start = parse_datetime(old_event["start"], lang=self.web.lang)
                end = parse_datetime(old_event["end"], lang=self.web.lang)
                if old_event["status"] == "planned" and start >= self.now:
                    self.web._slot(start, end, ignore=(event.id,), goal_block=False, habit_block=True)
                event.start_utc, event.end_utc = _naive(start), _naive(end)
                event.status = old_event["status"]
                event.resolved_at = _naive(parse_datetime(old_event["resolved_at"])) if old_event["resolved_at"] else None
                row.event_id = event.id
            row.day, row.status, row.prior_status = day, before["status"], before["prior_status"]
            row.reason, row.custom = before["reason"], before["custom"]
            row.completed_at = _naive(parse_datetime(before["completed_at"])) if before["completed_at"] else None
        else:
            self._withdraw(row, unlink=True)
            row.status, row.reason, row.custom, row.prior_status, row.completed_at = "cancelled", None, False, None, None
        row.revision += 1
        operation.undone = True
        self.session.flush()
        return {"message": self.t("已撤销操作", "Undone")}
