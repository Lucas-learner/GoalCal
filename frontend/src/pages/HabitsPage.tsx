import { useCallback, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { DateTime } from 'luxon';
import { api } from '../api/client';
import { invalidateAfterWrite, queryKeys } from '../api/queryKeys';
import type { CreateHabitIntent, Habit, HabitDetail, HabitOccurrence, HabitProgress, HabitRule, HabitStatus } from '../api/types';
import { HabitForm, useWeekdayLabels } from '../components/HabitForm';
import { HabitOccurrenceDialog, useHabitAction, useHabitLabels } from '../components/HabitOccurrenceDialog';
import { SparkleIcon } from '../components/Icons';
import { EmptyState, ErrorBanner, SkeletonCard } from '../components/StateFeedback';
import { useI18n } from '../i18n';
import { formatTimeRange } from '../lib/datetime';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useAppState } from '../state/AppStateContext';

function useRuleLabel() {
  const { t } = useI18n();
  const weekdays = useWeekdayLabels();
  return (rule: HabitRule) => {
    if (rule.mode === 'weekly') return t(`每周 ${rule.weekly_target} 次`, `${rule.weekly_target} times per week`);
    const repeat = rule.weekdays.length === 7 ? t('每天', 'Every day') : rule.weekdays.map((day) => weekdays[day]).join(' · ');
    return rule.start_time ? `${repeat} · ${rule.start_time}` : repeat;
  };
}

function useHabitStatusLabels() {
  const { t } = useI18n();
  return { active: t('进行中', 'Active'), paused: t('已暂停', 'Paused'), archived: t('已归档', 'Archived') };
}

function HabitListCard({ habit, selected }: { habit: Habit; selected: boolean }) {
  const { t } = useI18n();
  const ruleLabel = useRuleLabel();
  const next = habit.next;
  const progress = habit.progress;
  const summary = progress
    ? `${ruleLabel(habit.rule)} · ${t(`本周 ${progress.done}/${progress.target}`, `${progress.done}/${progress.target} this wk`)}`
    : next
      ? `${ruleLabel(habit.rule)} · ${t('下次', 'Next')} ${next.date.slice(5)}`
      : ruleLabel(habit.rule);
  return (
    <Link to={`/habits/${habit.id}`} className={`goal-row habit-row${selected ? ' active' : ''}`}
      aria-current={selected ? 'true' : undefined}>
      <div className="goal-row-title">
        {habit.pinned ? t('置顶 · ', 'Pinned · ') : ''}
        {habit.rule.title}
      </div>
      <div className="goal-row-meta">{summary}</div>
    </Link>
  );
}

