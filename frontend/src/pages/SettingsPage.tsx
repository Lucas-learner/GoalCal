import { useCallback, useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { invalidateAfterWrite, queryKeys } from '../api/queryKeys';
import type { Availability, FeedbackCategory, Goal, User } from '../api/types';
import { LogOutIcon } from '../components/Icons';
import { Modal } from '../components/Modal';
import { EmptyState, ErrorBanner, SkeletonCard } from '../components/StateFeedback';
import { WindowGroupEditor, validateWindowGroup } from '../components/TimePreferencesEditor';
import { type LangPreference, useI18n } from '../i18n';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useAppState } from '../state/AppStateContext';

const MAX_ICS_BYTES = 5 * 1024 * 1024;

type SettingsSectionId = 'account' | 'schedule' | 'archived' | 'import' | 'metrics';

const FEEDBACK_CATEGORIES: Array<{ value: FeedbackCategory; zh: string; en: string }> = [
  { value: '功能问题', zh: '功能问题', en: 'Bug or feature issue' },
  { value: 'AI 理解或计划问题', zh: 'AI 理解或计划问题', en: 'AI understanding or plan' },
  { value: '速度或连接问题', zh: '速度或连接问题', en: 'Speed or connection' },
  { value: '建议或其他', zh: '建议或其他', en: 'Suggestion or other' },
];

function formatPct(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  return `${Math.round(value * 100)}%`;
}

