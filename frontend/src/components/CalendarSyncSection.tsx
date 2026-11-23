import { useCallback, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { invalidateAfterWrite, queryKeys } from '../api/queryKeys';
import type { CalendarFeed, CalendarSource } from '../api/types';
import { useI18n } from '../i18n';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useAppState } from '../state/AppStateContext';
import { Modal } from './Modal';
import { ErrorBanner, SkeletonCard } from './StateFeedback';

const MAX_ICS_BYTES = 5 * 1024 * 1024;

const SYNC_INTERVAL_OPTIONS: Array<{ value: number; zh: string; en: string }> = [
  { value: 15, zh: '每 15 分钟', en: 'Every 15 min' },
  { value: 30, zh: '每 30 分钟', en: 'Every 30 min' },
  { value: 60, zh: '每 1 小时', en: 'Every 1 hour' },
  { value: 180, zh: '每 3 小时', en: 'Every 3 hours' },
  { value: 360, zh: '每 6 小时', en: 'Every 6 hours' },
  { value: 1440, zh: '每天 1 次', en: 'Once daily' },
];

function formatTimestamp(iso: string | null, timezone: string, lang: 'zh' | 'en'): string {
  if (!iso) return lang === 'zh' ? '尚未同步' : 'Never';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  try {
    return new Intl.DateTimeFormat(lang === 'zh' ? 'zh-CN' : 'en-US', {
      timeZone: timezone || 'UTC',
      month: 'numeric',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    }).format(date);
  } catch {
    return date.toLocaleString();
  }
}

function formatInterval(minutes: number, lang: 'zh' | 'en'): string {
  const preset = SYNC_INTERVAL_OPTIONS.find((item) => item.value === minutes);
  if (preset) return lang === 'zh' ? preset.zh : preset.en;
  if (minutes % 60 === 0) {
    const hrs = minutes / 60;
    return lang === 'zh' ? `每 ${hrs} 小时` : `Every ${hrs}h`;
  }
  return lang === 'zh' ? `每 ${minutes} 分钟` : `Every ${minutes} min`;
}

function buildFeedUrls(path: string | null): { httpUrl: string; webcalUrl: string } | null {
  if (!path) return null;
  if (typeof window === 'undefined' || !window.location?.host) {
    return { httpUrl: path, webcalUrl: path };
  }
  const httpUrl = `${window.location.origin}${path}`;
  const webcalUrl = `webcal://${window.location.host}${path}`;
  return { httpUrl, webcalUrl };
}

function truncateUrl(url: string): string {
  if (url.length <= 68) return url;
  return `${url.slice(0, 44)}…${url.slice(-18)}`;
}