function HabitDetails({ habit, hasExplicitSelection }: { habit: HabitDetail; hasExplicitSelection: boolean }) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const labels = useHabitLabels();
  const statusLabels = useHabitStatusLabels();
  const ruleLabel = useRuleLabel();
  const queryClient = useQueryClient();
  const { recordMutationResult } = useAppState();
  const [editOpen, setEditOpen] = useState(false);
  const [selectedItem, setSelectedItem] = useState<HabitOccurrence | null>(null);
  const [editingTime, setEditingTime] = useState(false);
  const [selectedWeek, setSelectedWeek] = useState<HabitProgress | null>(null);
  const switchStatus = useCallback((body: { status?: HabitStatus; pinned?: boolean }, key: string) =>
    api.changeHabitStatus(habit.id, { ...body, version: habit.version }, key), [habit.id, habit.version]);
  const statusChange = useIdempotentAction(switchStatus, {
    onSuccess: async (result, body) => {
      recordMutationResult(result);
      await invalidateAfterWrite(queryClient);
      if (body.status === 'archived' && hasExplicitSelection) {
        navigate('/habits', { replace: true });
      }
    },
    onError: async (error) => { if (error.status === 409) await invalidateAfterWrite(queryClient); },
  });
  const occurrenceAction = useHabitAction();
  const current = habit.progress;
  const next = habit.next;
  const weekdays = useWeekdayLabels();
  const weekItems = habit.week_items ?? [];
  const today = weekItems.find((item) => item.date === habit.today && item.status === 'pending') ?? current?.offer;
  const flexible = current?.mode === 'weekly';
  const canCompleteToday = today?.can_record && today.status === 'pending';
  const canScheduleToday = Boolean(!flexible && today && today.status === 'pending' && today.date >= habit.today);
  const remaining = current ? Math.max(0, current.target - current.done) : 0;
  const showNextOutsideWeek = Boolean(next && (!current || next.date > current.end));
  const laterScheduled = habit.scheduled.filter((item) => item.date !== next?.date && (!current || item.date > current.end));
  const renderItems = (items: HabitOccurrence[]) => items.map((item) => (
    <button key={item.id} type="button" className="habit-record" onClick={() => setSelectedItem(item)}>
      <span className="tabular-nums">{item.date}</span>
      {item.start && item.end ? <span>{formatTimeRange(item.start, item.end, habit.timezone)}</span> : null}
      <span className="chip">{labels.status[item.status]}</span>
    </button>
  ));
  return (
    <article className="card goal-detail">
      <div className="goal-detail-head">
        <div>
          <h2>{habit.rule.title}</h2>
          <div className="goal-meta"><span className="chip">{statusLabels[habit.status]}</span>
            {habit.pinned ? <> <span className="chip blue">{t('已置顶', 'Pinned')}</span></> : null}{' '}
            {ruleLabel(habit.rule)} · {habit.rule.est_min} {t('分钟', 'min')}</div>
        </div>
        <div className="habit-detail-actions">
          <button type="button" className="btn small" disabled={statusChange.isPending}
            onClick={() => void statusChange.run({ pinned: !habit.pinned })}>
            {habit.pinned ? t('取消置顶', 'Unpin') : t('置顶', 'Pin')}
          </button>
          {!editOpen && <button type="button" className="btn small" onClick={() => setEditOpen(true)}>{t('修改规则', 'Edit rule')}</button>}
          <button type="button" className="btn small" disabled={statusChange.isPending} onClick={() =>
            void statusChange.run({ status: habit.status === 'active' ? 'paused' : 'active' })}>
            {habit.status === 'active' ? t('暂停', 'Pause') : t('恢复', 'Resume')}
          </button>
          {habit.status !== 'archived' && <button type="button" className="btn small" disabled={statusChange.isPending}
            onClick={() => void statusChange.run({ status: 'archived' })}>{t('归档', 'Archive')}</button>}
        </div>
      </div>
      {habit.goal_id && habit.goal_title && (
        <p>{t('关联目标', 'Linked goal')} · {habit.goal_status === 'archived' ? habit.goal_title
          : <Link className="section-link" to={`/goals/${habit.goal_id}`}>{habit.goal_title}</Link>}</p>
      )}
      {habit.upcoming_rule && <p className="status-banner info">
        {habit.upcoming_rule.date} · {ruleLabel(habit.upcoming_rule.rule)} {t('生效', 'takes effect')}
      </p>}
      <ErrorBanner error={statusChange.actionError ?? occurrenceAction.actionError} />
      {editOpen && <HabitForm habit={habit} timezone={habit.timezone} presentation="inline" onClose={() => setEditOpen(false)} />}
      {current && <section className="habit-week">
        <div className="habit-section-heading">
          <div>
            <h3>{t('本周节奏', 'This week')}</h3>
            <p className="muted habit-week-caption">
              {t(`已完成 ${current.done} / ${current.target} 次`, `${current.done} of ${current.target} sessions completed`)}
              {flexible && (remaining > 0 ? t(` · 还差 ${remaining} 次`, ` · ${remaining} to go`) : t(' · 已达标', ' · Target met'))}
            </p>
          </div>
          {(canScheduleToday || canCompleteToday) && <div className="habit-detail-actions">
            {canScheduleToday && today && <button type="button" className="btn small" disabled={occurrenceAction.isPending}
              onClick={() => { setEditingTime(true); setSelectedItem(today); }}>
              {today.start ? t('调整时间', 'Reschedule') : t('安排时间', 'Schedule')}
            </button>}
            {canCompleteToday && today && <button type="button" className="btn primary small" disabled={occurrenceAction.isPending}
              onClick={() => void occurrenceAction.run({ id: habit.id, body: { date: today.date, version: today.version, action: 'done' } })}>
              {t('完成今天', 'Complete today')}
            </button>}
          </div>}
        </div>
        {flexible ? (
          weekItems.length > 0 ? <div>{renderItems(weekItems)}</div>
            : <p className="muted habit-week-caption">{t('本周还没有安排或记录。', 'No sessions planned or logged this week yet.')}</p>
        ) : <div className="habit-week-days">
          {weekdays.map((day, index) => {
            const date = DateTime.fromISO(current.week).plus({ days: index });
            const dateKey = date.toISODate();
            const item = weekItems.find((entry) => entry.date === dateKey);
            const isToday = dateKey === habit.today;
            const plannedTime = item?.status === 'pending' && item.start
              ? DateTime.fromISO(item.start, { zone: habit.timezone }).toFormat('HH:mm') : null;
            const contents = <>
              <span>{day} <span className="tabular-nums">{date.day}</span></span>
              <span>{plannedTime ?? (item ? labels.status[item.status] : '—')}</span>
            </>;
            return item ? (
              <button key={day} type="button" className="habit-day" data-status={item.status}
                aria-current={isToday ? 'date' : undefined} onClick={() => setSelectedItem(item)}>
                {contents}
              </button>
            ) : <div key={day} className="habit-day" aria-current={isToday ? 'date' : undefined}>{contents}</div>;
          })}
        </div>}
        {flexible && remaining > 0 && current.schedule_from && <button type="button" className="btn small" onClick={() => setSelectedWeek(current)}>
          {t('安排一次', 'Schedule a session')}
        </button>}
      </section>}
      {showNextOutsideWeek && next && <div className="habit-next">
        <div>
          <strong>{next.date === habit.today ? t('今天', 'Today') : t('下一次', 'Next session')} · {next.date}</strong>
          <div className="muted">{next.start && next.end ? formatTimeRange(next.start, next.end, habit.timezone)
            : labels.reason(next.reason) ?? t('待安排', 'Unscheduled')}</div>
        </div>
        <button type="button" className="btn small" onClick={() => {
          setEditingTime(!next.start);
          setSelectedItem(next);
        }}>{next.start ? t('查看安排', 'View session') : t('安排时间', 'Schedule')}</button>
      </div>}
      {laterScheduled.length > 0 && <details className="habit-history">
        <summary>{t('后续安排', 'Later sessions')}</summary>
        {renderItems(laterScheduled)}
      </details>}
      {selectedItem && <HabitOccurrenceDialog occurrence={
        [...weekItems, ...habit.scheduled, habit.next, current?.offer].find((item) => item?.id === selectedItem.id) ?? selectedItem
      } startInEditor={editingTime} onClose={() => { setSelectedItem(null); setEditingTime(false); }} />}
      {selectedWeek && <HabitOccurrenceDialog week={current?.week === selectedWeek.week ? current : selectedWeek}
        onClose={() => setSelectedWeek(null)} />}
    </article>
  );
}

