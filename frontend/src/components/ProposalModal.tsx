import { useCallback, useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { invalidateAfterWrite, queryKeys } from '../api/queryKeys';
import type { Proposal } from '../api/types';
import {
  datetimeLocalToUtcIso,
  formatDateTimeLabel,
  isIsoExpired,
  nowInZone,
} from '../lib/datetime';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useAppState } from '../state/AppStateContext';
import { Modal } from './Modal';
import { EmptyState, ErrorBanner } from './StateFeedback';

type PeriodKey = 'all' | 'morning' | 'afternoon' | 'evening' | 'tomorrow';

function computeWindowBounds(
  period: PeriodKey,
  zone: string,
): { window_start?: string; window_end?: string } {
  if (period === 'all') return {};
  const now = nowInZone(zone);
  const todayStart = now.startOf('day');
  let start = todayStart;
  let end = todayStart.plus({ days: 1 });
  if (period === 'morning') {
    start = todayStart.set({ hour: 6, minute: 0 });
    end = todayStart.set({ hour: 12, minute: 0 });
  } else if (period === 'afternoon') {
    start = todayStart.set({ hour: 12, minute: 0 });
    end = todayStart.set({ hour: 18, minute: 0 });
  } else if (period === 'evening') {
    start = todayStart.set({ hour: 18, minute: 0 });
    end = todayStart.set({ hour: 23, minute: 59 });
  } else if (period === 'tomorrow') {
    start = todayStart.plus({ days: 1 });
    end = todayStart.plus({ days: 2 });
  }
  if (end <= now) {
    // If today's period already passed, look at tomorrow's same period.
    start = start.plus({ days: 1 });
    end = end.plus({ days: 1 });
  }
  return {
    window_start: start.toUTC().toISO({ suppressMilliseconds: true })!,
    window_end: end.toUTC().toISO({ suppressMilliseconds: true })!,
  };
}