export function SettingsPage({ user }: { user: User | undefined }) {
  const queryClient = useQueryClient();
  const { recordMutationResult } = useAppState();
  const { pref, setPreference, t } = useI18n();
  const [section, setSection] = useState<SettingsSectionId>('account');

  const availabilityQuery = useQuery({
    queryKey: queryKeys.availability,
    queryFn: api.getAvailability,
  });

  const metricsQuery = useQuery({
    queryKey: queryKeys.metrics,
    queryFn: api.getMetrics,
  });

  const archivedGoalsQuery = useQuery({
    queryKey: [...queryKeys.goals, 'with-archived'],
    queryFn: api.getGoalsWithArchived,
    enabled: section === 'archived',
  });

  const [confirmDeleteArchivedGoal, setConfirmDeleteArchivedGoal] = useState<Goal | null>(null);

  const [draftAvail, setDraftAvail] = useState<Availability | null>(null);
  const [dirty, setDirty] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);

  // Sync from server only when not dirty (preserves edits across 409 refresh!)
  useEffect(() => {
    if (availabilityQuery.data && !dirty) {
      setDraftAvail(structuredClone(availabilityQuery.data));
    }
  }, [availabilityQuery.data, dirty]);

  const saveLangFn = useCallback(
    async (langChoice: '' | 'zh' | 'en', key: string) => api.updateMe({ lang: langChoice }, key),
    [],
  );

  const saveLangAction = useIdempotentAction(saveLangFn, {
    onSuccess: async (updatedUser) => {
      queryClient.setQueryData(queryKeys.me, updatedUser);
      await invalidateAfterWrite(queryClient, { includeAvailability: true });
    },
  });

  const handleLanguageChange = (nextPref: LangPreference) => {
    setPreference(nextPref);
    void saveLangAction.run(nextPref === 'auto' ? '' : nextPref);
  };

  const saveFn = useCallback(
    async (body: Availability, key: string) => api.saveAvailability(body, key),
    [],
  );

  const saveAction = useIdempotentAction(saveFn, {
    onSuccess: async (saved) => {
      setDirty(false);
      setValidationError(null);
      setDraftAvail(structuredClone(saved));
      queryClient.setQueryData(queryKeys.availability, saved);
      recordMutationResult({ message: t('设置已保存', 'Settings saved') });
      await invalidateAfterWrite(queryClient, { includeAvailability: true });
    },
    onError: async (err) => {
      if (err.status === 409) {
        await queryClient.invalidateQueries({ queryKey: queryKeys.availability });
      }
    },
  });

  // ICS import state
  const [icsFile, setIcsFile] = useState<File | null>(null);
  const [icsLocalError, setIcsLocalError] = useState<string | null>(null);
  const [icsResultMsg, setIcsResultMsg] = useState<string | null>(null);

  // Feedback state
  const [feedbackCategory, setFeedbackCategory] = useState<FeedbackCategory>('功能问题');
  const [feedbackMessage, setFeedbackMessage] = useState('');
  const [feedbackLocalError, setFeedbackLocalError] = useState<string | null>(null);
  const [feedbackResultMsg, setFeedbackResultMsg] = useState<string | null>(null);

  const importFn = useCallback(
    async (icsText: string, key: string) => api.importCalendarIcs({ ics: icsText }, key),
    [],
  );

  const importAction = useIdempotentAction(importFn, {
    onSuccess: async (res) => {
      setIcsLocalError(null);
      setIcsResultMsg(t(`${res.message}（共处理 ${res.count} 条日程）`, `${res.message} (${res.count} processed)`));
      recordMutationResult({ message: t(`${res.message}（${res.count} 条）`, `${res.message} (${res.count})`) });
      await invalidateAfterWrite(queryClient);
    },
  });

  const feedbackFn = useCallback(
    async (args: { category: FeedbackCategory; message: string }, key: string) =>
      api.submitFeedback(args, key),
    [],
  );

  const feedbackAction = useIdempotentAction(feedbackFn, {
    onSuccess: (res) => {
      setFeedbackLocalError(null);
      setFeedbackMessage('');
      setFeedbackResultMsg(res.message);
      recordMutationResult({ message: res.message });
    },
  });

  const restoreGoalFn = useCallback(
    async (args: { id: number; version: string }, key: string) =>
      api.updateGoal(args.id, { version: args.version, status: 'active' }, key),
    [],
  );

  const restoreGoalAction = useIdempotentAction(restoreGoalFn, {
    onSuccess: async (res, args) => {
      recordMutationResult(res);
      await invalidateAfterWrite(queryClient, { goalId: args.id });
    },
    onError: async (err, args) => {
      if (err.status === 409) {
        await invalidateAfterWrite(queryClient, { goalId: args.id });
      }
    },
  });

  const deleteArchivedGoalFn = useCallback(
    async (args: { id: number; version: string }, key: string) =>
      api.deleteGoal(args.id, { version: args.version }, key),
    [],
  );

  const deleteArchivedGoalAction = useIdempotentAction(deleteArchivedGoalFn, {
    onSuccess: async (res, args) => {
      recordMutationResult(res);
      setConfirmDeleteArchivedGoal(null);
      queryClient.removeQueries({ queryKey: queryKeys.goal(args.id) });
      await invalidateAfterWrite(queryClient);
    },
    onError: async (err, args) => {
      if (err.status === 409 || err.status === 404) {
        setConfirmDeleteArchivedGoal(null);
        await invalidateAfterWrite(queryClient, { goalId: args.id });
      }
    },
  });

  const handleSaveAvailability = (e: React.FormEvent) => {
    e.preventDefault();
    if (!draftAvail) return;
    setValidationError(null);

    const err =
      validateWindowGroup(t('可用时间', 'Available windows'), draftAvail.available_windows, t) ??
      validateWindowGroup(t('保护时段', 'Protected windows'), draftAvail.protected_windows, t) ??
      validateWindowGroup(t('偏好时段', 'Preferred windows'), draftAvail.preferred_windows, t);

    if (err) {
      setValidationError(err);
      return;
    }

    if (
      !Number.isFinite(draftAvail.buffer_minutes) ||
      draftAvail.buffer_minutes < 0 ||
      draftAvail.buffer_minutes > 120
    ) {
      setValidationError(t('日程缓冲时间须为 0 至 120 分钟。', 'Buffer time must be between 0 and 120 minutes.'));
      return;
    }

    if (
      !Number.isFinite(draftAvail.max_goal_minutes_per_day) ||
      draftAvail.max_goal_minutes_per_day < 1 ||
      draftAvail.max_goal_minutes_per_day > 1440
    ) {
      setValidationError(
        t(
          '每天最多投入目标时间须为 1 至 1440 分钟。',
          'Daily goal limit must be between 1 and 1440 minutes.',
        ),
      );
      return;
    }

    // The version belongs to the draft the user edited; a background refresh must not overwrite concurrent edits.
    void saveAction.run(draftAvail);
  };

  const handleImportIcs = async () => {
    setIcsLocalError(null);
    setIcsResultMsg(null);
    if (!icsFile) {
      setIcsLocalError(t('请先选择一个 .ics 日历文件。', 'Please choose a .ics calendar file first.'));
      return;
    }
    if (icsFile.size > MAX_ICS_BYTES) {
      setIcsLocalError(t('ICS 文件大小不能超过 5 MB。', 'ICS file size cannot exceed 5 MB.'));
      return;
    }
    try {
      const text = await icsFile.text();
      if (!text.includes('BEGIN:VCALENDAR')) {
        setIcsLocalError(
          t(
            '文件内容不是有效的 ICS 日历格式（缺少 BEGIN:VCALENDAR）。',
            'File is not a valid ICS calendar (missing BEGIN:VCALENDAR).',
          ),
        );
        return;
      }
      await importAction.run(text);
    } catch {
      setIcsLocalError(t('读取本地文件失败，请重新选择文件。', 'Failed to read local file; please select it again.'));
    }
  };

  const handleSubmitFeedback = (e: React.FormEvent) => {
    e.preventDefault();
    setFeedbackLocalError(null);
    setFeedbackResultMsg(null);
    const trimmed = feedbackMessage.trim();
    if (trimmed.length < 5 || trimmed.length > 2000) {
      setFeedbackLocalError(
        t('请输入 5–2000 字的反馈内容。', 'Please enter 5–2000 characters of feedback.'),
      );
      return;
    }
    void feedbackAction.run({ category: feedbackCategory, message: trimmed });
  };

  const metrics = metricsQuery.data;
  const acceptanceRate = metrics?.acceptance_rate ?? metrics?.acceptance ?? null;
  const recoveryRate = metrics?.recovery_rate_72h ?? metrics?.recovery_72h ?? null;
  const progressRate = metrics?.progress_rate ?? metrics?.progress ?? null;
  const archivedGoals = (archivedGoalsQuery.data?.goals ?? []).filter((g) => g.status === 'archived');
  const isArchivedBusy = restoreGoalAction.isPending || deleteArchivedGoalAction.isPending;

  return (
    <div className="settings-wrap">
      <div className="topbar">
        <h1>{t('设置', 'Settings')}</h1>
      </div>

      <p className="subtitle">
        {t('让计划适应你的生活节奏，而不是要求你适应系统。', 'Let your plan adapt to your life, not the other way around.')}
      </p>

      <div className="settings-layout">
        <nav className="settings-nav" aria-label={t('设置分组', 'Settings sections')}>
          {[
            { id: 'account' as const, label: t('通用与账号', 'General & Account') },
            { id: 'schedule' as const, label: t('时间偏好', 'Time Preferences') },
            { id: 'archived' as const, label: t('已归档目标', 'Archived goals') },
            { id: 'import' as const, label: t('日历导入', 'Calendar Import') },
            { id: 'metrics' as const, label: t('指标与反馈', 'Metrics & Feedback') },
          ].map((tab) => (
            <button
              key={tab.id}
              type="button"
              className={`settings-nav-btn${section === tab.id ? ' active' : ''}`}
              aria-current={section === tab.id ? 'true' : undefined}
              onClick={() => setSection(tab.id)}
            >
              {tab.label}
            </button>
          ))}
        </nav>

        <div>
          {section === 'account' ? (
            <section className="settings-section" aria-label={t('通用与账号', 'General & Account')}>
              <div className="settings-section-title">{t('通用与账号', 'General & Account')}</div>
              <ErrorBanner error={saveLangAction.actionError} />
              <div className="card-flat settings-card">
                <div className="setting-row">
                  <div className="setting-name">{t('账号', 'Account')}</div>
                  <div className="setting-control" style={{ fontSize: 12, fontWeight: 600 }}>
                    {user?.name || '—'}
                  </div>
                </div>
                <div className="setting-row">
                  <div className="setting-name">{t('邮箱', 'Email')}</div>
                  <div className="setting-control" style={{ fontSize: 12 }}>
                    {user?.email || t('未绑定', 'Not set')}
                  </div>
                </div>
                <div className="setting-row">
                  <div className="setting-name">{t('时区', 'Time zone')}</div>
                  <div className="setting-control tabular-nums" style={{ fontSize: 12 }}>
                    {user?.timezone || '—'}
                  </div>
                </div>
                <div className="setting-row">
                  <label className="setting-name" htmlFor="setting-lang-select">
                    {t('语言', 'Language')}
                  </label>
                  <div className="setting-control">
                    <select
                      id="setting-lang-select"
                      className="select"
                      value={pref}
                      disabled={saveLangAction.isPending}
                      onChange={(e) => handleLanguageChange(e.target.value as LangPreference)}
                    >
                      <option value="auto">{t('跟随系统', 'Follow system')}</option>
                      <option value="zh">简体中文</option>
                      <option value="en">English</option>
                    </select>
                  </div>
                </div>
              </div>
              <form className="settings-logout-form" method="post" action="/_goalcal/logout?next=/app/">
                <button type="submit" className="settings-logout-btn">
                  <LogOutIcon />
                  <span>{t('退出登录', 'Log out')}</span>
                </button>
              </form>
            </section>
          ) : null}

          {section === 'schedule' ? (
            availabilityQuery.isLoading ? (
              <SkeletonCard label={t('加载中', 'Loading')} />
            ) : availabilityQuery.isError ? (
              <ErrorBanner
                error={availabilityQuery.error}
                onRetry={() => void availabilityQuery.refetch()}
              />
            ) : draftAvail ? (
              <form onSubmit={handleSaveAvailability} noValidate>
                <ErrorBanner
                  error={saveAction.actionError}
                  onRefresh={() => {
                    setDirty(false);
                    if (availabilityQuery.data) {
                      setDraftAvail(structuredClone(availabilityQuery.data));
                    }
                  }}
                />

                <section className="settings-section" aria-label={t('每日节奏', 'Daily pace')}>
                  <div className="settings-section-title">{t('每日节奏', 'Daily pace')}</div>
                  <div className="card-flat settings-card">
                    <div className="setting-row">
                      <label className="setting-name" htmlFor="pref-max-minutes">
                        {t('每日目标上限（分钟）', 'Daily goal cap (min)')}
                      </label>
                      <div className="setting-control">
                        <input
                          id="pref-max-minutes"
                          type="number"
                          min={1}
                          max={1440}
                          step={1}
                          className="input tabular-nums"
                          value={draftAvail.max_goal_minutes_per_day}
                          disabled={saveAction.isPending}
                          onChange={(e) => {
                            setDirty(true);
                            setDraftAvail({
                              ...draftAvail,
                              max_goal_minutes_per_day: Number(e.target.value) || 0,
                            });
                          }}
                        />
                      </div>
                    </div>

                    <div className="setting-row">
                      <label className="setting-name" htmlFor="pref-buffer-minutes">
                        {t('日程缓冲（分钟）', 'Event buffer (min)')}
                      </label>
                      <div className="setting-control">
                        <input
                          id="pref-buffer-minutes"
                          type="number"
                          min={0}
                          max={120}
                          step={5}
                          className="input tabular-nums"
                          value={draftAvail.buffer_minutes}
                          disabled={saveAction.isPending}
                          onChange={(e) => {
                            setDirty(true);
                            setDraftAvail({
                              ...draftAvail,
                              buffer_minutes: Number(e.target.value) || 0,
                            });
                          }}
                        />
                      </div>
                    </div>
                  </div>
                </section>

                <section className="settings-section" aria-label={t('每周时段', 'Weekly schedule')}>
                  <div className="settings-section-title">{t('每周时段', 'Weekly schedule')}</div>
                  <div className="card-flat availability-card">
                    <WindowGroupEditor
                      groupKey="avail"
                      title={t('可用时间', 'Available windows')}
                      emptyNotice={t('未配置（不参与排期）', 'None configured (no slots suggested)')}
                      defaultWindow={{ days: [0, 1, 2, 3, 4], start: '09:00', end: '18:00' }}
                      windows={draftAvail.available_windows}
                      disabled={saveAction.isPending}
                      onChange={(next) => {
                        setDirty(true);
                        setDraftAvail({ ...draftAvail, available_windows: next });
                      }}
                    />

                    <WindowGroupEditor
                      groupKey="preferred"
                      title={t('偏好时段', 'Preferred windows')}
                      emptyNotice={t('未设置', 'None')}
                      defaultWindow={{ days: [0, 1, 2, 3, 4], start: '09:00', end: '12:00' }}
                      windows={draftAvail.preferred_windows}
                      disabled={saveAction.isPending}
                      onChange={(next) => {
                        setDirty(true);
                        setDraftAvail({ ...draftAvail, preferred_windows: next });
                      }}
                    />

                    <WindowGroupEditor
                      groupKey="protected"
                      title={t('保护时段', 'Protected windows')}
                      emptyNotice={t('未设置', 'None')}
                      defaultWindow={{ days: [0, 1, 2, 3, 4], start: '12:00', end: '13:30' }}
                      windows={draftAvail.protected_windows}
                      disabled={saveAction.isPending}
                      onChange={(next) => {
                        setDirty(true);
                        setDraftAvail({ ...draftAvail, protected_windows: next });
                      }}
                    />
                  </div>
                </section>

                {validationError ? (
                  <div className="field-error" role="alert">
                    {validationError}
                  </div>
                ) : null}

                <div className="save-row">
                  {dirty && availabilityQuery.data ? (
                    <button
                      type="button"
                      className="btn"
                      disabled={saveAction.isPending}
                      onClick={() => {
                        setDirty(false);
                        setValidationError(null);
                        setDraftAvail(structuredClone(availabilityQuery.data!));
                      }}
                    >
                      {t('还原', 'Reset')}
                    </button>
                  ) : null}
                  <button type="submit" className="btn primary" disabled={saveAction.isPending}>
                    {saveAction.isPending ? t('保存中…', 'Saving…') : t('保存更改', 'Save changes')}
                  </button>
                </div>
              </form>
            ) : null
          ) : null}

          {section === 'archived' ? (
            <section className="settings-section" aria-label={t('已归档目标', 'Archived goals')}>
              <div className="settings-section-title">{t('已归档目标', 'Archived goals')}</div>
              <ErrorBanner error={restoreGoalAction.actionError ?? deleteArchivedGoalAction.actionError} />
              {archivedGoalsQuery.isLoading ? (
                <SkeletonCard label={t('加载中', 'Loading')} />
              ) : archivedGoalsQuery.isError ? (
                <ErrorBanner
                  error={archivedGoalsQuery.error}
                  onRetry={() => void archivedGoalsQuery.refetch()}
                />
              ) : archivedGoals.length === 0 ? (
                <EmptyState title={t('暂无已归档目标', 'No archived goals')} />
              ) : (
                <div className="card-flat settings-card">
                  {archivedGoals.map((goal) => {
                    const doneCount = goal.steps.filter((s) => s.status === 'done').length;
                    return (
                      <div key={goal.id} className="setting-row">
                        <div style={{ minWidth: 0 }}>
                          <div className="setting-name">{goal.title}</div>
                          <div className="setting-desc tabular-nums">
                            {doneCount} / {goal.steps.length} {t('步', 'steps')} · {Math.round((goal.progress ?? 0) * 100)}%
                          </div>
                        </div>
                        <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>
                          <button
                            type="button"
                            className="btn small"
                            disabled={isArchivedBusy}
                            onClick={() =>
                              void restoreGoalAction.run({
                                id: goal.id,
                                version: goal.version,
                              })
                            }
                          >
                            {t('恢复', 'Restore')}
                          </button>
                          <button
                            type="button"
                            className="btn small danger"
                            disabled={isArchivedBusy}
                            onClick={() => setConfirmDeleteArchivedGoal(goal)}
                          >
                            {t('删除', 'Delete')}
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </section>
          ) : null}

          {section === 'import' ? (
            <section className="settings-section" aria-label={t('日历导入', 'Calendar Import')}>
              <div className="settings-section-title">{t('日历导入（.ics）', 'Calendar import (.ics)')}</div>
              <div className="card-flat" style={{ padding: 18 }}>
                <ErrorBanner error={importAction.actionError} />
                {icsLocalError ? (
                  <div className="field-error" role="alert" style={{ marginBottom: 12 }}>
                    {icsLocalError}
                  </div>
                ) : null}
                {icsResultMsg ? (
                  <div className="status-banner info" role="status">
                    <div className="status-banner-body">{icsResultMsg}</div>
                  </div>
                ) : null}

                <div className="field">
                  <label className="field-label" htmlFor="ics-file-input">
                    {t('选择 .ics 文件（上限 5 MB）', 'Choose .ics file (max 5 MB)')}
                  </label>
                  <input
                    id="ics-file-input"
                    type="file"
                    accept=".ics,text/calendar"
                    className="input"
                    disabled={importAction.isPending}
                    onChange={(e) => {
                      setIcsLocalError(null);
                      setIcsFile(e.target.files?.[0] ?? null);
                    }}
                  />
                </div>
                <button
                  type="button"
                  className="btn primary"
                  disabled={importAction.isPending || !icsFile}
                  onClick={() => void handleImportIcs()}
                >
                  {importAction.isPending ? t('导入中…', 'Importing…') : t('导入', 'Import')}
                </button>
              </div>
            </section>
          ) : null}

          {section === 'metrics' ? (
            <>
              <section className="settings-section" aria-label={t('近 7 天指标', 'Last 7 days')}>
                <div className="settings-section-title">{t('近 7 天指标', 'Last 7 days')}</div>
                {metricsQuery.isLoading ? (
                  <SkeletonCard label={t('加载中', 'Loading')} />
                ) : metricsQuery.isError ? (
                  <ErrorBanner
                    error={metricsQuery.error}
                    onRetry={() => void metricsQuery.refetch()}
                  />
                ) : (
                  <div className="metrics-grid">
                    <div className="card-flat metric-card">
                      <div className="metric-label">{t('建议接受率', 'Proposal acceptance')}</div>
                      <div className="metric-value">{formatPct(acceptanceRate)}</div>
                    </div>
                    <div className="card-flat metric-card">
                      <div className="metric-label">{t('72h 恢复率', '72h recovery')}</div>
                      <div className="metric-value">{formatPct(recoveryRate)}</div>
                    </div>
                    <div className="card-flat metric-card">
                      <div className="metric-label">{t('推进率', 'Progress rate')}</div>
                      <div className="metric-value">{formatPct(progressRate)}</div>
                    </div>
                  </div>
                )}
              </section>

              <section className="settings-section" aria-label={t('意见反馈', 'Feedback')}>
                <div className="settings-section-title">{t('意见反馈', 'Feedback')}</div>
                <form className="card-flat" style={{ padding: 18 }} onSubmit={handleSubmitFeedback} noValidate>
                  <ErrorBanner error={feedbackAction.actionError} />
                  {feedbackLocalError ? (
                    <div className="field-error" role="alert" style={{ marginBottom: 12 }}>
                      {feedbackLocalError}
                    </div>
                  ) : null}
                  {feedbackResultMsg ? (
                    <div className="status-banner info" role="status">
                      <div className="status-banner-body">{feedbackResultMsg}</div>
                    </div>
                  ) : null}

                  <div className="field">
                    <label className="field-label" htmlFor="feedback-category-select">
                      {t('分类', 'Category')}
                    </label>
                    <select
                      id="feedback-category-select"
                      className="select"
                      value={feedbackCategory}
                      disabled={feedbackAction.isPending}
                      onChange={(e) => setFeedbackCategory(e.target.value as FeedbackCategory)}
                    >
                      {FEEDBACK_CATEGORIES.map((item) => (
                        <option key={item.value} value={item.value}>
                          {t(item.zh, item.en)}
                        </option>
                      ))}
                    </select>
                  </div>

                  <div className="field">
                    <label className="field-label" htmlFor="feedback-message-input">
                      {t('内容', 'Message')}
                    </label>
                    <textarea
                      id="feedback-message-input"
                      className="goal-input"
                      style={{ minHeight: 96 }}
                      placeholder={t('描述问题或建议（5–2000 字）…', 'Describe the issue or suggestion (5–2000 chars)…')}
                      value={feedbackMessage}
                      disabled={feedbackAction.isPending}
                      onChange={(e) => setFeedbackMessage(e.target.value)}
                    />
                  </div>

                  <button type="submit" className="btn primary" disabled={feedbackAction.isPending}>
                    {feedbackAction.isPending ? t('提交中…', 'Sending…') : t('提交反馈', 'Submit feedback')}
                  </button>
                </form>
              </section>
            </>
          ) : null}
        </div>
      </div>

      {confirmDeleteArchivedGoal ? (
        <Modal
          title={t('确认删除目标', 'Delete goal')}
          onClose={() => {
            if (!deleteArchivedGoalAction.isPending) setConfirmDeleteArchivedGoal(null);
          }}
        >
          <p className="muted" style={{ fontSize: 13, lineHeight: 1.6, marginTop: 0 }}>
            {t(
              `删除「${confirmDeleteArchivedGoal.title}」及其全部步骤和关联日程？此操作不可撤销。`,
              `Delete "${confirmDeleteArchivedGoal.title}" and all its steps and scheduled blocks? This cannot be undone.`,
            )}
          </p>
          <ErrorBanner error={deleteArchivedGoalAction.actionError} />
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 18 }}>
            <button
              type="button"
              className="btn"
              disabled={deleteArchivedGoalAction.isPending}
              onClick={() => setConfirmDeleteArchivedGoal(null)}
            >
              {t('取消', 'Cancel')}
            </button>
            <button
              type="button"
              className="btn danger"
              disabled={deleteArchivedGoalAction.isPending}
              onClick={() =>
                void deleteArchivedGoalAction.run({
                  id: confirmDeleteArchivedGoal.id,
                  version: confirmDeleteArchivedGoal.version,
                })
              }
            >
              {deleteArchivedGoalAction.isPending ? t('删除中…', 'Deleting…') : t('确认删除', 'Confirm delete')}
            </button>
          </div>
        </Modal>
      ) : null}
    </div>
  );
}
