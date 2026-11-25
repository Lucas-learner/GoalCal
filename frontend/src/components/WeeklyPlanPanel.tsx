import { DateTime } from 'luxon';
import type {
  WeeklyPlan,
  WeeklyPlanScope,
  WeeklyPlanStrategy,
} from '../api/types';
import { formatTimeRange } from '../lib/datetime';
import { useI18n } from '../i18n';

interface WeeklyPlanPanelProps {
  plan: WeeklyPlan;
  timezone: string;
  selectedStepIds: number[];
  includedGoalIds: number[] | null;
  busy: boolean;
  onToggleStep: (stepId: number) => void;
  onChangeParams: (next: {
    scope?: WeeklyPlanScope;
    strategy?: WeeklyPlanStrategy;
    goal_ids?: number[];
    keep_existing_blocks?: boolean;
  }) => void;
  onAccept: () => void;
  onClose: () => void;
}

function formatDuration(minutes: number, t: (zh: string, en: string) => string): string {
  if (minutes < 60) return `${minutes} ${t('分钟', 'min')}`;
  const hrs = Math.round((minutes / 60) * 10) / 10;
  return `${hrs} ${t('小时', 'h')}`;
}

export function WeeklyPlanPanel({
  plan,
  timezone,
  selectedStepIds,
  includedGoalIds,
  busy,
  onToggleStep,
  onChangeParams,
  onAccept,
  onClose,
}: WeeklyPlanPanelProps) {
  const { lang, t } = useI18n();
  const activeGoalIds = includedGoalIds ?? plan.available_goals.map((g) => g.goal_id);
  const selectedSet = new Set(selectedStepIds);
  const selectedItems = plan.items.filter((item) => selectedSet.has(item.step_id));
  const selectedMinutes = selectedItems.reduce((sum, item) => sum + item.est_min, 0);

  const toggleGoal = (goalId: number) => {
    const next = activeGoalIds.includes(goalId)
      ? activeGoalIds.filter((id) => id !== goalId)
      : [...activeGoalIds, goalId];
    onChangeParams({
      scope: (plan.scope as WeeklyPlanScope) || 'auto',
      strategy: (plan.strategy as WeeklyPlanStrategy) || 'balanced',
      goal_ids: next,
      keep_existing_blocks: plan.keep_existing_blocks,
    });
  };

  return (
    <aside className="card-flat backlog weekly-plan-panel" aria-label={t('排期预览', 'Plan preview')}>
      <div className="weekly-plan-head">
        <div>
          <div className="backlog-title">{t('排期预览', 'Plan preview')}</div>
          <div className="weekly-plan-range tabular-nums">
            {plan.week_start.slice(5)} – {plan.week_end.slice(5)}
          </div>
        </div>
        <button type="button" className="btn small" onClick={onClose} disabled={busy}>
          {t('关闭', 'Close')}
        </button>
      </div>

      <div className="weekly-plan-seg" role="group" aria-label={t('规划周期', 'Planning horizon')}>
        <button
          type="button"
          className={`calendar-seg-btn${plan.scope === 'this_week' ? ' active' : ''}`}
          disabled={busy}
          onClick={() =>
            onChangeParams({
              scope: 'this_week',
              strategy: plan.strategy as WeeklyPlanStrategy,
              goal_ids: activeGoalIds,
              keep_existing_blocks: plan.keep_existing_blocks,
            })
          }
        >
          {t('本周', 'This week')}
        </button>
        <button
          type="button"
          className={`calendar-seg-btn${plan.scope === 'next_week' ? ' active' : ''}`}
          disabled={busy}
          onClick={() =>
            onChangeParams({
              scope: 'next_week',
              strategy: plan.strategy as WeeklyPlanStrategy,
              goal_ids: activeGoalIds,
              keep_existing_blocks: plan.keep_existing_blocks,
            })
          }
        >
          {t('下周', 'Next week')}
        </button>
      </div>

      <div className="weekly-plan-summary tabular-nums">
        {t(
          `已选 ${selectedItems.length} 块 · ${formatDuration(selectedMinutes, t)}`,
          `${selectedItems.length} block(s) · ${formatDuration(selectedMinutes, t)}`,
        )}
      </div>

      {plan.items.length === 0 ? (
        <div className="weekly-plan-empty muted">
          {t('当前条件下暂无可排入的时间块。', 'No blocks fit within the current criteria.')}
        </div>
      ) : (
        <div className="weekly-plan-items">
          {plan.items.map((item) => {
            const checked = selectedSet.has(item.step_id);
            const dayLabel = DateTime.fromISO(item.start)
              .setZone(timezone)
              .setLocale(lang === 'zh' ? 'zh-CN' : 'en')
              .toFormat(lang === 'zh' ? 'M/d ccc' : 'ccc M/d');
            return (
              <label key={item.step_id} className={`weekly-plan-item${checked ? ' is-selected' : ''}`}>
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={busy}
                  onChange={() => onToggleStep(item.step_id)}
                />
                <div className="weekly-plan-item-body">
                  <div className="weekly-plan-item-title">{item.step_title}</div>
                  <div className="weekly-plan-item-meta tabular-nums">
                    {item.goal_title} · {dayLabel} {formatTimeRange(item.start, item.end, timezone)}
                  </div>
                </div>
              </label>
            );
          })}
        </div>
      )}

      {plan.unscheduled.length > 0 ? (
        <div className="weekly-plan-unscheduled">
          <div className="weekly-plan-unscheduled-title">
            {t(`未排入（${plan.unscheduled.length}）`, `Unscheduled (${plan.unscheduled.length})`)}
          </div>
          {plan.unscheduled.map((u) => (
            <div key={u.step_id} className="weekly-plan-unscheduled-row">
              <strong>
                {u.goal_title} · {u.step_title}
              </strong>
              <span>{u.reason}</span>
            </div>
          ))}
        </div>
      ) : null}

      <details className="weekly-plan-options">
        <summary>{t('筛选与选项', 'Options')}</summary>
        <div className="weekly-plan-options-body">
          <label className="weekly-plan-field">
            <span>{t('排序偏好', 'Priority')}</span>
            <select
              value={plan.strategy}
              disabled={busy}
              onChange={(e) =>
                onChangeParams({
                  scope: plan.scope as WeeklyPlanScope,
                  strategy: e.target.value as WeeklyPlanStrategy,
                  goal_ids: activeGoalIds,
                  keep_existing_blocks: plan.keep_existing_blocks,
                })
              }
            >
              <option value="balanced">{t('均衡推进（默认）', 'Balanced (Default)')}</option>
              <option value="deadline">{t('截止优先', 'Deadline first')}</option>
              <option value="pinned">{t('置顶优先', 'Pinned first')}</option>
            </select>
          </label>

          <label className="weekly-plan-checkbox">
            <input
              type="checkbox"
              checked={plan.keep_existing_blocks}
              disabled={busy}
              onChange={(e) =>
                onChangeParams({
                  scope: plan.scope as WeeklyPlanScope,
                  strategy: plan.strategy as WeeklyPlanStrategy,
                  goal_ids: activeGoalIds,
                  keep_existing_blocks: e.target.checked,
                })
              }
            />
            <span>{t('保留已有目标块', 'Keep existing scheduled blocks')}</span>
          </label>

          {plan.available_goals.length > 0 ? (
            <div className="weekly-plan-goal-chips" role="group" aria-label={t('参与排期的目标', 'Goals to schedule')}>
              {plan.available_goals.map((g) => {
                const checked = activeGoalIds.includes(g.goal_id);
                return (
                  <button
                    key={g.goal_id}
                    type="button"
                    disabled={busy}
                    aria-pressed={checked}
                    className={`calendar-filter-chip${checked ? '' : ' hidden-source'}`}
                    onClick={() => toggleGoal(g.goal_id)}
                  >
                    {g.pinned ? '★ ' : ''}
                    {g.title}
                  </button>
                );
              })}
            </div>
          ) : null}
        </div>
      </details>

      <div className="weekly-plan-actions">
        <button
          type="button"
          className="btn primary"
          disabled={busy || selectedItems.length === 0}
          onClick={onAccept}
        >
          {busy
            ? t('处理中…', 'Working…')
            : t(`采纳（${selectedItems.length} 块）`, `Accept (${selectedItems.length})`)}
        </button>
        <button type="button" className="btn" disabled={busy} onClick={onClose}>
          {t('取消', 'Cancel')}
        </button>
      </div>
    </aside>
  );
}
