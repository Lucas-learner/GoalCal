import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { invalidateAfterWrite, queryKeys } from '../api/queryKeys';
import type { CalendarEvent, Goal, Step } from '../api/types';
import { SparkleIcon } from '../components/Icons';
import { EmptyState, ErrorBanner, SkeletonCard } from '../components/StateFeedback';
import { daysUntilDeadline, formatShortDate } from '../lib/datetime';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { type Lang, pickText, useI18n } from '../i18n';
import { useAppState } from '../state/AppStateContext';

const GOAL_STATUS_LABEL: Record<string, [string, string]> = {
  active: ['进行中', 'Active'],
  paused: ['已暂停', 'Paused'],
  done: ['已完成', 'Done'],
  archived: ['已归档', 'Archived'],
};

const STEP_STATUS_LABEL: Record<string, [string, string]> = {
  todo: ['待办', 'Todo'],
  done: ['已完成', 'Done'],
  dropped: ['已放弃', 'Dropped'],
};

function computePhases(goal: Goal) {
  const names =
    goal.outline && goal.outline.length > 0
      ? goal.outline
      : Array.from(new Set(goal.steps.map((s) => s.milestone).filter(Boolean)));

  let currentAssigned = false;
  return names.map((name) => {
    const stepsInPhase = goal.steps.filter((s) => s.milestone === name);
    const done = stepsInPhase.filter((s) => s.status === 'done').length;
    const todo = stepsInPhase.filter((s) => s.status === 'todo').length;
    let state: 'done' | 'current' | 'todo' = 'todo';
    if (todo > 0 && !currentAssigned) {
      state = 'current';
      currentAssigned = true;
    } else if (todo > 0) {
      state = 'todo';
    } else if (stepsInPhase.length > 0) {
      state = 'done';
    }
    return { name, done, todo, state };
  });
}

function formatDeadlineSummary(goal: Goal, timezone: string, lang: Lang): string {
  if (!goal.deadline) return pickText(lang, '无期限', 'No deadline');
  const daysLeft = daysUntilDeadline(goal.deadline, timezone);
  const short = formatShortDate(goal.deadline, timezone, lang);
  if (daysLeft == null) return pickText(lang, `截止 ${short}`, `Due ${short}`);
  if (daysLeft < 0) {
    return pickText(
      lang,
      `截止 ${short} · 已逾期 ${Math.abs(daysLeft)} 天`,
      `Due ${short} · ${Math.abs(daysLeft)}d overdue`,
    );
  }
  if (daysLeft === 0) return pickText(lang, '今天截止', `Due today`);
  return pickText(lang, `截止 ${short} · 剩 ${daysLeft} 天`, `Due ${short} · ${daysLeft}d left`);
}

