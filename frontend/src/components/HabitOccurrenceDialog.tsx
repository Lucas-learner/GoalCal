import { useCallback, useId, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { DateTime } from 'luxon';
import { api } from '../api/client';
import { invalidateAfterWrite, queryKeys } from '../api/queryKeys';
import type { HabitActionBody, HabitOccurrence, HabitProgress } from '../api/types';
import { useI18n } from '../i18n';
import { datetimeLocalToUtcIso, durationMinutes, formatDateTimeLabel } from '../lib/datetime';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useAppState } from '../state/AppStateContext';
import { Modal } from './Modal';
import { EmptyState, ErrorBanner } from './StateFeedback';

type HabitSlotPeriod = 'all' | 'morning' | 'afternoon' | 'evening';

export function useHabitAction(onSuccess?: () => void) {
  const queryClient = useQueryClient();
  const { recordMutationResult } = useAppState();
  const call = useCallback((args: { id: number; body: HabitActionBody }, key: string) =>
    api.habitAction(args.id, args.body, key), []);
  return useIdempotentAction(call, {
    onSuccess: async (result) => {
      recordMutationResult(result);
      await invalidateAfterWrite(queryClient);
      onSuccess?.();
    },
    onError: async (error) => {
      if (error.status === 409) await invalidateAfterWrite(queryClient);
    },
  });
}

export function useHabitLabels() {
  const { t } = useI18n();
  return {
    habit: t('习惯', 'Habit'),
    status: {
      pending: t('待完成', 'Pending'),
      done: t('已完成', 'Done'),
      skipped: t('已跳过', 'Skipped'),
      missed: t('未完成', 'Missed'),
    },
    reason: (reason: string | null) => {
      if (!reason) return null;
      if (reason === 'unconfirmed') return t('时段待确认', 'Time not confirmed');
      if (reason === 'time_passed') return t('默认时间已过', 'Default time passed');
      if (reason === 'outside_availability') return t('超出可用时段', 'Outside available hours');
      if (reason === 'invalid_time') return t('请选择其他时间', 'Choose another time');
      return t('时间冲突', 'Time conflict');
    },
  };
}

type Props = ({ occurrence: HabitOccurrence; week?: never } | { occurrence?: never; week: HabitProgress }) & {
  onClose: () => void;
  startInEditor?: boolean;
};

