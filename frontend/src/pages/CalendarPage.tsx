import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import FullCalendar from '@fullcalendar/react';
import type {
  DateSelectArg,
  DatesSetArg,
  EventChangeArg,
  EventClickArg,
  EventContentArg,
  EventInput,
} from '@fullcalendar/core';
import zhCnLocale from '@fullcalendar/core/locales/zh-cn';
import dayGridPlugin from '@fullcalendar/daygrid';
import interactionPlugin, {
  Draggable,
  type DateClickArg,
  type EventReceiveArg,
} from '@fullcalendar/interaction';
import luxonPlugin from '@fullcalendar/luxon3';
import timeGridPlugin from '@fullcalendar/timegrid';
import { DateTime } from 'luxon';
import { api } from '../api/client';
import { invalidateAfterWrite, queryKeys } from '../api/queryKeys';
import type { CalendarEvent, HabitActionBody, HabitOccurrence, HabitProgress } from '../api/types';
import { EventDialog } from '../components/EventDialog';
import { HabitForm } from '../components/HabitForm';
import { HabitOccurrenceDialog, useHabitAction, useHabitLabels } from '../components/HabitOccurrenceDialog';
import { SparkleIcon } from '../components/Icons';
import { NewEventDialog } from '../components/NewEventDialog';
import { EmptyState, ErrorBanner } from '../components/StateFeedback';
import {
  calendarStrToUtcIso,
  durationMinutes,
  formatTimeRange,
  getRangeBoundsUtc,
  todayIsoDateInZone,
} from '../lib/datetime';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useI18n } from '../i18n';
import { useAppState } from '../state/AppStateContext';

const STATUS_BADGE: Record<string, [string, string]> = {
  moved: ['已调整', 'Moved'],
  done: ['已完成', 'Done'],
  partial: ['部分完成', 'Partial'],
  missed: ['未完成', 'Missed'],
};

function readMediaMatch(query: string, fallbackWidthCheck?: () => boolean): boolean {
  if (typeof window === 'undefined') return false;
  if (typeof window.matchMedia === 'function') {
    return window.matchMedia(query).matches;
  }
  return fallbackWidthCheck ? fallbackWidthCheck() : false;
}

function useMediaQuery(query: string, fallbackWidthCheck?: () => boolean): boolean {
  const [matches, setMatches] = useState(() => readMediaMatch(query, fallbackWidthCheck));

  useEffect(() => {
    if (typeof window === 'undefined') return;
    if (typeof window.matchMedia === 'function') {
      const mql = window.matchMedia(query);
      setMatches(mql.matches);
      const onChange = (e: MediaQueryListEvent) => setMatches(e.matches);
      mql.addEventListener('change', onChange);
      return () => mql.removeEventListener('change', onChange);
    }
    if (!fallbackWidthCheck) return;
    const onResize = () => setMatches(fallbackWidthCheck());
    onResize();
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query]);

  return matches;
}

