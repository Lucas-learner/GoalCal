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
  formatTimeHHmm,
  formatTimeRange,
  isExecutionWaitingCheckin,
} from '../lib/datetime';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useAppState } from '../state/AppStateContext';

const EVENT_STATUS_TEXT: Record<string, string> = {
  done: '已完成',
  partial: '部分完成',
  missed: '未完成',
  moved: '已调整',
};

export function TodayPage({ fallbackTimezone }: { fallbackTimezone: string }) {
  const queryClient = useQueryClient();
  const { openAssistant, openProposalModal, recordMutationResult } = useAppState();
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
        <div>
          <div className="eyebrow">
            {data?.date ? formatEyebrowDate(data.date, tz) : '今日视图'}
          </div>
          <h1>今天</h1>
        </div>
        <button
          type="button"
          className="assistant-trigger"
          onClick={(e) => openAssistant(e.currentTarget)}
        >
          <SparkleIcon />
          <span>问 GoalCal</span>
        </button>
      </div>

      {todayQuery.isLoading ? (
        <SkeletonCard label="正在加载今日安排" />
      ) : todayQuery.isError ? (
        <div style={{ marginTop: 24 }}>
          <ErrorBanner error={todayQuery.error} onRetry={() => void todayQuery.refetch()} />
        </div>
      ) : data ? (
        <>
          <div style={{ marginTop: 16 }}>
            <ErrorBanner error={anyError} onRefresh={() => void todayQuery.refetch()} />
          </div>

          {execution ? (
            <section className="focus-card" aria-label="当前执行">
              <div className="section-label">
                {waitingCheckin ? '进行中 · 已到预期结束时间，等待签到' : '进行中'}
              </div>
              <div className="focus-heading">
                <div style={{ minWidth: 0 }}>
                  <div className="goal-name">{execution.goal_title}</div>
                  <div className="focus-title">{execution.step_title}</div>
                  <div className="focus-meta tabular-nums">
                    开始于 {formatTimeHHmm(execution.started_at, tz)} · 预计结束{' '}
                    {formatTimeHHmm(execution.expected_end_at, tz)}
                  </div>
                </div>
                <span className="focus-time">
                  {waitingCheckin ? '等待签到' : formatTimeHHmm(execution.expected_end_at, tz)}
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
                  完成
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
                  延长 15 分钟
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
                  调整 / 暂停
                </button>
              </div>
            </section>
          ) : data.next ? (
            <section className="focus-card" aria-label="下一步焦点">
              <div className="section-label">接下来</div>
              <div className="focus-heading">
                <div style={{ minWidth: 0 }}>
                  <div className="goal-name">{data.next.goal.title}</div>
                  <div className="focus-title">{data.next.step.title}</div>
                  <div className="focus-meta">
                    {data.next.step.est_min} 分钟
                    {data.next.step.milestone ? ` · ${data.next.step.milestone}` : ''}
                    {data.next.event
                      ? ` · 已安排 ${formatTimeRange(data.next.event.start, data.next.event.end, tz)}`
                      : ' · 尚未安排时间'}
                  </div>
                </div>
                <span className="focus-time">
                  {data.next.event ? formatTimeHHmm(data.next.event.start, tz) : '未排期'}
                </span>
              </div>
              <div className="actions">
                <button
                  type="button"
                  className="btn primary"
                  disabled={
                    startFocusAction.isPending ||
                    completeNextAction.isPending ||
                    proposeAction.isPending
                  }
                  onClick={() => void startFocusAction.run(data.next!)}
                >
                  {startFocusAction.isPending ? '开始中…' : '开始'}
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
                  {completeNextAction.isPending ? '保存中…' : '完成'}
                </button>
                <button
                  type="button"
                  className="btn"
                  disabled={
                    startFocusAction.isPending ||
                    completeNextAction.isPending ||
                    proposeAction.isPending
                  }
                  onClick={() => void proposeAction.run(data.next!.step.id)}
                >
                  {proposeAction.isPending
                    ? '查找中…'
                    : data.next.event
                      ? '调整时间'
                      : '找时间'}
                </button>
              </div>
            </section>
          ) : (
            <div style={{ marginTop: 29 }}>
              <EmptyState
                title="今天还没有待推进步骤"
                copy="你可以查看现有目标继续拆解，或创建一个新的目标。"
                actions={
                  <>
                    <Link to="/goals" className="btn">
                      查看目标
                    </Link>
                    <Link to="/goals/new" className="btn primary">
                      + 新目标
                    </Link>
                  </>
                }
              />
            </div>
          )}

          {data.recovery.length > 0 ? (
            <section className="attention-list" aria-label="待恢复与逾期签到">
              {data.recovery.map((ev) => (
                <div key={ev.id} className="attention">
                  <div className="attention-text">
                    <span>
                      <strong>{ev.title}</strong>
                      {ev.goal_title ? `（${ev.goal_title}）` : ''} ·{' '}
                      <span className="tabular-nums">
                        {formatDateTimeLabel(ev.start, ev.end, tz)}
                      </span>{' '}
                      尚未签到
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
                      已完成
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
                      部分完成
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
                      没来得及
                    </button>
                    {ev.step_id != null ? (
                      <button
                        type="button"
                        className="inline-link-btn primary-link"
                        disabled={checkinRecoveryAction.isPending || proposeAction.isPending}
                        onClick={() => void proposeAction.run(ev.step_id!)}
                      >
                        重新安排
                      </button>
                    ) : null}
                  </div>
                </div>
              ))}
            </section>
          ) : null}

          <div className="section-head">
            <span className="section-title">今日安排</span>
            <Link className="section-link" to="/calendar">
              查看日历
            </Link>
          </div>

          {visibleEvents.length === 0 ? (
            <EmptyState
              title="今天还没有日程安排"
              copy="你可以在日历中添加固定日程，或为待办步骤寻找合适时间。"
              actions={
                <Link to="/calendar" className="btn">
                  打开日历
                </Link>
              }
            />
          ) : (
            <section className="timeline" aria-label="今日时间线">
              {visibleEvents.map((ev) => {
                const isCurrent =
                  execution?.event_id === ev.id || data.next?.event?.id === ev.id;
                const isResolved =
                  ev.status === 'done' || ev.status === 'partial' || ev.status === 'missed';
                const statusText = EVENT_STATUS_TEXT[ev.status];
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
                          {ev.kind === 'buffer' ? '缓冲时间' : ev.goal_title}
                        </div>
                      ) : null}
                    </div>
                    <div className="event-right">
                      {statusText ? <span className="chip">{statusText}</span> : null}
                      <span className="event-duration">
                        {durationMinutes(ev.start, ev.end)} 分钟
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
