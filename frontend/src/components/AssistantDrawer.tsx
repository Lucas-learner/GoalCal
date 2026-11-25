import React, { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, api, generateIdempotencyKey } from '../api/client';
import { invalidateAfterWrite, queryKeys } from '../api/queryKeys';
import type { AssistantState, Proposal, ReplanGoalIntent } from '../api/types';
import { formatDateTimeLabel, isIsoExpired } from '../lib/datetime';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useI18n } from '../i18n';
import { useAppState } from '../state/AppStateContext';
import { AiProgressHint, type AiProgressStage } from './AiProgressHint';
import { SparkleIcon } from './Icons';
import { ErrorBanner, SkeletonCard } from './StateFeedback';

const FOCUSABLE_SELECTOR =
  'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

type TurnBody = { text: string; version: string; page_goal_id?: number };
interface QueuedTurn { text: string; idempotencyKey: string; body?: TurnBody }
interface FailedTurn {
  body: TurnBody;
  text: string;
  idempotencyKey: string;
  error: ApiError;
}

export function AssistantDrawer({
  timezone,
  isModalOverlay,
}: {
  timezone: string;
  isModalOverlay: boolean;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const titleId = useId();
  const { lang, t } = useI18n();

  const {
    assistantOpen,
    closeAssistant,
    pendingAssistantPrompt,
    clearPendingAssistantPrompt,
    pageGoalId,
    recordMutationResult,
    setPrefillNewGoal,
  } = useAppState();

  const assistantQuery = useQuery({
    queryKey: queryKeys.assistant,
    queryFn: api.getAssistant,
  });

  const [composerText, setComposerText] = useState('');
  const [queuedCount, setQueuedCount] = useState(0);
  const [isSendingTurn, setIsSendingTurn] = useState(false);
  const [activeTurnText, setActiveTurnText] = useState<string | null>(null);
  const [failedTurn, setFailedTurn] = useState<FailedTurn | null>(null);
  const [acceptingIndex, setAcceptingIndex] = useState<number | null>(null);

  const panelRef = useRef<HTMLElement | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);

  const turnQueueRef = useRef<QueuedTurn[]>([]);
  const loopActiveRef = useRef(false);
  const latestVersionRef = useRef<string>('0');
  const pageGoalIdRef = useRef<number | undefined>(pageGoalId);
  pageGoalIdRef.current = pageGoalId;

  useEffect(() => {
    if (assistantQuery.data?.version != null) {
      latestVersionRef.current = assistantQuery.data.version;
    }
  }, [assistantQuery.data?.version]);

  // Auto-resize composer up to ~5 lines, keeping single-line vertically centered with send button.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = '44px';
    el.style.height = `${Math.min(Math.max(el.scrollHeight, 44), 120)}px`;
  }, [composerText]);

  // Scroll to bottom when new messages arrive.
  useEffect(() => {
    if (assistantOpen) {
      messagesEndRef.current?.scrollIntoView?.({ block: 'end' });
    }
  }, [assistantOpen, assistantQuery.data?.messages.length, assistantQuery.data?.proposal?.id, isSendingTurn]);

  // Modal focus management when in overlay/mobile sheet mode.
  useEffect(() => {
    if (!assistantOpen) return;
    if (isModalOverlay) {
      const prevOverflow = document.body.style.overflow;
      document.body.style.overflow = 'hidden';
      requestAnimationFrame(() => textareaRef.current?.focus());

      const handleKeyDown = (e: KeyboardEvent) => {
        if (e.key === 'Escape') {
          e.preventDefault();
          closeAssistant();
          return;
        }
        if (e.key === 'Tab' && panelRef.current) {
          const focusables = Array.from(
            panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
          );
          if (focusables.length === 0) return;
          const first = focusables[0];
          const last = focusables[focusables.length - 1];
          if (e.shiftKey) {
            if (document.activeElement === first || !panelRef.current.contains(document.activeElement)) {
              e.preventDefault();
              last.focus();
            }
          } else if (document.activeElement === last || !panelRef.current.contains(document.activeElement)) {
            e.preventDefault();
            first.focus();
          }
        }
      };

      document.addEventListener('keydown', handleKeyDown);
      return () => {
        document.removeEventListener('keydown', handleKeyDown);
        document.body.style.overflow = prevOverflow;
      };
    }
    return undefined;
  }, [assistantOpen, isModalOverlay, closeAssistant]);

  const processQueue = useCallback(async () => {
    if (loopActiveRef.current) return;
    loopActiveRef.current = true;
    setIsSendingTurn(true);

    try {
      while (turnQueueRef.current.length > 0) {
        const nextItem = turnQueueRef.current.shift()!;
        setQueuedCount(turnQueueRef.current.length);
        setActiveTurnText(nextItem.text);

        let body = nextItem.body;
        try {
          // Ensure we have initial version if query hadn't finished yet (defaults to '0').
          const cached = queryClient.getQueryData<AssistantState>(queryKeys.assistant);
          const version = cached?.version ?? latestVersionRef.current ?? '0';

          body = body ?? {
            text: nextItem.text, version,
            ...(pageGoalIdRef.current != null ? { page_goal_id: pageGoalIdRef.current } : {}),
          };
          const response = await api.sendAssistantTurn(body, nextItem.idempotencyKey);

          latestVersionRef.current = response.version;
          queryClient.setQueryData<AssistantState>(queryKeys.assistant, {
            messages: response.messages,
            proposal: response.proposal,
            version: response.version,
            create_goal: response.create_goal,
            create_habit: response.create_habit,
            replan_goal: response.replan_goal,
          });
          setFailedTurn(null);

          if (response.operation_id || response.message) {
            recordMutationResult({
              message: response.message,
              operation_id: response.operation_id,
            });
          }

          if (response.operation_id || response.proposal || response.goal) {
            await invalidateAfterWrite(queryClient, response.goal ? { goalId: response.goal.id } : undefined);
          }
        } catch (rawErr) {
          const apiErr =
            rawErr instanceof ApiError
              ? rawErr
              : new ApiError({
                  status: 0,
                  code: 'unknown_error',
                  message: rawErr instanceof Error ? rawErr.message : t('发送失败，请重试。', 'Failed to send. Please retry.'),
                  idempotencyKey: nextItem.idempotencyKey,
                  unknownOutcome: true,
                });

          if (apiErr.status === 409) {
            // Stale conversation version: refresh GET /assistant so user can resend cleanly.
            try {
              const refreshed = await queryClient.fetchQuery({
                queryKey: queryKeys.assistant,
                queryFn: api.getAssistant,
              });
              latestVersionRef.current = refreshed.version ?? '0';
            } catch {
              await queryClient.invalidateQueries({ queryKey: queryKeys.assistant });
            }
          }

          setFailedTurn({
            body: body!,
            text: nextItem.text,
            idempotencyKey: nextItem.idempotencyKey,
            error: apiErr,
          });

          // Preserve unsent message text in composer if composer is empty so user can edit or resend.
          const unsent = turnQueueRef.current.map((item) => item.text);
          setComposerText((curr) => [nextItem.text, ...unsent, curr].filter(Boolean).join('\n'));
          // Stop queue on error so subsequent queued items aren't sent out of order.
          turnQueueRef.current = [];
          setQueuedCount(0);
          break;
        }
      }
    } finally {
      loopActiveRef.current = false;
      setIsSendingTurn(false);
      setActiveTurnText(null);
      setQueuedCount(0);
    }
  }, [queryClient, recordMutationResult]);

  const enqueueSend = useCallback(
    (text: string, existingKey?: string, originalBody?: TurnBody) => {
      const trimmed = text.trim();
      if (!trimmed) return;
      setFailedTurn(null);
      turnQueueRef.current.push({
        text: trimmed,
        body: originalBody,
        idempotencyKey: existingKey ?? generateIdempotencyKey(),
      });
      setQueuedCount(turnQueueRef.current.length);
      void processQueue();
    },
    [processQueue],
  );

  // Automatically send a kickoff prompt when opened from an action like "Replan".
  useEffect(() => {
    if (assistantOpen && pendingAssistantPrompt && assistantQuery.isSuccess) {
      const prompt = pendingAssistantPrompt;
      clearPendingAssistantPrompt();
      enqueueSend(prompt);
    }
  }, [assistantOpen, pendingAssistantPrompt, assistantQuery.isSuccess, clearPendingAssistantPrompt, enqueueSend]);

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = composerText.trim();
    if (!trimmed) return;
    setComposerText('');
    enqueueSend(trimmed);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      const trimmed = composerText.trim();
      if (!trimmed) return;
      setComposerText('');
      enqueueSend(trimmed);
    }
  };

  // Direct candidate execution inside assistant
  const acceptFn = useCallback(
    async (args: { proposal: Proposal; index: number }, key: string) => {
      setAcceptingIndex(args.index);
      return api.acceptProposal(
        args.proposal.id,
        { version: args.proposal.version, index: args.index },
        key,
      );
    },
    [],
  );

  const acceptAction = useIdempotentAction(acceptFn, {
    onSuccess: async (res, args) => {
      setAcceptingIndex(null);
      recordMutationResult(res);
      await invalidateAfterWrite(queryClient, { goalId: args.proposal.goal_id });
    },
    onError: async (err, args) => {
      setAcceptingIndex(null);
      if (err.status === 409) {
        await invalidateAfterWrite(queryClient, { goalId: args.proposal.goal_id });
      }
    },
  });

  const regenFn = useCallback(
    async (stepId: number, key: string) => api.createStepProposals(stepId, {}, key),
    [],
  );

  const regenerateAction = useIdempotentAction(regenFn, {
    onSuccess: async (newProposal) => {
      acceptAction.clearError();
      queryClient.setQueryData<AssistantState>(queryKeys.assistant, (prev) =>
        prev
          ? { ...prev, proposal: newProposal }
          : { messages: [], proposal: newProposal, version: '0' },
      );
      await invalidateAfterWrite(queryClient, { goalId: newProposal.goal_id });
    },
  });

  const applyReplanFn = useCallback(
    async (replan: ReplanGoalIntent, key: string) =>
      api.replanGoal(
        replan.goal_id,
        {
          version: replan.goal_version,
          ...(replan.steps_changed === false
            ? {}
            : {
                steps: replan.steps.map((s) => ({
                  title: s.title,
                  est_min: s.est_min,
                  milestone: s.milestone,
                  energy: s.energy,
                })),
                outline: replan.outline,
              }),
          ...(replan.deadline !== undefined && replan.deadline !== null ? { deadline: replan.deadline } : {}),
        },
        key,
      ),
    [],
  );

  const applyReplanAction = useIdempotentAction(applyReplanFn, {
    onSuccess: async (res, replan) => {
      recordMutationResult(res);
      await invalidateAfterWrite(queryClient, { goalId: replan.goal_id });
    },
    onError: async (err, replan) => {
      if (err.status === 409) {
        await invalidateAfterWrite(queryClient, { goalId: replan.goal_id });
      }
    },
  });

  const clearFn = useCallback(
    async (version: string, key: string) => api.clearAssistantThread({ version }, key),
    [],
  );

  const clearAction = useIdempotentAction(clearFn, {
    onSuccess: async (res) => {
      acceptAction.clearError();
      latestVersionRef.current = res.version;
      queryClient.setQueryData<AssistantState>(queryKeys.assistant, {
        messages: res.messages,
        proposal: res.proposal,
        version: res.version,
        create_goal: res.create_goal,
        create_habit: res.create_habit,
        replan_goal: res.replan_goal,
      });
      setFailedTurn(null);
      if (res.message) {
        recordMutationResult({ message: res.message });
      }
      await invalidateAfterWrite(queryClient);
    },
    onError: async (err) => {
      if (err.status === 409) {
        await queryClient.invalidateQueries({ queryKey: queryKeys.assistant });
      }
    },
  });

  if (!assistantOpen) return null;

  const state = assistantQuery.data;
  const proposal = state?.proposal ?? null;
  const proposalExpired =
    proposal != null && (proposal.status !== 'open' || isIsoExpired(proposal.expires_at, timezone));

  const panelContent = (
    <aside
      ref={panelRef}
      className="assistant-panel"
      aria-label={t('GoalCal 助手', 'GoalCal Assistant')}
      {...(isModalOverlay ? { role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId } : {})}
    >
      <div className="assistant-header">
        <div id={titleId} className="assistant-title">
          <SparkleIcon />
          <span>{t('GoalCal 助手', 'GoalCal Assistant')}</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          {(state?.messages?.length ?? 0) > 0 || proposal != null || state?.create_goal || state?.create_habit || state?.replan_goal ? (
            <button
              type="button"
              className="btn small"
              disabled={isSendingTurn || clearAction.isPending}
              onClick={() => void clearAction.run(state?.version ?? latestVersionRef.current ?? '0')}
            >
              {clearAction.isPending ? t('清空中…', 'Clearing…') : t('清空', 'Clear')}
            </button>
          ) : null}
          <button
            type="button"
            className="icon-close-btn"
            aria-label={t('关闭助手', 'Close assistant')}
            onClick={closeAssistant}
          >
            ×
          </button>
        </div>
      </div>

      <div className="messages">
        <ErrorBanner
          error={clearAction.actionError}
          onRefresh={() => void queryClient.invalidateQueries({ queryKey: queryKeys.assistant })}
        />
        {assistantQuery.isLoading ? (
          <SkeletonCard label={t('正在加载助手对话', 'Loading conversation')} />
        ) : assistantQuery.isError ? (
          <ErrorBanner
            error={assistantQuery.error}
            onRetry={() => void assistantQuery.refetch()}
          />
        ) : null}

        {!assistantQuery.isLoading &&
        !assistantQuery.isError &&
        (!state?.messages || state.messages.length === 0) ? (
          <div className="empty-state" style={{ padding: 18 }}>
            <div className="empty-state-title">{t('有什么可以帮你？', 'How can I help?')}</div>
            <div className="empty-state-copy" style={{ marginBottom: 0 }}>
              {t('起草目标和习惯，或调整日程。试试「我想每周跑三次步」。',
                'Draft a goal or habit, or adjust your calendar. Try “I want to run three times a week.”')}
            </div>
          </div>
        ) : null}

        {state?.messages.map((msg) => (
          <div
            key={msg.id}
            className={msg.role === 'user' ? 'message-user' : 'message-ai'}
          >
            {msg.content}
          </div>
        ))}

        {state?.create_goal || state?.create_habit ? (
          <div className="proposal-card" style={{ marginTop: 6 }}>
            <div className="proposal-goal">{state.create_habit ? t('新习惯草稿', 'New habit draft') : t('新目标草稿', 'New goal draft')}</div>
            <div className="proposal-step">{state.create_habit?.title ?? state.create_goal?.title}</div>
            {state.create_habit?.weekly_target && <div className="proposal-goal">
              {t(`每周 ${state.create_habit.weekly_target} 次`, `${state.create_habit.weekly_target} times per week`)}
            </div>}
            <div className="actions" style={{ marginTop: 12 }}>
              <button
                type="button"
                className="btn primary small"
                onClick={() => {
                  if (isModalOverlay) closeAssistant();
                  if (state.create_habit) {
                    navigate('/habits/new', { state: { draft: state.create_habit } });
                  } else if (state.create_goal) {
                    setPrefillNewGoal(state.create_goal);
                    navigate('/goals/new');
                  }
                }}
              >
                {t('编辑草稿', 'Edit draft')}
              </button>
            </div>
          </div>
        ) : null}

        {state?.replan_goal ? (
          <div className="proposal-card" style={{ marginTop: 6 }} aria-label={t('重新规划方案', 'Replan proposal')}>
            <div className="proposal-goal">
              {t('重新规划方案', 'Updated plan')}
              {state.replan_goal.deadline === ''
                ? ` · ${t('取消截止日期', 'Remove deadline')}`
                : state.replan_goal.deadline
                  ? state.replan_goal.previous_deadline && state.replan_goal.previous_deadline !== state.replan_goal.deadline
                    ? ` · ${t('截止', 'Due')} ${state.replan_goal.previous_deadline} → ${state.replan_goal.deadline}`
                    : ` · ${t('截止', 'Due')} ${state.replan_goal.deadline}`
                  : ''}
            </div>
            <div className="proposal-step">{state.replan_goal.goal_title}</div>
            {state.replan_goal.steps_changed === false ? (
              <div className="field-help" style={{ margin: '10px 0' }}>
                {t(
                  `保留现有 ${state.replan_goal.steps.length} 个待办步骤不变，仅更新截止日期。`,
                  `Keeps the ${state.replan_goal.steps.length} existing todo steps unchanged and updates only the deadline.`,
                )}
              </div>
            ) : (
              <>
                <ol style={{ margin: '10px 0', paddingLeft: 18, fontSize: 12, lineHeight: 1.6 }}>
                  {state.replan_goal.steps.map((step, idx) => (
                    <li key={`${step.title}-${idx}`}>
                      <span>{step.title}</span>
                      <span className="muted tabular-nums"> · {step.est_min} {t('分钟', 'min')}</span>
                    </li>
                  ))}
                </ol>
                <div className="field-help" style={{ marginBottom: 10 }}>
                  {t('已完成步骤会保留，确认后替换剩余待办步骤。', 'Completed steps are kept; confirming replaces remaining todo steps.')}
                </div>
              </>
            )}
            <ErrorBanner
              error={applyReplanAction.actionError}
              onRefresh={() => void invalidateAfterWrite(queryClient, { goalId: state.replan_goal!.goal_id })}
            />
            <div className="actions" style={{ marginTop: 10 }}>
              <button
                type="button"
                className="btn primary small"
                disabled={applyReplanAction.isPending || isSendingTurn}
                onClick={() => void applyReplanAction.run(state.replan_goal!)}
              >
                {applyReplanAction.isPending ? t('应用中…', 'Applying…') : t('确认修改', 'Apply changes')}
              </button>
              <button
                type="button"
                className="btn small"
                disabled={applyReplanAction.isPending || isSendingTurn}
                onClick={() => textareaRef.current?.focus()}
              >
                {t('继续调整', 'Keep adjusting')}
              </button>
            </div>
          </div>
        ) : null}

        {proposal ? (
          <div className="proposal-card" aria-label={t('时间候选', 'Suggested time slots')}>
            <div className="proposal-head">
              <div className="proposal-goal">{proposal.goal_title}</div>
              <div className="proposal-step">{proposal.step_title}</div>
            </div>

            <ErrorBanner
              error={acceptAction.actionError ?? regenerateAction.actionError}
              onRefresh={() => void invalidateAfterWrite(queryClient, { goalId: proposal.goal_id })}
            />

            {proposalExpired ? (
              <div className="status-banner info" role="status" style={{ marginBottom: 8 }}>
                <div className="status-banner-body">
                  <div className="status-banner-title">{t('该候选已过期或已被取代', 'These slots expired or were replaced')}</div>
                  <div>{t('请重新查找可用时段，或在下方继续告诉我你的偏好。', 'Search again for open slots, or tell me your preference below.')}</div>
                </div>
                <div className="status-banner-actions">
                  <button
                    type="button"
                    className="btn small primary"
                    disabled={regenerateAction.isPending}
                    onClick={() => void regenerateAction.run(proposal.step_id)}
                  >
                    {regenerateAction.isPending ? t('生成中…', 'Finding…') : t('重新找时间', 'Find new slots')}
                  </button>
                </div>
              </div>
            ) : null}

            {proposal.candidates.length === 0 ? (
              <div className="field-help">
                {t(
                  '当前没有找到合适空档。你可以点击重新找时间，或在下方说明期望的时间范围。',
                  'No open slots fit right now. Try finding slots again or tell me a preferred time window below.',
                )}
                <div style={{ marginTop: 8 }}>
                  <button
                    type="button"
                    className="btn small"
                    disabled={regenerateAction.isPending}
                    onClick={() => void regenerateAction.run(proposal.step_id)}
                  >
                    {regenerateAction.isPending ? t('生成中…', 'Finding…') : t('重新找时间', 'Find new slots')}
                  </button>
                </div>
              </div>
            ) : (
              <div>
                {proposal.candidates.map((cand, index) => {
                  const label = formatDateTimeLabel(cand.start, cand.end, timezone, lang);
                  return (
                    <button
                      key={`${cand.start}-${index}`}
                      type="button"
                      className="slot-btn"
                      aria-label={`${label} · ${proposal.goal_title} · ${proposal.step_title}`}
                      disabled={
                        proposalExpired || acceptAction.isPending || regenerateAction.isPending
                      }
                      onClick={() => void acceptAction.run({ proposal, index })}
                    >
                      <div className="slot-time">
                        {label}
                        {acceptAction.isPending && acceptingIndex === index ? t('（安排中…）', ' (Scheduling…)') : ''}
                      </div>
                    </button>
                  );
                })}

                {acceptAction.actionError ? (
                  <div style={{ marginTop: 10, display: 'flex', justifyContent: 'flex-end' }}>
                    <button
                      type="button"
                      className="btn small"
                      disabled={regenerateAction.isPending}
                      onClick={() => void regenerateAction.run(proposal.step_id)}
                    >
                      {regenerateAction.isPending ? t('生成中…', 'Finding…') : t('重新找时间', 'Find new slots')}
                    </button>
                  </div>
                ) : null}
              </div>
            )}
          </div>
        ) : null}

        {isSendingTurn ? (() => {
          const isPlanningTurn = Boolean(
            activeTurnText &&
              /重新规划|重排|拆解|拆细|计划|调整方案|replan|break down|draft|plan/i.test(activeTurnText),
          );
          const turnStages: readonly AiProgressStage[] = isPlanningTurn
            ? [
                { afterMs: 0, label: t('正在读取目标与当前进度…', 'Reading your goal and progress…') },
                { afterMs: 2500, label: t('正在重新规划剩余步骤与节奏…', 'Replanning remaining steps and pace…') },
                { afterMs: 7000, label: t('正在核对已完成步骤与截止日期…', 'Checking completed steps and deadline…') },
                { afterMs: 14000, label: t('方案较复杂，仍在整理中，请稍候…', 'Still putting the updated plan together…') },
              ]
            : [
                { afterMs: 0, label: t('正在理解你的请求…', 'Understanding your request…') },
                { afterMs: 3000, label: t('正在结合日程与目标整理回复…', 'Reviewing your schedule and goals…') },
                { afterMs: 8000, label: t('仍在思考中，请稍候…', 'Still working on a reply…') },
              ];
          return (
            <AiProgressHint
              active
              compact
              stages={turnStages}
              sublabel={
                queuedCount > 0
                  ? t(`另有 ${queuedCount} 条待发送`, `${queuedCount} more queued`)
                  : undefined
              }
            />
          );
        })() : null}

        {failedTurn ? (
          <div className="status-banner" role="alert">
            <div className="status-banner-body">
              <div className="status-banner-title">
                {failedTurn.error.status === 409
                  ? t('对话已更新，已刷新最新状态', 'Conversation updated; refreshed latest state')
                  : t('消息发送失败', 'Failed to send message')}
              </div>
              <div>{failedTurn.error.message}</div>
            </div>
            <div className="status-banner-actions">
              <button
                type="button"
                className="btn small primary"
                onClick={() => {
                  setComposerText((current) => current === failedTurn.text ? '' :
                    current.startsWith(failedTurn.text + '\n') ? current.slice(failedTurn.text.length + 1) : current);
                  enqueueSend(
                    failedTurn.text,
                    failedTurn.error.status === 409
                      ? generateIdempotencyKey()
                      : failedTurn.idempotencyKey,
                    failedTurn.error.status === 409 ? undefined : failedTurn.body,
                  );
                }}
              >
                {failedTurn.error.status === 409 ? t('使用最新状态重发', 'Resend with latest state') : t('重试发送', 'Retry')}
              </button>
              <button
                type="button"
                className="btn small"
                onClick={() => setFailedTurn(null)}
              >
                {t('关闭', 'Dismiss')}
              </button>
            </div>
          </div>
        ) : null}

        <div ref={messagesEndRef} />
      </div>

      <form className="composer-wrap" onSubmit={handleSubmit}>
        <label className="sr-only" htmlFor="assistant-composer-input">
          {t('向 GoalCal 助手发送消息', 'Message GoalCal Assistant')}
        </label>
        <div className="composer">
          <textarea
            id="assistant-composer-input"
            ref={textareaRef}
            className="composer-textarea"
            rows={1}
            placeholder={t('问问、安排或调整…', 'Ask, schedule, or adjust…')}
            value={composerText}
            onChange={(e) => setComposerText(e.target.value)}
            onKeyDown={handleKeyDown}
          />
          <button
            type="submit"
            className="send-hit"
            aria-label={t('发送消息', 'Send message')}
            disabled={!composerText.trim()}
          >
            <span className="send-circle" aria-hidden="true">
              ↑
            </span>
          </button>
        </div>
      </form>
    </aside>
  );

  if (isModalOverlay) {
    return (
      <div
        className="assistant-overlay-backdrop"
        role="presentation"
        onMouseDown={(e) => {
          if (e.target === e.currentTarget) {
            closeAssistant();
          }
        }}
      >
        {panelContent}
      </div>
    );
  }

  return panelContent;
}
