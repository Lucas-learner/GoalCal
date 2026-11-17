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
import { useAppState } from '../state/AppStateContext';
import { Modal } from './Modal';
import { ErrorBanner } from './StateFeedback';

const STATUS_LABEL: Record<string, string> = {
  planned: '已计划',
  done: '已完成',
  partial: '部分完成',
  missed: '未完成',
  skipped: '已取消',
  moved: '已调整',
};

const KIND_LABEL: Record<string, string> = {
  block: '目标时间块',
  external: '固定日程',
  buffer: '缓冲',
};

interface EventDialogProps {
  event: CalendarEvent;
  timezone: string;
  onClose: () => void;
}

export function EventDialog({ event, timezone, onClose }: EventDialogProps) {
  const queryClient = useQueryClient();
  const { recordMutationResult, openProposalModal } = useAppState();

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

  const isBusy =
    moveAction.isPending ||
    cancelAction.isPending ||
    startAction.isPending ||
    proposeAction.isPending;

  const handleSaveTime = (e: React.FormEvent) => {
    e.preventDefault();
    setValidationError(null);
    const startUtc = datetimeLocalToUtcIso(startLocal, timezone);
    const endUtc = datetimeLocalToUtcIso(endLocal, timezone);
    if (!startUtc || !endUtc) {
      setValidationError('请填写完整的开始和结束时间。');
      return;
    }
    if (endUtc <= startUtc) {
      setValidationError('结束时间必须晚于开始时间。');
      return;
    }
    void moveAction.run({ start: startUtc, end: endUtc });
  };

  return (
    <Modal title={event.title} onClose={onClose} hasUnsavedChanges={dirty}>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
        <span className={event.kind === 'block' ? 'chip blue' : 'chip'}>
          {KIND_LABEL[event.kind] ?? event.kind}
        </span>
        <span className="chip">{STATUS_LABEL[event.status] ?? event.status}</span>
      </div>

      <div className="muted" style={{ fontSize: 12, marginBottom: 10 }}>
        当前时间（{timezone}）：{formatDateTimeLabel(event.start, event.end, timezone)}
      </div>

      {event.goal_id != null ? (
        <div style={{ marginBottom: 14, fontSize: 12 }}>
          关联目标：{' '}
          <Link
            to={`/goals/${event.goal_id}`}
            className="section-link"
            style={{ textDecoration: 'underline' }}
            onClick={onClose}
          >
            {event.goal_title || `查看目标 #${event.goal_id}`}
          </Link>
        </div>
      ) : null}

      <ErrorBanner
        error={
          moveAction.actionError ??
          cancelAction.actionError ??
          startAction.actionError ??
          proposeAction.actionError
        }
        onRefresh={() => void invalidateAfterWrite(queryClient, { goalId: event.goal_id })}
      />

      <form onSubmit={handleSaveTime} noValidate>
        <div className="field">
          <label className="field-label" htmlFor="event-start-local">
            开始时间（{timezone}）
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
            结束时间（{timezone}）
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
            {event.kind === 'block' && ['planned', 'moved'].includes(event.status) ? (
              <button
                type="button"
                className="btn primary"
                disabled={isBusy || !['planned', 'moved'].includes(event.status)}
                onClick={() => void startAction.run(undefined)}
              >
                {startAction.isPending ? '开始中…' : '开始执行'}
              </button>
            ) : null}
            <button type="submit" className="btn" disabled={isBusy || !['planned', 'moved'].includes(event.status)}>
              {moveAction.isPending ? '保存中…' : '保存时间修改'}
            </button>
            {event.step_id != null && ['planned', 'moved'].includes(event.status) ? (
              <button
                type="button"
                className="btn"
                disabled={isBusy || !['planned', 'moved'].includes(event.status)}
                onClick={() => void proposeAction.run(event.step_id!)}
              >
                {proposeAction.isPending ? '查找中…' : '找替代时间'}
              </button>
            ) : null}
          </div>

          {['planned', 'moved'].includes(event.status) ? (
            <button
              type="button"
              className="btn"
              disabled={isBusy || !['planned', 'moved'].includes(event.status)}
              onClick={() => void cancelAction.run(undefined)}
            >
              {cancelAction.isPending ? '取消中…' : '取消此日程'}
            </button>
          ) : null}
        </div>
      </form>
    </Modal>
  );
}