export function GoalsPage({ timezone }: { timezone: string }) {
  const { goalId: goalIdParam } = useParams<{ goalId?: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { openAssistant, setPageGoalId, openProposalModal, recordMutationResult } = useAppState();
  const { lang, t } = useI18n();

  const parsedParamId = goalIdParam ? Number(goalIdParam) : undefined;
  const hasExplicitSelection = parsedParamId != null && Number.isFinite(parsedParamId);

  const goalsQuery = useQuery({
    queryKey: queryKeys.goals,
    queryFn: api.getGoals,
  });

  const allGoals = goalsQuery.data?.goals ?? [];
  const goals = useMemo(
    () => allGoals.filter((g) => g.status !== 'archived'),
    [allGoals],
  );

  const activeGoalId = useMemo(() => {
    if (hasExplicitSelection) return parsedParamId;
    return goals[0]?.id;
  }, [hasExplicitSelection, parsedParamId, goals]);

  useEffect(() => {
    setPageGoalId(activeGoalId);
    return () => setPageGoalId(undefined);
  }, [activeGoalId, setPageGoalId]);

  const detailQuery = useQuery({
    queryKey: activeGoalId != null ? queryKeys.goal(activeGoalId) : ['goals', 'none'],
    queryFn: () => api.getGoal(activeGoalId!),
    enabled: activeGoalId != null,
  });

  const detailGoal = detailQuery.data;

  // Goal title edit state (preserved on 409)
  const [editingGoalTitle, setEditingGoalTitle] = useState(false);
  const [goalTitleDraft, setGoalTitleDraft] = useState('');

  // Step inline edit state (preserved on 409)
  const [editingStepId, setEditingStepId] = useState<number | null>(null);
  const [stepTitleDraft, setStepTitleDraft] = useState('');
  const [stepEstDraft, setStepEstDraft] = useState('30');
  const [selectedPhase, setSelectedPhase] = useState<string | null>(null);
  const [showFinishedSteps, setShowFinishedSteps] = useState(false);

  useEffect(() => {
    setSelectedPhase(null);
    setShowFinishedSteps(false);
  }, [activeGoalId]);

  const updateGoalFn = useCallback(
    async (
      args: { id: number; version: string; title?: string; status?: string; pinned?: boolean },
      key: string,
    ) => {
      const { id, ...body } = args;
      return api.updateGoal(id, body, key);
    },
    [],
  );

  const updateGoalAction = useIdempotentAction(updateGoalFn, {
    onSuccess: async (res, args) => {
      recordMutationResult(res);
      if (args.title != null) {
        setEditingGoalTitle(false);
      }
      await invalidateAfterWrite(queryClient, { goalId: args.id });
      if (args.status === 'archived' && hasExplicitSelection) {
        navigate('/goals', { replace: true });
      }
    },
    onError: async (err, args) => {
      if (err.status === 409) {
        await invalidateAfterWrite(queryClient, { goalId: args.id });
      }
    },
  });

  const updateStepFn = useCallback(
    async (
      args: {
        goalId: number;
        stepId: number;
        version: string;
        title?: string;
        est_min?: number;
        status?: 'todo' | 'done' | 'dropped';
      },
      key: string,
    ) => {
      const { stepId, version, title, est_min, status } = args;
      return api.updateStep(stepId, { version, title, est_min, status }, key);
    },
    [],
  );

  const updateStepAction = useIdempotentAction(updateStepFn, {
    onSuccess: async (res, args) => {
      recordMutationResult(res);
      if (args.title != null || args.est_min != null) {
        setEditingStepId(null);
      }
      await invalidateAfterWrite(queryClient, { goalId: args.goalId });
    },
    onError: async (err, args) => {
      if (err.status === 409) {
        await invalidateAfterWrite(queryClient, { goalId: args.goalId });
      }
    },
  });

  const proposeFn = useCallback(
    async (stepId: number, key: string) => api.createStepProposals(stepId, {}, key),
    [],
  );

  const proposeAction = useIdempotentAction(proposeFn, {
    onSuccess: async (proposal) => {
      await invalidateAfterWrite(queryClient, { goalId: proposal.goal_id });
      openProposalModal(proposal);
    },
  });

  const startStepFn = useCallback(
    async (args: { stepId: number; version: string; goalId: number; event?: CalendarEvent | null }, key: string) =>
      args.event ? api.startEvent(args.event.id, { version: args.event.version }, key)
        : api.startStep(args.stepId, { version: args.version }, key),
    [],
  );

  const startStepAction = useIdempotentAction(startStepFn, {
    onSuccess: async (res, args) => {
      recordMutationResult(res);
      await invalidateAfterWrite(queryClient, { goalId: args.goalId });
      navigate('/');
    },
    onError: async (err, args) => {
      if (err.status === 409) {
        await invalidateAfterWrite(queryClient, { goalId: args.goalId });
      }
    },
  });

  const refillFn = useCallback(
    (args: { id: number; version: string }, key: string) => api.refillGoal(args.id, { version: args.version }, key), [],
  );
  const refillAction = useIdempotentAction(refillFn, {
    onSuccess: async (res, args) => {
      recordMutationResult(res);
      await invalidateAfterWrite(queryClient, { goalId: args.id });
    },
    onError: async (err, args) => {
      if (err.status === 409) await invalidateAfterWrite(queryClient, { goalId: args.id });
    },
  });

  const beginEditStep = (step: Step) => {
    setEditingStepId(step.id);
    setStepTitleDraft(step.title);
    setStepEstDraft(String(step.est_min));
  };

  const anyActionError =
    updateGoalAction.actionError ??
    updateStepAction.actionError ??
    proposeAction.actionError ??
    startStepAction.actionError ?? refillAction.actionError;

  const isBusy =
    updateGoalAction.isPending ||
    updateStepAction.isPending ||
    proposeAction.isPending ||
    startStepAction.isPending || refillAction.isPending;

  const phases = detailGoal ? computePhases(detailGoal) : [];
  const nextTodoStep = detailGoal?.steps.find((s) => s.status === 'todo') ?? null;

  return (
    <div>
      <div className="topbar">
        <h1>{t('目标', 'Goals')}</h1>
        <div className="topbar-actions">
          <button
            type="button"
            className="assistant-trigger"
            onClick={(e) => openAssistant(e.currentTarget)}
          >
            <SparkleIcon />
            <span>{t('问 GoalCal', 'Ask GoalCal')}</span>
          </button>
          <Link className="btn primary" to="/goals/new">
            {t('+ 新目标', '+ New goal')}
          </Link>
        </div>
      </div>

      <p className="subtitle">
        {t(
          '不必一次看清全部，只需要知道每个目标的下一步。',
          "You don't need the whole roadmap upfront — just the clear next step for each goal.",
        )}
      </p>

      {goalsQuery.isLoading ? (
        <SkeletonCard label={t('正在加载目标列表', 'Loading goals')} />
      ) : goalsQuery.isError ? (
        <ErrorBanner error={goalsQuery.error} onRetry={() => void goalsQuery.refetch()} />
      ) : goals.length === 0 && !hasExplicitSelection ? (
        <EmptyState
          title={t('从一个想实现的结果开始', 'Start with an outcome that matters')}
          copy={t(
            '把你想要推进的长期目标告诉我，先生成可编辑的步骤草稿，再按自己的节奏排进日历。',
            "Tell me what you're working toward. I'll draft editable steps you can schedule at your own pace.",
          )}
          actions={
            <Link to="/goals/new" className="btn primary">
              {t('+ 新目标', '+ New goal')}
            </Link>
          }
        />
      ) : (
        <div
          className={`goals-layout${hasExplicitSelection ? ' mobile-has-selection' : ''}`}
        >
          <div className="card goal-list" aria-label={t('目标列表', 'Goals list')}>
            <div className="goal-list-label">
              {t('目标', 'Goals')} · {goals.length}
            </div>
            {goals.map((g) => {
              const doneCount = g.steps.filter((s) => s.status === 'done').length;
              const pct = Math.round((g.progress ?? 0) * 100);
              const isSelected = activeGoalId === g.id;
              const statusPair = GOAL_STATUS_LABEL[g.status];
              return (
                <button
                  key={g.id}
                  type="button"
                  className={`goal-row${isSelected ? ' active' : ''}`}
                  aria-current={isSelected ? 'true' : undefined}
                  onClick={() => navigate(`/goals/${g.id}`)}
                >
                  <div className="goal-row-title">
                    {g.pinned ? t('置顶 · ', 'Pinned · ') : ''}
                    {g.title}
                  </div>
                  <div className="goal-row-meta">
                    {statusPair ? t(statusPair[0], statusPair[1]) : g.status} · {formatDeadlineSummary(g, timezone, lang)}
                  </div>
                  <div
                    className="progress"
                    role="progressbar"
                    aria-label={t(`${g.title} 进度`, `${g.title} progress`)}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={pct}
                  >
                    <span style={{ width: `${pct}%` }} />
                  </div>
                  <div className="goal-row-foot">
                    <span>
                      {doneCount} / {g.steps.length} {t('步', 'steps')}
                    </span>
                    <span>{pct}%</span>
                  </div>
                </button>
              );
            })}
          </div>

          <div className="goal-detail-wrap">
            {hasExplicitSelection ? (
              <button
                type="button"
                className="btn small back-list-btn"
                onClick={() => navigate('/goals')}
              >
                {t('← 返回目标列表', '← All goals')}
              </button>
            ) : null}

            {detailQuery.isLoading ? (
              <SkeletonCard label={t('正在加载目标详情', 'Loading goal details')} />
            ) : detailQuery.isError ? (
              <EmptyState
                title={t('无法显示该目标', 'Unable to load goal')}
                copy={detailQuery.error.message || t('目标不存在或已被归档。', 'Goal not found or archived.')}
                actions={
                  <>
                    <button
                      type="button"
                      className="btn"
                      onClick={() => void detailQuery.refetch()}
                    >
                      {t('重试', 'Retry')}
                    </button>
                    <Link to="/goals" className="btn primary">
                      {t('返回目标列表', 'Back to goals')}
                    </Link>
                  </>
                }
              />
            ) : detailGoal ? (
              <section className="card goal-detail" aria-label={detailGoal.title}>
                <ErrorBanner
                  error={anyActionError}
                  onRefresh={() => void invalidateAfterWrite(queryClient, { goalId: detailGoal.id })}
                />

                <div className="goal-detail-head">
                  <div style={{ minWidth: 0, flex: 1 }}>
                    {editingGoalTitle ? (
                      <form
                        style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}
                        onSubmit={(e) => {
                          e.preventDefault();
                          const trimmed = goalTitleDraft.trim();
                          if (!trimmed) return;
                          void updateGoalAction.run({
                            id: detailGoal.id,
                            version: detailGoal.version,
                            title: trimmed,
                          });
                        }}
                      >
                        <label className="sr-only" htmlFor="edit-goal-title-input">
                          {t('目标标题', 'Goal title')}
                        </label>
                        <input
                          id="edit-goal-title-input"
                          type="text"
                          autoFocus
                          className="input"
                          style={{ flex: 1, minWidth: 200 }}
                          value={goalTitleDraft}
                          onChange={(e) => setGoalTitleDraft(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === 'Escape') {
                              setEditingGoalTitle(false);
                            }
                          }}
                          disabled={isBusy}
                        />
                        <button type="submit" className="btn small primary" disabled={isBusy}>
                          {t('保存', 'Save')}
                        </button>
                        <button
                          type="button"
                          className="btn small"
                          disabled={isBusy}
                          onClick={() => setEditingGoalTitle(false)}
                        >
                          {t('取消', 'Cancel')}
                        </button>
                      </form>
                    ) : (
                      <h2>
                        <button
                          type="button"
                          className="goal-title-trigger"
                          title={t('点击修改标题', 'Click to rename')}
                          disabled={isBusy}
                          onClick={() => {
                            setGoalTitleDraft(detailGoal.title);
                            setEditingGoalTitle(true);
                          }}
                        >
                          <span>{detailGoal.title}</span>
                          <span className="goal-title-edit-icon" aria-hidden="true">✎</span>
                        </button>
                      </h2>
                    )}

                    <div className="goal-meta">
                      <span className="chip" style={{ marginRight: 8 }}>
                        {GOAL_STATUS_LABEL[detailGoal.status]
                          ? t(GOAL_STATUS_LABEL[detailGoal.status][0], GOAL_STATUS_LABEL[detailGoal.status][1])
                          : detailGoal.status}
                      </span>
                      {detailGoal.pinned ? (
                        <span className="chip blue" style={{ marginRight: 8 }}>
                          {t('已置顶', 'Pinned')}
                        </span>
                      ) : null}
                      <span>{formatDeadlineSummary(detailGoal, timezone, lang)}</span>
                    </div>
                  </div>

                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                    <button
                      type="button"
                      className="btn small"
                      disabled={isBusy}
                      onClick={() =>
                        void updateGoalAction.run({
                          id: detailGoal.id,
                          version: detailGoal.version,
                          pinned: !detailGoal.pinned,
                        })
                      }
                    >
                      {detailGoal.pinned ? t('取消置顶', 'Unpin') : t('置顶', 'Pin')}
                    </button>
                    {detailGoal.status === 'paused' ? (
                      <button
                        type="button"
                        className="btn small"
                        disabled={isBusy}
                        onClick={() =>
                          void updateGoalAction.run({
                            id: detailGoal.id,
                            version: detailGoal.version,
                            status: 'active',
                          })
                        }
                      >
                        {t('恢复', 'Resume')}
                      </button>
                    ) : null}
                    <button
                      type="button"
                      className="btn small"
                      disabled={isBusy}
                      onClick={() =>
                        void updateGoalAction.run({
                          id: detailGoal.id,
                          version: detailGoal.version,
                          status: 'archived',
                        })
                      }
                    >
                      {t('归档', 'Archive')}
                    </button>
                  </div>
                </div>

                {detailGoal.outcome && detailGoal.outcome !== detailGoal.title ? (
                  <p className="muted" style={{ fontSize: 12, marginTop: 10 }}>
                    {t('目标结果：', 'Outcome: ')}
                    {detailGoal.outcome}
                  </p>
                ) : null}

                {(() => {
                  const pct = Math.round((detailGoal.progress ?? 0) * 100);
                  const doneSteps = detailGoal.steps.filter((s) => s.status === 'done').length;
                  return (
                    <div className="goal-progress">
                      <div
                        className="progress"
                        role="progressbar"
                        aria-label={t(`${detailGoal.title} 整体进度`, `${detailGoal.title} overall progress`)}
                        aria-valuemin={0}
                        aria-valuemax={100}
                        aria-valuenow={pct}
                      >
                        <span style={{ width: `${pct}%` }} />
                      </div>
                      <div className="goal-progress-text">
                        {doneSteps} / {detailGoal.steps.length} {t('步', 'steps')} · {pct}%
                      </div>
                    </div>
                  );
                })()}

                {nextTodoStep && detailGoal.status === 'active' ? (
                  <div className="next-box">
                    <div className="section-label">{t('下一步', 'Next step')}</div>
                    <div className="next-title">{nextTodoStep.title}</div>
                    <div className="next-meta">
                      {nextTodoStep.est_min} {t('分钟', 'min')}
                      {nextTodoStep.outcome ? `${t(' · 完成标准：', ' · Done when: ')}${nextTodoStep.outcome}` : ''}
                    </div>
                    <div className="next-actions">
                      <button
                        type="button"
                        className="btn primary"
                        disabled={isBusy}
                        onClick={() =>
                          void startStepAction.run({
                            event: nextTodoStep.scheduled_event,
                            stepId: nextTodoStep.id,
                            version: nextTodoStep.version,
                            goalId: detailGoal.id,
                          })
                        }
                      >
                        {t('开始', 'Start')}
                      </button>
                      <button
                        type="button"
                        className="btn"
                        disabled={isBusy}
                        onClick={() =>
                          void updateStepAction.run({
                            goalId: detailGoal.id,
                            stepId: nextTodoStep.id,
                            version: nextTodoStep.version,
                            status: 'done',
                          })
                        }
                      >
                        {t('完成', 'Done')}
                      </button>
                      <button
                        type="button"
                        className="btn"
                        disabled={isBusy}
                        onClick={() => void proposeAction.run(nextTodoStep.id)}
                      >
                        {t('找时间', 'Find time')}
                      </button>
                    </div>
                  </div>
                ) : null}

                {phases.length > 0 ? (
                  <div className="phase-section">
                    <div className="section-head">
                      <span className="section-title">{t('阶段', 'Phases')}</span>
                      <span className="muted" style={{ fontSize: 11 }}>
                        {t(`${phases.length} 个阶段 · 点击筛选`, `${phases.length} phases · click to filter`)}
                      </span>
                    </div>
                    {phases.map((p) => {
                      const hasSteps = p.done + p.todo > 0;
                      const isPhaseSelected = selectedPhase === p.name;
                      const inner = (
                        <>
                          <span className="phase-dot" aria-hidden="true">
                            {p.state === 'done' ? '✓' : ''}
                          </span>
                          <span>
                            {p.name}
                            {p.state === 'current' ? t('（当前）', ' (current)') : ''}
                          </span>
                          <span className="phase-meta">
                            {p.state === 'done'
                              ? t(`已完成 ${p.done} 步`, `${p.done} done`)
                              : hasSteps
                                ? t(`${p.done} / ${p.done + p.todo} 步`, `${p.done} / ${p.done + p.todo} steps`)
                                : t('当前阶段完成后生成', 'Generates after current phase')}
                          </span>
                        </>
                      );
                      return hasSteps ? (
                        <button
                          key={p.name}
                          type="button"
                          className={`phase phase-btn ${p.state}${isPhaseSelected ? ' selected' : ''}`}
                          aria-pressed={isPhaseSelected}
                          onClick={() => setSelectedPhase((prev) => (prev === p.name ? null : p.name))}
                        >
                          {inner}
                        </button>
                      ) : (
                        <div key={p.name} className={`phase ${p.state}`}>
                          {inner}
                        </div>
                      );
                    })}
                  </div>
                ) : null}

                {detailGoal.status === 'active' && !nextTodoStep &&
                  detailGoal.outline?.some((phase) => !detailGoal.steps.some((s) => s.milestone === phase)) ? (
                  <div className="actions">
                    <button type="button" className="btn primary" disabled={isBusy}
                      onClick={() => void refillAction.run({ id: detailGoal.id, version: detailGoal.version })}>
                      {t('生成下一批步骤', 'Generate next batch')}
                    </button>
                  </div>
                ) : null}
                {(() => {
                  const phaseSteps = selectedPhase
                    ? detailGoal.steps.filter((s) => s.milestone === selectedPhase)
                    : detailGoal.steps;
                  const todoSteps = phaseSteps.filter((s) => s.status === 'todo');
                  const finishedSteps = phaseSteps.filter((s) => s.status !== 'todo');
                  const visibleSteps = selectedPhase
                    ? phaseSteps
                    : showFinishedSteps
                      ? [...todoSteps, ...finishedSteps]
                      : todoSteps;

                  return (
                    <div className="steps-section">
                      <div className="section-head">
                        <span className="section-title">
                          {selectedPhase ? `${t('步骤', 'Steps')} · ${selectedPhase}` : t('待办步骤', 'Todo steps')}
                        </span>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                          {selectedPhase ? (
                            <button
                              type="button"
                              className="inline-link-btn primary-link"
                              onClick={() => setSelectedPhase(null)}
                            >
                              {t('只看待办', 'Todo only')}
                            </button>
                          ) : null}
                          <span className="muted" style={{ fontSize: 11 }}>
                            {selectedPhase
                              ? t(`共 ${phaseSteps.length} 步`, `${phaseSteps.length} steps`)
                              : t(
                                  `${todoSteps.length} 步待办 · ${finishedSteps.length} 步已完成`,
                                  `${todoSteps.length} todo · ${finishedSteps.length} finished`,
                                )}
                          </span>
                        </div>
                      </div>

                      {detailGoal.steps.length === 0 ? (
                        <div className="muted" style={{ fontSize: 12 }}>
                          {t('该目标暂时还没有步骤。', 'No steps in this goal yet.')}
                        </div>
                      ) : visibleSteps.length === 0 && !selectedPhase ? (
                        <div className="muted" style={{ fontSize: 12 }}>
                          {t('暂无待办步骤', 'No pending steps right now.')}
                        </div>
                      ) : (
                        visibleSteps.map((step) => {
                          const isDone = step.status === 'done';
                          const isDropped = step.status === 'dropped';
                          const isEditing = editingStepId === step.id;
                          const stepStatusPair = STEP_STATUS_LABEL[step.status];

                          return (
                            <div
                              key={step.id}
                              className={`step-row${isDone ? ' done' : ''}${
                                isDropped ? ' dropped' : ''
                              }`}
                            >
                              <button
                                type="button"
                                className="step-check-btn"
                                aria-label={`${isDone ? t('取消完成', 'Mark todo') : t('标记完成', 'Mark done')}: ${step.title}`}
                                aria-pressed={isDone}
                                disabled={isBusy || isDropped}
                                onClick={() =>
                                  void updateStepAction.run({
                                    goalId: detailGoal.id,
                                    stepId: step.id,
                                    version: step.version,
                                    status: isDone ? 'todo' : 'done',
                                  })
                                }
                              >
                                <span className="step-check" aria-hidden="true">
                                  {isDone ? '✓' : ''}
                                </span>
                              </button>

                              <div style={{ minWidth: 0 }}>
                                {isEditing ? (
                                  <form
                                    style={{ display: 'grid', gap: 8 }}
                                    onSubmit={(e) => {
                                      e.preventDefault();
                                      const trimmed = stepTitleDraft.trim();
                                      const estNum = Number(stepEstDraft);
                                      if (!trimmed || !Number.isFinite(estNum) || estNum < 5) return;
                                      void updateStepAction.run({
                                        goalId: detailGoal.id,
                                        stepId: step.id,
                                        version: step.version,
                                        title: trimmed,
                                        est_min: Math.round(estNum),
                                      });
                                    }}
                                  >
                                    <label className="sr-only" htmlFor={`step-title-${step.id}`}>
                                      {t('步骤名称', 'Step title')}
                                    </label>
                                    <input
                                      id={`step-title-${step.id}`}
                                      type="text"
                                      className="input"
                                      value={stepTitleDraft}
                                      onChange={(e) => setStepTitleDraft(e.target.value)}
                                      disabled={isBusy}
                                    />
                                    <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                                      <label htmlFor={`step-est-${step.id}`} style={{ fontSize: 11 }}>
                                        {t('预计分钟', 'Est. min')}
                                      </label>
                                      <input
                                        id={`step-est-${step.id}`}
                                        type="number"
                                        min={5}
                                        max={240}
                                        className="input time-input-small"
                                        value={stepEstDraft}
                                        onChange={(e) => setStepEstDraft(e.target.value)}
                                        disabled={isBusy}
                                      />
                                      <button type="submit" className="btn small primary" disabled={isBusy}>
                                        {t('保存', 'Save')}
                                      </button>
                                      <button
                                        type="button"
                                        className="btn small"
                                        disabled={isBusy}
                                        onClick={() => setEditingStepId(null)}
                                      >
                                        {t('取消', 'Cancel')}
                                      </button>
                                    </div>
                                  </form>
                                ) : (
                                  <>
                                    <div className="step-title">{step.title}</div>
                                    <div className="step-meta">
                                      {step.est_min} {t('分钟', 'min')}
                                      {step.status !== 'todo'
                                        ? ` · ${stepStatusPair ? t(stepStatusPair[0], stepStatusPair[1]) : step.status}`
                                        : ''}
                                      {step.outcome ? ` · ${step.outcome}` : ''}
                                    </div>
                                  </>
                                )}
                              </div>

                              {!isEditing ? (
                                <div className="step-actions">
                                  {step.status === 'todo' ? (
                                    <button
                                      type="button"
                                      className="inline-link-btn primary-link"
                                      disabled={isBusy}
                                      onClick={() => void proposeAction.run(step.id)}
                                    >
                                      {t('找时间', 'Find time')}
                                    </button>
                                  ) : null}
                                  <button
                                    type="button"
                                    className="inline-link-btn"
                                    disabled={isBusy}
                                    onClick={() => beginEditStep(step)}
                                  >
                                    {t('编辑', 'Edit')}
                                  </button>
                                  {step.status === 'todo' ? (
                                    <button
                                      type="button"
                                      className="inline-link-btn"
                                      disabled={isBusy}
                                      onClick={() =>
                                        void updateStepAction.run({
                                          goalId: detailGoal.id,
                                          stepId: step.id,
                                          version: step.version,
                                          status: 'dropped',
                                        })
                                      }
                                    >
                                      {t('放弃', 'Drop')}
                                    </button>
                                  ) : null}
                                </div>
                              ) : null}
                            </div>
                          );
                        })
                      )}

                      {!selectedPhase && finishedSteps.length > 0 ? (
                        <div className="steps-history-bar">
                          <span className="muted" style={{ fontSize: 11 }}>
                            {t(
                              `已完成 / 已放弃 ${finishedSteps.length} 步`,
                              `${finishedSteps.length} finished / dropped`,
                            )}
                          </span>
                          <button
                            type="button"
                            className="inline-link-btn"
                            onClick={() => setShowFinishedSteps((prev) => !prev)}
                          >
                            {showFinishedSteps
                              ? t('收起已完成', 'Hide finished')
                              : t(`展开查看 (${finishedSteps.length})`, `Show (${finishedSteps.length})`)}
                          </button>
                        </div>
                      ) : null}
                    </div>
                  );
                })()}
              </section>
            ) : null}
          </div>
        </div>
      )}
    </div>
  );
}
