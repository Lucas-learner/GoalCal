import { useCallback, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { invalidateAfterWrite } from '../api/queryKeys';
import { datetimeLocalToUtcIso } from '../lib/datetime';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useI18n } from '../i18n';
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
  const { t } = useI18n();

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
      setValidationError(t('请输入日程名称。', 'Please enter an event title.'));
      return;
    }
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
    void createAction.run({ title: trimmed, start: startUtc, end: endUtc });
  };

  const isDirty = title.trim().length > 0;

  return (
    <Modal title={t('新建日程', 'New event')} onClose={onClose} hasUnsavedChanges={isDirty}>
      <ErrorBanner error={createAction.actionError} />
      <div className="muted" style={{ fontSize: 11, marginBottom: 10 }}>
        {t('时区', 'Time zone')}: {timezone}
      </div>
      <form onSubmit={handleSubmit} noValidate>
        <div className="field">
          <label className="field-label" htmlFor="new-event-title">
            {t('名称', 'Title')}
          </label>
          <input
            id="new-event-title"
            type="text"
            className="input"
            placeholder={t('例如：产品周会', 'e.g. Weekly sync')}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            disabled={createAction.isPending}
          />
        </div>

        <div className="field">
          <label className="field-label" htmlFor="new-event-start">
            {t('开始', 'Start')}
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
            {t('结束', 'End')}
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
            {t('取消', 'Cancel')}
          </button>
          <button type="submit" className="btn primary" disabled={createAction.isPending}>
            {createAction.isPending ? t('保存中…', 'Saving…') : t('创建', 'Create')}
          </button>
        </div>
      </form>
    </Modal>
  );
}
