import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { invalidateAfterWrite, queryKeys } from '../api/queryKeys';
import type { Availability, FeedbackCategory, Goal, Habit, User } from '../api/types';
import { CalendarSyncSection } from '../components/CalendarSyncSection';
import { LogOutIcon } from '../components/Icons';
import { Modal } from '../components/Modal';
import { RemindersSection } from '../components/RemindersSection';
import { ErrorBanner, SkeletonCard } from '../components/StateFeedback';
import { WindowGroupEditor, validateWindowGroup } from '../components/TimePreferencesEditor';
import { type LangPreference, useI18n } from '../i18n';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useAppState } from '../state/AppStateContext';
import { type CalendarPalette, type ThemePreference, useTheme } from '../theme';

type SettingsSectionId = 'account' | 'schedule' | 'reminders' | 'sync' | 'archived' | 'feedback';

const SETTINGS_SECTIONS: readonly SettingsSectionId[] = [
  'account',
  'schedule',
  'reminders',
  'sync',
  'archived',
  'feedback',
];

const COMMON_TIMEZONES: Array<{ value: string; zh: string; en: string }> = [
  { value: 'Asia/Shanghai', zh: '中国标准时间（Asia/Shanghai）', en: 'China (Asia/Shanghai)' },
  { value: 'Asia/Hong_Kong', zh: '香港时间（Asia/Hong_Kong）', en: 'Hong Kong (Asia/Hong_Kong)' },
  { value: 'Asia/Taipei', zh: '台北时间（Asia/Taipei）', en: 'Taipei (Asia/Taipei)' },
  { value: 'Asia/Tokyo', zh: '东京（Asia/Tokyo）', en: 'Tokyo (Asia/Tokyo)' },
  { value: 'Asia/Singapore', zh: '新加坡（Asia/Singapore）', en: 'Singapore (Asia/Singapore)' },
  { value: 'Europe/London', zh: '伦敦（Europe/London）', en: 'London (Europe/London)' },
  { value: 'Europe/Paris', zh: '巴黎（Europe/Paris）', en: 'Paris (Europe/Paris)' },
  { value: 'Europe/Berlin', zh: '柏林（Europe/Berlin）', en: 'Berlin (Europe/Berlin)' },
  { value: 'America/New_York', zh: '美国东部（America/New_York）', en: 'US Eastern (America/New_York)' },
  { value: 'America/Chicago', zh: '美国中部（America/Chicago）', en: 'US Central (America/Chicago)' },
  { value: 'America/Los_Angeles', zh: '美国太平洋（America/Los_Angeles）', en: 'US Pacific (America/Los_Angeles)' },
  { value: 'Australia/Sydney', zh: '悉尼（Australia/Sydney）', en: 'Sydney (Australia/Sydney)' },
  { value: 'UTC', zh: '协调世界时（UTC）', en: 'UTC' },
];

function parseSettingsSection(raw: string | null): SettingsSectionId {
  if (raw === 'metrics') return 'feedback';
  if (raw && (SETTINGS_SECTIONS as readonly string[]).includes(raw)) {
    return raw as SettingsSectionId;
  }
  return 'account';
}

const FEEDBACK_CATEGORIES: Array<{ value: FeedbackCategory; zh: string; en: string }> = [
  { value: '功能问题', zh: '功能问题', en: 'Bug or feature issue' },
  { value: 'AI 理解或计划问题', zh: 'AI 理解或计划问题', en: 'AI understanding or plan' },
  { value: '速度或连接问题', zh: '速度或连接问题', en: 'Speed or connection' },
  { value: '建议或其他', zh: '建议或其他', en: 'Suggestion or other' },
];

