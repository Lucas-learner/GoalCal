"""Deterministic multi-goal global weekly balanced planner (Phase 1, no LLM)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

from ..i18n import pick
from . import scheduler
from .scheduler import BusyBlock, Candidate, Preferences, Requirement

MAX_STEPS_PER_GOAL_PER_WEEK = 5


@dataclass(frozen=True)
class WeeklyStepInput:
    step_id: int
    seq: int
    title: str
    est_min: int
    energy: int = 1
    kind: str = "deep"
    milestone: str = ""
    replaces_event_id: int | None = None


@dataclass(frozen=True)
class WeeklyGoalInput:
    goal_id: int
    title: str
    pinned: bool = False
    deadline: date | None = None
    total_todo_minutes: int = 0
    recent_activity: bool = False
    existing_window_minutes: int = 0
    steps: tuple[WeeklyStepInput, ...] = ()


@dataclass(frozen=True)
class PlannedBlock:
    goal_id: int
    goal_title: str
    step_id: int
    step_title: str
    milestone: str
    est_min: int
    start: datetime
    end: datetime
    score: float
    reasons: list[str]
    replaces_event_id: int | None = None


@dataclass(frozen=True)
class UnscheduledStep:
    goal_id: int
    goal_title: str
    step_id: int
    step_title: str
    est_min: int
    reason_code: str  # past_deadline|daily_limit_reached|step_too_long|no_open_slot
    reason: str


@dataclass(frozen=True)
class GoalPlanSummary:
    goal_id: int
    goal_title: str
    pinned: bool
    deadline: date | None
    weight: float
    existing_minutes: int
    planned_minutes: int
    planned_count: int
    total_todo_minutes: int


@dataclass
class WeeklyPlanResult:
    scope: str
    week_start: date
    week_end: date
    window_start: datetime
    window_end: datetime
    strategy: str
    keep_existing_blocks: bool
    items: list[PlannedBlock] = field(default_factory=list)
    unscheduled: list[UnscheduledStep] = field(default_factory=list)
    goal_summaries: list[GoalPlanSummary] = field(default_factory=list)
    capacity_free_minutes: int = 0
    capacity_weekly_limit_minutes: int = 0
    capacity_existing_minutes: int = 0
    capacity_planned_minutes: int = 0


def resolve_week_window(now_local: datetime, scope: str = "auto") -> tuple[str, date, date, datetime, datetime]:
    """Resolve the planning horizon (Monday-Sunday) in local time.

    When scope == "auto" and today is Sunday (weekday() == 6), automatically
    switches to next week (next Monday 00:00 -> next Sunday 24:00).
    """
    if now_local.tzinfo is None:
        raise ValueError("now_local must be timezone-aware")
    today = now_local.date()
    this_monday = today - timedelta(days=today.weekday())
    resolved_scope = scope
    if scope == "auto":
        resolved_scope = "next_week" if today.weekday() == 6 else "this_week"
    if resolved_scope == "next_week":
        week_start = this_monday + timedelta(days=7)
    elif resolved_scope == "this_week":
        week_start = this_monday
    else:
        raise ValueError(f"Unsupported weekly plan scope: {scope}")
    week_end = week_start + timedelta(days=6)
    raw_start = datetime.combine(week_start, time(), tzinfo=now_local.tzinfo)
    window_start = max(now_local, raw_start)
    window_end = datetime.combine(week_end + timedelta(days=1), time(), tzinfo=now_local.tzinfo)
    return resolved_scope, week_start, week_end, window_start, window_end


def _day_goal_minutes(day: date, busy: list[BusyBlock], tz) -> int:
    day_start = datetime.combine(day, time(), tzinfo=tz).astimezone(timezone.utc)
    day_end = datetime.combine(day + timedelta(days=1), time(), tzinfo=tz).astimezone(timezone.utc)
    total = 0.0
    for b in busy:
        if b.kind != "block":
            continue
        b_start = b.start.astimezone(timezone.utc)
        b_end = b.end.astimezone(timezone.utc)
        if b_end > day_start and b_start < day_end:
            total += max(0.0, (min(b_end, day_end) - max(b_start, day_start)).total_seconds() / 60.0)
    return int(round(total))


def _window_free_capacities(
    window_start: datetime,
    window_end: datetime,
    busy: list[BusyBlock],
    prefs: Preferences,
) -> tuple[dict[date, int], int, int]:
    """Compute per-day remaining schedulable minutes (respecting daily limit & availability)."""
    tz = window_start.tzinfo
    first_day = window_start.date()
    last_day = (window_end - timedelta(microseconds=1)).date()
    per_day_cap: dict[date, int] = {}
    max_open_gap = 0
    existing_minutes = 0

    avail_cfg = prefs.web_availability
    if avail_cfg is not None:
        from ..web.availability import intervals as avail_intervals
    else:
        avail_intervals = None

    day = first_day
    while day <= last_day:
        d_start = max(window_start, datetime.combine(day, time(), tzinfo=tz))
        d_end = min(window_end, datetime.combine(day + timedelta(days=1), time(), tzinfo=tz))
        if d_start < d_end:
            used_goal = _day_goal_minutes(day, busy, tz)
            existing_minutes += used_goal
            rem_limit = max(0, prefs.max_goal_minutes_per_day - used_goal)

            day_busy_utc = [BusyBlock(b.start.astimezone(timezone.utc), b.end.astimezone(timezone.utc), b.kind) for b in busy]
            gaps = scheduler.free_intervals(
                day_busy_utc,
                d_start.astimezone(timezone.utc),
                d_end.astimezone(timezone.utc),
                buffer_minutes=prefs.buffer_minutes,
                min_minutes=5,
            )
            free_avail_min = 0
            if avail_cfg is not None and avail_intervals is not None:
                a_spans = avail_intervals(d_start, avail_cfg["available_windows"])
                p_spans = avail_intervals(d_start, avail_cfg["protected_windows"])
                for g in gaps:
                    gs = g.start.astimezone(tz)
                    ge = g.end.astimezone(tz)
                    for a0, a1 in a_spans:
                        s0, e0 = max(gs, a0), min(ge, a1)
                        if e0 <= s0:
                            continue
                        mins = int((e0.astimezone(timezone.utc) - s0.astimezone(timezone.utc)).total_seconds() // 60)
                        prot_overlap = 0
                        for p0, p1 in p_spans:
                            ov = min(e0, p1) - max(s0, p0)
                            if ov.total_seconds() > 0:
                                prot_overlap += int(ov.total_seconds() // 60)
                        usable = max(0, mins - prot_overlap)
                        free_avail_min += usable
                        if usable > max_open_gap:
                            max_open_gap = usable
            else:
                for g in gaps:
                    mins = g.minutes
                    free_avail_min += mins
                    if mins > max_open_gap:
                        max_open_gap = mins
            per_day_cap[day] = min(rem_limit, free_avail_min)
        else:
            per_day_cap[day] = 0
        day += timedelta(days=1)

    return per_day_cap, max_open_gap, existing_minutes


def _goal_weight(
    goal: WeeklyGoalInput,
    today: date,
    prefs: Preferences,
    *,
    strategy: str,
) -> float:
    weight = 1.0
    if strategy == "pinned":
        weight *= 2.4 if goal.pinned else 0.8
    elif strategy == "deadline":
        weight *= 1.2 if goal.pinned else 1.0
    else:
        weight *= 1.45 if goal.pinned else 1.0

    if goal.deadline is not None:
        days_left = max(1, (goal.deadline - today).days)
        daily_need = max(0, goal.total_todo_minutes) / days_left
        baseline = max(30.0, prefs.max_goal_minutes_per_day * 0.5)
        workload_ratio = min(2.0, daily_need / baseline)
        if (goal.deadline - today).days <= 7:
            prox = 0.6
        elif (goal.deadline - today).days <= 14:
            prox = 0.3
        else:
            prox = 0.1
        deadline_bonus = (prox + 0.5 * workload_ratio) * (1.8 if strategy == "deadline" else 1.0)
        weight += deadline_bonus

    if not goal.recent_activity and goal.existing_window_minutes == 0:
        weight *= 1.25
    return round(weight, 4)


def _diagnose_unscheduled(
    goal: WeeklyGoalInput,
    step: WeeklyStepInput,
    window_start: datetime,
    window_end: datetime,
    busy: list[BusyBlock],
    prefs: Preferences,
    *,
    lang: str,
) -> tuple[str, str]:
    tz = window_start.tzinfo
    if goal.deadline is not None:
        deadline_end = datetime.combine(goal.deadline + timedelta(days=1), time(), tzinfo=tz)
        if deadline_end <= window_start:
            return (
                "past_deadline",
                pick(lang, "目标截止日期早于当前排期窗口", "Goal deadline is earlier than this planning window"),
            )

    day = window_start.date()
    last_day = (window_end - timedelta(microseconds=1)).date()
    any_day_room = False
    while day <= last_day:
        if prefs.max_goal_minutes_per_day - _day_goal_minutes(day, busy, tz) >= step.est_min:
            any_day_room = True
            break
        day += timedelta(days=1)
    if not any_day_room:
        return (
            "daily_limit_reached",
            pick(lang, "本周每日任务时长已达上限", "Daily task limits leave no room for this step"),
        )

    per_day_cap, max_open_gap, _ = _window_free_capacities(window_start, window_end, busy, prefs)
    total_rem = sum(per_day_cap.values())
    if step.est_min > max_open_gap and max_open_gap > 0:
        return (
            "step_too_long",
            pick(
                lang,
                f"步骤时长（{step.est_min} 分钟）大于剩余连续空档，可先拆小后再排",
                f"Step duration ({step.est_min} min) exceeds remaining continuous open gaps; consider splitting it",
            ),
        )

    if total_rem < step.est_min:
        return (
            "daily_limit_reached",
            pick(lang, "本周每日任务时长已达上限", "Daily task limits leave no room for this step"),
        )

    if goal.deadline is not None:
        return (
            "no_open_slot",
            pick(lang, "目标截止前已无足够连续空档", "No continuous open slot available before the goal deadline"),
        )
    return (
        "no_open_slot",
        pick(lang, "本周剩余可用时段暂无足够连续空档", "No continuous open slot remaining within this week's available hours"),
    )


def plan_week(
    goals: list[WeeklyGoalInput],
    now_local: datetime,
    window: tuple[datetime, datetime],
    busy: list[BusyBlock],
    prefs: Preferences,
    *,
    scope: str,
    week_start: date,
    week_end: date,
    strategy: str = "balanced",
    keep_existing_blocks: bool = True,
    lang: str = "zh",
) -> WeeklyPlanResult:
    """Compute a deterministic multi-goal weekly balanced schedule."""
    window_start, window_end = window
    tz = now_local.tzinfo
    today = now_local.date()

    working_busy = list(busy)
    per_day_cap, _, existing_minutes = _window_free_capacities(window_start, window_end, working_busy, prefs)
    free_capacity = sum(per_day_cap.values())
    days_in_window = max(1, (window_end.date() - window_start.date()).days)
    weekly_limit_minutes = days_in_window * prefs.max_goal_minutes_per_day

    weights = {g.goal_id: _goal_weight(g, today, prefs, strategy=strategy) for g in goals}
    eligible_goals = [g for g in goals if g.steps]

    # Step 1: Allocate step waves via weighted proportional quota + round-robin.
    total_weight = sum(weights[g.goal_id] for g in eligible_goals) or 1.0
    total_existing_selected = sum(g.existing_window_minutes for g in eligible_goals)
    total_budget = free_capacity + total_existing_selected

    goal_quota_steps: dict[int, list[WeeklyStepInput]] = {}
    for g in eligible_goals:
        share_min = max(0.0, total_budget * (weights[g.goal_id] / total_weight) - g.existing_window_minutes)
        ordered = sorted(g.steps, key=lambda s: s.seq)
        chosen: list[WeeklyStepInput] = []
        acc = 0
        for s in ordered:
            if len(chosen) >= MAX_STEPS_PER_GOAL_PER_WEEK:
                break
            if not chosen:
                if strategy == "balanced" and g.existing_window_minutes == 0:
                    chosen.append(s)
                    acc += s.est_min
                    continue
                if share_min <= 0 and free_capacity > 0:
                    chosen.append(s)
                    acc += s.est_min
                    continue
            if acc + s.est_min <= share_min * 1.15 or (not chosen and free_capacity > 0):
                chosen.append(s)
                acc += s.est_min
            else:
                break
        if chosen:
            goal_quota_steps[g.goal_id] = chosen

    # If capacity remains unused, expand in round-robin order up to MAX_STEPS_PER_GOAL_PER_WEEK
    selected_minutes_sum = sum(s.est_min for steps in goal_quota_steps.values() for s in steps)
    target_cap = int(free_capacity * 1.25)
    if selected_minutes_sum < target_cap:
        progressed = True
        while progressed and selected_minutes_sum < target_cap:
            progressed = False
            for g in sorted(eligible_goals, key=lambda x: (-weights[x.goal_id], x.goal_id)):
                cur = goal_quota_steps.setdefault(g.goal_id, [])
                ordered = sorted(g.steps, key=lambda s: s.seq)
                if len(cur) < min(MAX_STEPS_PER_GOAL_PER_WEEK, len(ordered)):
                    nxt = ordered[len(cur)]
                    if selected_minutes_sum + nxt.est_min <= max(target_cap, nxt.est_min):
                        cur.append(nxt)
                        selected_minutes_sum += nxt.est_min
                        progressed = True
                        if selected_minutes_sum >= target_cap:
                            break

    # Build waves: wave k takes the k-th step of each goal.
    max_waves = max((len(v) for v in goal_quota_steps.values()), default=0)
    goal_by_id = {g.goal_id: g for g in goals}

    day_goal_minutes: dict[date, int] = {}
    day_cur = window_start.date()
    last_day = (window_end - timedelta(microseconds=1)).date()
    while day_cur <= last_day:
        day_goal_minutes[day_cur] = _day_goal_minutes(day_cur, working_busy, tz)
        day_cur += timedelta(days=1)

    goal_day_counts: dict[tuple[int, date], int] = {}
    goal_last_end: dict[int, datetime] = {}
    goal_last_day: dict[int, date] = {}
    stopped_goals: set[int] = set()

    planned: list[PlannedBlock] = []
    unscheduled: list[UnscheduledStep] = []

    for wave_idx in range(max_waves):
        wave_entries: list[tuple[WeeklyGoalInput, WeeklyStepInput]] = []
        for gid, steps in goal_quota_steps.items():
            if gid in stopped_goals or wave_idx >= len(steps):
                continue
            wave_entries.append((goal_by_id[gid], steps[wave_idx]))

        wave_entries.sort(key=lambda pair: (-pair[1].energy, -weights[pair[0].goal_id], pair[0].goal_id))

        for g, step in wave_entries:
            step_win_start = max(window_start, goal_last_end.get(g.goal_id, window_start))
            step_win_end = window_end
            if g.deadline is not None:
                dl_end = datetime.combine(g.deadline + timedelta(days=1), time(), tzinfo=tz)
                step_win_end = min(step_win_end, dl_end)

            candidates: list[Candidate] = []
            if step_win_end > step_win_start:
                req = Requirement(
                    est_min=step.est_min,
                    energy=step.energy,
                    kind=step.kind,
                    min_chunk=min(20, step.est_min),
                )
                candidates = scheduler.generate_candidates(
                    req,
                    now_local,
                    working_busy,
                    prefs=prefs,
                    deadline=g.deadline,
                    window=(step_win_start, step_win_end),
                    lang=lang,
                )

            if not candidates:
                code, reason = _diagnose_unscheduled(
                    g, step, step_win_start, step_win_end if step_win_end > step_win_start else window_end,
                    working_busy, prefs, lang=lang,
                )
                unscheduled.append(
                    UnscheduledStep(
                        goal_id=g.goal_id,
                        goal_title=g.title,
                        step_id=step.step_id,
                        step_title=step.title,
                        est_min=step.est_min,
                        reason_code=code,
                        reason=reason,
                    )
                )
                stopped_goals.add(g.goal_id)
                continue

            def _adjusted_score(cand: Candidate) -> tuple[float, datetime]:
                c_day = cand.start.date()
                days_from_now = max(0, (c_day - today).days)
                adj = cand.score
                # Front-load earlier days so current batches finish sooner and unlock rolling steps
                adj -= 0.16 * min(days_from_now, 6)
                # Gentle tie-breaker so multiple goals interleave naturally within a day
                same_goal_cnt = goal_day_counts.get((g.goal_id, c_day), 0)
                adj -= 0.03 * same_goal_cnt
                return (-round(adj, 6), cand.start)

            best = min(candidates, key=_adjusted_score)
            best_adj_score = max(0.0, min(1.0, -_adjusted_score(best)[0]))
            chosen_day = best.start.date()

            reasons = list(best.reasons)

            planned.append(
                PlannedBlock(
                    goal_id=g.goal_id,
                    goal_title=g.title,
                    step_id=step.step_id,
                    step_title=step.title,
                    milestone=step.milestone,
                    est_min=step.est_min,
                    start=best.start,
                    end=best.end,
                    score=round(best_adj_score, 3),
                    reasons=reasons,
                    replaces_event_id=step.replaces_event_id,
                )
            )
            working_busy.append(BusyBlock(best.start, best.end, "block"))
            day_goal_minutes[chosen_day] = day_goal_minutes.get(chosen_day, 0) + step.est_min
            goal_day_counts[(g.goal_id, chosen_day)] = goal_day_counts.get((g.goal_id, chosen_day), 0) + 1
            goal_last_end[g.goal_id] = best.end
            goal_last_day[g.goal_id] = chosen_day

    planned.sort(key=lambda item: (item.start, item.goal_id, item.step_id))

    planned_by_goal: dict[int, list[PlannedBlock]] = {}
    for item in planned:
        planned_by_goal.setdefault(item.goal_id, []).append(item)

    summaries: list[GoalPlanSummary] = []
    for g in sorted(goals, key=lambda x: (-weights[x.goal_id], x.goal_id)):
        g_items = planned_by_goal.get(g.goal_id, [])
        summaries.append(
            GoalPlanSummary(
                goal_id=g.goal_id,
                goal_title=g.title,
                pinned=g.pinned,
                deadline=g.deadline,
                weight=weights[g.goal_id],
                existing_minutes=g.existing_window_minutes,
                planned_minutes=sum(i.est_min for i in g_items),
                planned_count=len(g_items),
                total_todo_minutes=g.total_todo_minutes,
            )
        )

    return WeeklyPlanResult(
        scope=resolved_scope if scope == "auto" else scope,
        week_start=week_start,
        week_end=week_end,
        window_start=window_start,
        window_end=window_end,
        strategy=strategy,
        keep_existing_blocks=keep_existing_blocks,
        items=planned,
        unscheduled=unscheduled,
        goal_summaries=summaries,
        capacity_free_minutes=free_capacity,
        capacity_weekly_limit_minutes=weekly_limit_minutes,
        capacity_existing_minutes=existing_minutes,
        capacity_planned_minutes=sum(i.est_min for i in planned),
    )
