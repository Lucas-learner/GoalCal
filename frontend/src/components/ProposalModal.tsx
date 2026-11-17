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
import { useI18n } from '../i18n';
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
  const { lang, t } = useI18n();
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
      setCustomError(t('请选择有效的本地日期和时间。', 'Please select a valid local date and time.'));
      return;
    }
    if (!currentStep) {
      setCustomError(t('正在读取步骤信息，请稍后再试。', 'Loading step details; please try again in a moment.'));
      return;
    }
    void scheduleAction.run({
      stepId: activeProposal.step_id,
      version: currentStep.version,
      start: startIso,
    });
  };

  const periodOptions: Array<{ key: PeriodKey; label: string }> = [
    { key: 'all', label: t('全部', 'All') },
    { key: 'morning', label: t('上午', 'Morning') },
    { key: 'afternoon', label: t('下午', 'Afternoon') },
    { key: 'evening', label: t('晚上', 'Evening') },
    { key: 'tomorrow', label: t('明天', 'Tomorrow') },
  ];

  return (
    <Modal title={t('选择时间', 'Pick a time')} onClose={closeProposalModal}>
      <div style={{ marginBottom: 12 }}>
        <div className="proposal-goal">{activeProposal.goal_title}</div>
        <div className="proposal-step">{activeProposal.step_title}</div>
      </div>

      <div
        role="group"
        aria-label={t('筛选推荐时段', 'Filter candidate windows')}
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
            <div className="status-banner-title">{t('该时间提议已过期或已被取代', 'This proposal has expired or been replaced')}</div>
            <div>{t('请重新生成可用时间候选后再选择。', 'Generate fresh slots before choosing.')}</div>
          </div>
          <div className="status-banner-actions">
            <button
              type="button"
              className="btn small primary"
              disabled={isBusy}
              onClick={() => void regenerateAction.run({ stepId: activeProposal.step_id, period: activePeriod })}
            >
              {regenerateAction.isPending ? t('生成中…', 'Finding…') : t('重新找时间', 'Find new slots')}
            </button>
          </div>
        </div>
      ) : null}

      {activeProposal.candidates.length === 0 ? (
        <EmptyState
          title={t('该时段暂无空档', 'No open slots in this range')}
          actions={
            <button
              type="button"
              className="btn"
              disabled={isBusy}
              onClick={() => void regenerateAction.run({ stepId: activeProposal.step_id, period: 'all' })}
            >
              {regenerateAction.isPending ? t('查找中…', 'Finding…') : t('查看全部', 'Show all')}
            </button>
          }
        />
      ) : (
        <div>
          <div className="field-help" style={{ marginBottom: 8 }}>
            {t('点击时段即可安排', 'Click any slot to schedule')}
          </div>
          {activeProposal.candidates.map((cand, index) => {
            const label = formatDateTimeLabel(cand.start, cand.end, timezone, lang);
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
                  {acceptAction.isPending && selectedIndex === index ? t('（安排中…）', ' (Scheduling…)') : ''}
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
            {t('自定义时间 →', 'Custom time →')}
          </button>
        ) : (
          <form onSubmit={handleCustomSubmit} noValidate>
            <label className="field-label" htmlFor="custom-schedule-start">
              {t(`开始时间（${timezone}）`, `Start time (${timezone})`)}
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
                {scheduleAction.isPending ? t('安排中…', 'Scheduling…') : t('安排', 'Schedule')}
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
                {t('收起', 'Cancel')}
              </button>
            </div>
            {customError ? <div className="field-error">{customError}</div> : null}
          </form>
        )}
      </div>
    </Modal>
  );
}
