import { useCallback, useEffect, useMemo, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { invalidateAfterWrite, queryKeys } from '../api/queryKeys';
import type { CalendarEvent, Goal, Step } from '../api/types';
import { SparkleIcon } from '../components/Icons';
import { Modal } from '../components/Modal';
import { EmptyState, ErrorBanner, SkeletonCard } from '../components/StateFeedback';
import { daysUntilDeadline, formatShortDate } from '../lib/datetime';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useAppState } from '../state/AppStateContext';

const GOAL_STATUS_LABEL: Record<string, string> = {
  active: '进行中',
  paused: '已暂停',
  done: '已完成',
  archived: '已归档',
};

const STEP_STATUS_LABEL: Record<string, string> = {
  todo: '待办',
  done: '已完成',
  dropped: '已放弃',
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

function formatDeadlineSummary(goal: Goal, timezone: string): string {
  if (!goal.deadline) return '没有明确期限';
  const daysLeft = daysUntilDeadline(goal.deadline, timezone);
  const short = formatShortDate(goal.deadline, timezone);
  if (daysLeft == null) return `截止 ${short}`;
  if (daysLeft < 0) return `截止 ${short} · 已逾期 ${Math.abs(daysLeft)} 天`;
  if (daysLeft === 0) return `截止今天 (${short})`;
  return `截止 ${short} · 还剩 ${daysLeft} 天`;
}

export function GoalsPage({ timezone }: { timezone: string }) {
  const { goalId: goalIdParam } = useParams<{ goalId?: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { openAssistant, setPageGoalId, openProposalModal, recordMutationResult } = useAppState();

  const parsedParamId = goalIdParam ? Number(goalIdParam) : undefined;
  const hasExplicitSelection = parsedParamId != null && Number.isFinite(parsedParamId);

  const [showArchived, setShowArchived] = useState(false);
  const [confirmDeleteGoal, setConfirmDeleteGoal] = useState<Goal | null>(null);

  const goalsQuery = useQuery({
    queryKey: showArchived ? [...queryKeys.goals, 'with-archived'] : queryKeys.goals,
    queryFn: () => (showArchived ? api.getGoalsWithArchived() : api.getGoals()),
  });

  const allGoals = goalsQuery.data?.goals ?? [];
  const goals = useMemo(
    () => (showArchived ? allGoals : allGoals.filter((g) => g.status !== 'archived')),
    [allGoals, showArchived],
  );

  const activeGoalId = useMemo(() => {
    if (hasExplicitSelection) return parsedParamId;
    const preferred = goals.find((g) => g.status !== 'archived') ?? goals[0];
    return preferred?.id;
  }, [hasExplicitSelection, parsedParamId, goals]);

  useEffect(() => {
    setPageGoalId(activeGoalId);
    return () => setPageGoalId(undefined);
  }, [activeGoalId, setPageGoalId]);

  const detailQuery = useQuery({
    queryKey:
      activeGoalId != null
        ? showArchived
          ? [...queryKeys.goal(activeGoalId), 'with-archived']
          : queryKeys.goal(activeGoalId)
        : ['goals', 'none'],
    queryFn: () => api.getGoal(activeGoalId!, { includeArchived: showArchived }),
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
      if (args.status === 'archived' && !showArchived && hasExplicitSelection) {
        navigate('/goals', { replace: true });
      }
    },
    onError: async (err, args) => {
      if (err.status === 409) {
        await invalidateAfterWrite(queryClient, { goalId: args.id });
      }
    },
  });

  const deleteGoalFn = useCallback(
    async (args: { id: number; version: string }, key: string) =>
      api.deleteGoal(args.id, { version: args.version }, key),
    [],
  );

  const deleteGoalAction = useIdempotentAction(deleteGoalFn, {
    onSuccess: async (res, args) => {
      recordMutationResult(res);
      setConfirmDeleteGoal(null);
      queryClient.removeQueries({ queryKey: queryKeys.goal(args.id) });
      await invalidateAfterWrite(queryClient);
      if (hasExplicitSelection) {
        navigate('/goals', { replace: true });
      }
    },
    onError: async (err, args) => {
      if (err.status === 409 || err.status === 404) {
        setConfirmDeleteGoal(null);
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
    deleteGoalAction.actionError ??
    updateStepAction.actionError ??
    proposeAction.actionError ??
    startStepAction.actionError ?? refillAction.actionError;

  const isBusy =
    updateGoalAction.isPending ||
    deleteGoalAction.isPending ||
    updateStepAction.isPending ||
    proposeAction.isPending ||
    startStepAction.isPending || refillAction.isPending;

  const phases = detailGoal ? computePhases(detailGoal) : [];
  const nextTodoStep = detailGoal?.steps.find((s) => s.status === 'todo') ?? null;

  return (
    <div>
      <div className="topbar">
        <div>
          <div className="eyebrow">你的长期方向</div>
          <h1>目标</h1>
        </div>
        <div className="topbar-actions">
          <button
            type="button"
            className="assistant-trigger"
            onClick={(e) => openAssistant(e.currentTarget)}
          >
            <SparkleIcon />
            <span>问 GoalCal</span>
          </button>
          <Link className="btn primary" to="/goals/new">
            + 新目标
          </Link>
        </div>
      </div>

      <p className="subtitle">不必一次看清全部，只需要知道每个目标的下一步。</p>

      {goalsQuery.isLoading ? (
        <SkeletonCard label="正在加载目标列表" />
      ) : goalsQuery.isError ? (
        <ErrorBanner error={goalsQuery.error} onRetry={() => void goalsQuery.refetch()} />
      ) : goals.length === 0 && !hasExplicitSelection ? (
        <EmptyState
          title="从一个想实现的结果开始"
          copy="把你想要推进的长期目标告诉我，先生成可编辑的步骤草稿，再按自己的节奏排进日历。"
          actions={
            <>
              <Link to="/goals/new" className="btn primary">
                + 新目标
              </Link>
              <button
                type="button"
                className="btn"
                onClick={() => setShowArchived((prev) => !prev)}
              >
                {showArchived ? '隐藏已归档' : '查看已归档'}
              </button>
            </>
          }
        />
      ) : (
        <div
          className={`goals-layout${hasExplicitSelection ? ' mobile-has-selection' : ''}`}
        >
          <div className="card goal-list" aria-label="目标列表">
            <div
              className="goal-list-label"
              style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8 }}
            >
              <span>
                {showArchived ? '全部目标（含已归档）' : '进行中目标'} · {goals.length}
              </span>
              <button
                type="button"
                className="inline-link-btn"
                onClick={() => setShowArchived((prev) => !prev)}
              >
                {showArchived ? '隐藏已归档' : '已归档'}
              </button>
            </div>
            {goals.map((g) => {
              const doneCount = g.steps.filter((s) => s.status === 'done').length;
              const pct = Math.round((g.progress ?? 0) * 100);
              const isSelected = activeGoalId === g.id;
              return (
                <button
                  key={g.id}
                  type="button"
                  className={`goal-row${isSelected ? ' active' : ''}`}
                  aria-current={isSelected ? 'true' : undefined}
                  onClick={() => navigate(`/goals/${g.id}`)}
                >
                  <div className="goal-row-title">
                    {g.pinned ? '置顶 · ' : ''}
                    {g.title}
                  </div>
                  <div className="goal-row-meta">
                    {GOAL_STATUS_LABEL[g.status] ?? g.status} · {formatDeadlineSummary(g, timezone)}
                  </div>
                  <div
                    className="progress"
                    role="progressbar"
                    aria-label={`${g.title} 进度`}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={pct}
                  >
                    <span style={{ width: `${pct}%` }} />
                  </div>
                  <div className="goal-row-foot">
                    <span>
                      {doneCount} / {g.steps.length} 步
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
                ← 返回目标列表
              </button>
            ) : null}

            {detailQuery.isLoading ? (
              <SkeletonCard label="正在加载目标详情" />
            ) : detailQuery.isError ? (
              <EmptyState
                title="无法显示该目标"
                copy={detailQuery.error.message || '目标不存在或已被归档。'}
                actions={
                  <>
                    <button
                      type="button"
                      className="btn"
                      onClick={() => void detailQuery.refetch()}
                    >
                      重试
                    </button>
                    <Link to="/goals" className="btn primary">
                      返回目标列表
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
                          目标标题
                        </label>
                        <input
                          id="edit-goal-title-input"
                          type="text"
                          className="input"
                          style={{ flex: 1, minWidth: 200 }}
                          value={goalTitleDraft}
                          onChange={(e) => setGoalTitleDraft(e.target.value)}
                          disabled={isBusy}
                        />
                        <button type="submit" className="btn small primary" disabled={isBusy}>
                          保存标题
                        </button>
                        <button
                          type="button"
                          className="btn small"
                          disabled={isBusy}
                          onClick={() => setEditingGoalTitle(false)}
                        >
                          取消
                        </button>
                      </form>
                    ) : (
                      <h2>{detailGoal.title}</h2>
                    )}

                    <div className="goal-meta">
                      <span className="chip" style={{ marginRight: 8 }}>
                        {GOAL_STATUS_LABEL[detailGoal.status] ?? detailGoal.status}
                      </span>
                      {detailGoal.pinned ? (
                        <span className="chip blue" style={{ marginRight: 8 }}>
                          已置顶
                        </span>
                      ) : null}
                      <span>{formatDeadlineSummary(detailGoal, timezone)}</span>
                    </div>
                  </div>

                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                    {!editingGoalTitle ? (
                      <button
                        type="button"
                        className="btn small"
                        disabled={isBusy}
                        onClick={() => {
                          setGoalTitleDraft(detailGoal.title);
                          setEditingGoalTitle(true);
                        }}
                      >
                        改标题
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
                          pinned: !detailGoal.pinned,
                        })
                      }
                    >
                      {detailGoal.pinned ? '取消置顶' : '置顶'}
                    </button>
                    {detailGoal.status !== 'archived' ? (
                      <button
                        type="button"
                        className="btn small"
                        disabled={isBusy}
                        onClick={() =>
                          void updateGoalAction.run({
                            id: detailGoal.id,
                            version: detailGoal.version,
                            status: detailGoal.status === 'paused' ? 'active' : 'paused',
                          })
                        }
                      >
                        {detailGoal.status === 'paused' ? '恢复' : '暂停'}
                      </button>
                    ) : null}
                    {detailGoal.status !== 'archived' ? (
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
                        归档
                      </button>
                    ) : (
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
                        恢复目标
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn small danger"
                      disabled={isBusy}
                      onClick={() => setConfirmDeleteGoal(detailGoal)}
                    >
                      删除
                    </button>
                  </div>
                </div>

                {detailGoal.outcome && detailGoal.outcome !== detailGoal.title ? (
                  <p className="muted" style={{ fontSize: 12, marginTop: 10 }}>
                    目标结果：{detailGoal.outcome}
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
                        aria-label={`${detailGoal.title} 整体进度`}
                        aria-valuemin={0}
                        aria-valuemax={100}
                        aria-valuenow={pct}
                      >
                        <span style={{ width: `${pct}%` }} />
                      </div>
                      <div className="goal-progress-text">
                        {doneSteps} / {detailGoal.steps.length} 步 · {pct}%
                      </div>
                    </div>
                  );
                })()}

                {nextTodoStep && detailGoal.status === 'active' ? (
                  <div className="next-box">
                    <div className="section-label">下一步</div>
                    <div className="next-title">{nextTodoStep.title}</div>
                    <div className="next-meta">
                      {nextTodoStep.est_min} 分钟
                      {nextTodoStep.outcome ? ` · 完成标准：${nextTodoStep.outcome}` : ''}
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
                        立即开始
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
                        标记完成
                      </button>
                      <button
                        type="button"
                        className="btn"
                        disabled={isBusy}
                        onClick={() => void proposeAction.run(nextTodoStep.id)}
                      >
                        找时间安排
                      </button>
                    </div>
                  </div>
                ) : null}

                {phases.length > 0 ? (
                  <div className="phase-section">
                    <div className="section-head">
                      <span className="section-title">阶段</span>
                      <span className="muted" style={{ fontSize: 11 }}>
                        {phases.length} 个阶段 · 点击阶段可查看对应步骤
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
                            {p.state === 'current' ? '（当前阶段）' : ''}
                          </span>
                          <span className="phase-meta">
                            {p.state === 'done'
                              ? `已完成 ${p.done} 步`
                              : hasSteps
                                ? `${p.done} / ${p.done + p.todo} 步`
                                : '完成当前阶段后自动生成'}
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
                      生成下一批步骤
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
                          {selectedPhase ? `步骤 · ${selectedPhase}` : '待推进步骤'}
                        </span>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                          {selectedPhase ? (
                            <button
                              type="button"
                              className="inline-link-btn primary-link"
                              onClick={() => setSelectedPhase(null)}
                            >
                              只看待办
                            </button>
                          ) : null}
                          <span className="muted" style={{ fontSize: 11 }}>
                            {selectedPhase
                              ? `共 ${phaseSteps.length} 步`
                              : `${todoSteps.length} 步待办 · ${finishedSteps.length} 步已完成`}
                          </span>
                        </div>
                      </div>

                      {detailGoal.steps.length === 0 ? (
                        <div className="muted" style={{ fontSize: 12 }}>
                          该目标暂时还没有步骤。
                        </div>
                      ) : visibleSteps.length === 0 && !selectedPhase ? (
                        <div className="muted" style={{ fontSize: 12 }}>
                          当前没有待办步骤，可展开下方已完成步骤或点击对应阶段查看。
                        </div>
                      ) : (
                        visibleSteps.map((step) => {
                          const isDone = step.status === 'done';
                          const isDropped = step.status === 'dropped';
                          const isEditing = editingStepId === step.id;

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
                                aria-label={`${isDone ? '取消完成' : '标记完成'}：${step.title}`}
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
                                      步骤名称
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
                                        预计分钟
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
                                        保存
                                      </button>
                                      <button
                                        type="button"
                                        className="btn small"
                                        disabled={isBusy}
                                        onClick={() => setEditingStepId(null)}
                                      >
                                        取消
                                      </button>
                                    </div>
                                  </form>
                                ) : (
                                  <>
                                    <div className="step-title">{step.title}</div>
                                    <div className="step-meta">
                                      {step.est_min} 分钟
                                      {step.status !== 'todo'
                                        ? ` · ${STEP_STATUS_LABEL[step.status] ?? step.status}`
                                        : ''}
                                      {step.outcome ? ` · 完成标准：${step.outcome}` : ''}
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
                                      找时间
                                    </button>
                                  ) : null}
                                  <button
                                    type="button"
                                    className="inline-link-btn"
                                    disabled={isBusy}
                                    onClick={() => beginEditStep(step)}
                                  >
                                    编辑
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
                                      放弃
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
                            已完成 / 已放弃 {finishedSteps.length} 步
                          </span>
                          <button
                            type="button"
                            className="inline-link-btn"
                            onClick={() => setShowFinishedSteps((prev) => !prev)}
                          >
                            {showFinishedSteps ? '收起已完成' : `展开查看 (${finishedSteps.length})`}
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

      {confirmDeleteGoal ? (
        <Modal
          title="确认删除目标"
          onClose={() => {
            if (!deleteGoalAction.isPending) setConfirmDeleteGoal(null);
          }}
        >
          <p className="muted" style={{ fontSize: 13, lineHeight: 1.6, marginTop: 0 }}>
            确定要彻底删除「{confirmDeleteGoal.title}」吗？这会同时删除它的全部步骤和关联的日程时间块，且无法撤销。
          </p>
          <ErrorBanner error={deleteGoalAction.actionError} />
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 18 }}>
            <button
              type="button"
              className="btn"
              disabled={deleteGoalAction.isPending}
              onClick={() => setConfirmDeleteGoal(null)}
            >
              取消
            </button>
            <button
              type="button"
              className="btn danger"
              disabled={deleteGoalAction.isPending}
              onClick={() =>
                void deleteGoalAction.run({
                  id: confirmDeleteGoal.id,
                  version: confirmDeleteGoal.version,
                })
              }
            >
              {deleteGoalAction.isPending ? '正在删除…' : '确认删除'}
            </button>
          </div>
        </Modal>
      ) : null}
    </div>
  );
}