export function NewHabitPage({ timezone }: { timezone: string }) {
  const navigate = useNavigate();
  const location = useLocation();
  const draft = (location.state as { draft?: CreateHabitIntent } | null)?.draft;
  return <HabitForm key={location.key} draft={draft} timezone={timezone} presentation="page" onClose={() => navigate('/habits')}
    onSaved={(id) => navigate(`/habits/${id}`, { replace: true })} />;
}

export function HabitsPage() {
  const { t } = useI18n();
  const { habitId } = useParams();
  const { openAssistant } = useAppState();
  const statuses = useHabitStatusLabels();
  const habits = useQuery({ queryKey: queryKeys.habits, queryFn: api.getHabits });
  const items = (habits.data?.habits ?? []).filter((habit) => habit.status !== 'archived');
  const id = habitId ? Number(habitId) : (items.find((habit) => habit.status === 'active') ?? items[0])?.id;
  const detail = useQuery({ queryKey: queryKeys.habit(id ?? 0), queryFn: () => api.getHabit(id!), enabled: id != null,
    placeholderData: (previous) => previous?.id === id ? previous : undefined });
  const selected = detail.data;
  const groups = Object.entries(statuses).filter(([status]) => status !== 'archived');
  const newHabitButton = (
    <Link className="btn primary" to="/habits/new">
      {t('+ 新习惯', '+ New habit')}
    </Link>
  );
  return (
    <div>
      <div className="topbar">
        <h1>{t('习惯', 'Habits')}</h1>
        <div className="topbar-actions">
          <button type="button" className="assistant-trigger" onClick={(event) => openAssistant(event.currentTarget)}>
            <SparkleIcon />
            <span>{t('问 GoalCal', 'Ask GoalCal')}</span>
          </button>
          {newHabitButton}
        </div>
      </div>
      <p className="subtitle">{t('给重要的小事留一点时间，让它慢慢成为日常。',
        'Make time for the small things that matter, until they become part of your day.')}</p>
      {habits.isPending ? <SkeletonCard label={t('正在加载习惯', 'Loading habits')} />
        : habits.isError ? <ErrorBanner error={habits.error} onRetry={() => void habits.refetch()} />
          : items.length === 0 && !habitId ? (
            <EmptyState title={t('从一个想坚持的习惯开始', 'Start with a habit you want to keep')}
              copy={t('比如每天阅读 30 分钟，或每周运动 3 次。', 'For example, read for 30 minutes a day or exercise three times a week.')}
              actions={newHabitButton} />
          ) : (
            <div className={`goals-layout habits-layout${habitId ? ' mobile-has-selection' : ''}`}>
              <div className="card goal-list" aria-label={t('习惯列表', 'Habits list')}>
                {groups.map(([status, label]) => {
                  const group = items.filter((habit) => habit.status === status);
                  return group.length ? <section key={status} aria-label={label}>
                    <div className="goal-list-label">{label} · {group.length}</div>
                    {group.map((habit) => <HabitListCard habit={habit} selected={id === habit.id} key={habit.id} />)}
                  </section> : null;
                })}
              </div>
              <div className="goal-detail-wrap">
                {habitId && <Link className="btn small back-list-btn" to="/habits">{t('← 返回习惯列表', '← All habits')}</Link>}
                {detail.isError ? <ErrorBanner error={detail.error} onRetry={() => void detail.refetch()} />
                  : selected ? <HabitDetails key={selected.id} habit={selected} hasExplicitSelection={Boolean(habitId)} />
                    : <SkeletonCard label={t('正在加载习惯', 'Loading habit')} />}
              </div>
            </div>
          )}
    </div>
  );
}