export function CalendarSyncSection({ timezone }: { timezone: string }) {
  const queryClient = useQueryClient();
  const { recordMutationResult } = useAppState();
  const { lang, t } = useI18n();

  const syncQuery = useQuery({
    queryKey: queryKeys.calendarSync,
    queryFn: api.getCalendarSync,
  });

  // Outbound feed UI state
  const [copiedFeed, setCopiedFeed] = useState(false);
  const [confirmRotateFeed, setConfirmRotateFeed] = useState(false);

  // Inbound add source form state
  const [sourceName, setSourceName] = useState('');
  const [sourceUrl, setSourceUrl] = useState('');
  const [sourceInterval, setSourceInterval] = useState(30);
  const [sourceLocalError, setSourceLocalError] = useState<string | null>(null);
  const [syncStatusMsg, setSyncStatusMsg] = useState<string | null>(null);

  // Edit / delete source modals
  const [editingSource, setEditingSource] = useState<CalendarSource | null>(null);
  const [editName, setEditName] = useState('');
  const [editUrl, setEditUrl] = useState('');
  const [editInterval, setEditInterval] = useState(30);
  const [editError, setEditError] = useState<string | null>(null);
  const [confirmDeleteSource, setConfirmDeleteSource] = useState<CalendarSource | null>(null);
  const [syncingSourceId, setSyncingSourceId] = useState<number | null>(null);

  // Manual ICS file import state
  const [icsFile, setIcsFile] = useState<File | null>(null);
  const [icsLocalError, setIcsLocalError] = useState<string | null>(null);
  const [icsResultMsg, setIcsResultMsg] = useState<string | null>(null);

  const updateFeedFn = useCallback(
    async (
      args: {
        enabled: boolean;
        include_blocks: boolean;
        include_habits: boolean;
        include_manual: boolean;
        version?: string;
      },
      key: string,
    ) => api.updateCalendarFeed(args, key),
    [],
  );

  const updateFeedAction = useIdempotentAction(updateFeedFn, {
    onSuccess: async (res) => {
      queryClient.setQueryData(queryKeys.calendarSync, (prev: { feed: CalendarFeed; sources: CalendarSource[] } | undefined) =>
        prev ? { ...prev, feed: res.feed } : undefined,
      );
      recordMutationResult({ message: res.message });
      await queryClient.invalidateQueries({ queryKey: queryKeys.calendarSync });
    },
    onError: async (err) => {
      if (err.status === 409) {
        await queryClient.invalidateQueries({ queryKey: queryKeys.calendarSync });
      }
    },
  });

  const rotateFeedFn = useCallback(
    async (version: string | undefined, key: string) =>
      api.rotateCalendarFeed(version ? { version } : {}, key),
    [],
  );

  const rotateFeedAction = useIdempotentAction(rotateFeedFn, {
    onSuccess: async (res) => {
      setConfirmRotateFeed(false);
      setCopiedFeed(false);
      queryClient.setQueryData(queryKeys.calendarSync, (prev: { feed: CalendarFeed; sources: CalendarSource[] } | undefined) =>
        prev ? { ...prev, feed: res.feed } : undefined,
      );
      recordMutationResult({ message: res.message });
      await queryClient.invalidateQueries({ queryKey: queryKeys.calendarSync });
    },
    onError: async (err) => {
      if (err.status === 409) {
        setConfirmRotateFeed(false);
        await queryClient.invalidateQueries({ queryKey: queryKeys.calendarSync });
      }
    },
  });

  const createSourceFn = useCallback(
    async (args: { name: string; url: string; sync_interval_minutes: number }, key: string) =>
      api.createCalendarSource(args, key),
    [],
  );

  const createSourceAction = useIdempotentAction(createSourceFn, {
    onSuccess: async (res) => {
      setSourceName('');
      setSourceUrl('');
      setSourceInterval(30);
      setSourceLocalError(null);
      setSyncStatusMsg(res.message);
      recordMutationResult({ message: res.message });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.calendarSync }),
        invalidateAfterWrite(queryClient),
      ]);
    },
  });

  const updateSourceFn = useCallback(
    async (
      args: {
        id: number;
        version: string;
        name?: string;
        url?: string;
        enabled?: boolean;
        sync_interval_minutes?: number;
      },
      key: string,
    ) => {
      const { id, ...body } = args;
      return api.updateCalendarSource(id, body, key);
    },
    [],
  );

  const updateSourceAction = useIdempotentAction(updateSourceFn, {
    onSuccess: async (res) => {
      setEditingSource(null);
      setEditError(null);
      recordMutationResult({ message: res.message });
      await queryClient.invalidateQueries({ queryKey: queryKeys.calendarSync });
    },
    onError: async (err) => {
      if (err.status === 409) {
        await queryClient.invalidateQueries({ queryKey: queryKeys.calendarSync });
      }
    },
  });

  const syncSourceFn = useCallback(
    async (source: CalendarSource, key: string) => {
      setSyncingSourceId(source.id);
      try {
        return await api.syncCalendarSource(source.id, {}, key);
      } finally {
        setSyncingSourceId(null);
      }
    },
    [],
  );

  const syncSourceAction = useIdempotentAction(syncSourceFn, {
    onSuccess: async (res) => {
      setSyncStatusMsg(res.message);
      recordMutationResult({ message: res.message });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.calendarSync }),
        invalidateAfterWrite(queryClient),
      ]);
    },
  });

  const deleteSourceFn = useCallback(
    async (args: { id: number; version: string }, key: string) =>
      api.deleteCalendarSource(args.id, { version: args.version }, key),
    [],
  );

  const deleteSourceAction = useIdempotentAction(deleteSourceFn, {
    onSuccess: async (res) => {
      setConfirmDeleteSource(null);
      setSyncStatusMsg(res.message);
      recordMutationResult({ message: res.message });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.calendarSync }),
        invalidateAfterWrite(queryClient),
      ]);
    },
    onError: async (err) => {
      if (err.status === 409 || err.status === 404) {
        setConfirmDeleteSource(null);
        await queryClient.invalidateQueries({ queryKey: queryKeys.calendarSync });
      }
    },
  });

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

  const feed = syncQuery.data?.feed;
  const sources = syncQuery.data?.sources ?? [];
  const feedUrls = buildFeedUrls(feed?.path ?? null);

  const handleFeedChange = (patch: Partial<Pick<CalendarFeed, 'enabled' | 'include_blocks' | 'include_habits' | 'include_manual'>>) => {
    const base = feed ?? {
      enabled: false,
      include_blocks: true,
      include_habits: true,
      include_manual: true,
      version: null,
    };
    void updateFeedAction.run({
      enabled: patch.enabled ?? base.enabled,
      include_blocks: patch.include_blocks ?? base.include_blocks,
      include_habits: patch.include_habits ?? base.include_habits,
      include_manual: patch.include_manual ?? base.include_manual,
      version: base.version ?? undefined,
    });
  };

  const handleCopyFeedUrl = async () => {
    if (!feedUrls) return;
    try {
      await navigator.clipboard.writeText(feedUrls.httpUrl);
      setCopiedFeed(true);
      recordMutationResult({ message: t('已复制订阅链接', 'Subscription link copied') });
      setTimeout(() => setCopiedFeed(false), 2500);
    } catch {
      recordMutationResult({ message: t('请手动选中上方链接复制', 'Select and copy the link above') });
    }
  };

  const handleAddSource = (e: React.FormEvent) => {
    e.preventDefault();
    setSourceLocalError(null);
    setSyncStatusMsg(null);
    const trimmedName = sourceName.trim();
    const trimmedUrl = sourceUrl.trim();
    if (!trimmedName) {
      setSourceLocalError(t('请填写日历名称（例如：公司日历 / iCloud）。', 'Please enter a calendar name (e.g. Work / iCloud).'));
      return;
    }
    if (!/^(https?|webcals?):\/\//i.test(trimmedUrl)) {
      setSourceLocalError(
        t(
          '请输入以 https://、http:// 或 webcal:// 开头的日历订阅链接。',
          'Please enter a calendar URL starting with https://, http://, or webcal://.',
        ),
      );
      return;
    }
    void createSourceAction.run({
      name: trimmedName,
      url: trimmedUrl,
      sync_interval_minutes: sourceInterval,
    });
  };

  const openEditSource = (source: CalendarSource) => {
    setEditingSource(source);
    setEditName(source.name);
    setEditUrl(source.url);
    setEditInterval(source.sync_interval_minutes);
    setEditError(null);
  };

  const handleSaveEditSource = (e: React.FormEvent) => {
    e.preventDefault();
    if (!editingSource) return;
    setEditError(null);
    const trimmedName = editName.trim();
    const trimmedUrl = editUrl.trim();
    if (!trimmedName) {
      setEditError(t('请填写日历名称。', 'Please enter a calendar name.'));
      return;
    }
    if (!/^(https?|webcals?):\/\//i.test(trimmedUrl)) {
      setEditError(
        t(
          '请输入以 https://、http:// 或 webcal:// 开头的日历订阅链接。',
          'Please enter a calendar URL starting with https://, http://, or webcal://.',
        ),
      );
      return;
    }
    void updateSourceAction.run({
      id: editingSource.id,
      version: editingSource.version,
      name: trimmedName,
      url: trimmedUrl,
      sync_interval_minutes: editInterval,
    });
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

  return (
    <>
      <section className="settings-section" aria-label={t('导出到外部日历', 'Subscribe in external calendars')}>
        <div className="settings-section-title">
          {t('导出到外部日历（.ics 订阅）', 'Export to external calendars (.ics feed)')}
        </div>
        <p className="settings-section-copy">
          {t(
            '生成专属只读订阅链接，在 Apple 日历、Google Calendar、Outlook 等日历中查看 GoalCal 安排。自动排除外部导入日程，避免重复。',
            'Generate a private read-only subscription URL for Apple Calendar, Google Calendar, Outlook, and more. External imported events are excluded to prevent duplicates.',
          )}
        </p>

        {syncQuery.isLoading ? (
          <SkeletonCard label={t('正在加载同步设置', 'Loading sync settings')} />
        ) : syncQuery.isError ? (
          <ErrorBanner error={syncQuery.error} onRetry={() => void syncQuery.refetch()} />
        ) : (
          <div className="card-flat sync-card">
            <ErrorBanner error={updateFeedAction.actionError ?? rotateFeedAction.actionError} />

            <div className="sync-feed-header">
              <div>
                <div className="setting-name">{t('启用订阅链接', 'Enable subscription feed')}</div>
                <div className="sync-hint">
                  {feed?.enabled
                    ? t('任何持有该链接的日历客户端均可只读拉取你的 GoalCal 日程，请妥善保管。', 'Anyone with this private link can read your GoalCal schedule. Rotate it anytime if leaked.')
                    : t('启用后将为你生成一个专属 .ics 订阅链接。', 'Enable to generate your private .ics subscription URL.')}
                </div>
              </div>
              <button
                type="button"
                className={feed?.enabled ? 'btn small' : 'btn primary small'}
                disabled={updateFeedAction.isPending || rotateFeedAction.isPending}
                onClick={() => handleFeedChange({ enabled: !(feed?.enabled ?? false) })}
              >
                {updateFeedAction.isPending
                  ? t('更新中…', 'Updating…')
                  : feed?.enabled
                    ? t('停用链接', 'Disable')
                    : t('生成订阅链接', 'Enable feed')}
              </button>
            </div>

            <div className="sync-scope-group" role="group" aria-label={t('导出范围', 'Included items')}>
              <span className="sync-scope-label">{t('包含内容', 'Include')}</span>
              <label className="sync-check">
                <input
                  type="checkbox"
                  checked={feed?.include_blocks ?? true}
                  disabled={updateFeedAction.isPending || !feed?.enabled}
                  onChange={(e) => handleFeedChange({ include_blocks: e.target.checked })}
                />
                <span>{t('目标执行块', 'Goal blocks')}</span>
              </label>
              <label className="sync-check">
                <input
                  type="checkbox"
                  checked={feed?.include_habits ?? true}
                  disabled={updateFeedAction.isPending || !feed?.enabled}
                  onChange={(e) => handleFeedChange({ include_habits: e.target.checked })}
                />
                <span>{t('习惯安排', 'Habit sessions')}</span>
              </label>
              <label className="sync-check">
                <input
                  type="checkbox"
                  checked={feed?.include_manual ?? true}
                  disabled={updateFeedAction.isPending || !feed?.enabled}
                  onChange={(e) => handleFeedChange({ include_manual: e.target.checked })}
                />
                <span>{t('手动添加的日程', 'Manual events')}</span>
              </label>
            </div>

            {feed?.enabled && feedUrls ? (
              <div className="sync-feed-box">
                <label className="field-label" htmlFor="calendar-feed-url">
                  {t('专属订阅链接', 'Private subscription URL')}
                </label>
                <div className="sync-url-row">
                  <input
                    id="calendar-feed-url"
                    type="text"
                    readOnly
                    className="input sync-url-input tabular-nums"
                    value={feedUrls.httpUrl}
                    onFocus={(e) => e.currentTarget.select()}
                  />
                  <button type="button" className="btn small" onClick={() => void handleCopyFeedUrl()}>
                    {copiedFeed ? t('已复制', 'Copied') : t('复制链接', 'Copy')}
                  </button>
                </div>

                <div className="sync-feed-actions">
                  <a className="btn small" href={feedUrls.webcalUrl}>
                    {t('在系统日历打开', 'Open in Calendar app')}
                  </a>
                  <button
                    type="button"
                    className="btn small"
                    disabled={rotateFeedAction.isPending}
                    onClick={() => setConfirmRotateFeed(true)}
                  >
                    {t('重置链接', 'Rotate link')}
                  </button>
                  {feed.last_accessed_at ? (
                    <span className="sync-meta tabular-nums">
                      {t('上次外部拉取：', 'Last fetched: ')}
                      {formatTimestamp(feed.last_accessed_at, timezone, lang)}
                    </span>
                  ) : null}
                </div>

                <details className="sync-guide">
                  <summary>{t('如何订阅到 Apple / Google / Outlook 日历？', 'How to subscribe in Apple / Google / Outlook?')}</summary>
                  <ul>
                    <li>
                      <strong>{t('Apple 日历（iPhone / Mac）', 'Apple Calendar (iOS / macOS)')}</strong>：
                      {t(
                        '点击上方「在系统日历打开」，或在日历 App 中选择「添加订阅日历」粘贴链接，建议自动刷新设为 15 分钟。',
                        'Click "Open in Calendar app" above, or add a Subscription Calendar and paste the URL (set auto-refresh to 15 min).',
                      )}
                    </li>
                    <li>
                      <strong>Google Calendar</strong>：
                      {t(
                        '网页端左侧「其他日历」→「+」→「通过网址」粘贴链接。提示：Google 对 ICS 订阅的刷新周期通常为 12–24 小时。',
                        'On web, open "Other calendars" → "+" → "From URL" and paste the link. Note: Google refreshes ICS feeds every 12–24 hours.',
                      )}
                    </li>
                    <li>
                      <strong>Outlook / Microsoft 365</strong>：
                      {t(
                        '日历页「添加日历」→「从 Web 订阅」，粘贴链接并命名即可。',
                        'Choose "Add calendar" → "Subscribe from web" and paste the link.',
                      )}
                    </li>
                  </ul>
                </details>
              </div>
            ) : null}
          </div>
        )}
      </section>

      <section className="settings-section" aria-label={t('自动同步外部日历', 'External calendar subscriptions')}>
        <div className="settings-section-title">
          {t('自动同步外部日历（订阅源）', 'Auto-sync external calendars')}
        </div>
        <p className="settings-section-copy">
          {t(
            '添加 Apple、Google、Outlook、飞书等外部日历的 .ics / webcal:// 私密链接，GoalCal 会按频率自动拉取更新并清理已取消日程，排期时自动避让。',
            'Connect private .ics or webcal:// URLs from Apple, Google, Outlook, or other calendars. GoalCal refreshes them automatically and clears cancelled events.',
          )}
        </p>

        <div className="card-flat sync-card">
          <ErrorBanner error={createSourceAction.actionError ?? updateSourceAction.actionError ?? syncSourceAction.actionError ?? deleteSourceAction.actionError} />
          {syncStatusMsg ? (
            <div className="status-banner info" role="status" style={{ marginBottom: 14 }}>
              <div className="status-banner-body">{syncStatusMsg}</div>
            </div>
          ) : null}

          {sources.length > 0 ? (
            <div className="sync-source-list">
              {sources.map((src) => {
                const isSyncingThis = syncingSourceId === src.id && syncSourceAction.isPending;
                const statusBadge = !src.enabled
                  ? { cls: 'chip', text: t('已暂停', 'Paused') }
                  : src.last_status === 'error'
                    ? { cls: 'chip sync-chip-error', text: t('同步失败', 'Error') }
                    : src.last_status === 'ok'
                      ? { cls: 'chip sync-chip-ok', text: t('已同步', 'Synced') }
                      : { cls: 'chip', text: t('待同步', 'Pending') };

                return (
                  <div key={src.id} className="sync-source-item">
                    <div className="sync-source-head">
                      <div className="sync-source-title-row">
                        <span className="sync-source-name">{src.name}</span>
                        <span className="chip">{t('ICS 订阅', 'ICS feed')}</span>
                        <span className={statusBadge.cls}>{statusBadge.text}</span>
                      </div>
                      <div className="sync-source-actions">
                        <button
                          type="button"
                          className="btn small"
                          disabled={syncSourceAction.isPending || updateSourceAction.isPending}
                          onClick={() => void syncSourceAction.run(src)}
                        >
                          {isSyncingThis ? t('同步中…', 'Syncing…') : t('立即同步', 'Sync now')}
                        </button>
                        <button
                          type="button"
                          className="btn small"
                          disabled={updateSourceAction.isPending}
                          onClick={() =>
                            void updateSourceAction.run({
                              id: src.id,
                              version: src.version,
                              enabled: !src.enabled,
                            })
                          }
                        >
                          {src.enabled ? t('暂停', 'Pause') : t('启用', 'Enable')}
                        </button>
                        <button
                          type="button"
                          className="btn small"
                          disabled={updateSourceAction.isPending}
                          onClick={() => openEditSource(src)}
                        >
                          {t('编辑', 'Edit')}
                        </button>
                        <button
                          type="button"
                          className="btn small danger"
                          disabled={deleteSourceAction.isPending}
                          onClick={() => setConfirmDeleteSource(src)}
                        >
                          {t('删除', 'Delete')}
                        </button>
                      </div>
                    </div>

                    <div className="sync-source-url tabular-nums" title={src.url}>
                      {truncateUrl(src.url)}
                    </div>

                    <div className="sync-source-meta tabular-nums">
                      <span>{formatInterval(src.sync_interval_minutes, lang)}</span>
                      <span>·</span>
                      <span>
                        {t('上次同步：', 'Last sync: ')}
                        {formatTimestamp(src.last_synced_at, timezone, lang)}
                      </span>
                      {src.last_status === 'ok' ? (
                        <>
                          <span>·</span>
                          <span>
                            {t(
                              `新增 ${src.last_added} · 更新 ${src.last_updated} · 清理 ${src.last_removed}`,
                              `+${src.last_added} · ~${src.last_updated} · -${src.last_removed}`,
                            )}
                          </span>
                        </>
                      ) : null}
                    </div>

                    {src.last_status === 'error' && src.last_error ? (
                      <div className="field-error" role="alert" style={{ marginTop: 8, marginBottom: 0 }}>
                        {src.last_error}
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="sync-empty-hint">
              {t('尚未添加外部日历订阅源。在下方粘贴外部日历的 .ics / webcal:// 链接即可开始自动同步。', 'No external calendar subscriptions yet. Add an .ics or webcal:// URL below to start auto-syncing.')}
            </div>
          )}

          <form className="sync-add-form" onSubmit={handleAddSource} noValidate>
            <div className="sync-form-title">{t('添加外部日历链接', 'Add external calendar URL')}</div>
            {sourceLocalError ? (
              <div className="field-error" role="alert" style={{ marginBottom: 12 }}>
                {sourceLocalError}
              </div>
            ) : null}

            <div className="sync-form-grid">
              <div className="field" style={{ marginBottom: 0 }}>
                <label className="field-label" htmlFor="sync-source-name">
                  {t('日历名称', 'Calendar name')}
                </label>
                <input
                  id="sync-source-name"
                  type="text"
                  className="input"
                  placeholder={t('例如：公司日历 / iCloud', 'e.g. Work / iCloud')}
                  maxLength={120}
                  value={sourceName}
                  disabled={createSourceAction.isPending}
                  onChange={(e) => setSourceName(e.target.value)}
                />
              </div>

              <div className="field" style={{ marginBottom: 0 }}>
                <label className="field-label" htmlFor="sync-source-interval">
                  {t('自动同步频率', 'Sync frequency')}
                </label>
                <select
                  id="sync-source-interval"
                  className="select"
                  value={sourceInterval}
                  disabled={createSourceAction.isPending}
                  onChange={(e) => setSourceInterval(Number(e.target.value))}
                >
                  {SYNC_INTERVAL_OPTIONS.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {t(opt.zh, opt.en)}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <div className="field" style={{ marginTop: 12 }}>
              <label className="field-label" htmlFor="sync-source-url">
                {t('外部日历订阅地址（.ics / webcal://）', 'Calendar subscription URL (.ics / webcal://)')}
              </label>
              <input
                id="sync-source-url"
                type="url"
                className="input"
                placeholder="https://calendar.google.com/calendar/ical/.../basic.ics"
                maxLength={2000}
                value={sourceUrl}
                disabled={createSourceAction.isPending}
                onChange={(e) => setSourceUrl(e.target.value)}
              />
            </div>

            <button type="submit" className="btn primary" disabled={createSourceAction.isPending}>
              {createSourceAction.isPending ? t('验证并同步中…', 'Validating & syncing…') : t('添加并同步', 'Add & sync')}
            </button>
          </form>
        </div>
      </section>

      <section className="settings-section" aria-label={t('手动导入', 'Manual import')}>
        <div className="settings-section-title">{t('手动导入（.ics 文件）', 'Manual import (.ics file)')}</div>
        <p className="settings-section-copy">
          {t(
            '适合一次性导入本地导出的日历快照；与上方自动订阅源相互独立，不会互相覆盖。',
            'Import a one-off local .ics snapshot. Kept separate from auto-synced external calendars.',
          )}
        </p>
        <div className="card-flat sync-card">
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
            className="btn"
            disabled={importAction.isPending || !icsFile}
            onClick={() => void handleImportIcs()}
          >
            {importAction.isPending ? t('导入中…', 'Importing…') : t('导入', 'Import')}
          </button>
        </div>
      </section>

      {confirmRotateFeed ? (
        <Modal
          title={t('重置订阅链接', 'Rotate subscription link')}
          onClose={() => {
            if (!rotateFeedAction.isPending) setConfirmRotateFeed(false);
          }}
        >
          <p className="muted" style={{ fontSize: 13, lineHeight: 1.6, marginTop: 0 }}>
            {t(
              '重置后会立刻生成全新链接，原有链接将立即失效，你需要在 Apple / Google / Outlook 日历中重新订阅新链接。确定继续？',
              'Rotating generates a brand-new link and immediately invalidates the old one. You will need to re-subscribe in your external calendar apps. Continue?',
            )}
          </p>
          <ErrorBanner error={rotateFeedAction.actionError} />
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 18 }}>
            <button
              type="button"
              className="btn"
              disabled={rotateFeedAction.isPending}
              onClick={() => setConfirmRotateFeed(false)}
            >
              {t('取消', 'Cancel')}
            </button>
            <button
              type="button"
              className="btn danger"
              disabled={rotateFeedAction.isPending}
              onClick={() => void rotateFeedAction.run(feed?.version ?? undefined)}
            >
              {rotateFeedAction.isPending ? t('重置中…', 'Rotating…') : t('确认重置', 'Confirm rotate')}
            </button>
          </div>
        </Modal>
      ) : null}

      {editingSource ? (
        <Modal
          title={t('编辑外部日历源', 'Edit calendar source')}
          onClose={() => {
            if (!updateSourceAction.isPending) setEditingSource(null);
          }}
        >
          <form onSubmit={handleSaveEditSource} noValidate>
            <ErrorBanner error={updateSourceAction.actionError} />
            {editError ? (
              <div className="field-error" role="alert" style={{ marginBottom: 12 }}>
                {editError}
              </div>
            ) : null}
            <div className="field">
              <label className="field-label" htmlFor="edit-source-name">
                {t('日历名称', 'Calendar name')}
              </label>
              <input
                id="edit-source-name"
                type="text"
                className="input"
                maxLength={120}
                value={editName}
                disabled={updateSourceAction.isPending}
                onChange={(e) => setEditName(e.target.value)}
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="edit-source-url">
                {t('订阅地址', 'Subscription URL')}
              </label>
              <input
                id="edit-source-url"
                type="url"
                className="input"
                maxLength={2000}
                value={editUrl}
                disabled={updateSourceAction.isPending}
                onChange={(e) => setEditUrl(e.target.value)}
              />
            </div>
            <div className="field">
              <label className="field-label" htmlFor="edit-source-interval">
                {t('自动同步频率', 'Sync frequency')}
              </label>
              <select
                id="edit-source-interval"
                className="select"
                value={editInterval}
                disabled={updateSourceAction.isPending}
                onChange={(e) => setEditInterval(Number(e.target.value))}
              >
                {SYNC_INTERVAL_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {t(opt.zh, opt.en)}
                  </option>
                ))}
              </select>
            </div>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 18 }}>
              <button
                type="button"
                className="btn"
                disabled={updateSourceAction.isPending}
                onClick={() => setEditingSource(null)}
              >
                {t('取消', 'Cancel')}
              </button>
              <button type="submit" className="btn primary" disabled={updateSourceAction.isPending}>
                {updateSourceAction.isPending ? t('保存中…', 'Saving…') : t('保存', 'Save')}
              </button>
            </div>
          </form>
        </Modal>
      ) : null}

      {confirmDeleteSource ? (
        <Modal
          title={t('删除外部日历源', 'Remove calendar source')}
          onClose={() => {
            if (!deleteSourceAction.isPending) setConfirmDeleteSource(null);
          }}
        >
          <p className="muted" style={{ fontSize: 13, lineHeight: 1.6, marginTop: 0 }}>
            {t(
              `删除「${confirmDeleteSource.name}」并清理从该来源同步进来的所有外部日程？你的目标块、习惯和手动日程不会受影响。`,
              `Remove "${confirmDeleteSource.name}" and clear all external events synced from it? Your goal blocks, habits, and manual events will not be affected.`,
            )}
          </p>
          <ErrorBanner error={deleteSourceAction.actionError} />
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 18 }}>
            <button
              type="button"
              className="btn"
              disabled={deleteSourceAction.isPending}
              onClick={() => setConfirmDeleteSource(null)}
            >
              {t('取消', 'Cancel')}
            </button>
            <button
              type="button"
              className="btn danger"
              disabled={deleteSourceAction.isPending}
              onClick={() =>
                void deleteSourceAction.run({
                  id: confirmDeleteSource.id,
                  version: confirmDeleteSource.version,
                })
              }
            >
              {deleteSourceAction.isPending ? t('删除中…', 'Removing…') : t('确认删除', 'Confirm remove')}
            </button>
          </div>
        </Modal>
      ) : null}
    </>
  );
}
