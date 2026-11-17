import React, { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, api, generateIdempotencyKey } from '../api/client';
import { invalidateAfterWrite, queryKeys } from '../api/queryKeys';
import type { AssistantState, Proposal } from '../api/types';
import { formatDateTimeLabel, isIsoExpired } from '../lib/datetime';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useAppState } from '../state/AppStateContext';
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

  const {
    assistantOpen,
    closeAssistant,
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

  // Auto-resize composer up to ~5 lines.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
  }, [composerText]);

  // Scroll to bottom when new messages arrive.
  useEffect(() => {
    if (assistantOpen) {
      messagesEndRef.current?.scrollIntoView?.({ block: 'end' });
    }
  }, [assistantOpen, assistantQuery.data?.messages.length, assistantQuery.data?.proposal?.id]);

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
          });
          setFailedTurn(null);

          if (response.operation_id || response.message) {
            recordMutationResult({
              message: response.message,
              operation_id: response.operation_id,
            });
          }

          await invalidateAfterWrite(queryClient);
        } catch (rawErr) {
          const apiErr =
            rawErr instanceof ApiError
              ? rawErr
              : new ApiError({
                  status: 0,
                  code: 'unknown_error',
                  message: rawErr instanceof Error ? rawErr.message : '发送失败，请重试。',
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

  if (!assistantOpen) return null;

  const state = assistantQuery.data;
  const proposal = state?.proposal ?? null;
  const proposalExpired =
    proposal != null && (proposal.status !== 'open' || isIsoExpired(proposal.expires_at, timezone));

  const panelContent = (
    <aside
      ref={panelRef}
      className="assistant-panel"
      aria-label="GoalCal 助手"
      {...(isModalOverlay ? { role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': titleId } : {})}
    >
      <div className="assistant-header">
        <div id={titleId} className="assistant-title">
          <SparkleIcon />
          <span>GoalCal 助手</span>
        </div>
        <button
          type="button"
          className="icon-close-btn"
          aria-label="关闭助手"
          onClick={closeAssistant}
        >
          ×
        </button>
      </div>

      <div className="messages">
        {assistantQuery.isLoading ? (
          <SkeletonCard label="正在加载助手对话" />
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
            <div className="empty-state-title">需要我帮你做什么？</div>
            <div className="empty-state-copy" style={{ marginBottom: 0 }}>
              你可以让我帮你安排目标的下一步、调整已有日程，或把一个新想法整理成结构化目标。
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

        {state?.create_goal ? (
          <div className="proposal-card" style={{ marginTop: 6 }}>
            <div className="proposal-goal">新建目标草稿</div>
            <div className="proposal-step">{state.create_goal.title}</div>
            <div className="field-help" style={{ marginTop: 4 }}>
              将进入结构化目标草稿页，确认并编辑步骤后才会保存。
            </div>
            <div className="actions" style={{ marginTop: 12 }}>
              <button
                type="button"
                className="btn primary small"
                onClick={() => {
                  setPrefillNewGoal(state.create_goal!);
                  if (isModalOverlay) closeAssistant();
                  navigate('/goals/new');
                }}
              >
                前往编辑计划草稿
              </button>
            </div>
          </div>
        ) : null}

        {proposal ? (
          <div className="proposal-card" aria-label="时间候选">
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
                  <div className="status-banner-title">该候选已过期或已被取代</div>
                  <div>请重新查找可用时段，或在下方继续告诉我你的偏好。</div>
                </div>
                <div className="status-banner-actions">
                  <button
                    type="button"
                    className="btn small primary"
                    disabled={regenerateAction.isPending}
                    onClick={() => void regenerateAction.run(proposal.step_id)}
                  >
                    {regenerateAction.isPending ? '生成中…' : '重新找时间'}
                  </button>
                </div>
              </div>
            ) : null}

            {proposal.candidates.length === 0 ? (
              <div className="field-help">
                当前没有找到合适空档。你可以点击重新找时间，或在下方说明期望的时间范围。
                <div style={{ marginTop: 8 }}>
                  <button
                    type="button"
                    className="btn small"
                    disabled={regenerateAction.isPending}
                    onClick={() => void regenerateAction.run(proposal.step_id)}
                  >
                    {regenerateAction.isPending ? '生成中…' : '重新找时间'}
                  </button>
                </div>
              </div>
            ) : (
              <div>
                {proposal.candidates.map((cand, index) => {
                  const label = formatDateTimeLabel(cand.start, cand.end, timezone);
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
                        {acceptAction.isPending && acceptingIndex === index ? '（安排中…）' : ''}
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
                      {regenerateAction.isPending ? '生成中…' : '重新找时间'}
                    </button>
                  </div>
                ) : null}
              </div>
            )}
          </div>
        ) : null}

        {isSendingTurn ? (
          <div className="muted" style={{ fontSize: 11 }} role="status">
            正在处理回复{queuedCount > 0 ? `（另有 ${queuedCount} 条待发送）` : '…'}
          </div>
        ) : null}

        {failedTurn ? (
          <div className="status-banner" role="alert">
            <div className="status-banner-body">
              <div className="status-banner-title">
                {failedTurn.error.status === 409 ? '对话已更新，已刷新最新状态' : '消息发送失败'}
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
                {failedTurn.error.status === 409 ? '使用最新状态重发' : '重试发送'}
              </button>
              <button
                type="button"
                className="btn small"
                onClick={() => setFailedTurn(null)}
              >
                关闭
              </button>
            </div>
          </div>
        ) : null}

        <div ref={messagesEndRef} />
      </div>

      <form className="composer-wrap" onSubmit={handleSubmit}>
        <label className="sr-only" htmlFor="assistant-composer-input">
          向 GoalCal 助手发送消息
        </label>
        <div className="composer">
          <textarea
            id="assistant-composer-input"
            ref={textareaRef}
            className="composer-textarea"
            rows={1}
            placeholder="问问、安排或调整…"
            value={composerText}
            onChange={(e) => setComposerText(e.target.value)}
            onKeyDown={handleKeyDown}
          />
          <button
            type="submit"
            className="send-hit"
            aria-label="发送消息"
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
