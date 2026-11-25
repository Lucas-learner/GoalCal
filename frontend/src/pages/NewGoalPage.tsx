import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { invalidateAfterWrite } from '../api/queryKeys';
import type {
  Draft,
  DraftStep,
  GoalIntakeOption,
  GoalIntakeResponse,
} from '../api/types';
import { AiProgressHint, type AiProgressStage } from '../components/AiProgressHint';
import { ErrorBanner } from '../components/StateFeedback';
import {
  addDaysIsoDate,
  addMonthsIsoDate,
  todayIsoDateInZone,
} from '../lib/datetime';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useI18n } from '../i18n';
import { useAppState } from '../state/AppStateContext';

type DeadlinePreset = '7d' | '1m' | '3m' | 'custom' | 'none';

export function NewGoalPage({ timezone }: { timezone: string }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { t } = useI18n();
  const {
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

  // Calibration state (when goal intake spots a gap/conflict before drafting)
  const [intake, setIntake] = useState<GoalIntakeResponse | null>(null);
  const [calibrationTitle, setCalibrationTitle] = useState('');
  const [titleManuallyEdited, setTitleManuallyEdited] = useState(false);
  const [calibrationDeadline, setCalibrationDeadline] = useState<string | null>(null);
  const [selectedOptions, setSelectedOptions] = useState<Record<string, GoalIntakeOption>>({});
  const [calibrationNote, setCalibrationNote] = useState('');
  const [calibratedRaw, setCalibratedRaw] = useState<string | undefined>(undefined);

  // Draft editing state
  const [draft, setDraft] = useState<Draft | null>(null);
  const [draftTitle, setDraftTitle] = useState('');
  const [draftDeadline, setDraftDeadline] = useState<string | null>(null);
  const [draftSteps, setDraftSteps] = useState<DraftStep[]>([]);
  const [showRegenPanel, setShowRegenPanel] = useState(false);
  const [regenInstruction, setRegenInstruction] = useState('');

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
    async (
      body: {
        title: string;
        raw?: string;
        level?: string;
        target?: string;
        instruction?: string;
        current_steps?: DraftStep[];
        current_outline?: string[];
        deadline: string | null;
      },
      key: string,
    ) => api.createGoalDraft(body, key),
    [],
  );

  const generateDraftAction = useIdempotentAction(generateDraftFn, {
    onSuccess: (res, args) => {
      setCalibratedRaw(args.raw);
      setDraft(res);
      setDraftTitle(res.title);
      setDraftDeadline(res.deadline);
      setDraftSteps(res.steps.map((s) => ({ ...s })));
      setShowRegenPanel(false);
      setRegenInstruction('');
      setValidationError(null);
    },
  });

  const evaluateIntakeFn = useCallback(
    async (body: { description: string; deadline: string | null }, key: string) =>
      api.evaluateGoalIntake(body, key),
    [],
  );

  const intakeAction = useIdempotentAction(evaluateIntakeFn, {
    onSuccess: (res, args) => {
      setValidationError(null);
      if (res?.needs_calibration && Array.isArray(res.questions) && res.questions.length > 0) {
        const nextTitle = (res.suggested_title || '').trim() || args.description;
        setIntake(res);
        setCalibrationTitle(nextTitle);
        setTitleManuallyEdited(false);
        setCalibrationDeadline(args.deadline);
        setSelectedOptions({});
        setCalibrationNote('');
        return;
      }
      void generateDraftAction.run({
        title: (res?.suggested_title || '').trim() || args.description,
        raw: args.description,
        target: res?.suggested_outcome || undefined,
        deadline: args.deadline,
      });
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
      setValidationError(t('请先填写你想实现的目标。', 'Please describe the goal you want to achieve.'));
      return;
    }
    if (preset === 'custom' && !customDate) {
      setValidationError(t('请选择具体的目标截止日期。', 'Please pick a target deadline date.'));
      return;
    }
    void intakeAction.run({
      description: trimmed,
      deadline: computedDeadline(),
    });
  };

  const isFixedDeadlineSelected = Object.entries(selectedOptions).some(
    ([qId, opt]) => /deadline/i.test(qId) && /fixed/i.test(opt.id),
  );

  const handleSelectOption = (questionId: string, opt: GoalIntakeOption) => {
    const pickedFixed = /deadline/i.test(questionId) && /fixed/i.test(opt.id);
    setSelectedOptions((prev) => {
      const next: Record<string, GoalIntakeOption> = { ...prev, [questionId]: opt };
      if (pickedFixed) {
        for (const [k, v] of Object.entries(next)) {
          if (k !== questionId && v.suggested_deadline) {
            delete next[k];
          }
        }
      }
      return next;
    });
    if (pickedFixed) {
      setCalibrationDeadline(computedDeadline());
      if (!titleManuallyEdited && intake?.suggested_title) {
        setCalibrationTitle(intake.suggested_title);
      }
      return;
    }
    if (opt.suggested_title && !titleManuallyEdited) {
      setCalibrationTitle(opt.suggested_title);
    }
    if (opt.suggested_deadline) {
      setCalibrationDeadline(opt.suggested_deadline);
    } else if (/stage|narrow|focus|keep/i.test(opt.id)) {
      setCalibrationDeadline(computedDeadline());
    }
  };

  const handleCalibrationSubmit = (keepOriginal: boolean) => {
    setValidationError(null);
    const orig = title.trim();
    if (keepOriginal) {
      void generateDraftAction.run({
        title: calibrationTitle.trim() || intake?.suggested_title || orig,
        raw: orig,
        deadline: computedDeadline(),
      });
      return;
    }
    const finalTitle = calibrationTitle.trim();
    if (!finalTitle) {
      setValidationError(t('目标标题不能为空。', 'Goal title cannot be empty.'));
      return;
    }
    const notes: string[] = [orig];
    for (const q of intake?.questions ?? []) {
      const picked = selectedOptions[q.id];
      if (picked) {
        notes.push(picked.adjusted_scope || `${q.prompt} ${picked.label}`);
      }
    }
    if (calibrationNote.trim()) {
      notes.push(calibrationNote.trim());
    }
    void generateDraftAction.run({
      title: finalTitle,
      raw: notes.join('\n'),
      target: intake?.suggested_outcome || undefined,
      deadline: calibrationDeadline,
    });
  };

  const handleSave = (scheduleFirst: boolean) => {
    if (!draft) return;
    setValidationError(null);
    const trimmedTitle = draftTitle.trim();
    if (!trimmedTitle) {
      setValidationError(t('目标标题不能为空。', 'Goal title cannot be empty.'));
      return;
    }
    if (draftSteps.length === 0) {
      setValidationError(t('请至少保留一个执行步骤。', 'Please keep at least one action step.'));
      return;
    }
    for (let i = 0; i < draftSteps.length; i++) {
      const st = draftSteps[i];
      if (!st.title.trim()) {
        setValidationError(t(`第 ${i + 1} 步的标题不能为空。`, `Step ${i + 1} title cannot be empty.`));
        return;
      }
      if (!Number.isFinite(st.est_min) || st.est_min < 5 || st.est_min > 240) {
        setValidationError(
          t(
            `第 ${i + 1} 步的预计时长须在 5 至 240 分钟之间。`,
            `Step ${i + 1} duration must be between 5 and 240 minutes.`,
          ),
        );
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
          milestone: s.milestone.trim() || t('自定义阶段', 'Custom phase'),
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
      t('执行步骤', 'Action step');
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

  const isBusy = intakeAction.isPending || generateDraftAction.isPending || saveGoalAction.isPending;

  const intakeStages: readonly AiProgressStage[] = [
    { afterMs: 0, label: t('正在理解目标与时间约束…', 'Understanding your goal and timeline…') },
    { afterMs: 3000, label: t('正在评估期限与范围是否需要校准…', 'Checking whether scope or deadline needs calibration…') },
    { afterMs: 8000, label: t('仍在细化校准建议，请稍候…', 'Still refining calibration suggestions…') },
  ];

  const createDraftStages: readonly AiProgressStage[] = [
    { afterMs: 0, label: t('正在整理目标阶段大纲…', 'Mapping out goal phases…') },
    { afterMs: 3000, label: t('正在拆解可执行步骤与最小产出…', 'Breaking down actionable steps and outcomes…') },
    { afterMs: 8000, label: t('正在估算每步时长与精力节奏…', 'Estimating step durations and energy pace…') },
    { afterMs: 16000, label: t('AI 仍在打磨细节，马上就好…', 'Still polishing step details, almost ready…') },
  ];

  const regenDraftStages: readonly AiProgressStage[] = [
    { afterMs: 0, label: t('正在结合你的调整偏好重新评估…', 'Reviewing your revision preferences…') },
    { afterMs: 3000, label: t('正在重排阶段与步骤粒度…', 'Reworking phases and step granularity…') },
    { afterMs: 9000, label: t('正在收敛新的草稿方案，请稍候…', 'Finalizing the revised draft…') },
  ];

  const activeDraftBusy = intakeAction.isPending || generateDraftAction.isPending;
  const activeDraftStages = intakeAction.isPending
    ? intakeStages
    : draft
      ? regenDraftStages
      : createDraftStages;

  return (
    <div className="new-goal-wrap">
      <div className="topbar">
        <h1>
          {draft
            ? t('调整计划草稿', 'Review plan draft')
            : intake
              ? t('先确认目标与节奏', 'Calibrate goal & pace')
              : t('你想实现什么？', 'What do you want to achieve?')}
        </h1>
      </div>

      <p className="subtitle">
        {draft
          ? t('确认或调整步骤后再保存。', 'Review or adjust steps before saving.')
          : intake
            ? t('确认时间性质与范围，生成的步骤才会真正适合你。', 'Clarify constraints and scope so the draft fits your reality.')
            : t('输入目标与期限，生成可编辑步骤。', 'Describe your goal and timeline to draft editable steps.')}
      </p>

      <ErrorBanner
        error={intakeAction.actionError ?? generateDraftAction.actionError ?? saveGoalAction.actionError}
      />

      {!draft && !intake ? (
        <form className="card goal-form" onSubmit={handleGenerate} noValidate>
          <div className="field">
            <label className="field-label" htmlFor="new-goal-title-input">
              {t('目标', 'Goal')}
            </label>
            <textarea
              id="new-goal-title-input"
              className="goal-input"
              placeholder={t(
                '例如：我希望在 30 天内发布第一条关于个人知识管理的短视频。',
                'e.g. Publish my first short video about personal knowledge management within 30 days.',
              )}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              disabled={isBusy}
            />
          </div>

          <div className="field">
            <span className="field-label" id="deadline-group-label">
              {t('期限', 'Deadline')}
            </span>
            <div className="deadline-options" role="group" aria-labelledby="deadline-group-label">
              {[
                { key: '7d' as const, label: t('7 天', '7 days') },
                { key: '1m' as const, label: t('1 个月', '1 month') },
                { key: '3m' as const, label: t('3 个月', '3 months') },
                { key: 'custom' as const, label: t('选择日期', 'Pick date') },
                { key: 'none' as const, label: t('无期限', 'No deadline') },
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
                  {t('自定义截止日期', 'Custom deadline')}
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

          <AiProgressHint active={activeDraftBusy} stages={activeDraftStages} />

          <div className="goal-form-actions" style={{ justifyContent: 'flex-end' }}>
            <div className="goal-form-buttons">
              <button
                type="button"
                className="btn"
                disabled={isBusy}
                onClick={() => navigate('/goals')}
              >
                {t('取消', 'Cancel')}
              </button>
              <button type="submit" className="btn primary" disabled={isBusy}>
                {intakeAction.isPending
                  ? t('分析中…', 'Analyzing…')
                  : generateDraftAction.isPending
                    ? t('拆解中…', 'Drafting…')
                    : t('生成草稿', 'Generate draft')}
              </button>
            </div>
          </div>
        </form>
      ) : !draft && intake ? (
        <div className="card goal-form">
          {intake.reason ? (
            <div className="status-banner info" role="status">
              <div className="status-banner-body">{intake.reason}</div>
            </div>
          ) : null}

          {intake.questions.map((q) => {
            const visibleOptions = q.options.filter(
              (opt) => !(isFixedDeadlineSelected && !/deadline/i.test(q.id) && Boolean(opt.suggested_deadline)),
            );
            if (visibleOptions.length === 0) return null;
            return (
              <div key={q.id} className="field">
                <div className="field-label">{q.prompt}</div>
                <div className="deadline-options" role="group" aria-label={q.prompt}>
                  {visibleOptions.map((opt) => {
                    const selected = selectedOptions[q.id]?.id === opt.id;
                    return (
                      <button
                        key={opt.id}
                        type="button"
                        className={`deadline-option${selected ? ' selected' : ''}`}
                        aria-pressed={selected}
                        disabled={isBusy}
                        onClick={() => handleSelectOption(q.id, opt)}
                      >
                        {opt.label}
                      </button>
                    );
                  })}
                </div>
              </div>
            );
          })}

          <div className="field">
            <label className="field-label" htmlFor="calibration-title-input">
              {t('目标标题', 'Goal title')}
            </label>
            <input
              id="calibration-title-input"
              type="text"
              className="input"
              value={calibrationTitle}
              disabled={isBusy}
              onChange={(e) => {
                setTitleManuallyEdited(true);
                setCalibrationTitle(e.target.value);
              }}
            />
          </div>

          <div className="field">
            <label className="field-label" htmlFor="calibration-deadline-input">
              {t('目标期限（选填）', 'Target date (optional)')}
            </label>
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
              <input
                id="calibration-deadline-input"
                type="date"
                className="input"
                style={{ maxWidth: 220 }}
                value={calibrationDeadline ?? ''}
                disabled={isBusy}
                onChange={(e) => setCalibrationDeadline(e.target.value || null)}
              />
              {calibrationDeadline ? (
                <button
                  type="button"
                  className="btn small"
                  disabled={isBusy}
                  onClick={() => setCalibrationDeadline(null)}
                >
                  {t('清除期限', 'Clear')}
                </button>
              ) : null}
            </div>
          </div>

          <div className="field">
            <label className="field-label" htmlFor="calibration-note-input">
              {t('补充说明（选填）', 'Note (optional)')}
            </label>
            <input
              id="calibration-note-input"
              type="text"
              className="input"
              placeholder={t('例如：每周只有周六能投入 3 小时', 'e.g. Only 3 hours available on Saturdays')}
              value={calibrationNote}
              disabled={isBusy}
              onChange={(e) => setCalibrationNote(e.target.value)}
            />
          </div>

          {validationError ? (
            <div className="field-error" role="alert">
              {validationError}
            </div>
          ) : null}

          <AiProgressHint active={generateDraftAction.isPending} stages={createDraftStages} />

          <div className="goal-form-actions">
            <button
              type="button"
              className="btn"
              disabled={isBusy}
              onClick={() => {
                setIntake(null);
                setValidationError(null);
              }}
            >
              {t('返回修改', 'Back')}
            </button>
            <div className="goal-form-buttons">
              <button
                type="button"
                className="btn"
                disabled={isBusy}
                onClick={() => handleCalibrationSubmit(true)}
              >
                {t('按原目标生成', 'Keep original')}
              </button>
              <button
                type="button"
                className="btn primary"
                disabled={isBusy}
                onClick={() => handleCalibrationSubmit(false)}
              >
                {generateDraftAction.isPending ? t('生成中…', 'Generating…') : t('生成草稿', 'Generate draft')}
              </button>
            </div>
          </div>
        </div>
      ) : draft ? (
        <div className="card goal-form">
          <div className="status-banner info" role="status">
            <div className="status-banner-body">
              <div className="status-banner-title">
                {draft.source === 'llm'
                  ? t('AI 生成草稿', 'AI-tailored draft')
                  : t(
                      `模板草稿（${draft.source}）：由内置模板生成，保存前可修改`,
                      `Template draft (${draft.source}): preset fallback, edit before saving`,
                    )}
              </div>
            </div>
          </div>

          <div className="field">
            <label className="field-label" htmlFor="draft-title-input">
              {t('目标标题', 'Goal title')}
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
              {t('截止日期（选填）', 'Deadline (optional)')}
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
                  {t('清除期限', 'Clear deadline')}
                </button>
              ) : null}
            </div>
          </div>

          {draft.outline.length > 0 ? (
            <div className="field">
              <div className="field-label">{t('阶段大纲', 'Phase outline')}</div>
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
              <span className="section-title">
                {t(`可编辑步骤（${draftSteps.length}）`, `Editable steps (${draftSteps.length})`)}
              </span>
              <button type="button" className="btn small" disabled={isBusy} onClick={addStep}>
                {t('+ 添加步骤', '+ Add step')}
              </button>
            </div>

            {draftSteps.map((step, index) => (
              <div key={index} className="draft-step-card">
                <div>
                  <label className="field-label" htmlFor={`draft-step-title-${index}`}>
                    {t(`步骤 ${index + 1}`, `Step ${index + 1}`)}
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

                <div className="draft-step-grid">
                  <div>
                    <label className="field-label" htmlFor={`draft-step-outcome-${index}`}>
                      {t('产出', 'Outcome')}
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
                  <div>
                    <label className="field-label" htmlFor={`draft-step-est-${index}`}>
                      {t('分钟', 'Minutes')}
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
                    <button
                      type="button"
                      className="btn draft-step-remove-btn"
                      disabled={isBusy || draftSteps.length <= 1}
                      onClick={() => removeStep(index)}
                    >
                      {t('移除', 'Remove')}
                    </button>
                  </div>
                </div>
              </div>
            ))}
          </div>

          {showRegenPanel ? (
            <div className="window-row" style={{ marginTop: 12 }}>
              <label className="field-label" htmlFor="draft-regen-instruction">
                {t('希望如何调整当前方案？', 'How would you like to revise this draft?')}
              </label>
              <div className="deadline-options" style={{ marginBottom: 4 }}>
                {[
                  t('每步拆短到 20 分钟内', 'Keep steps under 20 min'),
                  t('多安排动手实操', 'More hands-on practice'),
                  t('节奏更宽松一点', 'Lighter overall pace'),
                ].map((chip) => (
                  <button
                    key={chip}
                    type="button"
                    className={`deadline-option${regenInstruction === chip ? ' selected' : ''}`}
                    disabled={isBusy}
                    onClick={() => setRegenInstruction(chip)}
                  >
                    {chip}
                  </button>
                ))}
              </div>
              <input
                id="draft-regen-instruction"
                type="text"
                className="input"
                placeholder={t('例如：减少前期准备，直接开始第一个小作品', 'e.g. Less prep, start with a small hands-on piece')}
                value={regenInstruction}
                disabled={isBusy}
                onChange={(e) => setRegenInstruction(e.target.value)}
              />
              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
                <button
                  type="button"
                  className="btn small"
                  disabled={isBusy}
                  onClick={() => setShowRegenPanel(false)}
                >
                  {t('取消', 'Cancel')}
                </button>
                <button
                  type="button"
                  className="btn small primary"
                  disabled={isBusy}
                  onClick={() =>
                    void generateDraftAction.run({
                      title: draftTitle.trim() || title.trim(),
                      raw: calibratedRaw,
                      instruction: regenInstruction.trim() || t('结合当前方案重新优化步骤安排', 'Refine steps based on current plan'),
                      current_steps: draftSteps.map((s) => ({
                        ...s,
                        title: s.title.trim() || t('未命名步骤', 'Untitled step'),
                        outcome: s.outcome.trim(),
                        milestone: s.milestone.trim() || t('自定义阶段', 'Custom phase'),
                        est_min: Math.max(5, Math.min(240, Math.round(s.est_min) || 30)),
                      })),
                      current_outline: draft.outline,
                      deadline: draftDeadline,
                    })
                  }
                >
                  {generateDraftAction.isPending ? t('生成中…', 'Regenerating…') : t('按此偏好重新生成', 'Apply & regenerate')}
                </button>
              </div>
            </div>
          ) : null}

          {validationError ? (
            <div className="field-error" role="alert">
              {validationError}
            </div>
          ) : null}

          <AiProgressHint active={generateDraftAction.isPending} stages={regenDraftStages} />

          <div className="goal-form-actions">
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <button
                type="button"
                className="btn"
                disabled={isBusy}
                onClick={() => navigate('/goals')}
              >
                {t('取消', 'Cancel')}
              </button>
              <button
                type="button"
                className="btn"
                disabled={isBusy}
                onClick={() => setShowRegenPanel((prev) => !prev)}
              >
                {generateDraftAction.isPending ? t('生成中…', 'Regenerating…') : t('重新生成', 'Regenerate')}
              </button>
            </div>
            <div className="goal-form-buttons">
              <button
                type="button"
                className="btn"
                disabled={isBusy}
                onClick={() => handleSave(false)}
              >
                {saveGoalAction.isPending ? t('保存中…', 'Saving…') : t('仅保存', 'Save only')}
              </button>
              <button
                type="button"
                className="btn primary"
                disabled={isBusy}
                onClick={() => handleSave(true)}
              >
                {saveGoalAction.isPending ? t('保存中…', 'Saving…') : t('保存并安排第一步', 'Save & schedule step 1')}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
