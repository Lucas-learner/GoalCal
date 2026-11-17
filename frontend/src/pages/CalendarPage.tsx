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
import interactionPlugin, { type DateClickArg } from '@fullcalendar/interaction';
import luxonPlugin from '@fullcalendar/luxon3';
import timeGridPlugin from '@fullcalendar/timegrid';
import { DateTime } from 'luxon';
import { api } from '../api/client';
import { invalidateAfterWrite, queryKeys } from '../api/queryKeys';
import type { CalendarEvent } from '../api/types';
import { EventDialog } from '../components/EventDialog';
import { SparkleIcon } from '../components/Icons';
import { NewEventDialog } from '../components/NewEventDialog';
import { EmptyState, ErrorBanner } from '../components/StateFeedback';
import {
  calendarStrToUtcIso,
  formatTimeRange,
  formatWeekRangeLabel,
  getRangeBoundsUtc,
  todayIsoDateInZone,
} from '../lib/datetime';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useAppState } from '../state/AppStateContext';

const STATUS_BADGE: Record<string, string> = {
  moved: '已调整',
  done: '已完成',
  partial: '部分完成',
  missed: '未完成',
};

export function CalendarPage({ timezone }: { timezone: string }) {
  const queryClient = useQueryClient();
  const { openAssistant, openProposalModal, recordMutationResult } = useAppState();
  const calendarRef = useRef<FullCalendar | null>(null);

  const [isMobile, setIsMobile] = useState(() =>
    typeof window !== 'undefined' ? window.innerWidth <= 720 : false,
  );
  const [viewMode, setViewMode] = useState<'week' | 'day'>(() =>
    typeof window !== 'undefined' && window.innerWidth <= 720 ? 'day' : 'week',
  );
  const [anchorDate, setAnchorDate] = useState(() => todayIsoDateInZone(timezone));

  const initialBounds = useMemo(
    () => getRangeBoundsUtc(anchorDate, viewMode, timezone),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );
  const [range, setRange] = useState(initialBounds);

  const [selectedEvent, setSelectedEvent] = useState<CalendarEvent | null>(null);
  const [newEventRange, setNewEventRange] = useState<{
    startLocal: string;
    endLocal: string;
  } | null>(null);

  useEffect(() => {
    const handleResize = () => {
      const mobile = window.innerWidth <= 720;
      setIsMobile(mobile);
    };
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

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

  const moveFn = useCallback(
    async (
      args: { event: CalendarEvent; startUtc: string; endUtc: string; revert: () => void },
      key: string,
    ) =>
      api.moveCalendarEvent(
        args.event.id,
        { version: args.event.version, start: args.startUtc, end: args.endUtc },
        key,
      ),
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

  const visibleEvents = useMemo(
    () => (eventsQuery.data?.events ?? []).filter((ev) => ev.status !== 'skipped'),
    [eventsQuery.data?.events],
  );

  const fcEvents: EventInput[] = useMemo(() => {
    const normalEvents: EventInput[] = visibleEvents.map((ev) => {
      const classes = [`gc-ev-${ev.kind}`];
      if (ev.status === 'done') classes.push('gc-ev-done');
      return {
        id: String(ev.id),
        title: ev.title,
        start: ev.start,
        end: ev.end,
        editable: ['planned', 'moved'].includes(ev.status),
        classNames: classes,
        extendedProps: { rawEvent: ev },
      };
    });

    const protectedBg: EventInput[] = (availabilityQuery.data?.protected_windows ?? []).map(
      (win, idx) => ({
        id: `protected-${idx}`,
        title: '保护时段',
        display: 'background',
        daysOfWeek: win.days.map((d) => (d + 1) % 7),
        startTime: `${win.start}:00`,
        endTime: `${win.end}:00`,
        classNames: ['gc-protected-bg'],
      }),
    );

    return [...protectedBg, ...normalEvents];
  }, [visibleEvents, availabilityQuery.data?.protected_windows]);

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
            estMin: step.est_min,
            goalId: goal.id,
            goalTitle: goal.title,
          });
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
      const label = formatWeekRangeLabel(arg.startStr, arg.endStr, viewMode, timezone);
      setRange({ startUtc, endUtc, label });
    }
  };

  const switchView = (mode: 'week' | 'day') => {
    setViewMode(mode);
    const apiFc = calendarRef.current?.getApi();
    if (apiFc) {
      apiFc.changeView(mode === 'day' ? 'timeGridDay' : 'timeGridWeek');
    }
  };

  const handleNav = (dir: 'prev' | 'next' | 'today') => {
    const apiFc = calendarRef.current?.getApi();
    if (!apiFc) return;
    if (dir === 'prev') apiFc.prev();
    else if (dir === 'next') apiFc.next();
    else apiFc.today();
  };

  const handleDateJump = (isoDate: string) => {
    if (!isoDate) return;
    setAnchorDate(isoDate);
    calendarRef.current?.getApi().gotoDate(isoDate);
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
    if (raw) {
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

  const renderEventContent = (arg: EventContentArg) => {
    const raw = arg.event.extendedProps?.rawEvent as CalendarEvent | undefined;
    if (!raw) {
      return <div>{arg.event.title}</div>;
    }
    const statusText = STATUS_BADGE[raw.status];
    const timeText = formatTimeRange(raw.start, raw.end, timezone);
    return (
      <div>
        <div style={{ fontWeight: 620, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {statusText ? `[${statusText}] ` : ''}
          {raw.kind === 'buffer' ? `缓冲 · ${raw.title}` : raw.title}
        </div>
        <div className="tabular-nums" style={{ opacity: 0.85, fontSize: 9 }}>
          {timeText}
        </div>
      </div>
    );
  };

  return (
    <div className="calendar-page">
      <div className="topbar">
        <div>
          <div className="eyebrow">
            {range.label} · {timezone}
          </div>
          <h1>日历</h1>
        </div>
        <div className="calendar-controls">
          <button type="button" className="btn small" onClick={() => handleNav('prev')}>
            {viewMode === 'day' ? '上一天' : '上一周'}
          </button>
          <button type="button" className="btn small" onClick={() => handleNav('today')}>
            今天
          </button>
          <button type="button" className="btn small" onClick={() => handleNav('next')}>
            {viewMode === 'day' ? '下一天' : '下一周'}
          </button>

          {isMobile ? (
            <label className="sr-only" htmlFor="calendar-mobile-date">
              选择日期
            </label>
          ) : null}
          <input
            id="calendar-mobile-date"
            type="date"
            aria-label="跳转日期"
            className="input"
            style={{ width: 'auto', minHeight: 31, padding: '4px 8px', fontSize: 12 }}
            value={anchorDate}
            onChange={(e) => handleDateJump(e.target.value)}
          />

          <button
            type="button"
            className={`btn small${viewMode === 'week' ? ' primary' : ''}`}
            onClick={() => switchView('week')}
          >
            周
          </button>
          <button
            type="button"
            className={`btn small${viewMode === 'day' ? ' primary' : ''}`}
            onClick={() => switchView('day')}
          >
            日
          </button>

          <button
            type="button"
            className="btn primary small"
            onClick={() => {
              const now = DateTime.now().setZone(timezone).plus({ hours: 1 }).startOf('hour');
              openNewEventAt(now);
            }}
          >
            + 新日程
          </button>
          <button
            type="button"
            className="assistant-trigger"
            onClick={(e) => openAssistant(e.currentTarget)}
          >
            <SparkleIcon />
            <span>问 GoalCal</span>
          </button>
        </div>
      </div>

      <p className="subtitle">固定日程与目标时间块放在一起，但保留不同的调整自由度。</p>

      <ErrorBanner
        error={
          eventsQuery.error ?? moveAction.actionError ?? proposeAction.actionError
        }
        onRetry={eventsQuery.isError ? () => void eventsQuery.refetch() : undefined}
        onRefresh={() => void invalidateAfterWrite(queryClient)}
      />

      <div className="calendar-layout">
        <div>
          {!eventsQuery.isLoading && !eventsQuery.isError && visibleEvents.length === 0 ? (
            <div className="status-banner info" role="status">
              <div className="status-banner-body">
                {viewMode === 'day' ? '这一天还没有安排。' : '这一周还没有日程安排。'}
                点击空白时段可新建日程，或从待安排列表中找时间。
              </div>
            </div>
          ) : null}

          <div className="card-flat calendar-card gc-fc">
            <FullCalendar
              ref={calendarRef}
              plugins={[timeGridPlugin, dayGridPlugin, interactionPlugin, luxonPlugin]}
              initialView={viewMode === 'day' ? 'timeGridDay' : 'timeGridWeek'}
              initialDate={anchorDate}
              timeZone={timezone}
              locale={zhCnLocale}
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
              selectable
              selectMirror
              editable
              eventInteractive
              events={fcEvents}
              datesSet={handleDatesSet}
              dateClick={handleDateClick}
              select={handleSelect}
              eventClick={handleEventClick}
              eventDrop={handleEventChange}
              eventResize={handleEventChange}
              eventContent={renderEventContent}
            />
          </div>

          <div className="legend" aria-label="日历图例">
            <span className="legend-item">
              <i className="legend-swatch goal" aria-hidden="true" />
              目标时间块
            </span>
            <span className="legend-item">
              <i className="legend-swatch" aria-hidden="true" />
              固定日程
            </span>
            <span className="legend-item">
              <i className="legend-swatch protected" aria-hidden="true" />
              保护时段
            </span>
            <span className="legend-item">点击日程可查看或键盘调整时间，支持拖拽改期</span>
          </div>
        </div>

        <aside className="card-flat backlog" aria-label="尚未安排的步骤">
          <div className="backlog-title">尚未安排</div>
          <div className="backlog-copy">选择一个待办步骤，让 GoalCal 为你推荐不冲突的时间段。</div>

          {goalsQuery.isLoading ? (
            <div className="muted" style={{ fontSize: 11 }}>
              正在加载待办步骤…
            </div>
          ) : goalsQuery.isError ? (
            <ErrorBanner error={goalsQuery.error} onRetry={() => void goalsQuery.refetch()} />
          ) : backlogSteps.length === 0 ? (
            <EmptyState
              title="所有待办步骤都已有时间"
              copy="当前活跃目标的待办步骤都已安排在当前日历视图中。"
            />
          ) : (
            backlogSteps.map((item) => (
              <div key={item.stepId} className="backlog-item">
                <div className="backlog-item-title">{item.stepTitle}</div>
                <div className="backlog-item-meta">
                  {item.estMin} 分钟 · {item.goalTitle}
                </div>
                <button
                  type="button"
                  className="btn small"
                  disabled={proposeAction.isPending}
                  onClick={() => void proposeAction.run(item.stepId)}
                >
                  找时间 →
                </button>
              </div>
            ))
          )}
        </aside>
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
        />
      ) : null}
    </div>
  );
}
