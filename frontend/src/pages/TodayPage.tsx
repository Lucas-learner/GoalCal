import { useCallback, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { invalidateAfterWrite, queryKeys } from '../api/queryKeys';
import type { CalendarEvent, TodayNext } from '../api/types';
import { EventDialog } from '../components/EventDialog';
import { SparkleIcon } from '../components/Icons';
import { EmptyState, ErrorBanner, SkeletonCard } from '../components/StateFeedback';
import {
  durationMinutes,
  formatDateTimeLabel,
  formatEyebrowDate,
  formatRelativeEventDay,
  formatTimeHHmm,
  formatTimeRange,
  isEventOnIsoDate,
  isExecutionWaitingCheckin,
} from '../lib/datetime';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useI18n } from '../i18n';
import { useAppState } from '../state/AppStateContext';

const EVENT_STATUS_TEXT: Record<string, [string, string]> = {
  done: ['已完成', 'Done'],
  partial: ['部分完成', 'Partial'],
  missed: ['未完成', 'Missed'],
  moved: ['已调整', 'Moved'],
};

export function TodayPage({ fallbackTimezone }: { fallbackTimezone: string }) {
  const queryClient = useQueryClient();
  const { openAssistant, openProposalModal, recordMutationResult } = useAppState();
  const { lang, t } = useI18n();
  const [selectedEvent, setSelectedEvent] = useState<CalendarEvent | null>(null);

  const todayQuery = useQuery({
    queryKey: queryKeys.today,
    queryFn: api.getToday,
  });

  const tz = todayQuery.data?.timezone || fallbackTimezone;

  const startFocusFn = useCallback(
    async (next: TodayNext, key: string) => {
      if (next.event !== null) {
        return api.startEvent(next.event.id, { version: next.event.version }, key);
      }
      return api.startStep(next.step.id, { version: next.step.version }, key);
    },
    [],
  );

  const startFocusAction = useIdempotentAction(startFocusFn, {
    onSuccess: async (res, next) => {
      recordMutationResult(res);
      await invalidateAfterWrite(queryClient, { goalId: next.goal.id });
    },
    onError: async (err, next) => {
      if (err.status === 409) {
        await invalidateAfterWrite(queryClient, { goalId: next.goal.id });
      }
    },
  });

  const completeNextFn = useCallback(
    async (next: TodayNext, key: string) =>
      api.updateStep(next.step.id, { version: next.step.version, status: 'done' }, key),
    [],
  );

  const completeNextAction = useIdempotentAction(completeNextFn, {
    onSuccess: async (res, next) => {
      recordMutationResult(res);
      await invalidateAfterWrite(queryClient, { goalId: next.goal.id });
    },
    onError: async (err, next) => {
      if (err.status === 409) {
        await invalidateAfterWrite(queryClient, { goalId: next.goal.id });
      }
    },
  });

  const proposeStepFn = useCallback(
    async (stepId: number, key: string) => api.createStepProposals(stepId, {}, key),
    [],
  );

  const proposeAction = useIdempotentAction(proposeStepFn, {
    onSuccess: async (proposal) => {
      await invalidateAfterWrite(queryClient, { goalId: proposal.goal_id });
      openProposalModal(proposal);
    },
  });

  const checkinExecutionFn = useCallback(
    async (
      args: { executionId: string | number; version: string; outcome: 'done' | 'extend' | 'adjust' },
      key: string,
    ) =>
      api.checkinExecution(
        args.executionId,
        { version: args.version, outcome: args.outcome },
        key,
      ),
    [],
  );

  const checkinExecutionAction = useIdempotentAction(checkinExecutionFn, {
    onSuccess: async (res) => {
      recordMutationResult(res);
      await invalidateAfterWrite(queryClient);
    },
    onError: async (err) => {
      if (err.status === 409) {
        await invalidateAfterWrite(queryClient);
      }
    },
  });

  const checkinRecoveryFn = useCallback(
    async (
      args: { eventId: number; version: string; outcome: 'done' | 'missed' | 'partial'; goalId: number | null },
      key: string,
    ) => api.checkinEvent(args.eventId, { version: args.version, outcome: args.outcome }, key),
    [],
  );

  const checkinRecoveryAction = useIdempotentAction(checkinRecoveryFn, {
    onSuccess: async (res, args) => {
      recordMutationResult(res);
      await invalidateAfterWrite(queryClient, { goalId: args.goalId });
    },
    onError: async (err, args) => {
      if (err.status === 409) {
        await invalidateAfterWrite(queryClient, { goalId: args.goalId });
      }
    },
  });

  const data = todayQuery.data;
  const visibleEvents = (data?.events ?? []).filter((ev) => ev.status !== 'skipped');
  const execution = data?.execution && data.execution.status === 'running' ? data.execution : null;
  const waitingCheckin = execution
    ? isExecutionWaitingCheckin(execution.expected_end_at, tz)
    : false;

  const anyError =
    startFocusAction.actionError ??
    completeNextAction.actionError ??
    proposeAction.actionError ??
    checkinExecutionAction.actionError ??
    checkinRecoveryAction.actionError;

  return (
    <div>
      <div className="topbar">
        <h1>{t('今天', 'Today')}</h1>
        <button
          type="button"
          className="assistant-trigger"
          onClick={(e) => openAssistant(e.currentTarget)}
        >
          <SparkleIcon />
          <span>{t('问 GoalCal', 'Ask GoalCal')}</span>
        </button>
      </div>

      <p className="subtitle">
        {data?.date ? formatEyebrowDate(data.date, tz, lang) : t('今日视图', 'Today view')}
      </p>

      {todayQuery.isLoading ? (
        <SkeletonCard label={t('正在加载今日安排', 'Loading today')} />
      ) : todayQuery.isError ? (
        <ErrorBanner error={todayQuery.error} onRetry={() => void todayQuery.refetch()} />
      ) : data ? (
        <>
          <ErrorBanner error={anyError} onRefresh={() => void todayQuery.refetch()} />

          {execution ? (
            <section className="focus-card" aria-label={t('当前执行', 'Active session')}>
              <div className="section-label">
                {waitingCheckin
                  ? t('进行中 · 已到预期结束时间，等待签到', 'In progress · Expected finish passed, check in')
                  : t('进行中', 'In progress')}
              </div>
              <div className="focus-heading">
                <div style={{ minWidth: 0 }}>
                  <div className="goal-name">{execution.goal_title}</div>
                  <div className="focus-title">{execution.step_title}</div>
                  <div className="focus-meta tabular-nums">
                    {t('开始于 ', 'Started at ')}
                    {formatTimeHHmm(execution.started_at, tz)}
                    {t(' · 预计结束 ', ' · Expected end ')}
                    {formatTimeHHmm(execution.expected_end_at, tz)}
                  </div>
                </div>
                <span className="focus-time">
                  {waitingCheckin ? t('等待签到', 'Check in') : formatTimeHHmm(execution.expected_end_at, tz)}
                </span>
              </div>
              <div className="actions">
                <button
                  type="button"
                  className="btn primary"
                  disabled={checkinExecutionAction.isPending}
                  onClick={() =>
                    void checkinExecutionAction.run({
                      executionId: execution.id,
                      version: execution.version,
                      outcome: 'done',
                    })
                  }
                >
                  {t('完成', 'Done')}
                </button>
                <button
                  type="button"
                  className="btn"
                  disabled={checkinExecutionAction.isPending}
                  onClick={() =>
                    void checkinExecutionAction.run({
                      executionId: execution.id,
                      version: execution.version,
                      outcome: 'extend',
                    })
                  }
                >
                  {t('延长 15 分钟', 'Extend 15m')}
                </button>
                <button
                  type="button"
                  className="btn"
                  disabled={checkinExecutionAction.isPending}
                  onClick={() =>
                    void checkinExecutionAction.run({
                      executionId: execution.id,
                      version: execution.version,
                      outcome: 'adjust',
                    })
                  }
                >
                  {t('调整 / 暂停', 'Pause / adjust')}
                </button>
              </div>
            </section>
          ) : data.next ? (() => {
            const nextEvent = data.next.event;
            const isFutureScheduled = Boolean(
              nextEvent && !isEventOnIsoDate(nextEvent.start, data.date, tz),
            );
            const futureDayLabel =
              nextEvent && isFutureScheduled
                ? formatRelativeEventDay(nextEvent.start, data.date, tz, lang)
                : '';
            const scheduledRange = nextEvent
              ? formatTimeRange(nextEvent.start, nextEvent.end, tz)
              : '';
            return (
              <section className="focus-card" aria-label={t('下一步焦点', 'Up next')}>
                <div className="section-label">
                  {futureDayLabel
                    ? `${t('接下来', 'Up next')} · ${futureDayLabel}`
                    : t('接下来', 'Up next')}
                </div>
                <div className="focus-heading">
                  <div style={{ minWidth: 0 }}>
                    <div className="goal-name">{data.next.goal.title}</div>
                    <div className="focus-title">{data.next.step.title}</div>
                    <div className="focus-meta">
                      {data.next.step.est_min} {t('分钟', 'min')}
                      {data.next.step.milestone ? ` · ${data.next.step.milestone}` : ''}
                      {nextEvent
                        ? `${t(' · 已安排 ', ' · Scheduled ')}${futureDayLabel ? `${futureDayLabel} ` : ''}${scheduledRange}`
                        : t(' · 尚未安排时间', ' · Unscheduled')}
                    </div>
                  </div>
                  <span className="focus-time">
                    {nextEvent
                      ? `${futureDayLabel ? `${futureDayLabel} ` : ''}${formatTimeHHmm(nextEvent.start, tz)}`
                      : t('未排期', 'Unscheduled')}
                  </span>
                </div>
                <div className="actions">
                  <button
                    type="button"
                    className={nextEvent && !isFutureScheduled ? 'btn primary' : 'btn'}
                    disabled={
                      startFocusAction.isPending ||
                      completeNextAction.isPending ||
                      proposeAction.isPending
                    }
                    onClick={() => void startFocusAction.run(data.next!)}
                  >
                    {startFocusAction.isPending ? t('开始中…', 'Starting…') : t('开始', 'Start')}
                  </button>
                  <button
                    type="button"
                    className="btn"
                    disabled={
                      startFocusAction.isPending ||
                      completeNextAction.isPending ||
                      proposeAction.isPending
                    }
                    onClick={() => void completeNextAction.run(data.next!)}
                  >
                    {completeNextAction.isPending ? t('保存中…', 'Saving…') : t('完成', 'Complete')}
                  </button>
                  <button
                    type="button"
                    className={nextEvent ? 'btn' : 'btn primary'}
                    disabled={
                      startFocusAction.isPending ||
                      completeNextAction.isPending ||
                      proposeAction.isPending
                    }
                    onClick={() => void proposeAction.run(data.next!.step.id)}
                  >
                    {proposeAction.isPending
                      ? t('查找中…', 'Finding…')
                      : nextEvent
                        ? t('调整时间', 'Reschedule')
                        : t('找时间', 'Find time')}
                  </button>
                </div>
              </section>
            );
          })() : (
            <div style={{ marginTop: 29 }}>
              <EmptyState
                title={t('今天还没有待推进步骤', 'Nothing pending for today')}
                copy={t('你可以查看现有目标继续拆解，或创建一个新的目标。', 'Review existing goals for next steps, or create a new goal.')}
                actions={
                  <>
                    <Link to="/goals" className="btn">
                      {t('查看目标', 'View goals')}
                    </Link>
                    <Link to="/goals/new" className="btn primary">
                      {t('+ 新目标', '+ New goal')}
                    </Link>
                  </>
                }
              />
            </div>
          )}

          {data.recovery.length > 0 ? (
            <section className="attention-list" aria-label={t('待恢复与逾期签到', 'Pending check-ins')}>
              {data.recovery.map((ev) => (
                <div key={ev.id} className="attention">
                  <div className="attention-text">
                    <span>
                      <strong>{ev.title}</strong>
                      {ev.goal_title ? ` (${ev.goal_title})` : ''} ·{' '}
                      <span className="tabular-nums">
                        {formatDateTimeLabel(ev.start, ev.end, tz, lang)}
                      </span>{' '}
                      {t('尚未签到', 'needs check-in')}
                    </span>
                  </div>
                  <div className="attention-actions">
                    <button
                      type="button"
                      className="inline-link-btn primary-link"
                      disabled={checkinRecoveryAction.isPending || proposeAction.isPending}
                      onClick={() =>
                        void checkinRecoveryAction.run({
                          eventId: ev.id,
                          version: ev.version,
                          outcome: 'done',
                          goalId: ev.goal_id,
                        })
                      }
                    >
                      {t('已完成', 'Done')}
                    </button>
                    <button
                      type="button"
                      className="inline-link-btn"
                      disabled={checkinRecoveryAction.isPending || proposeAction.isPending}
                      onClick={() =>
                        void checkinRecoveryAction.run({
                          eventId: ev.id,
                          version: ev.version,
                          outcome: 'partial',
                          goalId: ev.goal_id,
                        })
                      }
                    >
                      {t('部分完成', 'Partial')}
                    </button>
                    <button
                      type="button"
                      className="inline-link-btn"
                      disabled={checkinRecoveryAction.isPending || proposeAction.isPending}
                      onClick={() =>
                        void checkinRecoveryAction.run({
                          eventId: ev.id,
                          version: ev.version,
                          outcome: 'missed',
                          goalId: ev.goal_id,
                        })
                      }
                    >
                      {t('没来得及', 'Missed')}
                    </button>
                    {ev.step_id != null ? (
                      <button
                        type="button"
                        className="inline-link-btn primary-link"
                        disabled={checkinRecoveryAction.isPending || proposeAction.isPending}
                        onClick={() => void proposeAction.run(ev.step_id!)}
                      >
                        {t('重新安排', 'Reschedule')}
                      </button>
                    ) : null}
                  </div>
                </div>
              ))}
            </section>
          ) : null}

          <div className="section-head">
            <span className="section-title">{t('今日安排', "Today's schedule")}</span>
            <Link className="section-link" to="/calendar">
              {t('查看日历', 'View calendar')}
            </Link>
          </div>

          {visibleEvents.length === 0 ? (
            <EmptyState
              title={t('今天还没有日程安排', 'No events scheduled today')}
              copy={t('你可以在日历中添加固定日程，或为待办步骤寻找合适时间。', 'Add fixed events on the calendar or schedule a goal step.')}
              actions={
                <Link to="/calendar" className="btn">
                  {t('打开日历', 'Open calendar')}
                </Link>
              }
            />
          ) : (
            <section className="timeline" aria-label={t('今日时间线', "Today's timeline")}>
              {visibleEvents.map((ev) => {
                const isCurrent =
                  execution?.event_id === ev.id || data.next?.event?.id === ev.id;
                const isResolved =
                  ev.status === 'done' || ev.status === 'partial' || ev.status === 'missed';
                const statusPair = EVENT_STATUS_TEXT[ev.status];
                const statusText = statusPair ? t(statusPair[0], statusPair[1]) : undefined;
                return (
                  <button
                    key={ev.id}
                    type="button"
                    className={`event-row${ev.kind === 'block' ? ' goal' : ''}${
                      isCurrent ? ' current' : ''
                    }${isResolved ? ' resolved' : ''}`}
                    onClick={() => setSelectedEvent(ev)}
                  >
                    <span className="event-time">{formatTimeHHmm(ev.start, tz)}</span>
                    <div className="event-main">
                      <div className="event-name">{ev.title}</div>
                      {ev.goal_title || ev.kind === 'buffer' ? (
                        <div className="event-sub">
                          {ev.kind === 'buffer' ? t('缓冲时间', 'Buffer') : ev.goal_title}
                        </div>
                      ) : null}
                    </div>
                    <div className="event-right">
                      {statusText ? <span className="chip">{statusText}</span> : null}
                      <span className="event-duration">
                        {durationMinutes(ev.start, ev.end)} {t('分钟', 'min')}
                      </span>
                    </div>
                  </button>
                );
              })}
            </section>
          )}
        </>
      ) : null}

      {(() => {
        const liveSelected = selectedEvent
          ? visibleEvents.find((e) => e.id === selectedEvent.id) ?? selectedEvent
          : null;
        return liveSelected ? (
          <EventDialog
            event={liveSelected}
            timezone={tz}
            onClose={() => setSelectedEvent(null)}
          />
        ) : null;
      })()}
    </div>
  );
}
