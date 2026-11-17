import { useCallback, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { invalidateAfterWrite } from '../api/queryKeys';
import { datetimeLocalToUtcIso } from '../lib/datetime';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useAppState } from '../state/AppStateContext';
import { Modal } from './Modal';
import { ErrorBanner } from './StateFeedback';

interface NewEventDialogProps {
  timezone: string;
  initialStartLocal: string;
  initialEndLocal: string;
  onClose: () => void;
}

export function NewEventDialog({
  timezone,
  initialStartLocal,
  initialEndLocal,
  onClose,
}: NewEventDialogProps) {
  const queryClient = useQueryClient();
  const { recordMutationResult } = useAppState();

  const [title, setTitle] = useState('');
  const [startLocal, setStartLocal] = useState(initialStartLocal);
  const [endLocal, setEndLocal] = useState(initialEndLocal);
  const [validationError, setValidationError] = useState<string | null>(null);

  const createFn = useCallback(
    async (body: { title: string; start: string; end: string }, key: string) =>
      api.createCalendarEvent(body, key),
    [],
  );

  const createAction = useIdempotentAction(createFn, {
    onSuccess: async (res) => {
      recordMutationResult(res);
      await invalidateAfterWrite(queryClient);
      onClose();
    },
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setValidationError(null);
    const trimmed = title.trim();
    if (!trimmed) {
      setValidationError('请输入日程名称。');
      return;
    }
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
    void createAction.run({ title: trimmed, start: startUtc, end: endUtc });
  };

  const isDirty = title.trim().length > 0;

  return (
    <Modal title="新建日程" onClose={onClose} hasUnsavedChanges={isDirty}>
      <ErrorBanner error={createAction.actionError} />
      <form onSubmit={handleSubmit} noValidate>
        <div className="field">
          <label className="field-label" htmlFor="new-event-title">
            日程名称
          </label>
          <input
            id="new-event-title"
            type="text"
            className="input"
            placeholder="例如：产品周会"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            disabled={createAction.isPending}
          />
        </div>

        <div className="field">
          <label className="field-label" htmlFor="new-event-start">
            开始时间（{timezone}）
          </label>
          <input
            id="new-event-start"
            type="datetime-local"
            className="input tabular-nums"
            value={startLocal}
            onChange={(e) => setStartLocal(e.target.value)}
            disabled={createAction.isPending}
          />
        </div>

        <div className="field">
          <label className="field-label" htmlFor="new-event-end">
            结束时间（{timezone}）
          </label>
          <input
            id="new-event-end"
            type="datetime-local"
            className="input tabular-nums"
            value={endLocal}
            onChange={(e) => setEndLocal(e.target.value)}
            disabled={createAction.isPending}
          />
        </div>

        {validationError ? (
          <div className="field-error" role="alert">
            {validationError}
          </div>
        ) : null}

        <div className="actions" style={{ justifyContent: 'flex-end', marginTop: 18 }}>
          <button
            type="button"
            className="btn"
            onClick={onClose}
            disabled={createAction.isPending}
          >
            取消
          </button>
          <button type="submit" className="btn primary" disabled={createAction.isPending}>
            {createAction.isPending ? '保存中…' : '创建日程'}
          </button>
        </div>
      </form>
    </Modal>
  );
}
