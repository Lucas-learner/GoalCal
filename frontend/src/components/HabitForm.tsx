import { useCallback, useId, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { invalidateAfterWrite, queryKeys } from '../api/queryKeys';
import type { CreateHabitIntent, Habit, HabitPreview, HabitRule } from '../api/types';
import { useI18n } from '../i18n';
import { formatTimeRange, todayIsoDateInZone } from '../lib/datetime';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useAppState } from '../state/AppStateContext';
import { useHabitLabels } from './HabitOccurrenceDialog';
import { Modal } from './Modal';
import { ErrorBanner } from './StateFeedback';

type RuleForm = HabitRule & { start_date: string; goal_id: number | null };
const EVERY_DAY = [0, 1, 2, 3, 4, 5, 6];

export function useWeekdayLabels() {
  const { lang } = useI18n();
  return lang === 'zh' ? ['周一', '周二', '周三', '周四', '周五', '周六', '周日'] : ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
}

export function HabitForm({ habit, timezone, initial, draft, onClose, onSaved, presentation = 'modal' }: {
  habit?: Habit;
  timezone: string;
  initial?: { date: string; time: string; minutes: number };
  draft?: CreateHabitIntent;
  onClose: () => void;
  onSaved?: (id: number) => void;
  presentation?: 'modal' | 'page' | 'inline';
}) {
  const { t } = useI18n();
  const labels = useHabitLabels();
  const weekdays = useWeekdayLabels();
  const id = useId();
  const queryClient = useQueryClient();
  const { recordMutationResult } = useAppState();
  const today = habit?.today || todayIsoDateInZone(timezone);
  const original = habit?.upcoming_rule?.rule ?? habit?.rule;
  const page = presentation === 'page';
  const [values, setValues] = useState<RuleForm>(() => ({
    ...(original ?? {
      title: draft?.title ?? '',
      mode: draft?.weekly_target ? 'weekly' : 'fixed',
      weekdays: draft?.weekdays ?? EVERY_DAY,
      est_min: draft?.est_min ?? initial?.minutes ?? 30,
      start_time: draft?.start_time ?? (initial?.time || null),
      weekly_target: draft?.weekly_target ?? null,
    }),
    start_date: habit?.start_date ?? (initial && initial.date >= today ? initial.date : today),
    goal_id: habit?.goal_id ?? null,
  }));
  const [choice, setChoice] = useState<'daily' | 'days' | 'weekly'>(
    values.mode === 'weekly' ? 'weekly' : values.weekdays.length < 7 ? 'days' : 'daily',
  );
  const [preview, setPreview] = useState<HabitPreview | null>(null);
  const [edited, setEdited] = useState(false);
  const [validation, setValidation] = useState('');
  const goals = useQuery({ queryKey: queryKeys.goals, queryFn: api.getGoals });
  const update = (fields: Partial<RuleForm>) => {
    setValues((previous) => ({ ...previous, ...fields }));
    setEdited(true);
    setPreview(null);
  };

  const previewCall = useCallback((body: RuleForm & { habit_id?: number }, key: string) =>
    api.previewHabit(body, key), []);
  const previewAction = useIdempotentAction(previewCall, { onSuccess: setPreview });
  const saveCall = useCallback((args: { values: RuleForm; effective?: string }, key: string) => {
    if (!habit) return api.createHabit(args.values, key);
    const { start_date, ...definition } = args.values;
    return api.updateHabit(
      habit.id,
      { ...definition, ...(args.effective ? { effective_date: args.effective } : {}), version: habit.version },
      key,
    );
  }, [habit]);
  const saveAction = useIdempotentAction(saveCall, {
    onSuccess: async (result) => {
      recordMutationResult(result);
      await invalidateAfterWrite(queryClient);
      if (onSaved) onSaved(result.habit.id);
      else onClose();
    },
    onError: async (error) => {
      if (error.status === 409) {
        setPreview(null);
        await invalidateAfterWrite(queryClient);
      }
    },
  });

  const busy = saveAction.isPending || previewAction.isPending;
  const close = () => { if (!busy) onClose(); };

  const cadence = {
    daily: t('每天', 'Every day'),
    days: t('指定星期', 'Selected weekdays'),
    weekly: t('每周几次', 'Times per week'),
  };
  const title = habit ? t('修改规则', 'Edit rule') : t('新习惯', 'New habit');
  const settings = (
    <div className="habit-settings-fields">
      {choice !== 'weekly' && <div className="field">
        <label className="field-label" htmlFor={`${id}-time`}>{t('默认时间（可选）', 'Default time (optional)')}</label>
        <input id={`${id}-time`} className="input" type="time" value={values.start_time ?? ''}
          onChange={(e) => update({ start_time: e.target.value || null })} />
      </div>}
      {!habit && <div className="field">
        <label className="field-label" htmlFor={`${id}-start`}>{t('开始日期', 'Start date')}</label>
        <input id={`${id}-start`} className="input" type="date" min={today} required
          value={values.start_date} onChange={(e) => update({ start_date: e.target.value })} />
      </div>}
      <div className="field">
        <label className="field-label" htmlFor={`${id}-minutes`}>{t('预计时长（分钟）', 'Estimated minutes')}</label>
        <input id={`${id}-minutes`} className="input" type="number" min={5} max={480} required
          value={values.est_min || ''} onChange={(e) => update({ est_min: Number(e.target.value) })} />
      </div>
      <div className="field">
        <label className="field-label" htmlFor={`${id}-goal`}>{t('关联目标（可选）', 'Linked goal (optional)')}</label>
        <select id={`${id}-goal`} className="input" value={values.goal_id ?? ''}
          onChange={(e) => update({ goal_id: e.target.value ? Number(e.target.value) : null })}>
          <option value="">{t('不关联', 'No linked goal')}</option>
          {(goals.data?.goals ?? []).filter((goal) => goal.status === 'active').map((goal) =>
            <option key={goal.id} value={goal.id}>{goal.title}</option>)}
          {habit?.goal_id && !(goals.data?.goals ?? []).some((goal) => goal.id === habit.goal_id && goal.status === 'active')
            ? <option value={habit.goal_id}>{habit.goal_title}</option> : null}
        </select>
      </div>
    </div>
  );
  const form = (
    <form className={presentation === 'modal' ? undefined : 'card goal-form'} onSubmit={(event) => {
      event.preventDefault();
      const title = values.title.trim();
      if (!title || (choice === 'days' && !values.weekdays.length)) {
        setValidation(!title ? t('请输入习惯名称', 'Enter a habit name') : t('至少选择一天', 'Select at least one day'));
        return;
      }
      setValidation('');
      const normalized: RuleForm = {
        ...values, title, mode: choice === 'weekly' ? 'weekly' : 'fixed',
        weekdays: choice === 'daily' ? EVERY_DAY : choice === 'days' ? values.weekdays : [],
        start_time: choice === 'weekly' ? null : values.start_time,
        weekly_target: choice === 'weekly' ? values.weekly_target : null,
      };
      if (habit) void saveAction.run({ values: normalized });
      else if (preview) void saveAction.run({ values: normalized, effective: preview.effective_date });
      else void previewAction.run(normalized);
    }}>
      <ErrorBanner error={saveAction.actionError ?? previewAction.actionError} />
      {!(page && preview) && <fieldset disabled={busy} className="habit-fields">
        <div className="field">
          <label className="field-label" htmlFor={`${id}-title`}>{t('名称', 'Name')}</label>
          <input id={`${id}-title`} className="input" maxLength={200} required
            placeholder={page ? t('比如：跑步、阅读、练钢琴', 'e.g. Run, read, practice piano') : undefined}
            value={values.title} onChange={(e) => update({ title: e.target.value })} />
        </div>
        <fieldset className="field habit-fields">
          <legend className="field-label">{t('重复频率', 'Repeat pattern')}</legend>
          <div className="deadline-options">
            {(Object.keys(cadence) as (keyof typeof cadence)[]).map((option) => (
              <button key={option} type="button" className={`deadline-option${choice === option ? ' selected' : ''}`}
                aria-pressed={choice === option} onClick={() => {
                  setChoice(option);
                  update(option === 'weekly' ? { weekly_target: values.weekly_target ?? 3 }
                    : option === 'days' && values.weekdays.length === 7 ? { weekdays: [0, 2, 4] } : {});
                }}>{cadence[option]}</button>
            ))}
          </div>
        </fieldset>
        {choice === 'days' && <fieldset className="habit-fields habit-days">
          <legend className="field-label">{t('选择星期', 'Days of the week')}</legend>
          {weekdays.map((day, index) => <label key={day}>
            <input type="checkbox" checked={values.weekdays.includes(index)} onChange={(e) =>
              update({ weekdays: e.target.checked ? [...values.weekdays, index] : values.weekdays.filter((x) => x !== index) })} />
            {day}
          </label>)}
        </fieldset>}
        {choice === 'weekly' ? <div className="field">
          <label className="field-label" htmlFor={`${id}-target`}>{t('每周次数', 'Times per week')}</label>
          <input id={`${id}-target`} className="input" type="number" min={1} max={7} required
            value={values.weekly_target ?? ''} onChange={(e) => update({ weekly_target: Number(e.target.value) || null })} />
        </div> : null}
        {page ? <details className="habit-settings" open={draft?.start_time || draft?.est_min ? true : undefined}
          onInvalid={(event) => { event.currentTarget.open = true; }}>
          <summary>{t('时间与关联目标', 'Time & linked goal')}</summary>
          {settings}
        </details> : settings}
      </fieldset>}
      {validation && <p className="field-error" role="alert">{validation}</p>}
      {preview && <div className="card-flat habit-preview" role="status">
        {page && <>
          <h2>{values.title.trim()}</h2>
          <p>{choice === 'days' ? values.weekdays.slice().sort().map((day) => weekdays[day]).join(' · ')
            : choice === 'weekly'
              ? t(`每周 ${values.weekly_target} 次`, `${values.weekly_target} times a week`) : cadence.daily}
            {' · '}{t(`每次 ${values.est_min} 分钟`, `${values.est_min} min per session`)}</p>
        </>}
        <strong>{t('安排预览', 'Schedule preview')}</strong>
        <p>{t('生效日期', 'Effective date')} · {preview.effective_date}</p>
        {preview.weekly_target ? <p>{page
          ? t('具体日期可以灵活安排。', 'Choose which days as you go.')
          : t(`每周 ${preview.weekly_target} 次`, `${preview.weekly_target} times a week`)}</p> : null}
        {preview.slots.length > 0 && <ul>{preview.slots.map((slot) =>
          <li key={slot.date}>{slot.date} · {slot.reason ? labels.reason(slot.reason)
            : slot.start && slot.end ? formatTimeRange(slot.start, slot.end, timezone) : t('待安排', 'Unscheduled')}</li>)}</ul>}
        {!!preview.replaces.length && <p>{t('将替换已调整的安排', 'Replaces adjusted sessions')} · {preview.replaces.join(', ')}</p>}
      </div>}
      <div className="goal-form-actions" style={{ justifyContent: 'flex-end' }}>
        <div className="goal-form-buttons">
          <button type="button" className="btn" disabled={busy} onClick={() => {
            if (page && preview) { setPreview(null); return; }
            if (!edited || window.confirm(t('放弃未保存的修改？', 'Discard unsaved edits?'))) close();
          }}>{page && preview ? t('返回修改', 'Back to edit') : t('取消', 'Cancel')}</button>
          <button type="submit" className="btn primary" disabled={busy}>
            {busy ? t('处理中…', 'Working…') : habit ? t('保存修改', 'Save changes') : preview ? t('保存习惯', 'Save habit') : t('预览安排', 'Preview schedule')}
          </button>
        </div>
      </div>
    </form>
  );
  if (presentation === 'modal') {
    return <Modal title={title} onClose={close} hasUnsavedChanges={edited && !busy}>{form}</Modal>;
  }
  if (presentation === 'inline') return form;
  return (
    <div className="new-goal-wrap">
      <div className="topbar"><h1>{preview ? t('确认习惯安排', 'Review habit schedule') : t('你想坚持什么？', 'What do you want to keep doing?')}</h1></div>
      <p className="subtitle">{preview ? t('确认日期与时段后保存。', 'Review the dates and times before saving.')
        : t('写下想重复做的事，选一个适合自己的节奏。', 'Name what you want to keep doing and choose a rhythm that fits.')}</p>
      {form}
    </div>
  );
}