export function ProposalModal({ timezone }: { timezone: string }) {
  const queryClient = useQueryClient();
  const { activeProposal, openProposalModal, closeProposalModal, recordMutationResult } = useAppState();
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);
  const [activePeriod, setActivePeriod] = useState<PeriodKey>('all');
  const [showCustomTime, setShowCustomTime] = useState(false);
  const [customStartLocal, setCustomStartLocal] = useState('');
  const [customError, setCustomError] = useState<string | null>(null);

  useEffect(() => {
    setActivePeriod('all');
    setShowCustomTime(false);
    setCustomError(null);
    if (activeProposal) {
      const nextHour = nowInZone(timezone).plus({ hours: 1 }).startOf('hour');
      setCustomStartLocal(nextHour.toFormat("yyyy-MM-dd'T'HH:mm"));
    }
  }, [activeProposal?.step_id, timezone]);

  const goalQuery = useQuery({
    queryKey: activeProposal ? queryKeys.goal(activeProposal.goal_id) : ['goals', 'none'],
    queryFn: () => api.getGoal(activeProposal!.goal_id),
    enabled: Boolean(activeProposal && showCustomTime),
  });

  const acceptFn = useCallback(
    async (
      args: { proposalId: string; version: string; index: number },
      key: string,
    ) => {
      setSelectedIndex(args.index);
      return api.acceptProposal(args.proposalId, { version: args.version, index: args.index }, key);
    },
    [],
  );

  const acceptAction = useIdempotentAction(acceptFn, {
    onSuccess: async (res) => {
      recordMutationResult(res);
      await invalidateAfterWrite(queryClient, { goalId: activeProposal?.goal_id });
      setSelectedIndex(null);
      closeProposalModal();
    },
    onError: async (err) => {
      setSelectedIndex(null);
      if (err.status === 409) {
        await invalidateAfterWrite(queryClient, { goalId: activeProposal?.goal_id });
      }
    },
  });

  const regenFn = useCallback(
    async (
      args: { stepId: number; period: PeriodKey },
      key: string,
    ) => api.createStepProposals(args.stepId, computeWindowBounds(args.period, timezone), key),
    [timezone],
  );

  const regenerateAction = useIdempotentAction(regenFn, {
    onSuccess: async (newProposal: Proposal, args) => {
      acceptAction.clearError();
      setActivePeriod(args.period);
      openProposalModal(newProposal);
      await invalidateAfterWrite(queryClient, { goalId: newProposal.goal_id });
    },
  });

  const scheduleFn = useCallback(
    async (args: { stepId: number; version: string; start: string }, key: string) =>
      api.scheduleStep(args.stepId, { version: args.version, start: args.start }, key),
    [],
  );

  const scheduleAction = useIdempotentAction(scheduleFn, {
    onSuccess: async (res) => {
      recordMutationResult(res);
      await invalidateAfterWrite(queryClient, { goalId: activeProposal?.goal_id });
      closeProposalModal();
    },
    onError: async (err) => {
      if (err.status === 409) {
        await invalidateAfterWrite(queryClient, { goalId: activeProposal?.goal_id });
      }
    },
  });

  if (!activeProposal) return null;

  const expired =
    activeProposal.status !== 'open' || isIsoExpired(activeProposal.expires_at, timezone);
  const currentStep = goalQuery.data?.steps.find((s) => s.id === activeProposal.step_id);
  const isBusy = acceptAction.isPending || regenerateAction.isPending || scheduleAction.isPending;

  const handleCustomSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setCustomError(null);
    const startIso = datetimeLocalToUtcIso(customStartLocal, timezone);
    if (!startIso) {
      setCustomError('请选择有效的本地日期和时间。');
      return;
    }
    if (!currentStep) {
      setCustomError('正在读取步骤信息，请稍后再试。');
      return;
    }
    void scheduleAction.run({
      stepId: activeProposal.step_id,
      version: currentStep.version,
      start: startIso,
    });
  };

  const periodOptions: Array<{ key: PeriodKey; label: string }> = [
    { key: 'all', label: '全部推荐' },
    { key: 'morning', label: '上午' },
    { key: 'afternoon', label: '下午' },
    { key: 'evening', label: '晚上' },
    { key: 'tomorrow', label: '明天' },
  ];

  return (
    <Modal title="选择合适时间" onClose={closeProposalModal}>
      <div style={{ marginBottom: 12 }}>
        <div className="proposal-goal">{activeProposal.goal_title}</div>
        <div className="proposal-step">{activeProposal.step_title}</div>
      </div>

      <div
        role="group"
        aria-label="筛选推荐时段"
        style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 14 }}
      >
        {periodOptions.map((opt) => (
          <button
            key={opt.key}
            type="button"
            className={`btn small${activePeriod === opt.key ? ' primary' : ''}`}
            disabled={isBusy}
            onClick={() => void regenerateAction.run({ stepId: activeProposal.step_id, period: opt.key })}
          >
            {opt.label}
          </button>
        ))}
      </div>

      <ErrorBanner
        error={acceptAction.actionError ?? regenerateAction.actionError ?? scheduleAction.actionError}
        onRefresh={() => void invalidateAfterWrite(queryClient, { goalId: activeProposal.goal_id })}
      />

      {expired ? (
        <div className="status-banner info" role="status">
          <div className="status-banner-body">
            <div className="status-banner-title">该时间提议已过期或已被取代</div>
            <div>请重新生成可用时间候选后再选择。</div>
          </div>
          <div className="status-banner-actions">
            <button
              type="button"
              className="btn small primary"
              disabled={isBusy}
              onClick={() => void regenerateAction.run({ stepId: activeProposal.step_id, period: activePeriod })}
            >
              {regenerateAction.isPending ? '生成中…' : '重新找时间'}
            </button>
          </div>
        </div>
      ) : null}

      {activeProposal.candidates.length === 0 ? (
        <EmptyState
          title="该时段没有足够空档"
          copy="你可以切换上方的上午/下午/明天筛选，或直接在下方自己指定时间。"
          actions={
            <button
              type="button"
              className="btn"
              disabled={isBusy}
              onClick={() => void regenerateAction.run({ stepId: activeProposal.step_id, period: 'all' })}
            >
              {regenerateAction.isPending ? '生成中…' : '查看全部推荐'}
            </button>
          }
        />
      ) : (
        <div>
          <div className="field-help" style={{ marginBottom: 8 }}>
            点击任意候选时间将直接安排到日历，安排后可随时撤销。
          </div>
          {activeProposal.candidates.map((cand, index) => {
            const label = formatDateTimeLabel(cand.start, cand.end, timezone);
            return (
              <button
                key={`${cand.start}-${index}`}
                type="button"
                className="slot-btn"
                aria-label={`${label} · ${activeProposal.goal_title} · ${activeProposal.step_title}`}
                disabled={expired || isBusy}
                onClick={() =>
                  void acceptAction.run({
                    proposalId: activeProposal.id,
                    version: activeProposal.version,
                    index,
                  })
                }
              >
                <div className="slot-time">
                  {label}
                  {acceptAction.isPending && selectedIndex === index ? '（安排中…）' : ''}
                </div>
              </button>
            );
          })}
        </div>
      )}

      <div
        style={{
          marginTop: 14,
          paddingTop: 12,
          borderTop: '1px solid var(--line)',
        }}
      >
        {!showCustomTime ? (
          <button
            type="button"
            className="inline-link-btn"
            onClick={() => setShowCustomTime(true)}
          >
            都不合适？自己指定开始时间 →
          </button>
        ) : (
          <form onSubmit={handleCustomSubmit} noValidate>
            <label className="field-label" htmlFor="custom-schedule-start">
              自选开始时间（{timezone}）
            </label>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
              <input
                id="custom-schedule-start"
                type="datetime-local"
                className="input tabular-nums"
                style={{ flex: 1, minWidth: 200 }}
                value={customStartLocal}
                onChange={(e) => setCustomStartLocal(e.target.value)}
                disabled={isBusy}
              />
              <button type="submit" className="btn small primary" disabled={isBusy || !currentStep}>
                {scheduleAction.isPending ? '安排中…' : '按此时间安排'}
              </button>
              <button
                type="button"
                className="btn small"
                disabled={isBusy}
                onClick={() => {
                  setShowCustomTime(false);
                  setCustomError(null);
                }}
              >
                收起
              </button>
            </div>
            {customError ? <div className="field-error">{customError}</div> : null}
          </form>
        )}
      </div>
    </Modal>
  );
}