export function CalendarPage({ timezone }: { timezone: string }) {
  const queryClient = useQueryClient();
  const { openAssistant, openProposalModal, recordMutationResult } = useAppState();
  const { lang, t } = useI18n();
  const calendarRef = useRef<FullCalendar | null>(null);
  const backlogRef = useRef<HTMLElement | null>(null);

  const isMobile = useMediaQuery('(max-width: 720px)', () => window.innerWidth <= 720);
  const isSingleColumn = useMediaQuery('(max-width: 960px)', () => window.innerWidth <= 960);
  const isCoarsePointer = useMediaQuery('(pointer: coarse)');
  const enableDrag = !isMobile && !isCoarsePointer;

  const [viewMode, setViewMode] = useState<'week' | 'day'>(() =>
    readMediaMatch('(max-width: 720px)', () => window.innerWidth <= 720) ? 'day' : 'week',
  );
  const [anchorDate, setAnchorDate] = useState(() => todayIsoDateInZone(timezone));
  const [backlogOpen, setBacklogOpen] = useState(false);

  const initialBounds = useMemo(
    () => getRangeBoundsUtc(anchorDate, viewMode, timezone),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  const [range, setRange] = useState(() => ({
    startUtc: initialBounds.startUtc,
    endUtc: initialBounds.endUtc,
  }));

  const [selectedEvent, setSelectedEvent] = useState<CalendarEvent | null>(null);
  const [selectedHabit, setSelectedHabit] = useState<{ item: HabitOccurrence; edit?: boolean } | null>(null);
  const [selectedWeek, setSelectedWeek] = useState<HabitProgress | null>(null);
  const [newHabit, setNewHabit] = useState<{ date: string; time: string; minutes: number } | null>(null);
  const habitLabels = useHabitLabels();
  const [newEventRange, setNewEventRange] = useState<{
    startLocal: string;
    endLocal: string;
  } | null>(null);

  const eventsQuery = useQuery({
    queryKey: queryKeys.calendarRange(range.startUtc, range.endUtc),
    queryFn: () => api.getCalendarEvents(range.startUtc, range.endUtc),
  });

  const availabilityQuery = useQuery({
    queryKey: queryKeys.availability,
    queryFn: api.getAvailability,
  });

  const goalsQuery = useQuery({
    queryKey: queryKeys.goals,
    queryFn: api.getGoals,
  });

  const habitsQuery = useQuery({
    queryKey: queryKeys.habits,
    queryFn: api.getHabits,
  });

  const habitAction = useHabitAction();
  const habitItems = eventsQuery.data?.habits?.items ?? [];
  const habitWeeks = eventsQuery.data?.habits?.weeks ?? [];
  const activeHabits = useMemo(
    () => (habitsQuery.data?.habits ?? []).filter((habit) => habit.status === 'active'),
    [habitsQuery.data?.habits],
  );
  const backlogHabits = useMemo(() => {
    const items: HabitOccurrence[] = [];
    for (const habit of activeHabits) {
      if (habit.next_unscheduled) items.push(habit.next_unscheduled);
    }
    return items.sort((a, b) => a.date.localeCompare(b.date) || a.habit_id - b.habit_id);
  }, [activeHabits]);
  const backlogHabitWeeks = useMemo(() => {
    const weeks: HabitProgress[] = [];
    for (const habit of activeHabits) {
      if (!habit.next_unscheduled && habit.next_week) weeks.push(habit.next_week);
    }
    return weeks.sort((a, b) => a.week.localeCompare(b.week) || a.habit_id - b.habit_id);
  }, [activeHabits]);
  const backlogCount = backlogHabits.length + backlogHabitWeeks.length;

  const moveFn = useCallback(
    async (
      args: { event: CalendarEvent; startUtc: string; endUtc: string; revert: () => void },
      key: string,
    ) => {
      if (args.event.habit) {
        const item = args.event.habit;
        const targetDate = DateTime.fromISO(args.startUtc).setZone(item.timezone).toISODate()!;
        return api.habitAction(item.habit_id, { version: item.version, action: 'schedule', date: item.date,
          target_date: targetDate, start: args.startUtc, minutes: Math.round(durationMinutes(args.startUtc, args.endUtc)) }, key);
      }
      return api.moveCalendarEvent(
        args.event.id,
        { version: args.event.version, start: args.startUtc, end: args.endUtc },
        key,
      );
    },
    [],
  );

  const moveAction = useIdempotentAction(moveFn, {
    onSuccess: async (res, args) => {
      recordMutationResult(res);
      await invalidateAfterWrite(queryClient, { goalId: args.event.goal_id });
    },
    onError: async (err, args) => {
      args.revert();
      if (err.status === 409) {
        await invalidateAfterWrite(queryClient, { goalId: args.event.goal_id });
      }
    },
  });

  const proposeFn = useCallback(
    async (stepId: number, key: string) => api.createStepProposals(stepId, {}, key),
    [],
  );

  const proposeAction = useIdempotentAction(proposeFn, {
    onSuccess: async (proposal) => {
      await invalidateAfterWrite(queryClient, { goalId: proposal.goal_id });
      openProposalModal(proposal);
    },
  });

  const scheduleStepFn = useCallback(
    async (
      args: {
        stepId: number;
        version: string;
        goalId: number;
        startUtc: string;
        removeTempEvent: () => void;
      },
      key: string,
    ) => api.scheduleStep(args.stepId, { version: args.version, start: args.startUtc }, key),
    [],
  );

  const scheduleStepAction = useIdempotentAction(scheduleStepFn, {
    onSuccess: async (res, args) => {
      recordMutationResult(res);
      await invalidateAfterWrite(queryClient, { goalId: args.goalId });
      args.removeTempEvent();
    },
    onError: async (err, args) => {
      args.removeTempEvent();
      if (err.status === 409) {
        await invalidateAfterWrite(queryClient, { goalId: args.goalId });
      }
    },
  });

  useEffect(() => {
    const containerEl = backlogRef.current;
    if (!enableDrag || !containerEl) return;
    const draggable = new Draggable(containerEl, {
      itemSelector: '.backlog-item',
      minDistance: 5,
      eventData: (el) => {
        const estMin = Math.max(5, Number(el.dataset.estMin) || 30);
        return {
          title: el.dataset.title || t('待办步骤', 'Todo step'),
          duration: { minutes: estMin },
          create: true,
          classNames: ['gc-ev-block'],
          extendedProps: {
            backlogStepId: Number(el.dataset.stepId),
            backlogStepVersion: el.dataset.stepVersion ?? '',
            backlogGoalId: Number(el.dataset.goalId),
            backlogHabitId: el.dataset.habitId,
          },
        };
      },
    });
    return () => draggable.destroy();
  }, [enableDrag, t]);

  const visibleEvents = useMemo(
    () => (eventsQuery.data?.events ?? []).filter((ev) => ev.status !== 'skipped' || ev.habit),
    [eventsQuery.data?.events],
  );

  const fcEvents: EventInput[] = useMemo(() => {
    const normalEvents: EventInput[] = visibleEvents.map((ev) => {
      const classes = [`gc-ev-${ev.kind}`];
      if (ev.status === 'done' || ev.status === 'skipped') classes.push('gc-ev-done');
      return {
        id: String(ev.id),
        title: ev.title,
        start: ev.start,
        end: ev.end,
        editable: enableDrag && ['planned', 'moved'].includes(ev.status),
        classNames: classes,
        extendedProps: { rawEvent: ev },
      };
    });

    const protectedBg: EventInput[] = (availabilityQuery.data?.protected_windows ?? []).map(
      (win, idx) => ({
        id: `protected-${idx}`,
        title: t('保护时段', 'Protected'),
        display: 'background',
        daysOfWeek: win.days.map((d) => (d + 1) % 7),
        startTime: `${win.start}:00`,
        endTime: `${win.end}:00`,
        classNames: ['gc-protected-bg'],
      }),
    );

    return [...protectedBg, ...normalEvents];
  }, [visibleEvents, availabilityQuery.data?.protected_windows, enableDrag, t]);

  const scheduledStepIds = useMemo(() => {
    const ids = new Set<number>();
    for (const ev of visibleEvents) {
      if (ev.kind === 'block' && ['planned', 'moved'].includes(ev.status) && ev.step_id != null) {
        ids.add(ev.step_id);
      }
    }
    return ids;
  }, [visibleEvents]);

  const backlogSteps = useMemo(() => {
    const items: Array<{
      stepId: number;
      stepTitle: string;
      stepVersion: string;
      estMin: number;
      goalId: number;
      goalTitle: string;
    }> = [];
    for (const goal of goalsQuery.data?.goals ?? []) {
      if (goal.status !== 'active') continue;
      for (const step of goal.steps) {
        if (step.status === 'todo' && !step.scheduled_event && !scheduledStepIds.has(step.id)) {
          items.push({
            stepId: step.id,
            stepTitle: step.title,
            stepVersion: step.version,
            estMin: step.est_min,
            goalId: goal.id,
            goalTitle: goal.title,
          });
          break;
        }
      }
    }
    return items;
  }, [goalsQuery.data?.goals, scheduledStepIds]);

  const handleDatesSet = (arg: DatesSetArg) => {
    const startUtc = calendarStrToUtcIso(arg.startStr, timezone);
    const endUtc = calendarStrToUtcIso(arg.endStr, timezone);
    const nextAnchor = DateTime.fromISO(arg.startStr, { zone: timezone }).toISODate();
    if (nextAnchor) setAnchorDate(nextAnchor);
    if (startUtc && endUtc) {
      setRange({ startUtc, endUtc });
    }
  };

  const switchView = (mode: 'week' | 'day') => {
    setViewMode(mode);
    const apiFc = calendarRef.current?.getApi();
    if (apiFc) {
      apiFc.changeView(mode === 'day' ? 'timeGridDay' : 'timeGridWeek');
    }
  };

  const handleNav = (dir: 'prev' | 'next') => {
    const apiFc = calendarRef.current?.getApi();
    if (!apiFc) return;
    if (dir === 'prev') apiFc.prev();
    else apiFc.next();
  };

  const handleDateJump = (isoDate: string) => {
    if (!isoDate) return;
    setAnchorDate(isoDate);
    calendarRef.current?.getApi().gotoDate(isoDate);
  };

  const handleNavLinkDayClick = (date: Date) => {
    const iso = DateTime.fromJSDate(date, { zone: timezone }).toISODate();
    if (!iso) return;
    setAnchorDate(iso);
    setViewMode('day');
    calendarRef.current?.getApi().changeView('timeGridDay', iso);
  };

  const openNewEventAt = (startDt: DateTime, endDt?: DateTime) => {
    const start = startDt.setZone(timezone);
    const end = (endDt ?? start.plus({ minutes: 60 })).setZone(timezone);
    setNewEventRange({
      startLocal: start.toFormat("yyyy-MM-dd'T'HH:mm"),
      endLocal: end.toFormat("yyyy-MM-dd'T'HH:mm"),
    });
  };

  const handleDateClick = (info: DateClickArg) => {
    const base = DateTime.fromISO(info.dateStr, { zone: timezone });
    if (!base.isValid) return;
    const start = info.allDay ? base.set({ hour: 9, minute: 0 }) : base;
    openNewEventAt(start);
  };

  const handleSelect = (info: DateSelectArg) => {
    const start = DateTime.fromISO(info.startStr, { zone: timezone });
    const end = DateTime.fromISO(info.endStr, { zone: timezone });
    if (!start.isValid || !end.isValid) return;
    openNewEventAt(start, end);
  };

  const handleEventClick = (info: EventClickArg) => {
    const raw = info.event.extendedProps?.rawEvent as CalendarEvent | undefined;
    if (raw?.habit) {
      setSelectedHabit({ item: raw.habit });
    } else if (raw) {
      setSelectedEvent(raw);
    }
  };

  const handleEventChange = (info: EventChangeArg) => {
    const raw = info.event.extendedProps?.rawEvent as CalendarEvent | undefined;
    if (!raw) {
      info.revert();
      return;
    }
    const startUtc = calendarStrToUtcIso(info.event.startStr, timezone);
    const endUtc = calendarStrToUtcIso(info.event.endStr, timezone);
    if (!startUtc || !endUtc || endUtc <= startUtc) {
      info.revert();
      return;
    }
    void moveAction.run({ event: raw, startUtc, endUtc, revert: info.revert });
  };

  const handleEventReceive = (info: EventReceiveArg) => {
    const props = info.event.extendedProps as {
      backlogStepId?: number;
      backlogStepVersion?: string;
      backlogGoalId?: number;
      backlogHabitId?: string;
    };
    if (props.backlogHabitId) {
      const item = backlogHabits.find((habit) => habit.id === props.backlogHabitId);
      const start = calendarStrToUtcIso(info.event.startStr, timezone);
      const targetDate = start && item ? DateTime.fromISO(start).setZone(item.timezone).toISODate() : null;
      info.revert();
      if (item && start && targetDate) {
        const body: HabitActionBody = { action: 'schedule', date: item.date, version: item.version,
          target_date: targetDate, start };
        void habitAction.run({ id: item.habit_id, body });
      }
      return;
    }
    if (
      scheduleStepAction.isPending ||
      !props.backlogStepId ||
      !props.backlogStepVersion ||
      !props.backlogGoalId
    ) {
      info.revert();
      return;
    }
    const startUtc = calendarStrToUtcIso(info.event.startStr, timezone);
    if (!startUtc) {
      info.revert();
      return;
    }
    void scheduleStepAction.run({
      stepId: props.backlogStepId,
      version: props.backlogStepVersion,
      goalId: props.backlogGoalId,
      startUtc,
      removeTempEvent: () => info.event.remove(),
    });
  };

  const renderEventContent = (arg: EventContentArg) => {
    const raw = arg.event.extendedProps?.rawEvent as CalendarEvent | undefined;
    if (!raw) {
      return <div>{arg.event.title}</div>;
    }
    const statusPair = STATUS_BADGE[raw.status];
    const statusText = raw.habit?.status === 'skipped' ? habitLabels.status.skipped : statusPair ? t(statusPair[0], statusPair[1]) : undefined;
    const timeText = formatTimeRange(raw.start, raw.end, timezone);
    return (
      <div>
        <div style={{ fontWeight: 620, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {statusText ? `[${statusText}] ` : ''}
          {raw.habit && <span aria-label={habitLabels.habit} title={habitLabels.habit}>↻ </span>}
          {raw.kind === 'buffer' ? `${t('缓冲', 'Buffer')} · ${raw.title}` : raw.title}
        </div>
        <div className="tabular-nums" style={{ opacity: 0.85, fontSize: 9 }}>
          {timeText}
        </div>
      </div>
    );
  };

  const showDragCopy = enableDrag && !isSingleColumn;
  const emptyBannerText = showDragCopy
    ? viewMode === 'day'
      ? t('当天暂无日程，点击空白处新建或从右侧拖入。', 'No events today — click a slot to add or drag from Unscheduled.')
      : t('本周暂无日程，点击空白处新建或从右侧拖入。', 'No events this week — click a slot to add or drag from Unscheduled.')
    : viewMode === 'day'
      ? t('当天暂无日程，点击空白处新建，或通过「尚未安排」找时间。', 'No events today — tap a slot to add or find time from Unscheduled.')
      : t('本周暂无日程，点击空白处新建，或通过「尚未安排」找时间。', 'No events this week — tap a slot to add or find time from Unscheduled.');

  const backlogBody = (
    <>
      <div className="backlog-copy">
        {showDragCopy
          ? t('拖入日历，或点找时间', 'Drag to calendar, or click Find time')
          : t('点击「找时间」挑推荐空档，或自选开始时间', 'Click Find time for open slots or a custom time')}
      </div>

      {goalsQuery.isLoading ? (
        <div className="muted" style={{ fontSize: 11 }}>
          {t('加载中…', 'Loading…')}
        </div>
      ) : goalsQuery.isError ? (
        <ErrorBanner error={goalsQuery.error} onRetry={() => void goalsQuery.refetch()} />
      ) : backlogSteps.length === 0 ? (
        backlogCount === 0 ? <EmptyState title={t('暂无待安排事项', 'Nothing unscheduled')} /> : null
      ) : (
        backlogSteps.map((item) => (
          <div
            key={item.stepId}
            className={`backlog-item${enableDrag ? '' : ' no-drag'}`}
            data-step-id={item.stepId}
            data-step-version={item.stepVersion}
            data-goal-id={item.goalId}
            data-est-min={item.estMin}
            data-title={item.stepTitle}
          >
            <div className="backlog-item-title">{item.stepTitle}</div>
            <div className="backlog-item-meta">
              {item.estMin} {t('分钟', 'min')} · {item.goalTitle}
            </div>
            <button
              type="button"
              className="btn small"
              disabled={proposeAction.isPending || scheduleStepAction.isPending}
              onPointerDown={(e) => e.stopPropagation()}
              onMouseDown={(e) => e.stopPropagation()}
              onClick={() => void proposeAction.run(item.stepId)}
            >
              {t('找时间 →', 'Find time →')}
            </button>
          </div>
        ))
      )}
      {backlogHabits.map((item) => (
        <div key={item.id} className={`backlog-item${enableDrag ? '' : ' no-drag'}`}
          data-habit-id={item.id} data-est-min={item.est_min} data-title={item.title}>
          <div className="backlog-item-title">{item.title}</div>
          <div className="backlog-item-meta">{habitLabels.habit} · {item.date} · {item.est_min} {t('分钟', 'min')}</div>
          {item.reason && <div className="backlog-item-meta">{habitLabels.reason(item.reason)}</div>}
          <button type="button" className="btn small" onPointerDown={(e) => e.stopPropagation()}
            onMouseDown={(e) => e.stopPropagation()} onClick={() => setSelectedHabit({ item, edit: true })}>{t('找时间 →', 'Find time →')}</button>
        </div>
      ))}
      {backlogHabitWeeks.map((week) => {
        const currentMonday = DateTime.fromISO(week.today, { zone: week.timezone }).startOf('week').toISODate();
        const weekLabel = week.week === currentMonday
          ? t(`本周 ${week.done}/${week.target} 次`, `This week ${week.done}/${week.target}`)
          : t(`${week.week.slice(5)} 周 ${week.done}/${week.target} 次`, `Week of ${week.week.slice(5)} · ${week.done}/${week.target}`);
        return (
          <div key={`${week.habit_id}:${week.week}`} className="habit-week-card">
            <strong>{week.title}</strong>
            <span className="backlog-item-meta">{weekLabel}</span>
            {week.schedule_from && (
              <button type="button" className="btn small" onClick={() => setSelectedWeek(week)}>{t('找时间 →', 'Find time →')}</button>
            )}
          </div>
        );
      })}
    </>
  );

  const backlogAside = (
    <aside
      ref={backlogRef}
      className={`card-flat backlog${isMobile ? ' backlog-mobile' : ''}${isMobile && backlogSteps.length + backlogCount > 0 ? ' has-items' : ''}`}
      aria-label={t('尚未安排的事项', 'Unscheduled items')}
    >
      {isMobile ? (
        <>
          <button
            type="button"
            className="backlog-toggle-btn"
            aria-expanded={backlogOpen}
            onClick={() => setBacklogOpen((prev) => !prev)}
          >
            <span className="backlog-toggle-left">
              <span className="backlog-title">{t('尚未安排', 'Unscheduled')}</span>
              {goalsQuery.isLoading ? (
                <span className="backlog-toggle-hint">· {t('加载中…', 'Loading…')}</span>
              ) : backlogSteps.length + backlogCount > 0 ? (
                <span className="backlog-count">{backlogSteps.length + backlogCount}</span>
              ) : (
                <span className="backlog-toggle-hint">· {t('暂无待安排步骤', 'Nothing unscheduled')}</span>
              )}
            </span>
            <span className="backlog-toggle-state">
              {backlogOpen ? t('收起 ▴', 'Hide ▴') : t('展开 ▾', 'Show ▾')}
            </span>
          </button>
          {backlogOpen ? <div className="backlog-mobile-body">{backlogBody}</div> : null}
        </>
      ) : (
        <>
          <div className="backlog-title">{t('尚未安排', 'Unscheduled')}</div>
          {backlogBody}
        </>
      )}
    </aside>
  );

  return (
    <div className="calendar-page">
      <div className="topbar">
        <h1>{t('日历', 'Calendar')}</h1>
        <div className="topbar-actions">
          <button
            type="button"
            className="assistant-trigger"
            onClick={(e) => openAssistant(e.currentTarget)}
          >
            <SparkleIcon />
            <span>{t('问 GoalCal', 'Ask GoalCal')}</span>
          </button>
          <button
            type="button"
            className="btn primary"
            onClick={() => {
              const now = DateTime.now().setZone(timezone).plus({ hours: 1 }).startOf('hour');
              openNewEventAt(now);
            }}
          >
            {t('+ 新日程', '+ New event')}
          </button>
        </div>
      </div>

      <p className="subtitle calendar-subtitle">
        {t('固定日程、目标和习惯', 'Fixed events, goals & habits')}
      </p>

      <div className="calendar-toolbar">
        <div className="calendar-date-nav">
          <button
            type="button"
            className="calendar-nav-arrow"
            aria-label={viewMode === 'day' ? t('上一天', 'Prev day') : t('上一周', 'Prev week')}
            onClick={() => handleNav('prev')}
          >
            ‹
          </button>
          <input
            id="calendar-mobile-date"
            type="date"
            aria-label={t('跳转日期', 'Jump to date')}
            className="calendar-date-input tabular-nums"
            value={anchorDate}
            onChange={(e) => handleDateJump(e.target.value)}
          />
          <button
            type="button"
            className="calendar-nav-arrow next"
            aria-label={viewMode === 'day' ? t('下一天', 'Next day') : t('下一周', 'Next week')}
            onClick={() => handleNav('next')}
          >
            ›
          </button>
        </div>

        <div className="calendar-view-seg" role="group" aria-label={t('视图切换', 'Switch view')}>
          <button
            type="button"
            className={`calendar-seg-btn${viewMode === 'week' ? ' active' : ''}`}
            aria-pressed={viewMode === 'week'}
            onClick={() => switchView('week')}
          >
            {t('周', 'Week')}
          </button>
          <button
            type="button"
            className={`calendar-seg-btn${viewMode === 'day' ? ' active' : ''}`}
            aria-pressed={viewMode === 'day'}
            onClick={() => switchView('day')}
          >
            {t('日', 'Day')}
          </button>
        </div>
      </div>

      <ErrorBanner
        error={
          eventsQuery.error ??
          moveAction.actionError ??
          scheduleStepAction.actionError ??
          proposeAction.actionError ??
          habitAction.actionError
        }
        onRetry={eventsQuery.isError ? () => void eventsQuery.refetch() : undefined}
        onRefresh={() => void invalidateAfterWrite(queryClient)}
      />

      {isMobile ? backlogAside : null}

      <div className={`calendar-layout${isMobile ? ' is-mobile' : ''}`}>
        <div>
          {!eventsQuery.isLoading && !eventsQuery.isError && visibleEvents.length === 0 ? (
            <div className="status-banner info" role="status">
              <div className="status-banner-body">{emptyBannerText}</div>
            </div>
          ) : null}

          <div className="card-flat calendar-card gc-fc">
            <FullCalendar
              ref={calendarRef}
              plugins={[timeGridPlugin, dayGridPlugin, interactionPlugin, luxonPlugin]}
              initialView={viewMode === 'day' ? 'timeGridDay' : 'timeGridWeek'}
              initialDate={anchorDate}
              timeZone={timezone}
              locale={lang === 'zh' ? zhCnLocale : 'en'}
              firstDay={1}
              headerToolbar={false}
              allDaySlot={false}
              slotMinTime="00:00:00"
              slotMaxTime="24:00:00"
              scrollTime="08:00:00"
              slotDuration="00:30:00"
              slotLabelFormat={{ hour: '2-digit', minute: '2-digit', hour12: false }}
              eventTimeFormat={{ hour: '2-digit', minute: '2-digit', hour12: false }}
              height={640}
              nowIndicator
              navLinks={viewMode === 'week'}
              navLinkDayClick={handleNavLinkDayClick}
              selectable={enableDrag}
              selectMirror={enableDrag}
              editable={enableDrag}
              droppable={enableDrag}
              eventInteractive
              events={fcEvents}
              datesSet={handleDatesSet}
              dateClick={handleDateClick}
              select={handleSelect}
              eventClick={handleEventClick}
              eventDrop={handleEventChange}
              eventResize={handleEventChange}
              eventReceive={handleEventReceive}
              eventContent={renderEventContent}
            />
          </div>

          <div className="legend" aria-label={t('日历图例', 'Calendar legend')}>
            <span className="legend-item">
              <i className="legend-swatch goal" aria-hidden="true" />
              {t('目标时间块', 'Goal block')}
            </span>
            <span className="legend-item"><span aria-hidden="true">↻</span>{habitLabels.habit}</span>
            <span className="legend-item">
              <i className="legend-swatch" aria-hidden="true" />
              {t('固定日程', 'Fixed event')}
            </span>
            <span className="legend-item">
              <i className="legend-swatch protected" aria-hidden="true" />
              {t('保护时段', 'Protected')}
            </span>
          </div>
        </div>

        {!isMobile ? backlogAside : null}
      </div>

      {(() => {
        const liveSelected = selectedEvent
          ? visibleEvents.find((e) => e.id === selectedEvent.id) ?? selectedEvent
          : null;
        return liveSelected ? (
          <EventDialog
            event={liveSelected}
            timezone={timezone}
            onClose={() => setSelectedEvent(null)}
          />
        ) : null;
      })()}

      {newEventRange ? (
        <NewEventDialog
          timezone={timezone}
          initialStartLocal={newEventRange.startLocal}
          initialEndLocal={newEventRange.endLocal}
          onClose={() => setNewEventRange(null)}
          onNewHabit={(start, end) => {
            const startDate = DateTime.fromISO(start, { zone: timezone });
            const endDate = DateTime.fromISO(end, { zone: timezone });
            const minutes = Math.round(endDate.diff(startDate, 'minutes').minutes);
            setNewEventRange(null);
            setNewHabit({ date: startDate.toISODate() || todayIsoDateInZone(timezone),
              time: startDate.isValid ? startDate.toFormat('HH:mm') : '',
              minutes: minutes >= 5 && minutes <= 480 ? minutes : 30 });
          }}
        />
      ) : null}
      {newHabit && <HabitForm timezone={timezone} initial={newHabit} onClose={() => setNewHabit(null)} />}
      {selectedHabit && <HabitOccurrenceDialog
        occurrence={[...backlogHabits, ...habitItems].find((item) => item.id === selectedHabit.item.id) ?? selectedHabit.item}
        startInEditor={selectedHabit.edit} onClose={() => setSelectedHabit(null)} />}
      {selectedWeek && <HabitOccurrenceDialog week={[...backlogHabitWeeks, ...habitWeeks].find((week) => week.habit_id === selectedWeek.habit_id && week.week === selectedWeek.week) ?? selectedWeek}
        onClose={() => setSelectedWeek(null)} />}
    </div>
  );
}