export function HabitOccurrenceDialog({ occurrence, week, onClose, startInEditor }: Props) {
  const { lang, t } = useI18n();
  const labels = useHabitLabels();
  const id = useId();
  const habitId = occurrence?.habit_id ?? week!.habit_id;
  const title = occurrence?.title ?? week!.title;
  const zone = occurrence?.timezone ?? week!.timezone;
  const today = occurrence?.today ?? week!.today;
  const version = occurrence?.version;
  const habitVersion = occurrence?.habit_version ?? week!.version;
  const originalDate = occurrence?.date;
  const monday = originalDate
    ? DateTime.fromISO(originalDate, { zone }).startOf('week').toISODate()!
    : week!.week;
  const lastDay = DateTime.fromISO(monday, { zone }).plus({ days: 6 }).toISODate()!;
  const earliest = originalDate && occurrence.mode === 'fixed' ? originalDate
    : week?.schedule_from ?? (today > monday ? today : monday);
  const latest = originalDate && occurrence.mode === 'fixed' ? originalDate : lastDay;
  const defaultMinutes = occurrence?.start && occurrence.end
    ? Math.round(durationMinutes(occurrence.start, occurrence.end))
    : occurrence?.est_min ?? week?.est_min ?? 30;
  const [editing, setEditing] = useState(!occurrence || startInEditor || false);
  const [showCustomTime, setShowCustomTime] = useState(false);
  const [activePeriod, setActivePeriod] = useState<HabitSlotPeriod>('all');
  const [selectedSlotIndex, setSelectedSlotIndex] = useState<number | null>(null);
  const [date, setDate] = useState(originalDate ?? earliest);
  const [time, setTime] = useState(occurrence?.start ? DateTime.fromISO(occurrence.start).setZone(zone).toFormat('HH:mm') : '');
  const [minutes, setMinutes] = useState<number | ''>(defaultMinutes);
  const [validation, setValidation] = useState('');
  const operation = useHabitAction(() => {
    setSelectedSlotIndex(null);
    onClose();
  });
  const act = (action: HabitActionBody['action'], extra?: Partial<HabitActionBody>) =>
    void operation.run({ id: habitId, body: { date: originalDate ?? extra?.target_date ?? date, action,
      ...(version ? { version } : { habit_version: habitVersion }), ...extra } });
  const start = occurrence?.start;
  const end = occurrence?.end;
  const canSchedule = !occurrence || (occurrence.status === 'pending' && occurrence.date >= today);

  const slotsQuery = useQuery({
    queryKey: queryKeys.habitSlots(habitId, earliest, latest, activePeriod, originalDate),
    queryFn: () => api.getHabitSlots(habitId, {
      start_date: earliest,
      end_date: latest,
      period: activePeriod,
      ignore_date: originalDate,
    }),
    enabled: Boolean(canSchedule && editing && !showCustomTime),
  });

  const periodOptions: Array<{ key: HabitSlotPeriod; label: string }> = [
    { key: 'all', label: t('全部', 'All') },
    { key: 'morning', label: t('上午', 'Morning') },
    { key: 'afternoon', label: t('下午', 'Afternoon') },
    { key: 'evening', label: t('晚上', 'Evening') },
  ];
  const candidates = slotsQuery.data?.candidates ?? [];

  const canStart = Boolean(occurrence && occurrence.status === 'pending' && occurrence.date === today);

  return (
    <Modal title={title} onClose={() => { if (!operation.isPending) onClose(); }}>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
        <span className="chip blue">{labels.habit}</span>
        {occurrence ? <span className="chip">{labels.status[occurrence.status]}</span> : null}
      </div>
      <div className="muted" style={{ fontSize: 12, marginBottom: 10 }}>
        {start && end
          ? `${formatDateTimeLabel(start, end, zone, lang)} · ${zone}`
          : `${originalDate ?? `${monday} – ${lastDay}`} · ${zone}`}
      </div>
      <div style={{ marginBottom: 14, fontSize: 12 }}>
        {t('习惯：', 'Habit: ')}{' '}
        <Link
          to={`/habits/${habitId}`}
          className="section-link"
          style={{ textDecoration: 'underline' }}
          onClick={onClose}
        >
          {title}
        </Link>
      </div>
      {occurrence?.reason ? <p className="muted" style={{ fontSize: 12, marginBottom: 10 }}>{labels.reason(occurrence.reason)}</p> : null}
      <ErrorBanner error={operation.actionError ?? (editing && !showCustomTime ? slotsQuery.error : null)} />
      {canSchedule && editing ? (
        <>
          <div className="proposal-mode-tabs" role="group" aria-label={t('排期方式', 'Scheduling mode')}>
            <button
              type="button"
              aria-pressed={!showCustomTime}
              className={`proposal-mode-tab${!showCustomTime ? ' active' : ''}`}
              disabled={operation.isPending}
              onClick={() => { setShowCustomTime(false); setValidation(''); }}
            >
              {t('推荐空档', 'Open slots')}
            </button>
            <button
              type="button"
              aria-pressed={showCustomTime}
              className={`proposal-mode-tab${showCustomTime ? ' active' : ''}`}
              disabled={operation.isPending}
              onClick={() => setShowCustomTime(true)}
            >
              {t('自定义时间 →', 'Custom time →')}
            </button>
          </div>
          {!showCustomTime ? (
            <div>
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
                    disabled={operation.isPending || slotsQuery.isFetching}
                    onClick={() => setActivePeriod(opt.key)}
                  >
                    {opt.label}
                  </button>
                ))}
              </div>
              {slotsQuery.isLoading ? (
                <p className="muted">{t('正在查找空档…', 'Finding open slots…')}</p>
              ) : candidates.length === 0 ? (
                <EmptyState
                  title={t('该时段暂无空档', 'No open slots in this range')}
                  actions={
                    <>
                      {activePeriod !== 'all' && (
                        <button type="button" className="btn" disabled={operation.isPending}
                          onClick={() => setActivePeriod('all')}>
                          {t('查看全部', 'Show all')}
                        </button>
                      )}
                      <button type="button" className="btn primary" disabled={operation.isPending}
                        onClick={() => setShowCustomTime(true)}>
                        {t('改用自定义时间', 'Pick custom time')}
                      </button>
                    </>
                  }
                />
              ) : (
                <div>
                  <div className="field-help" style={{ marginBottom: 8 }}>
                    {t('点击时段即可安排', 'Click any slot to schedule')}
                  </div>
                  {candidates.map((cand, index) => {
                    const label = formatDateTimeLabel(cand.start, cand.end, zone, lang);
                    return (
                      <button
                        key={`${cand.start}-${index}`}
                        type="button"
                        className="slot-btn"
                        aria-label={`${label} · ${title}`}
                        disabled={operation.isPending}
                        onClick={() => {
                          setSelectedSlotIndex(index);
                          act('schedule', { target_date: cand.date, start: cand.start, minutes: defaultMinutes });
                        }}
                      >
                        <div className="slot-time">
                          {label}
                          {operation.isPending && selectedSlotIndex === index ? t('（安排中…）', ' (Scheduling…)') : ''}
                        </div>
                      </button>
                    );
                  })}
                </div>
              )}
              {occurrence && (
                <div className="actions" style={{ marginTop: 12 }}>
                  <button className="btn" type="button" onClick={() => setEditing(false)}
                    disabled={operation.isPending}>{t('返回', 'Back')}</button>
                </div>
              )}
            </div>
          ) : (
            <form onSubmit={(event) => {
              event.preventDefault();
              const local = time ? `${date}T${time}` : '';
              const utc = local ? datetimeLocalToUtcIso(local, zone) : undefined;
              if (local && !utc) {
                setValidation(t('请选择有效的开始时间', 'Choose a valid start time'));
                return;
              }
              setValidation('');
              act('schedule', { target_date: date, start: utc || undefined, minutes: time ? Number(minutes) : undefined });
            }}>
              <div className="field">
                <label className="field-label" htmlFor={`${id}-date`}>{t('日期', 'Date')}</label>
                <input id={`${id}-date`} className="input" type="date" required min={earliest} max={latest}
                  value={date} readOnly={earliest === latest} onChange={(e) => setDate(e.target.value)} />
              </div>
              <div className="field">
                <label className="field-label" htmlFor={`${id}-time`}>{t('开始时间（可选）', 'Start time (optional)')}</label>
                <input id={`${id}-time`} className="input" type="time" value={time} onChange={(e) => setTime(e.target.value)} />
              </div>
              {time ? <div className="field">
                <label className="field-label" htmlFor={`${id}-duration`}>{t('时长（分钟）', 'Duration (minutes)')}</label>
                <input id={`${id}-duration`} className="input" type="number" required min={5} max={480}
                  value={minutes} onChange={(e) => setMinutes(e.target.value === '' ? '' : Number(e.target.value))} />
              </div> : null}
              {validation && <p className="field-error" role="alert">{validation}</p>}
              <div className="actions">
                <button className="btn primary" type="submit" disabled={operation.isPending}>{t('保存安排', 'Save schedule')}</button>
                <button className="btn" type="button" onClick={() => setShowCustomTime(false)}
                  disabled={operation.isPending}>{t('返回推荐空档', 'Back to open slots')}</button>
                {occurrence && <button className="btn" type="button" onClick={() => { setShowCustomTime(false); setEditing(false); }}
                  disabled={operation.isPending}>{t('返回', 'Back')}</button>}
              </div>
            </form>
          )}
        </>
      ) : (
        <div className="actions" style={{ marginTop: 18 }}>
          {canStart && (
            <button className="btn primary" type="button" disabled={operation.isPending} onClick={() => act('start')}>
              {t('开始', 'Start')}
            </button>
          )}
          {occurrence?.can_record && occurrence.status !== 'done' && (
            <button className={canStart ? 'btn' : 'btn primary'} type="button" disabled={operation.isPending} onClick={() => act('done')}>
              {occurrence.status === 'pending' ? t('完成', 'Complete') : t('补记完成', 'Log completion')}
            </button>
          )}
          {canSchedule && (
            <>
              <button className="btn" type="button" disabled={operation.isPending} onClick={() => { setShowCustomTime(false); setEditing(true); }}>
                {start ? t('调整时间', 'Reschedule') : t('安排时间', 'Schedule')}
              </button>
              {start && <button className="btn" type="button" disabled={operation.isPending} onClick={() => act('schedule')}>
                {t('取消排期', 'Cancel schedule')}
              </button>}
              <button className="btn" type="button" disabled={operation.isPending} onClick={() => act('skip')}>
                {t('跳过本次', 'Skip this session')}
              </button>
              {occurrence?.mode === 'weekly' && !start && (
                <button className="btn" type="button" disabled={operation.isPending} onClick={() => act('cancel')}>
                  {t('取消这次安排', 'Cancel session')}
                </button>
              )}
            </>
          )}
          {occurrence?.status === 'done' && (
            <button className="btn" type="button" disabled={operation.isPending} onClick={() => act('restore')}>
              {t('撤销完成', 'Undo completion')}
            </button>
          )}
        </div>
      )}
    </Modal>
  );
}
