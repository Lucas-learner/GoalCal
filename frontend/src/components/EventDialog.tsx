import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { invalidateAfterWrite } from '../api/queryKeys';
import type { CalendarEvent } from '../api/types';
import {
  datetimeLocalToUtcIso,
  formatDateTimeLabel,
  isoToDatetimeLocal,
} from '../lib/datetime';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useI18n } from '../i18n';
import { useAppState } from '../state/AppStateContext';
import { Modal } from './Modal';
import { ErrorBanner } from './StateFeedback';

const STATUS_LABEL: Record<string, [string, string]> = {
  planned: ['已计划', 'Planned'],
  done: ['已完成', 'Done'],
  partial: ['部分完成', 'Partial'],
  missed: ['未完成', 'Missed'],
  skipped: ['已取消', 'Cancelled'],
  moved: ['已调整', 'Moved'],
};

const KIND_LABEL: Record<string, [string, string]> = {
  block: ['目标时间块', 'Goal block'],
  external: ['固定日程', 'Fixed event'],
  buffer: ['缓冲', 'Buffer'],
};

interface EventDialogProps {
  event: CalendarEvent;
  timezone: string;
  onClose: () => void;
}

export function EventDialog({ event, timezone, onClose }: EventDialogProps) {
  const queryClient = useQueryClient();
  const { recordMutationResult, openProposalModal } = useAppState();
  const { lang, t } = useI18n();

  const initialStart = isoToDatetimeLocal(event.start, timezone);
  const initialEnd = isoToDatetimeLocal(event.end, timezone);

  const [startLocal, setStartLocal] = useState(initialStart);
  const [endLocal, setEndLocal] = useState(initialEnd);
  const [dirty, setDirty] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);

  // Sync only if user hasn't modified inputs yet (preserves edits on 409 refresh).
  useEffect(() => {
    if (!dirty) {
      setStartLocal(isoToDatetimeLocal(event.start, timezone));
      setEndLocal(isoToDatetimeLocal(event.end, timezone));
    }
  }, [event.start, event.end, timezone, dirty]);

  const moveFn = useCallback(
    async (args: { start: string; end: string }, key: string) =>
      api.moveCalendarEvent(event.id, { version: event.version, start: args.start, end: args.end }, key),
    [event.id, event.version],
  );

  const moveAction = useIdempotentAction(moveFn, {
    onSuccess: async (res) => {
      setDirty(false);
      recordMutationResult(res);
      await invalidateAfterWrite(queryClient, { goalId: event.goal_id });
      onClose();
    },
    onError: async (err) => {
      if (err.status === 409) {
        await invalidateAfterWrite(queryClient, { goalId: event.goal_id });
      }
    },
  });

  const cancelFn = useCallback(
    async (_: void, key: string) =>
      api.cancelCalendarEvent(event.id, { version: event.version }, key),
    [event.id, event.version],
  );

  const cancelAction = useIdempotentAction(cancelFn, {
    onSuccess: async (res) => {
      recordMutationResult(res);
      await invalidateAfterWrite(queryClient, { goalId: event.goal_id });
      onClose();
    },
    onError: async (err) => {
      if (err.status === 409) {
        await invalidateAfterWrite(queryClient, { goalId: event.goal_id });
      }
    },
  });

  const startFn = useCallback(
    async (_: void, key: string) =>
      api.startEvent(event.id, { version: event.version }, key),
    [event.id, event.version],
  );

  const startAction = useIdempotentAction(startFn, {
    onSuccess: async (res) => {
      recordMutationResult(res);
      await invalidateAfterWrite(queryClient, { goalId: event.goal_id });
      onClose();
    },
    onError: async (err) => {
      if (err.status === 409) {
        await invalidateAfterWrite(queryClient, { goalId: event.goal_id });
      }
    },
  });

  const proposeFn = useCallback(
    async (stepId: number, key: string) => api.createStepProposals(stepId, {}, key),
    [],
  );

  const proposeAction = useIdempotentAction(proposeFn, {
    onSuccess: async (proposal) => {
      await invalidateAfterWrite(queryClient, { goalId: event.goal_id });
      onClose();
      openProposalModal(proposal);
    },
  });

  const completeStepFn = useCallback(
    async (args: { stepId: number; version: string }, key: string) =>
      api.updateStep(args.stepId, { version: args.version, status: 'done' }, key),
    [],
  );

  const completeStepAction = useIdempotentAction(completeStepFn, {
    onSuccess: async (res) => {
      recordMutationResult(res);
      await invalidateAfterWrite(queryClient, { goalId: event.goal_id });
      onClose();
    },
    onError: async (err) => {
      if (err.status === 409) {
        await invalidateAfterWrite(queryClient, { goalId: event.goal_id });
      }
    },
  });

  const isBusy =
    moveAction.isPending ||
    cancelAction.isPending ||
    startAction.isPending ||
    proposeAction.isPending ||
    completeStepAction.isPending;

  const handleSaveTime = (e: React.FormEvent) => {
    e.preventDefault();
    setValidationError(null);
    const startUtc = datetimeLocalToUtcIso(startLocal, timezone);
    const endUtc = datetimeLocalToUtcIso(endLocal, timezone);
    if (!startUtc || !endUtc) {
      setValidationError(t('请填写完整的开始和结束时间。', 'Please fill in both start and end time.'));
      return;
    }
    if (endUtc <= startUtc) {
      setValidationError(t('结束时间必须晚于开始时间。', 'End time must be after start time.'));
      return;
    }
    void moveAction.run({ start: startUtc, end: endUtc });
  };

  const kindPair = KIND_LABEL[event.kind];
  const statusPair = STATUS_LABEL[event.status];

  return (
    <Modal title={event.title} onClose={onClose} hasUnsavedChanges={dirty}>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
        <span className={event.kind === 'block' ? 'chip blue' : 'chip'}>
          {kindPair ? t(kindPair[0], kindPair[1]) : event.kind}
        </span>
        <span className="chip">
          {statusPair ? t(statusPair[0], statusPair[1]) : event.status}
        </span>
        {event.source === 'sync' ? (
          <span className="chip">{t('外部同步', 'Synced')}</span>
        ) : null}
      </div>

      <div className="muted" style={{ fontSize: 12, marginBottom: 10 }}>
        {formatDateTimeLabel(event.start, event.end, timezone, lang)} · {timezone}
      </div>

      {event.source === 'sync' ? (
        <div className="muted" style={{ fontSize: 11.5, marginBottom: 12, lineHeight: 1.5 }}>
          {t(
            '来自外部日历同步；本地修改会在下次同步时被覆盖。',
            'Synced from an external calendar; local edits will be overwritten on the next sync.',
          )}
        </div>
      ) : null}

      {event.goal_id != null ? (
        <div style={{ marginBottom: 14, fontSize: 12 }}>
          {t('目标：', 'Goal: ')}{' '}
          <Link
            to={`/goals/${event.goal_id}`}
            className="section-link"
            style={{ textDecoration: 'underline' }}
            onClick={onClose}
          >
            {event.goal_title || t(`目标 #${event.goal_id}`, `Goal #${event.goal_id}`)}
          </Link>
        </div>
      ) : null}

      <ErrorBanner
        error={
          moveAction.actionError ??
          cancelAction.actionError ??
          startAction.actionError ??
          proposeAction.actionError ??
          completeStepAction.actionError
        }
        onRefresh={() => void invalidateAfterWrite(queryClient, { goalId: event.goal_id })}
      />

      {event.kind === 'block' ? (
        ['planned', 'moved'].includes(event.status) ? (
          <div className="actions" style={{ marginTop: 18 }}>
            <button
              type="button"
              className="btn primary"
              disabled={isBusy}
              onClick={() => void startAction.run(undefined)}
            >
              {startAction.isPending ? t('开始中…', 'Starting…') : t('开始', 'Start')}
            </button>
            {event.step_id != null && event.step_version ? (
              <button
                type="button"
                className="btn"
                disabled={isBusy}
                onClick={() =>
                  void completeStepAction.run({
                    stepId: event.step_id!,
                    version: event.step_version!,
                  })
                }
              >
                {completeStepAction.isPending ? t('保存中…', 'Saving…') : t('完成', 'Complete')}
              </button>
            ) : null}
            {event.step_id != null ? (
              <button
                type="button"
                className="btn"
                disabled={isBusy}
                onClick={() => void proposeAction.run(event.step_id!)}
              >
                {proposeAction.isPending ? t('查找中…', 'Finding…') : t('换时间', 'Reschedule')}
              </button>
            ) : null}
            <button
              type="button"
              className="btn"
              disabled={isBusy}
              onClick={() => void cancelAction.run(undefined)}
            >
              {cancelAction.isPending ? t('取消中…', 'Cancelling…') : t('取消排期', 'Cancel schedule')}
            </button>
          </div>
        ) : null
      ) : (
        <form onSubmit={handleSaveTime} noValidate>
          <div className="field">
            <label className="field-label" htmlFor="event-start-local">
              {t('开始', 'Start')}
            </label>
            <input
              id="event-start-local"
              type="datetime-local"
              className="input tabular-nums"
              value={startLocal}
              onChange={(e) => {
                setDirty(true);
                setStartLocal(e.target.value);
              }}
              disabled={isBusy || !['planned', 'moved'].includes(event.status)}
            />
          </div>

          <div className="field">
            <label className="field-label" htmlFor="event-end-local">
              {t('结束', 'End')}
            </label>
            <input
              id="event-end-local"
              type="datetime-local"
              className="input tabular-nums"
              value={endLocal}
              onChange={(e) => {
                setDirty(true);
                setEndLocal(e.target.value);
              }}
              disabled={isBusy || !['planned', 'moved'].includes(event.status)}
            />
          </div>

          {validationError ? (
            <div className="field-error" role="alert">
              {validationError}
            </div>
          ) : null}

          <div className="actions" style={{ justifyContent: 'space-between', marginTop: 18 }}>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <button type="submit" className="btn primary" disabled={isBusy || !['planned', 'moved'].includes(event.status)}>
                {moveAction.isPending ? t('保存中…', 'Saving…') : t('保存', 'Save')}
              </button>
            </div>

            {['planned', 'moved'].includes(event.status) ? (
              <button
                type="button"
                className="btn"
                disabled={isBusy || !['planned', 'moved'].includes(event.status)}
                onClick={() => void cancelAction.run(undefined)}
              >
                {cancelAction.isPending ? t('取消中…', 'Cancelling…') : t('取消日程', 'Cancel event')}
              </button>
            ) : null}
          </div>
        </form>
      )}
    </Modal>
  );
}