export function SettingsPage({ user }: { user: User | undefined }) {
  const queryClient = useQueryClient();
  const { recordMutationResult } = useAppState();
  const { pref, setPreference, t } = useI18n();
  const {
    pref: themePref,
    setPreference: setThemePreference,
    calendarPalette,
    setCalendarPalette,
  } = useTheme();
  const [searchParams, setSearchParams] = useSearchParams();
  const [section, setSectionState] = useState<SettingsSectionId>(() =>
    parseSettingsSection(searchParams.get('section')),
  );

  useEffect(() => {
    const param = searchParams.get('section');
    if (param) {
      setSectionState(parseSettingsSection(param));
    }
  }, [searchParams]);

  const setSection = useCallback(
    (next: SettingsSectionId) => {
      setSectionState(next);
      setSearchParams(next === 'account' ? {} : { section: next }, { replace: true });
    },
    [setSearchParams],
  );

  const availabilityQuery = useQuery({
    queryKey: queryKeys.availability,
    queryFn: api.getAvailability,
    enabled: section === 'schedule',
  });

  const archivedGoalsQuery = useQuery({
    queryKey: [...queryKeys.goals, 'with-archived'],
    queryFn: api.getGoalsWithArchived,
    enabled: section === 'archived',
  });

  const archivedHabitsQuery = useQuery({
    queryKey: queryKeys.habits,
    queryFn: api.getHabits,
    enabled: section === 'archived',
  });

  const [confirmDeleteArchivedGoal, setConfirmDeleteArchivedGoal] = useState<Goal | null>(null);
  const [confirmDeleteArchivedHabit, setConfirmDeleteArchivedHabit] = useState<Habit | null>(null);

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

  const saveThemeFn = useCallback(
    async (themeChoice: '' | 'light' | 'dark', key: string) => api.updateMe({ theme: themeChoice }, key),
    [],
  );

  const saveThemeAction = useIdempotentAction(saveThemeFn, {
    onSuccess: (updatedUser) => {
      queryClient.setQueryData(queryKeys.me, updatedUser);
    },
  });

  const handleThemeChange = (nextPref: ThemePreference) => {
    setThemePreference(nextPref);
    void saveThemeAction.run(nextPref === 'auto' ? '' : nextPref);
  };

  const saveCalendarPaletteFn = useCallback(
    async (paletteChoice: '' | 'calm' | 'mono' | 'vivid', key: string) =>
      api.updateMe({ calendar_palette: paletteChoice }, key),
    [],
  );

  const saveCalendarPaletteAction = useIdempotentAction(saveCalendarPaletteFn, {
    onSuccess: (updatedUser) => {
      queryClient.setQueryData(queryKeys.me, updatedUser);
    },
  });

  const handleCalendarPaletteChange = (nextPalette: CalendarPalette) => {
    setCalendarPalette(nextPalette);
    void saveCalendarPaletteAction.run(nextPalette);
  };

  const saveTimezoneFn = useCallback(
    async (nextTz: string, key: string) => api.updateMe({ timezone: nextTz }, key),
    [],
  );

  const saveTimezoneAction = useIdempotentAction(saveTimezoneFn, {
    onSuccess: async (updatedUser) => {
      queryClient.setQueryData(queryKeys.me, updatedUser);
      recordMutationResult({ message: t('时区已更新', 'Time zone updated') });
      await Promise.all([
        invalidateAfterWrite(queryClient, { includeAvailability: true }),
        queryClient.invalidateQueries({ queryKey: queryKeys.reminders }),
      ]);
    },
  });

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

  // Feedback state
  const [feedbackCategory, setFeedbackCategory] = useState<FeedbackCategory>('功能问题');
  const [feedbackMessage, setFeedbackMessage] = useState('');
  const [feedbackLocalError, setFeedbackLocalError] = useState<string | null>(null);
  const [feedbackResultMsg, setFeedbackResultMsg] = useState<string | null>(null);

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

  const restoreHabitFn = useCallback(
    async (args: { id: number; version: string }, key: string) =>
      api.changeHabitStatus(args.id, { version: args.version, status: 'active' }, key),
    [],
  );

  const restoreHabitAction = useIdempotentAction(restoreHabitFn, {
    onSuccess: async (res) => {
      recordMutationResult(res);
      await invalidateAfterWrite(queryClient);
    },
    onError: async (err) => {
      if (err.status === 409) {
        await invalidateAfterWrite(queryClient);
      }
    },
  });

  const deleteArchivedHabitFn = useCallback(
    async (args: { id: number; version: string }, key: string) =>
      api.deleteHabit(args.id, { version: args.version }, key),
    [],
  );

  const deleteArchivedHabitAction = useIdempotentAction(deleteArchivedHabitFn, {
    onSuccess: async (res, args) => {
      recordMutationResult(res);
      setConfirmDeleteArchivedHabit(null);
      queryClient.removeQueries({ queryKey: queryKeys.habit(args.id) });
      await invalidateAfterWrite(queryClient);
    },
    onError: async (err) => {
      if (err.status === 409 || err.status === 404) {
        setConfirmDeleteArchivedHabit(null);
        await invalidateAfterWrite(queryClient);
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
      validateWindowGroup(t('可用时间', 'Available windows'), draftAvail.available_windows, t, 1) ??
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
      setValidationError(t('任务间隔须为 0 至 120 分钟。', 'Break between tasks must be between 0 and 120 minutes.'));
      return;
    }

    if (
      !Number.isFinite(draftAvail.max_goal_minutes_per_day) ||
      draftAvail.max_goal_minutes_per_day < 1 ||
      draftAvail.max_goal_minutes_per_day > 1440
    ) {
      setValidationError(
        t(
          '每日任务上限须为 1 至 1440 分钟。',
          'Daily task limit must be between 1 and 1440 minutes.',
        ),
      );
      return;
    }

    // The version belongs to the draft the user edited; a background refresh must not overwrite concurrent edits.
    void saveAction.run(draftAvail);
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

  const archivedGoals = (archivedGoalsQuery.data?.goals ?? []).filter((g) => g.status === 'archived');
  const archivedHabits = (archivedHabitsQuery.data?.habits ?? []).filter((h) => h.status === 'archived');
  const isArchivedBusy =
    restoreGoalAction.isPending ||
    restoreHabitAction.isPending ||
    deleteArchivedGoalAction.isPending ||
    deleteArchivedHabitAction.isPending;

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
            { id: 'reminders' as const, label: t('提醒与通知', 'Reminders') },
            { id: 'sync' as const, label: t('日历同步', 'Calendar Sync') },
            { id: 'archived' as const, label: t('已归档', 'Archived') },
            { id: 'feedback' as const, label: t('意见反馈', 'Feedback') },
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
              <ErrorBanner
                error={
                  saveLangAction.actionError ??
                  saveThemeAction.actionError ??
                  saveCalendarPaletteAction.actionError ??
                  saveTimezoneAction.actionError
                }
              />
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
                  <label className="setting-name" htmlFor="setting-timezone-select">
                    {t('时区', 'Time zone')}
                  </label>
                  <div className="setting-control">
                    <select
                      id="setting-timezone-select"
                      className="select"
                      value={user?.timezone || 'Asia/Shanghai'}
                      disabled={saveTimezoneAction.isPending}
                      onChange={(e) => void saveTimezoneAction.run(e.target.value)}
                    >
                      {user?.timezone && !COMMON_TIMEZONES.some((tz) => tz.value === user.timezone) ? (
                        <option value={user.timezone}>{user.timezone}</option>
                      ) : null}
                      {COMMON_TIMEZONES.map((tz) => (
                        <option key={tz.value} value={tz.value}>
                          {t(tz.zh, tz.en)}
                        </option>
                      ))}
                    </select>
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
                <div className="setting-row">
                  <label className="setting-name" htmlFor="setting-theme-select">
                    {t('外观', 'Appearance')}
                  </label>
                  <div className="setting-control">
                    <select
                      id="setting-theme-select"
                      className="select"
                      value={themePref}
                      disabled={saveThemeAction.isPending}
                      onChange={(e) => handleThemeChange(e.target.value as ThemePreference)}
                    >
                      <option value="auto">{t('跟随系统', 'Follow system')}</option>
                      <option value="light">{t('浅色', 'Light')}</option>
                      <option value="dark">{t('深色', 'Dark')}</option>
                    </select>
                  </div>
                </div>
                <div className="setting-row">
                  <label className="setting-name" htmlFor="setting-cal-palette-select">
                    {t('日历配色', 'Calendar colors')}
                  </label>
                  <div className="setting-control">
                    <select
                      id="setting-cal-palette-select"
                      className="select"
                      value={calendarPalette}
                      disabled={saveCalendarPaletteAction.isPending}
                      onChange={(e) => handleCalendarPaletteChange(e.target.value as CalendarPalette)}
                    >
                      <option value="calm">{t('矿物柔彩（默认）', 'Mineral (default)')}</option>
                      <option value="mono">{t('纸墨单色', 'Monochrome')}</option>
                      <option value="vivid">{t('明亮彩块', 'Vivid')}</option>
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
                        {t('每日任务上限（分钟）', 'Daily task limit (min)')}
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
                      <div className="setting-name">
                        <label htmlFor="pref-buffer-minutes">
                          {t('任务间隔（分钟）', 'Break between tasks (min)')}
                        </label>
                        <div className="setting-hint">
                          {t(
                            '相邻日程或任务之间预留的切换时间，填 0 允许紧挨着排',
                            'Gap reserved between events or tasks; set 0 for back-to-back',
                          )}
                        </div>
                      </div>
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
                      emptyNotice={t('至少保留 1 个可用时段', 'Keep at least 1 available window')}
                      defaultWindow={{ days: [0, 1, 2, 3, 4], start: '09:00', end: '18:00' }}
                      windows={draftAvail.available_windows}
                      minWindows={1}
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
            <section className="settings-section" aria-label={t('已归档', 'Archived')}>
              <div className="settings-section-title">{t('已归档目标', 'Archived goals')}</div>
              <ErrorBanner
                error={
                  restoreGoalAction.actionError ??
                  restoreHabitAction.actionError ??
                  deleteArchivedGoalAction.actionError
                }
              />
              {archivedGoalsQuery.isLoading ? (
                <SkeletonCard label={t('加载中', 'Loading')} />
              ) : archivedGoalsQuery.isError ? (
                <ErrorBanner
                  error={archivedGoalsQuery.error}
                  onRetry={() => void archivedGoalsQuery.refetch()}
                />
              ) : archivedGoals.length === 0 ? (
                <div className="card-flat settings-card">
                  <div className="setting-row">
                    <div className="setting-name muted" style={{ fontWeight: 400 }}>
                      {t('暂无已归档目标', 'No archived goals')}
                    </div>
                  </div>
                </div>
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

              <div className="settings-section-title" style={{ marginTop: 28 }}>
                {t('已归档习惯', 'Archived habits')}
              </div>
              {archivedHabitsQuery.isLoading ? (
                <SkeletonCard label={t('加载中', 'Loading')} />
              ) : archivedHabitsQuery.isError ? (
                <ErrorBanner
                  error={archivedHabitsQuery.error}
                  onRetry={() => void archivedHabitsQuery.refetch()}
                />
              ) : archivedHabits.length === 0 ? (
                <div className="card-flat settings-card">
                  <div className="setting-row">
                    <div className="setting-name muted" style={{ fontWeight: 400 }}>
                      {t('暂无已归档习惯', 'No archived habits')}
                    </div>
                  </div>
                </div>
              ) : (
                <div className="card-flat settings-card">
                  {archivedHabits.map((habit) => (
                    <div key={habit.id} className="setting-row">
                      <div style={{ minWidth: 0 }}>
                        <div className="setting-name">{habit.rule.title}</div>
                        <div className="setting-desc">
                          {habit.rule.mode === 'weekly'
                            ? t(`每周 ${habit.rule.weekly_target} 次`, `${habit.rule.weekly_target} times per week`)
                            : habit.rule.weekdays.length === 7
                              ? t('每天', 'Every day')
                              : t(`每周 ${habit.rule.weekdays.length} 天`, `${habit.rule.weekdays.length} days per week`)}
                          {' · '}
                          {habit.rule.est_min} {t('分钟', 'min')}
                        </div>
                      </div>
                      <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>
                        <button
                          type="button"
                          className="btn small"
                          disabled={isArchivedBusy}
                          onClick={() =>
                            void restoreHabitAction.run({
                              id: habit.id,
                              version: habit.version,
                            })
                          }
                        >
                          {t('恢复', 'Restore')}
                        </button>
                        <button
                          type="button"
                          className="btn small danger"
                          disabled={isArchivedBusy}
                          onClick={() => setConfirmDeleteArchivedHabit(habit)}
                        >
                          {t('删除', 'Delete')}
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </section>
          ) : null}

          {section === 'reminders' ? (
            <RemindersSection
              timezone={user?.timezone || 'UTC'}
              onGoToSync={() => setSection('sync')}
            />
          ) : null}

          {section === 'sync' ? (
            <CalendarSyncSection timezone={user?.timezone || 'UTC'} />
          ) : null}

          {section === 'feedback' ? (
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

      {confirmDeleteArchivedHabit ? (
        <Modal
          title={t('确认删除习惯', 'Delete habit')}
          onClose={() => {
            if (!deleteArchivedHabitAction.isPending) setConfirmDeleteArchivedHabit(null);
          }}
        >
          <p className="muted" style={{ fontSize: 13, lineHeight: 1.6, marginTop: 0 }}>
            {t(
              `删除「${confirmDeleteArchivedHabit.rule.title}」及其全部记录和关联日程？此操作不可撤销。`,
              `Delete "${confirmDeleteArchivedHabit.rule.title}" and all its records and scheduled sessions? This cannot be undone.`,
            )}
          </p>
          <ErrorBanner error={deleteArchivedHabitAction.actionError} />
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 18 }}>
            <button
              type="button"
              className="btn"
              disabled={deleteArchivedHabitAction.isPending}
              onClick={() => setConfirmDeleteArchivedHabit(null)}
            >
              {t('取消', 'Cancel')}
            </button>
            <button
              type="button"
              className="btn danger"
              disabled={deleteArchivedHabitAction.isPending}
              onClick={() =>
                void deleteArchivedHabitAction.run({
                  id: confirmDeleteArchivedHabit.id,
                  version: confirmDeleteArchivedHabit.version,
                })
              }
            >
              {deleteArchivedHabitAction.isPending ? t('删除中…', 'Deleting…') : t('确认删除', 'Confirm delete')}
            </button>
          </div>
        </Modal>
      ) : null}
    </div>
  );
}
