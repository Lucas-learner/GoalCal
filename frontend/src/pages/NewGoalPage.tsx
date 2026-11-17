import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { invalidateAfterWrite } from '../api/queryKeys';
import type { Draft, DraftStep } from '../api/types';
import { SparkleIcon } from '../components/Icons';
import { ErrorBanner } from '../components/StateFeedback';
import {
  addDaysIsoDate,
  addMonthsIsoDate,
  todayIsoDateInZone,
} from '../lib/datetime';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useAppState } from '../state/AppStateContext';

type DeadlinePreset = '7d' | '1m' | '3m' | 'custom' | 'none';

export function NewGoalPage({ timezone }: { timezone: string }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const {
    openAssistant,
    openProposalModal,
    recordMutationResult,
    prefillNewGoal,
    setPrefillNewGoal,
  } = useAppState();

  const todayIso = todayIsoDateInZone(timezone);

  const [title, setTitle] = useState('');
  const [preset, setPreset] = useState<DeadlinePreset>('1m');
  const [customDate, setCustomDate] = useState(addMonthsIsoDate(todayIso, 1, timezone));
  const [validationError, setValidationError] = useState<string | null>(null);

  // Draft editing state
  const [draft, setDraft] = useState<Draft | null>(null);
  const [draftTitle, setDraftTitle] = useState('');
  const [draftDeadline, setDraftDeadline] = useState<string | null>(null);
  const [draftSteps, setDraftSteps] = useState<DraftStep[]>([]);

  // Prefill from Assistant create_goal intent
  useEffect(() => {
    if (!prefillNewGoal) return;
    setTitle(prefillNewGoal.title || '');
    if (prefillNewGoal.deadline) {
      setPreset('custom');
      setCustomDate(prefillNewGoal.deadline);
    } else if (prefillNewGoal.deadline === null) {
      setPreset('none');
    }
    setPrefillNewGoal(null);
  }, [prefillNewGoal, setPrefillNewGoal]);

  const computedDeadline = (): string | null => {
    switch (preset) {
      case '7d':
        return addDaysIsoDate(todayIso, 7, timezone);
      case '1m':
        return addMonthsIsoDate(todayIso, 1, timezone);
      case '3m':
        return addMonthsIsoDate(todayIso, 3, timezone);
      case 'custom':
        return customDate || null;
      case 'none':
        return null;
    }
  };

  const generateDraftFn = useCallback(
    async (body: { title: string; deadline: string | null }, key: string) =>
      api.createGoalDraft(body, key),
    [],
  );

  const generateDraftAction = useIdempotentAction(generateDraftFn, {
    onSuccess: (res) => {
      setDraft(res);
      setDraftTitle(res.title);
      setDraftDeadline(res.deadline);
      setDraftSteps(res.steps.map((s) => ({ ...s })));
      setValidationError(null);
    },
  });

  const saveGoalFn = useCallback(
    async (
      args: {
        scheduleFirst: boolean;
        body: {
          draft_id: string;
          version: string;
          title: string;
          deadline: string | null;
          steps: DraftStep[];
        };
      },
      key: string,
    ) => {
      const mutation = await api.createGoal(args.body, key);
      let firstProposal = null;
      if (args.scheduleFirst && mutation.goal) {
        const firstStep =
          mutation.goal.steps.find((s) => s.status === 'todo') ?? mutation.goal.steps[0];
        if (firstStep) {
          firstProposal = await api.createStepProposals(firstStep.id);
        }
      }
      return { mutation, firstProposal };
    },
    [],
  );

  const saveGoalAction = useIdempotentAction(saveGoalFn, {
    onSuccess: async ({ mutation, firstProposal }) => {
      recordMutationResult(mutation);
      await invalidateAfterWrite(queryClient, { goalId: mutation.goal?.id });
      const targetUrl = mutation.goal ? `/goals/${mutation.goal.id}` : '/goals';
      navigate(targetUrl);
      if (firstProposal) {
        openProposalModal(firstProposal);
      }
    },
    onError: async (err) => {
      if (err.status === 409) {
        await invalidateAfterWrite(queryClient);
      }
    },
  });

  const handleGenerate = (e: React.FormEvent) => {
    e.preventDefault();
    setValidationError(null);
    const trimmed = title.trim();
    if (!trimmed) {
      setValidationError('请先填写你想实现的目标。');
      return;
    }
    if (preset === 'custom' && !customDate) {
      setValidationError('请选择具体的目标截止日期。');
      return;
    }
    void generateDraftAction.run({
      title: trimmed,
      deadline: computedDeadline(),
    });
  };

  const handleSave = (scheduleFirst: boolean) => {
    if (!draft) return;
    setValidationError(null);
    const trimmedTitle = draftTitle.trim();
    if (!trimmedTitle) {
      setValidationError('目标标题不能为空。');
      return;
    }
    if (draftSteps.length === 0) {
      setValidationError('请至少保留一个执行步骤。');
      return;
    }
    for (let i = 0; i < draftSteps.length; i++) {
      const st = draftSteps[i];
      if (!st.title.trim()) {
        setValidationError(`第 ${i + 1} 步的标题不能为空。`);
        return;
      }
      if (!Number.isFinite(st.est_min) || st.est_min < 5 || st.est_min > 240) {
        setValidationError(`第 ${i + 1} 步的预计时长须在 5 至 240 分钟之间。`);
        return;
      }
    }
    void saveGoalAction.run({
      scheduleFirst,
      body: {
        draft_id: draft.id,
        version: draft.version,
        title: trimmedTitle,
        deadline: draftDeadline,
        steps: draftSteps.map((s) => ({
          ...s,
          title: s.title.trim(),
          outcome: s.outcome.trim(),
          milestone: s.milestone.trim() || '自定义阶段',
          est_min: Math.round(s.est_min),
        })),
      },
    });
  };

  const updateStepField = (index: number, patch: Partial<DraftStep>) => {
    setDraftSteps((prev) => prev.map((s, idx) => (idx === index ? { ...s, ...patch } : s)));
  };

  const removeStep = (index: number) => {
    setDraftSteps((prev) => prev.filter((_, idx) => idx !== index));
  };

  const addStep = () => {
    const defaultMilestone =
      draftSteps[draftSteps.length - 1]?.milestone ||
      draft?.outline?.[0] ||
      '执行步骤';
    setDraftSteps((prev) => [
      ...prev,
      {
        title: '',
        outcome: '',
        est_min: 30,
        energy: 1,
        kind: 'deep',
        milestone: defaultMilestone,
      },
    ]);
  };

  const isBusy = generateDraftAction.isPending || saveGoalAction.isPending;

  return (
    <div className="new-goal-wrap">
      <div className="topbar">
        <div>
          <div className="eyebrow">新目标</div>
          <h1>{draft ? '确认并调整计划草稿' : '你想实现什么？'}</h1>
        </div>
        <button
          type="button"
          className="assistant-trigger"
          onClick={(e) => openAssistant(e.currentTarget)}
        >
          <SparkleIcon />
          <span>问 GoalCal</span>
        </button>
      </div>

      <p className="subtitle">
        {draft
          ? '先检查并修改目标与步骤。确认保存后才会写入你的目标列表，不会擅自修改日历。'
          : '先描述结果和大致期限。计划生成后，你仍然可以逐步修改。'}
      </p>

      <ErrorBanner error={generateDraftAction.actionError ?? saveGoalAction.actionError} />

      {!draft ? (
        <form className="card goal-form" onSubmit={handleGenerate} noValidate>
          <div className="field">
            <label className="field-label" htmlFor="new-goal-title-input">
              目标
            </label>
            <textarea
              id="new-goal-title-input"
              className="goal-input"
              placeholder="例如：我希望在 30 天内发布第一条关于个人知识管理的短视频。"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              disabled={isBusy}
            />
            <div className="field-help">
              尽量描述你希望看到的结果，不需要自己提前拆解所有步骤。
            </div>
          </div>

          <div className="field">
            <span className="field-label" id="deadline-group-label">
              期限（按当前时区 {timezone} 计算）
            </span>
            <div className="deadline-options" role="group" aria-labelledby="deadline-group-label">
              {[
                { key: '7d' as const, label: '7 天' },
                { key: '1m' as const, label: '1 个月' },
                { key: '3m' as const, label: '3 个月' },
                { key: 'custom' as const, label: '选择日期' },
                { key: 'none' as const, label: '没有明确期限' },
              ].map((opt) => (
                <button
                  key={opt.key}
                  type="button"
                  className={`deadline-option${preset === opt.key ? ' selected' : ''}`}
                  aria-pressed={preset === opt.key}
                  disabled={isBusy}
                  onClick={() => setPreset(opt.key)}
                >
                  {opt.label}
                </button>
              ))}
            </div>

            {preset === 'custom' ? (
              <div style={{ marginTop: 12 }}>
                <label className="field-label" htmlFor="new-goal-custom-date">
                  自定义截止日期
                </label>
                <input
                  id="new-goal-custom-date"
                  type="date"
                  className="input"
                  style={{ maxWidth: 220 }}
                  value={customDate}
                  onChange={(e) => setCustomDate(e.target.value)}
                  disabled={isBusy}
                />
              </div>
            ) : null}
          </div>

          {validationError ? (
            <div className="field-error" role="alert">
              {validationError}
            </div>
          ) : null}

          <div className="goal-form-actions">
            <div className="goal-form-note">
              下一步会先生成可编辑的计划草稿，不会立即创建目标或写入日历。
            </div>
            <div className="goal-form-buttons">
              <button
                type="button"
                className="btn"
                disabled={isBusy}
                onClick={() => navigate('/goals')}
              >
                取消
              </button>
              <button type="submit" className="btn primary" disabled={isBusy}>
                {generateDraftAction.isPending ? '生成草稿中…' : '生成计划草稿'}
              </button>
            </div>
          </div>
        </form>
      ) : (
        <div className="card goal-form">
          <div className="status-banner info" role="status">
            <div className="status-banner-body">
              <div className="status-banner-title">
                {draft.source === 'llm'
                  ? '计划来源：AI 生成草稿'
                  : `计划来源：模板回退（${draft.source}）`}
              </div>
              <div>
                {draft.source === 'llm'
                  ? '由 AI 根据你的目标描述拆解生成，保存前可任意修改。'
                  : `当前使用“${draft.source}”模板生成初始步骤（非 AI 个性化生成），你可以按实际情况编辑后再保存。`}
              </div>
            </div>
          </div>

          <div className="field">
            <label className="field-label" htmlFor="draft-title-input">
              目标标题
            </label>
            <input
              id="draft-title-input"
              type="text"
              className="input"
              value={draftTitle}
              onChange={(e) => setDraftTitle(e.target.value)}
              disabled={isBusy}
            />
          </div>

          <div className="field">
            <label className="field-label" htmlFor="draft-deadline-input">
              截止日期（留空表示没有明确期限）
            </label>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <input
                id="draft-deadline-input"
                type="date"
                className="input"
                style={{ maxWidth: 220 }}
                value={draftDeadline ?? ''}
                onChange={(e) => setDraftDeadline(e.target.value || null)}
                disabled={isBusy}
              />
              {draftDeadline ? (
                <button
                  type="button"
                  className="btn small"
                  disabled={isBusy}
                  onClick={() => setDraftDeadline(null)}
                >
                  清除期限
                </button>
              ) : null}
            </div>
          </div>

          {draft.outline.length > 0 ? (
            <div className="field">
              <div className="field-label">阶段大纲</div>
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                {draft.outline.map((phase, idx) => (
                  <span key={`${phase}-${idx}`} className="chip">
                    {idx + 1}. {phase}
                  </span>
                ))}
              </div>
            </div>
          ) : null}

          <div className="field">
            <div className="section-head" style={{ marginTop: 10 }}>
              <span className="section-title">可编辑步骤（{draftSteps.length}）</span>
              <button type="button" className="btn small" disabled={isBusy} onClick={addStep}>
                + 添加步骤
              </button>
            </div>

            {draftSteps.map((step, index) => (
              <div key={index} className="draft-step-card">
                <div>
                  <label className="field-label" htmlFor={`draft-step-title-${index}`}>
                    步骤 {index + 1} 标题
                  </label>
                  <input
                    id={`draft-step-title-${index}`}
                    type="text"
                    className="input"
                    value={step.title}
                    onChange={(e) => updateStepField(index, { title: e.target.value })}
                    disabled={isBusy}
                  />
                </div>

                <div>
                  <label className="field-label" htmlFor={`draft-step-outcome-${index}`}>
                    完成产出
                  </label>
                  <input
                    id={`draft-step-outcome-${index}`}
                    type="text"
                    className="input"
                    value={step.outcome}
                    onChange={(e) => updateStepField(index, { outcome: e.target.value })}
                    disabled={isBusy}
                  />
                </div>

                <div className="draft-step-grid">
                  <div>
                    <label className="field-label" htmlFor={`draft-step-milestone-${index}`}>
                      所属阶段
                    </label>
                    <input
                      id={`draft-step-milestone-${index}`}
                      type="text"
                      className="input"
                      value={step.milestone}
                      onChange={(e) => updateStepField(index, { milestone: e.target.value })}
                      disabled={isBusy}
                    />
                  </div>
                  <div>
                    <label className="field-label" htmlFor={`draft-step-est-${index}`}>
                      分钟
                    </label>
                    <input
                      id={`draft-step-est-${index}`}
                      type="number"
                      min={5}
                      max={240}
                      className="input tabular-nums"
                      value={step.est_min}
                      onChange={(e) =>
                        updateStepField(index, { est_min: Number(e.target.value) || 0 })
                      }
                      disabled={isBusy}
                    />
                  </div>
                  <div>
                    <label className="field-label" htmlFor={`draft-step-energy-${index}`}>
                      精力要求
                    </label>
                    <select
                      id={`draft-step-energy-${index}`}
                      className="select"
                      value={step.energy}
                      onChange={(e) => updateStepField(index, { energy: Number(e.target.value) })}
                      disabled={isBusy}
                    >
                      <option value={0}>低精力</option>
                      <option value={1}>中等精力</option>
                      <option value={2}>高度专注</option>
                    </select>
                  </div>
                  <div>
                    <button
                      type="button"
                      className="btn small"
                      disabled={isBusy || draftSteps.length <= 1}
                      onClick={() => removeStep(index)}
                    >
                      移除
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>

          {validationError ? (
            <div className="field-error" role="alert">
              {validationError}
            </div>
          ) : null}

          <div className="goal-form-actions">
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <button
                type="button"
                className="btn"
                disabled={isBusy}
                onClick={() => navigate('/goals')}
              >
                取消
              </button>
              <button
                type="button"
                className="btn"
                disabled={isBusy}
                onClick={() =>
                  void generateDraftAction.run({
                    title: draftTitle.trim() || title.trim(),
                    deadline: draftDeadline,
                  })
                }
              >
                {generateDraftAction.isPending ? '重新生成中…' : '重新生成草稿'}
              </button>
            </div>
            <div className="goal-form-buttons">
              <button
                type="button"
                className="btn"
                disabled={isBusy}
                onClick={() => handleSave(false)}
              >
                {saveGoalAction.isPending ? '保存中…' : '仅保存目标'}
              </button>
              <button
                type="button"
                className="btn primary"
                disabled={isBusy}
                onClick={() => handleSave(true)}
              >
                {saveGoalAction.isPending ? '保存中…' : '保存并安排第一步'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
