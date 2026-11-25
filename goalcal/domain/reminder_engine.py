"""Unified proactive reminder engine: pure evaluation, send-time guard & idempotent outbox."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import html
import json
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import db, mailer, memory
from ..models import CalendarSource, Checkin, Event, Goal, NotificationOutbox, Step, User
from ..timeutil import now_utc
from ..web.models import Habit, HabitOccurrence, HabitRule
from .calendar_sync import event_blocks_scheduling
from .recovery import unresolved_blocks
from .reminders import event_deep_link, get_email_digests, public_app_base_url

UTC = timezone.utc
_LOG = logging.getLogger("goalcal.reminders")
_BACKOFF_SECONDS = (60, 300, 900, 3600)
_DEFAULT_WEEKLY_HABIT_TIME = "11:30"
_DEFAULT_GOAL_STALL_TIME = "15:00"
_STALL_DAYS = 6


def _as_utc_aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _as_utc_naive(dt: datetime) -> datetime:
    return _as_utc_aware(dt).replace(tzinfo=None)


def _zone(tz: str | None) -> ZoneInfo:
    try:
        return ZoneInfo((tz or "").strip() or "Asia/Shanghai")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _hhmm_to_minutes(hhmm: str) -> int:
    hh, mm = hhmm.split(":", 1)
    return int(hh) * 60 + int(mm)


def is_in_quiet_hours(local_dt: datetime, quiet_start: str, quiet_end: str) -> bool:
    if quiet_start == quiet_end:
        return False
    cur = local_dt.hour * 60 + local_dt.minute
    start = _hhmm_to_minutes(quiet_start)
    end = _hhmm_to_minutes(quiet_end)
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def in_trigger_window(local_dt: datetime, target_hhmm: str, *, window_minutes: int = 20) -> bool:
    cur = local_dt.hour * 60 + local_dt.minute
    target = _hhmm_to_minutes(target_hhmm)
    return 0 <= (cur - target) < window_minutes


def _user_lang(session: Session, user_id: int, now_naive: datetime) -> str:
    prefs = memory.list_preferences(session, user_id, scope="ui", now=now_naive)
    row = next((p for p in prefs if p.key == "lang"), None)
    if row is None:
        return "zh"
    try:
        val = json.loads(row.value)
        return "en" if val == "en" else "zh"
    except ValueError:
        return "zh"


def _local_day_utc_bounds(local_day: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    start_aware = datetime.combine(local_day, time.min, zone).astimezone(UTC)
    end_aware = datetime.combine(local_day + timedelta(days=1), time.min, zone).astimezone(UTC)
    return start_aware.replace(tzinfo=None), end_aware.replace(tzinfo=None)


def _sent_today_count(session: Session, user_id: int, tz: str | None, now_aware: datetime) -> int:
    zone = _zone(tz)
    local_day = now_aware.astimezone(zone).date()
    day_lo, day_hi = _local_day_utc_bounds(local_day, zone)
    return int(
        session.scalar(
            select(func.count(NotificationOutbox.id)).where(
                NotificationOutbox.user_id == user_id,
                NotificationOutbox.status == "sent",
                NotificationOutbox.kind != "test_email",
                NotificationOutbox.sent_at.is_not(None),
                NotificationOutbox.sent_at >= day_lo,
                NotificationOutbox.sent_at < day_hi,
            )
        )
        or 0
    )


def _is_user_in_blocking_external_event(session: Session, user_id: int, now_naive: datetime) -> bool:
    externals = list(
        session.scalars(
            select(Event).where(
                Event.user_id == user_id,
                Event.kind == "external",
                Event.status != "skipped",
                Event.start_utc <= now_naive,
                Event.end_utc > now_naive,
            )
        )
    )
    if not externals:
        return False
    sources_by_id = {
        s.id: s
        for s in session.scalars(select(CalendarSource).where(CalendarSource.user_id == user_id))
    }
    return any(event_blocks_scheduling(ev, sources_by_id) for ev in externals)


def _habit_active_on(habit: Habit, day: date) -> bool:
    if habit.status != "active" or day < habit.start_date:
        return False
    iso = day.isoformat()
    try:
        periods = json.loads(habit.inactive_periods or "[]")
    except ValueError:
        periods = []
    return not any(start <= iso and (end is None or iso < end) for start, end in periods)


def _habit_rule_on(session: Session, habit_id: int, day: date) -> dict | None:
    rules = list(
        session.scalars(
            select(HabitRule)
            .where(HabitRule.habit_id == habit_id, HabitRule.effective_date <= day)
            .order_by(HabitRule.effective_date.desc())
        )
    )
    if not rules:
        return None
    try:
        return json.loads(rules[0].definition)
    except ValueError:
        return None


def _today_pending_habits(session: Session, user_id: int, local_day: date) -> list[dict]:
    habits = list(
        session.scalars(
            select(Habit).where(Habit.user_id == user_id, Habit.status == "active").order_by(Habit.id)
        )
    )
    if not habits:
        return []
    occs = {
        occ.habit_id: occ
        for occ in session.scalars(
            select(HabitOccurrence).where(
                HabitOccurrence.user_id == user_id,
                HabitOccurrence.day == local_day,
            )
        )
    }
    pending: list[dict] = []
    for habit in habits:
        if not _habit_active_on(habit, local_day):
            continue
        cfg = _habit_rule_on(session, habit.id, local_day)
        if not cfg:
            continue
        occ = occs.get(habit.id)
        if occ is not None and occ.status in ("done", "skipped", "cancelled"):
            continue
        if cfg.get("mode") == "fixed" and local_day.weekday() in cfg.get("weekdays", []):
            pending.append({"habit_id": habit.id, "title": cfg.get("title") or "Habit", "event_id": occ.event_id if occ else None})
        elif cfg.get("mode") == "weekly" and occ is not None and occ.status == "pending":
            pending.append({"habit_id": habit.id, "title": cfg.get("title") or "Habit", "event_id": occ.event_id})
    return pending


def _due_weekly_habits(session: Session, user_id: int, local_day: date) -> list[dict]:
    week_monday = local_day - timedelta(days=local_day.weekday())
    week_end = week_monday + timedelta(days=6)
    remaining_days = (week_end - local_day).days + 1
    habits = list(
        session.scalars(
            select(Habit).where(Habit.user_id == user_id, Habit.status == "active").order_by(Habit.id)
        )
    )
    due: list[dict] = []
    for habit in habits:
        if not _habit_active_on(habit, local_day):
            continue
        cfg = _habit_rule_on(session, habit.id, local_day)
        if not cfg or cfg.get("mode") != "weekly":
            continue
        target = int(cfg.get("weekly_target") or 0)
        if target <= 0:
            continue
        done_count = int(
            session.scalar(
                select(func.count(HabitOccurrence.id)).where(
                    HabitOccurrence.user_id == user_id,
                    HabitOccurrence.habit_id == habit.id,
                    HabitOccurrence.day >= week_monday,
                    HabitOccurrence.day <= week_end,
                    HabitOccurrence.status == "done",
                )
            )
            or 0
        )
        remaining_count = target - done_count
        if remaining_count > 0 and remaining_days <= remaining_count + 1:
            due.append({
                "habit_id": habit.id,
                "title": cfg.get("title") or "Habit",
                "done": done_count,
                "target": target,
                "remaining_count": remaining_count,
                "remaining_days": remaining_days,
            })
    return due


def _stalled_goals(session: Session, user_id: int, now_naive: datetime) -> list[dict]:
    cutoff = now_naive - timedelta(days=_STALL_DAYS)
    goals = list(
        session.scalars(
            select(Goal).where(Goal.user_id == user_id, Goal.status == "active").order_by(Goal.pinned.desc(), Goal.id)
        )
    )
    if not goals:
        return []
    open_future_step_ids = set(
        session.scalars(
            select(Event.step_id).where(
                Event.user_id == user_id,
                Event.kind == "block",
                Event.status.in_(("planned", "moved")),
                Event.end_utc > now_naive,
                Event.step_id.is_not(None),
            )
        )
    )
    stalled: list[dict] = []
    for goal in goals:
        todo_steps = [s for s in goal.steps if s.status == "todo"]
        if not todo_steps:
            continue
        step_ids = [s.id for s in goal.steps]
        if any(sid in open_future_step_ids for sid in step_ids):
            continue
        latest_checkin = session.scalar(
            select(func.max(Checkin.ts)).where(Checkin.step_id.in_(step_ids))
        )
        completed_times = [s.completed_at for s in goal.steps if s.completed_at is not None]
        candidates = [t for t in [goal.created_at, latest_checkin, *completed_times] if t is not None]
        last_activity = max(candidates) if candidates else now_naive
        if last_activity < cutoff:
            days_idle = max(_STALL_DAYS, (now_naive - last_activity).days)
            stalled.append({
                "goal_id": goal.id,
                "title": goal.title,
                "next_step_title": todo_steps[0].title,
                "days_idle": days_idle,
            })
    return stalled


def _format_local_hhmm(start_utc: datetime, zone: ZoneInfo) -> str:
    return _as_utc_aware(start_utc).astimezone(zone).strftime("%H:%M")


def _wrap_html(title: str, intro: str, bullets: list[str], cta_label: str, cta_url: str | None, footer: str) -> str:
    items_html = "".join(f"<li style='margin:6px 0'>{html.escape(b)}</li>" for b in bullets)
    btn_html = (
        f"<p style='margin:20px 0'><a href='{html.escape(cta_url)}' "
        "style='display:inline-block;padding:10px 16px;background:#2f5d62;color:#ffffff;"
        "text-decoration:none;border-radius:6px;font-weight:600'>"
        f"{html.escape(cta_label)}</a></p>"
        if cta_url
        else ""
    )
    return (
        "<!doctype html><html><body style='font-family:-apple-system,BlinkMacSystemFont,Segoe UI,sans-serif;"
        "color:#1f2937;background:#f9fafb;padding:24px;margin:0'>"
        "<div style='max-width:560px;margin:0 auto;background:#ffffff;border:1px solid #e5e7eb;"
        "border-radius:10px;padding:24px'>"
        f"<h2 style='margin:0 0 12px;font-size:18px;color:#111827'>{html.escape(title)}</h2>"
        f"<p style='margin:0 0 14px;line-height:1.6;color:#374151'>{html.escape(intro)}</p>"
        f"{f'<ul style=\"padding-left:18px;margin:0 0 16px;color:#1f2937\">{items_html}</ul>' if bullets else ''}"
        f"{btn_html}"
        f"<p style='margin:20px 0 0;font-size:12px;color:#6b7280;border-top:1px solid #f3f4f6;padding-top:12px'>"
        f"{html.escape(footer)}</p></div></body></html>"
    )


def render_email_digest(kind: str, lang: str, payload: dict, base_url: str | None = None) -> tuple[str, str, str]:
    base = public_app_base_url(base_url)
    is_en = lang == "en"
    footer = (
        f"You can adjust or turn off email reminders anytime in GoalCal Settings → Reminders ({base}/app/settings?section=reminders)."
        if base and is_en
        else "可在 GoalCal「设置 → 提醒与通知」随时调整或关闭邮件提醒。"
        + (f" ({base}/app/settings?section=reminders)" if base else "")
    )
    if is_en and not base:
        footer = "You can adjust or turn off email reminders anytime in GoalCal Settings → Reminders."

    if kind == "evening_wrapup":
        blocks = payload.get("blocks", [])
        habits = payload.get("habits", [])
        total = len(blocks) + len(habits)
        subject = (
            f"GoalCal · Gentle wrap-up: {total} open item(s) today"
            if is_en
            else f"GoalCal · 晚间收口：{total} 项安排待确认"
        )
        intro = (
            "No pressure if today shifted — mark what moved forward or reschedule with one tap so tomorrow stays clean."
            if is_en
            else "如果今天计划有变动也没关系，花 10 秒点一下完成或顺延重排，把明天留得更清爽。"
        )
        bullets = [
            (f"[Block] {b['title']} ({b['when']})" if is_en else f"[目标块] {b['title']}（{b['when']}）")
            for b in blocks[:5]
        ] + [
            (f"[Habit] {h['title']}" if is_en else f"[习惯] {h['title']}")
            for h in habits[:4]
        ]
        if len(blocks) > 5:
            bullets.append(f"...and {len(blocks) - 5} more block(s)" if is_en else f"……另有 {len(blocks) - 5} 个目标块待处理")
        cta_label = "Open Today & Check In" if is_en else "打开「今天」完成收口"
        cta_url = f"{base}/app/today" if base else None
    elif kind == "morning_focus":
        blocks = payload.get("blocks", [])
        habits = payload.get("habits", [])
        subject = (
            f"GoalCal · Today's focus ({len(blocks)} block(s))"
            if is_en
            else f"GoalCal · 今日聚焦（{len(blocks)} 个目标块）"
        )
        intro = (
            "Here is what is lined up for today. Focus on the minimum concrete outcome for each block."
            if is_en
            else "这是今天为你排好的节奏，每次只推进最小可见产出即可。"
        )
        bullets = []
        for b in blocks[:3]:
            out = f" — {b['outcome']}" if b.get("outcome") else ""
            bullets.append(f"{b['when']} {b['title']}{out}")
        for h in habits[:3]:
            bullets.append((f"[Habit] {h['title']}" if is_en else f"[习惯] {h['title']}"))
        cta_label = "View Today's Plan" if is_en else "查看今日计划"
        cta_url = f"{base}/app/today" if base else None
    elif kind == "weekly_habit_nudge":
        items = payload.get("habits", [])
        subject = (
            f"GoalCal · Weekly habit window ({len(items)} remaining)"
            if is_en
            else f"GoalCal · 灵活习惯本周余量提醒（{len(items)} 项）"
        )
        intro = (
            "There is still comfortable time left this week to pick a slot that fits your energy."
            if is_en
            else "本周剩下的天数刚好和目标次数接近，选个精力合适的空档安排上去吧。"
        )
        bullets = [
            (
                f"{i['title']}: {i['done']}/{i['target']} done ({i['remaining_days']} day(s) left)"
                if is_en
                else f"{i['title']}：本周已完成 {i['done']}/{i['target']} 次（本周还剩 {i['remaining_days']} 天）"
            )
            for i in items
        ]
        cta_label = "Schedule Habits" if is_en else "去习惯页挑时间"
        cta_url = f"{base}/app/habits" if base else None
    elif kind == "goal_stall":
        goals = payload.get("goals", [])
        subject = (
            f"GoalCal · Ready for one small step? ({len(goals)} goal(s) paused in motion)"
            if is_en
            else f"GoalCal · 要不要排一个小步继续推进？（{len(goals)} 个目标）"
        )
        intro = (
            "These active goals haven't had a scheduled block for a few days. Pick a tiny step or adjust the plan anytime."
            if is_en
            else "这几个进行中的目标有几天没有推进了，可以挑最小一步排个 25 分钟，或者让助手重新调整节奏。"
        )
        bullets = [
            (
                f"{g['title']} ({g['days_idle']}d idle) — Next: {g['next_step_title']}"
                if is_en
                else f"{g['title']}（已 {g['days_idle']} 天未排期）— 下一步：{g['next_step_title']}"
            )
            for g in goals[:4]
        ]
        cta_label = "Open Goals" if is_en else "打开目标页"
        cta_url = f"{base}/app/goals" if base else None
    elif kind == "weekly_review":
        blocks_done = int(payload.get("blocks_done", 0))
        habits_done = int(payload.get("habits_done", 0))
        open_goals = int(payload.get("open_goals", 0))
        subject = (
            f"GoalCal · Weekly review: {blocks_done} block(s), {habits_done} habit(s) completed"
            if is_en
            else f"GoalCal · 本周复盘：完成 {blocks_done} 个目标块 · {habits_done} 次习惯"
        )
        intro = (
            "Here is what moved forward this week. Whenever you're ready, line up the top 1–2 blocks for next week."
            if is_en
            else "这是你这一周实实在在推进的积累。准备好之后，顺手为下周排一两个关键时间块吧。"
        )
        bullets = (
            [
                f"Goal blocks completed: {blocks_done}",
                f"Habit sessions completed: {habits_done}",
                f"Active goals with upcoming steps: {open_goals}",
            ]
            if is_en
            else [
                f"本周完成目标时间块：{blocks_done} 个",
                f"本周完成习惯打卡：{habits_done} 次",
                f"进行中待继续推进的目标：{open_goals} 个",
            ]
        )
        cta_label = "Plan Next Week" if is_en else "打开日历安排下周"
        cta_url = f"{base}/app/calendar" if base else None
    else:
        subject = "GoalCal · Test Reminder Email" if is_en else "GoalCal · 主动提醒测试邮件"
        intro = (
            "SMTP delivery is working properly. GoalCal can now deliver evening wrap-ups and rhythm nudges to this address."
            if is_en
            else "SMTP 邮件通道已配置成功！GoalCal 后续将按你的设置发送晚间合并收口、习惯余量与周复盘提醒。"
        )
        bullets = []
        cta_label = "Open Reminder Settings" if is_en else "返回提醒与通知设置"
        cta_url = f"{base}/app/settings?section=reminders" if base else None

    text_lines = [subject, "", intro]
    if bullets:
        text_lines.append("")
        text_lines.extend(f"- {b}" for b in bullets)
    if cta_url:
        text_lines.extend(["", f"{cta_label}: {cta_url}"])
    text_lines.extend(["", footer])
    html_body = _wrap_html(subject, intro, bullets, cta_label, cta_url, footer)
    return subject, "\n".join(text_lines), html_body


def send_test_reminder_email(*, to_address: str, lang: str, tz: str) -> str:
    subject, text_body, html_body = render_email_digest("test_email", lang, {"tz": tz})
    mailer.send_email(
        to_address=to_address,
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )
    return subject


def evaluate_user_email_reminders(
    session: Session,
    user: User,
    now_aware: datetime,
    *,
    base_url: str | None = None,
) -> list[dict]:
    """Pure planner returning notification candidate dicts due at `now_aware` for one user."""
    if user.disabled or not (user.email or "").strip():
        return []
    now_utc_aware = _as_utc_aware(now_aware)
    now_naive = now_utc_aware.replace(tzinfo=None)
    digests = get_email_digests(session, user.id, now=now_naive)
    if not digests.get("enabled"):
        return []

    zone = _zone(user.tz)
    local_dt = now_utc_aware.astimezone(zone)
    if is_in_quiet_hours(local_dt, digests["quiet_start"], digests["quiet_end"]):
        return []

    sent_today = _sent_today_count(session, user.id, user.tz, now_utc_aware)
    remaining_budget = max(0, int(digests["daily_cap"]) - sent_today)
    if remaining_budget == 0:
        return []

    local_day = local_dt.date()
    week_monday = local_day - timedelta(days=local_day.weekday())
    lang = _user_lang(session, user.id, now_naive)
    day_lo, day_hi = _local_day_utc_bounds(local_day, zone)

    candidates: list[dict] = []

    def _add(kind: str, dedupe_key: str, payload: dict) -> None:
        if len(candidates) >= remaining_budget:
            return
        subject, _, _ = render_email_digest(kind, lang, payload, base_url=base_url)
        candidates.append({
            "user_id": user.id,
            "channel": "email",
            "kind": kind,
            "dedupe_key": dedupe_key,
            "recipient": (user.email or "").strip(),
            "subject": subject,
            "payload": payload,
            "lang": lang,
        })

    # 1. Evening merged wrap-up
    if digests.get("evening_wrapup") and in_trigger_window(local_dt, digests["evening_time"]):
        recent_cutoff = now_naive - timedelta(days=7)
        blocks = [
            ev for ev in unresolved_blocks(session, user.id, now_naive) if ev.end_utc >= recent_cutoff
        ]
        pending_habits = _today_pending_habits(session, user.id, local_day)
        if blocks or pending_habits:
            payload = {
                "blocks": [
                    {
                        "event_id": ev.id,
                        "title": ev.title,
                        "when": f"{_format_local_hhmm(ev.start_utc, zone)}-{_format_local_hhmm(ev.end_utc, zone)}",
                    }
                    for ev in blocks
                ],
                "habits": pending_habits,
                "date": local_day.isoformat(),
            }
            _add("evening_wrapup", f"email:evening_wrapup:{local_day.isoformat()}", payload)

    # 2. Morning focus
    if digests.get("morning_focus") and in_trigger_window(local_dt, digests["morning_time"]):
        day_blocks = list(
            session.scalars(
                select(Event)
                .where(
                    Event.user_id == user.id,
                    Event.kind == "block",
                    Event.status.in_(("planned", "moved")),
                    Event.start_utc >= day_lo,
                    Event.start_utc < day_hi,
                )
                .order_by(Event.start_utc)
            )
        )
        pending_habits = _today_pending_habits(session, user.id, local_day)
        if day_blocks or pending_habits:
            step_ids = {b.step_id for b in day_blocks if b.step_id is not None}
            steps = (
                {s.id: s for s in session.scalars(select(Step).where(Step.id.in_(step_ids)))}
                if step_ids
                else {}
            )
            payload = {
                "blocks": [
                    {
                        "event_id": b.id,
                        "title": b.title,
                        "when": f"{_format_local_hhmm(b.start_utc, zone)}-{_format_local_hhmm(b.end_utc, zone)}",
                        "outcome": (steps[b.step_id].outcome or "").strip()[:100]
                        if b.step_id in steps
                        else "",
                    }
                    for b in day_blocks[:5]
                ],
                "habits": pending_habits,
                "date": local_day.isoformat(),
            }
            _add("morning_focus", f"email:morning_focus:{local_day.isoformat()}", payload)

    # 3. Flexible weekly habit mid-week window nudge
    if digests.get("weekly_habit_nudge") and in_trigger_window(local_dt, _DEFAULT_WEEKLY_HABIT_TIME):
        due_habits = _due_weekly_habits(session, user.id, local_day)
        if due_habits:
            _add(
                "weekly_habit_nudge",
                f"email:weekly_habit_nudge:{week_monday.isoformat()}",
                {"habits": due_habits, "week": week_monday.isoformat()},
            )

    # 4. Goal stall gentle nudge
    if digests.get("goal_stall_nudge") and in_trigger_window(local_dt, _DEFAULT_GOAL_STALL_TIME):
        stalled = _stalled_goals(session, user.id, now_naive)
        if stalled:
            _add(
                "goal_stall",
                f"email:goal_stall:{week_monday.isoformat()}",
                {"goals": stalled, "week": week_monday.isoformat()},
            )

    # 5. Sunday weekly review
    if (
        digests.get("weekly_review")
        and local_dt.weekday() == 6
        and in_trigger_window(local_dt, digests["weekly_review_time"])
    ):
        week_lo, _ = _local_day_utc_bounds(week_monday, zone)
        blocks_done = int(
            session.scalar(
                select(func.count(Event.id)).where(
                    Event.user_id == user.id,
                    Event.kind == "block",
                    Event.status == "done",
                    or_(
                        Event.resolved_at >= week_lo,
                        Event.end_utc >= week_lo,
                    ),
                )
            )
            or 0
        )
        habits_done = int(
            session.scalar(
                select(func.count(HabitOccurrence.id)).where(
                    HabitOccurrence.user_id == user.id,
                    HabitOccurrence.status == "done",
                    HabitOccurrence.day >= week_monday,
                    HabitOccurrence.day <= local_day,
                )
            )
            or 0
        )
        open_goals = int(
            session.scalar(
                select(func.count(func.distinct(Goal.id)))
                .select_from(Goal)
                .join(Step, Step.goal_id == Goal.id)
                .where(
                    Goal.user_id == user.id,
                    Goal.status == "active",
                    Step.status == "todo",
                )
            )
            or 0
        )
        _add(
            "weekly_review",
            f"email:weekly_review:{week_monday.isoformat()}",
            {
                "blocks_done": blocks_done,
                "habits_done": habits_done,
                "open_goals": open_goals,
                "week": week_monday.isoformat(),
            },
        )

    return candidates


def send_time_guard(session: Session, user: User, item: NotificationOutbox, now_aware: datetime) -> tuple[str, str]:
    """Re-verify freshness, quiet hours, daily budget & busy external events right before sending."""
    now_utc_aware = _as_utc_aware(now_aware)
    now_naive = now_utc_aware.replace(tzinfo=None)
    if user.disabled or not (user.email or "").strip():
        return "skip", "disabled_or_missing_email"
    digests = get_email_digests(session, user.id, now=now_naive)
    if not digests.get("enabled"):
        return "skip", "reminders_disabled"
    if now_naive - item.scheduled_for > timedelta(hours=6):
        return "skip", "expired"

    zone = _zone(user.tz)
    local_dt = now_utc_aware.astimezone(zone)
    if is_in_quiet_hours(local_dt, digests["quiet_start"], digests["quiet_end"]):
        return "defer", "quiet_hours"

    if _sent_today_count(session, user.id, user.tz, now_utc_aware) >= int(digests["daily_cap"]):
        return "skip", "daily_cap_reached"

    if _is_user_in_blocking_external_event(session, user.id, now_naive):
        return "defer", "external_event_busy"

    local_day = local_dt.date()
    try:
        payload = json.loads(item.payload_json or "{}")
    except ValueError:
        payload = {}

    if item.kind == "evening_wrapup":
        recent_cutoff = now_naive - timedelta(days=7)
        blocks = [ev for ev in unresolved_blocks(session, user.id, now_naive) if ev.end_utc >= recent_cutoff]
        pending_habits = _today_pending_habits(session, user.id, local_day)
        if not blocks and not pending_habits:
            return "skip", "resolved_before_send"
    elif item.kind == "weekly_habit_nudge":
        if not _due_weekly_habits(session, user.id, local_day):
            return "skip", "resolved_before_send"
    elif item.kind == "goal_stall":
        nudged_ids = {int(g["goal_id"]) for g in payload.get("goals", []) if "goal_id" in g}
        still_stalled = {g["goal_id"] for g in _stalled_goals(session, user.id, now_naive)}
        if nudged_ids and not (nudged_ids & still_stalled):
            return "skip", "resolved_before_send"

    return "send", ""


def plan_due_notifications_once(*, now: datetime | None = None, base_url: str | None = None) -> int:
    """Evaluate active users and enqueue due notifications idempotently."""
    if not mailer.smtp_capabilities()["configured"]:
        return 0
    now_aware = _as_utc_aware(now or now_utc())
    now_naive = now_aware.replace(tzinfo=None)

    session = db.new_session()
    candidates: list[dict] = []
    try:
        users = list(
            session.scalars(
                select(User).where(
                    User.disabled.is_(False),
                    User.email.is_not(None),
                    User.email != "",
                )
            )
        )
        for user in users:
            candidates.extend(evaluate_user_email_reminders(session, user, now_aware, base_url=base_url))
    finally:
        session.close()

    if not candidates:
        return 0

    inserted = 0
    with db.get_engine().connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        ws = Session(bind=connection, expire_on_commit=False, join_transaction_mode="rollback_only")
        try:
            for cand in candidates:
                exists = ws.scalar(
                    select(NotificationOutbox.id).where(
                        NotificationOutbox.user_id == cand["user_id"],
                        NotificationOutbox.dedupe_key == cand["dedupe_key"],
                    )
                )
                if exists is not None:
                    continue
                ws.add(
                    NotificationOutbox(
                        user_id=cand["user_id"],
                        channel=cand["channel"],
                        kind=cand["kind"],
                        dedupe_key=cand["dedupe_key"],
                        recipient=cand["recipient"][:255],
                        subject=cand["subject"][:240],
                        payload_json=json.dumps(
                            {"lang": cand["lang"], **cand["payload"]},
                            ensure_ascii=False,
                        ),
                        scheduled_for=now_naive,
                        attempts=0,
                        next_attempt_at=now_naive,
                        status="pending",
                        created_at=now_naive,
                        updated_at=now_naive,
                    )
                )
                inserted += 1
            ws.flush()
            connection.commit()
        except BaseException:
            connection.rollback()
            _LOG.warning("plan_notifications_commit_failed", exc_info=True)
        finally:
            ws.close()
    return inserted


def process_notification_outbox_once(
    *,
    now: datetime | None = None,
    batch_size: int = 20,
    base_url: str | None = None,
) -> int:
    """Apply send-time guards and deliver due notifications outside SQLite write locks."""
    if not mailer.smtp_capabilities()["configured"]:
        return 0
    now_aware = _as_utc_aware(now or now_utc())
    now_naive = now_aware.replace(tzinfo=None)

    decisions: list[tuple[int, str, str, dict | None]] = []
    session = db.new_session()
    try:
        rows = session.execute(
            select(NotificationOutbox, User)
            .join(User, NotificationOutbox.user_id == User.id)
            .where(
                NotificationOutbox.status == "pending",
                NotificationOutbox.next_attempt_at <= now_naive,
            )
            .order_by(NotificationOutbox.next_attempt_at, NotificationOutbox.id)
            .limit(batch_size)
        ).all()
        for item, user in rows:
            verdict, reason = send_time_guard(session, user, item, now_aware)
            if verdict != "send":
                decisions.append((item.id, verdict, reason, None))
                continue
            try:
                payload = json.loads(item.payload_json or "{}")
            except ValueError:
                payload = {}
            lang = payload.get("lang") or _user_lang(session, user.id, now_naive)
            subject, text_body, html_body = render_email_digest(
                item.kind,
                lang,
                payload,
                base_url=base_url,
            )
            decisions.append(
                (
                    item.id,
                    "send",
                    "",
                    {
                        "to": (user.email or item.recipient or "").strip(),
                        "subject": subject,
                        "text_body": text_body,
                        "html_body": html_body,
                    },
                )
            )
    finally:
        session.close()

    sent_count = 0
    for outbox_id, verdict, reason, mail_job in decisions:
        err_msg: str | None = None
        if verdict == "send" and mail_job is not None:
            try:
                mailer.send_email(
                    to_address=mail_job["to"],
                    subject=mail_job["subject"],
                    text_body=mail_job["text_body"],
                    html_body=mail_job["html_body"],
                )
            except mailer.MailerError as exc:
                err_msg = str(exc)
            except Exception as exc:
                err_msg = f"Unexpected mail error ({type(exc).__name__})"

        with db.get_engine().connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            ws = Session(bind=connection, expire_on_commit=False, join_transaction_mode="rollback_only")
            try:
                row = ws.get(NotificationOutbox, outbox_id)
                if row is None or row.status != "pending":
                    connection.rollback()
                    continue
                row.updated_at = now_naive
                if verdict == "skip":
                    row.status = "skipped"
                    row.last_error = reason[:500]
                elif verdict == "defer":
                    row.next_attempt_at = now_naive + timedelta(minutes=15)
                    row.last_error = reason[:500]
                elif err_msg is not None:
                    row.attempts += 1
                    row.last_error = err_msg[:500]
                    if row.attempts >= row.max_attempts:
                        row.status = "dead"
                    else:
                        delay = _BACKOFF_SECONDS[min(row.attempts - 1, len(_BACKOFF_SECONDS) - 1)]
                        row.next_attempt_at = now_naive + timedelta(seconds=delay)
                else:
                    row.status = "sent"
                    row.sent_at = now_naive
                    row.last_error = ""
                    sent_count += 1
                ws.flush()
                connection.commit()
            except BaseException:
                connection.rollback()
                _LOG.warning("notification_outbox_commit_failed id=%s", outbox_id, exc_info=True)
            finally:
                ws.close()
    return sent_count


__all__ = [
    "evaluate_user_email_reminders",
    "in_trigger_window",
    "is_in_quiet_hours",
    "plan_due_notifications_once",
    "process_notification_outbox_once",
    "render_email_digest",
    "send_test_reminder_email",
    "send_time_guard",
]
