import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { invalidateAfterWrite, queryKeys } from '../api/queryKeys';
import type { CalendarConnection, CalendarFeed, CalendarSource } from '../api/types';
import { useI18n } from '../i18n';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useAppState } from '../state/AppStateContext';
import { Modal } from './Modal';
import { ErrorBanner, SkeletonCard } from './StateFeedback';

const MAX_ICS_BYTES = 5 * 1024 * 1024;

const SOURCE_COLOR_PRESETS = ['#6b7f89', '#0f766e', '#2563eb', '#d97706', '#9333ea', '#dc2626'];

const SYNC_INTERVAL_OPTIONS: Array<{ value: number; zh: string; en: string }> = [
  { value: 15, zh: '每 15 分钟', en: 'Every 15 min' },
  { value: 30, zh: '每 30 分钟', en: 'Every 30 min' },
  { value: 60, zh: '每 1 小时', en: 'Every 1 hour' },
  { value: 180, zh: '每 3 小时', en: 'Every 3 hours' },
  { value: 360, zh: '每 6 小时', en: 'Every 6 hours' },
  { value: 1440, zh: '每天 1 次', en: 'Once daily' },
];

const ALL_DAY_MODES: Array<{ value: 'ignore' | 'show' | 'busy'; zh: string; en: string }> = [
  { value: 'ignore', zh: '忽略全天事件', en: 'Ignore all-day' },
  { value: 'show', zh: '仅展示（不挡排期）', en: 'Show only (non-blocking)' },
  { value: 'busy', zh: '视为忙碌（阻塞排期）', en: 'Busy (blocks scheduling)' },
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

function providerLabel(provider: string, lang: 'zh' | 'en'): string {
  if (provider === 'caldav') return lang === 'zh' ? 'Apple / CalDAV' : 'Apple / CalDAV';
  if (provider === 'google') return 'Google Calendar';
  if (provider === 'outlook') return 'Outlook';
  return 'ICS';
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
  const [searchParams, setSearchParams] = useSearchParams();

  const syncQuery = useQuery({
    queryKey: queryKeys.calendarSync,
    queryFn: api.getCalendarSync,
  });

  // OAuth redirect feedback
  const [oauthNotice, setOauthNotice] = useState<{ kind: 'info' | 'error'; text: string } | null>(null);

  useEffect(() => {
    const connected = searchParams.get('connected');
    const oauthErr = searchParams.get('oauth_error');
    if (connected) {
      setOauthNotice({
        kind: 'info',
        text: t(
          `已成功连接 ${providerLabel(connected, 'zh')}，GoalCal 专属日历正在后台同步。`,
          `Connected ${providerLabel(connected, 'en')}; your GoalCal calendar is syncing in the background.`,
        ),
      });
      setSearchParams({ section: 'sync' }, { replace: true });
      void queryClient.invalidateQueries({ queryKey: queryKeys.calendarSync });
      void invalidateAfterWrite(queryClient);
    } else if (oauthErr) {
      const msgMap: Record<string, [string, string]> = {
        not_configured: ['服务端尚未配置该日历平台的 OAuth 密钥，请联系管理员配置环境变量。', 'OAuth client keys are not configured on this server yet.'],
        denied: ['已取消外部日历授权。', 'Calendar authorization was cancelled.'],
        invalid_state: ['授权状态已过期或校验失败，请重试。', 'Authorization state expired; please try again.'],
        exchange_failed: ['完成外部日历授权失败，请检查网络连接后重试。', 'Failed to complete calendar authorization; please retry.'],
      };
      const pair = msgMap[oauthErr] ?? ['外部日历授权失败，请重试。', 'Calendar authorization failed.'];
      setOauthNotice({ kind: 'error', text: t(pair[0], pair[1]) });
      setSearchParams({ section: 'sync' }, { replace: true });
    }
  }, [searchParams, setSearchParams, queryClient, t]);

  // CalDAV connection modal state
  const [showCalDavModal, setShowCalDavModal] = useState(false);
  const [caldavPreset, setCaldavPreset] = useState<'icloud' | 'fastmail' | 'custom'>('icloud');
  const [caldavServerUrl, setCaldavServerUrl] = useState('');
  const [caldavUsername, setCaldavUsername] = useState('');
  const [caldavPassword, setCaldavPassword] = useState('');
  const [caldavTwoWay, setCaldavTwoWay] = useState(true);
  const [caldavLocalError, setCaldavLocalError] = useState<string | null>(null);
  const [confirmDeleteConn, setConfirmDeleteConn] = useState<CalendarConnection | null>(null);

  // Outbound feed UI state
  const [copiedFeed, setCopiedFeed] = useState(false);
  const [confirmRotateFeed, setConfirmRotateFeed] = useState(false);

  // Inbound ICS add source form state
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
  const [editColor, setEditColor] = useState('#6b7f89');
  const [editBlocks, setEditBlocks] = useState(true);
  const [editAllDayMode, setEditAllDayMode] = useState<'ignore' | 'show' | 'busy'>('ignore');
  const [editWritable, setEditWritable] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const [confirmDeleteSource, setConfirmDeleteSource] = useState<CalendarSource | null>(null);
  const [syncingSourceId, setSyncingSourceId] = useState<number | null>(null);

  // Manual ICS file import state
  const [icsFile, setIcsFile] = useState<File | null>(null);
  const [icsLocalError, setIcsLocalError] = useState<string | null>(null);
  const [icsResultMsg, setIcsResultMsg] = useState<string | null>(null);

  const connectCalDavFn = useCallback(
    async (
      args: {
        preset: 'icloud' | 'fastmail' | 'custom';
        server_url?: string;
        username: string;
        password: string;
        enable_two_way?: boolean;
      },
      key: string,
    ) => api.connectCalDav(args, key),
    [],
  );

  const connectCalDavAction = useIdempotentAction(connectCalDavFn, {
    onSuccess: async (res) => {
      setShowCalDavModal(false);
      setCaldavPassword('');
      setCaldavLocalError(null);
      setSyncStatusMsg(res.message);
      recordMutationResult({ message: res.message });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.calendarSync }),
        invalidateAfterWrite(queryClient),
      ]);
    },
  });

  const refreshConnFn = useCallback(
    async (conn: CalendarConnection, key: string) => api.refreshCalendarConnection(conn.id, { version: conn.version }, key),
    [],
  );

  const refreshConnAction = useIdempotentAction(refreshConnFn, {
    onSuccess: async (res) => {
      setSyncStatusMsg(res.message);
      recordMutationResult({ message: res.message });
      await queryClient.invalidateQueries({ queryKey: queryKeys.calendarSync });
    },
  });

  const deleteConnFn = useCallback(
    async (args: { id: number; version: string }, key: string) =>
      api.deleteCalendarConnection(args.id, { version: args.version }, key),
    [],
  );

  const deleteConnAction = useIdempotentAction(deleteConnFn, {
    onSuccess: async (res) => {
      setConfirmDeleteConn(null);
      setSyncStatusMsg(res.message);
      recordMutationResult({ message: res.message });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.calendarSync }),
        invalidateAfterWrite(queryClient),
      ]);
    },
  });

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
      queryClient.setQueryData(queryKeys.calendarSync, (prev: typeof syncQuery.data) =>
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
      queryClient.setQueryData(queryKeys.calendarSync, (prev: typeof syncQuery.data) =>
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
        color?: string;
        blocks_scheduling?: boolean;
        all_day_mode?: 'ignore' | 'show' | 'busy';
        writable?: boolean;
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
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.calendarSync }),
        invalidateAfterWrite(queryClient),
      ]);
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
  const providers = syncQuery.data?.providers ?? {
    caldav: { configured: true, label: 'Apple iCloud / CalDAV' },
    google: { configured: false, label: 'Google Calendar' },
    outlook: { configured: false, label: 'Outlook / Microsoft 365' },
  };
  const connections = syncQuery.data?.connections ?? [];
  const allSources = syncQuery.data?.sources ?? [];
  const icsSources = allSources.filter((s) => !s.connection_id);
  const feedUrls = buildFeedUrls(feed?.path ?? null);
  const hasNativeTwoWay = allSources.some((s) => s.enabled && s.writable && s.is_goalcal_calendar);

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

  const handleConnectCalDav = (e: React.FormEvent) => {
    e.preventDefault();
    setCaldavLocalError(null);
    if (caldavPreset === 'custom' && !caldavServerUrl.trim().startsWith('https://')) {
      setCaldavLocalError(t('自定义 CalDAV 服务器地址须以 https:// 开头。', 'Custom CalDAV URL must start with https://.'));
      return;
    }
    if (!caldavUsername.trim()) {
      setCaldavLocalError(t('请填写账号或邮箱。', 'Please enter your account username or email.'));
      return;
    }
    if (!caldavPassword.trim()) {
      setCaldavLocalError(t('请填写应用专用密码。', 'Please enter your app-specific password.'));
      return;
    }
    void connectCalDavAction.run({
      preset: caldavPreset,
      server_url: caldavPreset === 'custom' ? caldavServerUrl.trim() : undefined,
      username: caldavUsername.trim(),
      password: caldavPassword,
      enable_two_way: caldavTwoWay,
    });
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
    setEditColor(source.color || '#6b7f89');
    setEditBlocks(source.blocks_scheduling ?? true);
    setEditAllDayMode((source.all_day_mode as 'ignore' | 'show' | 'busy') || 'ignore');
    setEditWritable(Boolean(source.writable));
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
    if (editingSource.provider === 'ics_url' && !/^(https?|webcals?):\/\//i.test(trimmedUrl)) {
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
      url: editingSource.provider === 'ics_url' ? trimmedUrl : undefined,
      sync_interval_minutes: editInterval,
      color: editColor,
      blocks_scheduling: editBlocks,
      all_day_mode: editAllDayMode,
      writable: editingSource.is_goalcal_calendar ? editWritable : undefined,
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

  const renderSourceRow = (src: CalendarSource) => {
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
            <span
              className="sync-color-dot"
              style={{ backgroundColor: src.color || '#6b7f89' }}
              aria-hidden="true"
            />
            <span className="sync-source-name">{src.name}</span>
            {src.is_goalcal_calendar ? (
              <span className="chip blue">{t('GoalCal 双向', 'GoalCal Two-way')}</span>
            ) : null}
            {src.blocks_scheduling === false && !src.is_goalcal_calendar ? (
              <span className="chip">{t('仅展示', 'Reference')}</span>
            ) : null}
            <span className={statusBadge.cls}>{statusBadge.text}</span>
            {(src.outbox_pending ?? 0) > 0 ? (
              <span className="chip tabular-nums">
                {t(`待推送 ${src.outbox_pending}`, `${src.outbox_pending} queued`)}
              </span>
            ) : null}
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
            {!src.connection_id ? (
              <button
                type="button"
                className="btn small danger"
                disabled={deleteSourceAction.isPending}
                onClick={() => setConfirmDeleteSource(src)}
              >
                {t('删除', 'Delete')}
              </button>
            ) : null}
          </div>
        </div>

        {!src.connection_id && src.url ? (
          <div className="sync-source-url tabular-nums" title={src.url}>
            {truncateUrl(src.url)}
          </div>
        ) : null}

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
  };

  return (
    <>
      {oauthNotice ? (
        <div
          className={oauthNotice.kind === 'error' ? 'field-error' : 'status-banner info'}
          role={oauthNotice.kind === 'error' ? 'alert' : 'status'}
          style={{ marginBottom: 16 }}
        >
          <div className={oauthNotice.kind === 'error' ? undefined : 'status-banner-body'}>{oauthNotice.text}</div>
        </div>
      ) : null}

      <section className="settings-section" aria-label={t('原生双向同步', 'Native two-way sync')}>
        <div className="settings-section-title">{t('原生双向同步', 'Native two-way sync')}</div>
        <p className="settings-section-copy">
          {t(
            '连接账号后自动创建专属「GoalCal」双向日历：排期改动秒级推送出去，在系统日历中拖拽改期或删除也会自动同步回来。',
            'Connect an account to sync a dedicated two-way "GoalCal" calendar cleanly with automatic push & pull.',
          )}
        </p>

        {syncQuery.isLoading ? (
          <SkeletonCard label={t('正在加载同步设置', 'Loading sync settings')} />
        ) : syncQuery.isError ? (
          <ErrorBanner error={syncQuery.error} onRetry={() => void syncQuery.refetch()} />
        ) : (
          <div className="card-flat sync-card">
            <ErrorBanner error={refreshConnAction.actionError ?? deleteConnAction.actionError} />

            <div className="sync-provider-grid">
              <div className="sync-provider-card">
                <div>
                  <div className="setting-name">Apple iCloud / CalDAV</div>
                  <div className="sync-hint">
                    {t('使用应用专用密码，无需公开日历。', 'App-specific password, private.')}
                  </div>
                </div>
                <button
                  type="button"
                  className="btn small"
                  onClick={() => {
                    setCaldavLocalError(null);
                    setShowCalDavModal(true);
                  }}
                >
                  {t('连接 Apple / CalDAV', 'Connect Apple / CalDAV')}
                </button>
              </div>

              <div className="sync-provider-card">
                <div>
                  <div className="setting-name">Google Calendar</div>
                  <div className="sync-hint">
                    {providers.google?.configured
                      ? t('OAuth 授权 + Webhook 实时增量同步。', 'OAuth 2.0 + instant push.')
                      : t('未配置服务端 Google OAuth 密钥。', 'Google OAuth keys needed.')}
                  </div>
                </div>
                {providers.google?.configured ? (
                  <a className="btn small" href="/_goalcal/oauth/google/start">
                    {t('连接 Google 日历', 'Connect Google')}
                  </a>
                ) : (
                  <button type="button" className="btn small" disabled title="GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET">
                    {t('待配置密钥', 'Key needed')}
                  </button>
                )}
              </div>

              <div className="sync-provider-card">
                <div>
                  <div className="setting-name">Outlook / Microsoft 365</div>
                  <div className="sync-hint">
                    {providers.outlook?.configured
                      ? t('Microsoft Graph OAuth 双向同步。', 'Microsoft Graph two-way.')
                      : t('未配置服务端 Outlook OAuth 密钥。', 'Outlook OAuth keys needed.')}
                  </div>
                </div>
                {providers.outlook?.configured ? (
                  <a className="btn small" href="/_goalcal/oauth/outlook/start">
                    {t('连接 Outlook', 'Connect Outlook')}
                  </a>
                ) : (
                  <button type="button" className="btn small" disabled title="MICROSOFT_CLIENT_ID / MICROSOFT_CLIENT_SECRET">
                    {t('待配置密钥', 'Key needed')}
                  </button>
                )}
              </div>
            </div>

            {connections.length > 0 ? (
              <div className="sync-connection-list">
                {connections.map((conn) => {
                  const connSources = allSources.filter((s) => s.connection_id === conn.id);
                  const isReauth = conn.status === 'reauth_required';
                  return (
                    <div key={conn.id} className="sync-connection-card">
                      <div className="sync-source-head">
                        <div className="sync-source-title-row">
                          <span className="sync-source-name">{conn.account_label || conn.username}</span>
                          <span className="chip">{providerLabel(conn.provider, lang)}</span>
                          <span className={`chip ${isReauth ? 'sync-chip-error' : 'sync-chip-ok'}`}>
                            {isReauth ? t('需重新授权', 'Reauth needed') : t('已连接', 'Connected')}
                          </span>
                        </div>
                        <div className="sync-source-actions">
                          {isReauth && conn.provider !== 'caldav' ? (
                            <a className="btn small primary" href={`/_goalcal/oauth/${conn.provider}/start`}>
                              {t('重新授权', 'Reconnect')}
                            </a>
                          ) : null}
                          <button
                            type="button"
                            className="btn small"
                            disabled={refreshConnAction.isPending}
                            onClick={() => void refreshConnAction.run(conn)}
                          >
                            {refreshConnAction.isPending ? t('刷新中…', 'Refreshing…') : t('刷新日历', 'Refresh')}
                          </button>
                          <button
                            type="button"
                            className="btn small danger"
                            disabled={deleteConnAction.isPending}
                            onClick={() => setConfirmDeleteConn(conn)}
                          >
                            {t('断开', 'Disconnect')}
                          </button>
                        </div>
                      </div>
                      {conn.last_error ? (
                        <div className="field-error" role="alert" style={{ marginTop: 8, marginBottom: 8 }}>
                          {conn.last_error}
                        </div>
                      ) : null}
                      {connSources.length > 0 ? (
                        <div className="sync-source-list" style={{ marginTop: 12, marginBottom: 0 }}>
                          {connSources.map(renderSourceRow)}
                        </div>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            ) : null}
          </div>
        )}
      </section>

      <section className="settings-section" aria-label={t('从外部日历同步', 'Sync from external calendars')}>
        <div className="settings-section-title">
          {t('从外部日历同步', 'Sync from external calendars')}
        </div>
        <p className="settings-section-copy">
          {t(
            '定时拉取外部日历链接，可在「编辑」中控制是否阻塞排期、全天事件处理及日历颜色。',
            'Periodically pull external calendar URLs with per-calendar conflict, all-day, and color controls.',
          )}
        </p>

        <div className="card-flat sync-card">
          <ErrorBanner error={createSourceAction.actionError ?? updateSourceAction.actionError ?? syncSourceAction.actionError ?? deleteSourceAction.actionError} />
          {syncStatusMsg ? (
            <div className="status-banner info" role="status" style={{ marginBottom: 14 }}>
              <div className="status-banner-body">{syncStatusMsg}</div>
            </div>
          ) : null}

          {icsSources.length > 0 ? (
            <div className="sync-source-list">
              {icsSources.map(renderSourceRow)}
            </div>
          ) : null}

          <form
            className={icsSources.length > 0 ? 'sync-add-form' : 'sync-add-form sync-add-form-first'}
            onSubmit={handleAddSource}
            noValidate
          >
            <div className="sync-form-title">{t('添加日历链接', 'Add calendar URL')}</div>
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
                  placeholder={t('例如：公司日历 / 校历', 'e.g. Work / Holidays')}
                  maxLength={120}
                  value={sourceName}
                  disabled={createSourceAction.isPending}
                  onChange={(e) => setSourceName(e.target.value)}
                />
              </div>

              <div className="field" style={{ marginBottom: 0 }}>
                <label className="field-label" htmlFor="sync-source-interval">
                  {t('同步频率', 'Sync interval')}
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
                {t('订阅链接（https:// 或 webcal://）', 'Subscription URL (https:// or webcal://)')}
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

          <details className="sync-guide">
            <summary>{t('如何获取外部日历链接？', 'Where to find a calendar URL?')}</summary>
            <ul>
              <li>
                <strong>{t('Apple iCloud 日历', 'Apple iCloud Calendar')}</strong>：
                {t(
                  '推荐上方「连接 Apple / CalDAV」（无需公开）；或右键日历开启「共享日历」复制链接。',
                  'Use "Connect Apple / CalDAV" above, or right-click a calendar to enable "Public Calendar".',
                )}
              </li>
              <li>
                <strong>Google Calendar</strong>：
                {t(
                  '网页端打开「日历设置」→「整合日历」，复制「iCal 格式的秘密地址」。',
                  'On web, open "Settings and sharing" → "Integrate calendar" and copy the "Secret address in iCal format".',
                )}
              </li>
              <li>
                <strong>{t('Outlook / 飞书等', 'Outlook / Lark / Others')}</strong>：
                {t(
                  '在对应日历设置中发布或获取以 .ics 结尾或 webcal:// 开头的订阅地址。',
                  'Publish or copy the .ics / webcal:// subscription address from your calendar settings.',
                )}
              </li>
            </ul>
          </details>

          <details className="sync-manual-import">
            <summary>{t('一次性导入本地 .ics 文件', 'Import a local .ics file')}</summary>
            <div className="sync-manual-import-body">
              <p className="sync-hint" style={{ margin: '0 0 10px' }}>
                {t(
                  '适用于无法开启订阅链接的日历或课表等静态日程文件，与自动同步互不覆盖。',
                  'For calendars without a subscription URL or one-off files like timetables; kept separate from auto-sync.',
                )}
              </p>
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
          </details>
        </div>
      </section>

      <section className="settings-section" aria-label={t('同步到外部日历', 'Sync to external calendars')}>
        <div className="settings-section-title">
          {t('同步到外部日历', 'Sync to external calendars')}
        </div>
        <p className="settings-section-copy">
          {t(
            '在系统日历或第三方日历中只读查看 GoalCal 安排（自动排除外部日程）。',
            'View your GoalCal schedule read-only in external calendar apps (external events are excluded).',
          )}
          {hasNativeTwoWay
            ? t(' 你已启用原生双向同步，此链接可作为只读备用。', ' Native two-way sync is active; this feed remains available as a read-only backup.')
            : ''}
        </p>

        {!syncQuery.isLoading && !syncQuery.isError ? (
          <div className="card-flat sync-card">
            <ErrorBanner error={updateFeedAction.actionError ?? rotateFeedAction.actionError} />

            <div className="sync-feed-header">
              <div>
                <div className="setting-name">{t('订阅链接', 'Subscription link')}</div>
                <div className="sync-hint">
                  {feed?.enabled
                    ? t('私有链接，泄露可随时重置。', 'Private link — rotate anytime if shared.')
                    : t('开启后生成专属链接。', 'Turn on to generate your private link.')}
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
                    ? t('停用', 'Disable')
                    : t('生成订阅链接', 'Enable link')}
              </button>
            </div>

            {feed?.enabled ? (
              <div className="sync-scope-group" role="group" aria-label={t('导出范围', 'Included items')}>
                <span className="sync-scope-label">{t('包含内容', 'Include')}</span>
                <label className="sync-check">
                  <input
                    type="checkbox"
                    checked={feed?.include_blocks ?? true}
                    disabled={updateFeedAction.isPending}
                    onChange={(e) => handleFeedChange({ include_blocks: e.target.checked })}
                  />
                  <span>{t('目标执行块', 'Goal blocks')}</span>
                </label>
                <label className="sync-check">
                  <input
                    type="checkbox"
                    checked={feed?.include_habits ?? true}
                    disabled={updateFeedAction.isPending}
                    onChange={(e) => handleFeedChange({ include_habits: e.target.checked })}
                  />
                  <span>{t('习惯安排', 'Habit sessions')}</span>
                </label>
                <label className="sync-check">
                  <input
                    type="checkbox"
                    checked={feed?.include_manual ?? true}
                    disabled={updateFeedAction.isPending}
                    onChange={(e) => handleFeedChange({ include_manual: e.target.checked })}
                  />
                  <span>{t('手动日程', 'Manual events')}</span>
                </label>
              </div>
            ) : null}

            {feed?.enabled && feedUrls ? (
              <div className="sync-feed-box">
                <label className="field-label" htmlFor="calendar-feed-url">
                  {t('订阅链接', 'Subscription link')}
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
                      {t('上次拉取：', 'Last fetched: ')}
                      {formatTimestamp(feed.last_accessed_at, timezone, lang)}
                    </span>
                  ) : null}
                </div>

                <details className="sync-guide">
                  <summary>{t('如何订阅到 Apple / Google / Outlook？', 'How to subscribe in Apple / Google / Outlook?')}</summary>
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
        ) : null}
      </section>

      {showCalDavModal ? (
        <Modal
          title={t('连接 Apple iCloud / CalDAV', 'Connect Apple iCloud / CalDAV')}
          onClose={() => {
            if (!connectCalDavAction.isPending) setShowCalDavModal(false);
          }}
        >
          <form onSubmit={handleConnectCalDav} noValidate>
            <ErrorBanner error={connectCalDavAction.actionError} />
            {caldavLocalError ? (
              <div className="field-error" role="alert" style={{ marginBottom: 12 }}>
                {caldavLocalError}
              </div>
            ) : null}

            <div className="field">
              <label className="field-label" htmlFor="caldav-preset">
                {t('服务商', 'Provider')}
              </label>
              <select
                id="caldav-preset"
                className="select"
                value={caldavPreset}
                disabled={connectCalDavAction.isPending}
                onChange={(e) => setCaldavPreset(e.target.value as 'icloud' | 'fastmail' | 'custom')}
              >
                <option value="icloud">Apple iCloud</option>
                <option value="fastmail">Fastmail</option>
                <option value="custom">{t('自定义 CalDAV 服务器', 'Custom CalDAV server')}</option>
              </select>
            </div>

            {caldavPreset === 'custom' ? (
              <div className="field">
                <label className="field-label" htmlFor="caldav-server-url">
                  {t('CalDAV 地址（https://）', 'CalDAV URL (https://)')}
                </label>
                <input
                  id="caldav-server-url"
                  type="url"
                  className="input"
                  placeholder="https://cal.example.com/dav/"
                  value={caldavServerUrl}
                  disabled={connectCalDavAction.isPending}
                  onChange={(e) => setCaldavServerUrl(e.target.value)}
                />
              </div>
            ) : null}

            <div className="field">
              <label className="field-label" htmlFor="caldav-username">
                {t('账号 / Apple ID 邮箱', 'Account / Apple ID email')}
              </label>
              <input
                id="caldav-username"
                type="text"
                className="input"
                placeholder="name@icloud.com"
                value={caldavUsername}
                disabled={connectCalDavAction.isPending}
                onChange={(e) => setCaldavUsername(e.target.value)}
              />
            </div>

            <div className="field">
              <label className="field-label" htmlFor="caldav-password">
                {t('应用专用密码', 'App-specific password')}
              </label>
              <input
                id="caldav-password"
                type="password"
                className="input"
                placeholder="xxxx-xxxx-xxxx-xxxx"
                value={caldavPassword}
                disabled={connectCalDavAction.isPending}
                onChange={(e) => setCaldavPassword(e.target.value)}
              />
            </div>

            <label className="sync-check" style={{ marginBottom: 14 }}>
              <input
                type="checkbox"
                checked={caldavTwoWay}
                disabled={connectCalDavAction.isPending}
                onChange={(e) => setCaldavTwoWay(e.target.checked)}
              />
              <span>{t('自动创建并启用「GoalCal」双向日历', 'Create & enable two-way "GoalCal" calendar')}</span>
            </label>

            {caldavPreset === 'icloud' ? (
              <details className="sync-guide" style={{ marginTop: 0, marginBottom: 14 }}>
                <summary>{t('如何生成 Apple 应用专用密码？', 'How to generate an Apple app-specific password?')}</summary>
                <ul>
                  <li>{t('1. 打开 account.apple.com 并登录你的 Apple 账号。', '1. Sign in at account.apple.com.')}</li>
                  <li>{t('2. 进入「登录与安全」→「App 专用密码」→ 点「+」生成密码。', '2. Open Sign-In and Security → App-Specific Passwords → Generate.')}</li>
                  <li>{t('3. 将生成的 xxxx-xxxx-xxxx-xxxx 密码粘贴到上方（加密存储）。', '3. Paste the xxxx-xxxx-xxxx-xxxx password above (stored encrypted).')}</li>
                </ul>
              </details>
            ) : null}

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
              <button
                type="button"
                className="btn"
                disabled={connectCalDavAction.isPending}
                onClick={() => setShowCalDavModal(false)}
              >
                {t('取消', 'Cancel')}
              </button>
              <button type="submit" className="btn primary" disabled={connectCalDavAction.isPending}>
                {connectCalDavAction.isPending ? t('连接中…', 'Connecting…') : t('验证并连接', 'Verify & connect')}
              </button>
            </div>
          </form>
        </Modal>
      ) : null}

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
          title={t('编辑同步日历', 'Edit synced calendar')}
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

            {editingSource.provider === 'ics_url' ? (
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
            ) : null}

            <div className="field">
              <label className="field-label" htmlFor="edit-source-interval">
                {t('同步频率', 'Sync interval')}
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

            <div className="field">
              <span className="field-label">{t('日历标识色', 'Calendar color')}</span>
              <div className="sync-color-presets" role="group" aria-label={t('选择颜色', 'Choose color')}>
                {SOURCE_COLOR_PRESETS.map((hex) => (
                  <button
                    key={hex}
                    type="button"
                    className={`sync-color-btn${editColor === hex ? ' selected' : ''}`}
                    style={{ backgroundColor: hex }}
                    aria-label={hex}
                    aria-pressed={editColor === hex}
                    onClick={() => setEditColor(hex)}
                  />
                ))}
              </div>
            </div>

            {!editingSource.is_goalcal_calendar ? (
              <>
                <div className="field">
                  <label className="field-label" htmlFor="edit-source-allday">
                    {t('全天事件处理', 'All-day events')}
                  </label>
                  <select
                    id="edit-source-allday"
                    className="select"
                    value={editAllDayMode}
                    disabled={updateSourceAction.isPending}
                    onChange={(e) => setEditAllDayMode(e.target.value as 'ignore' | 'show' | 'busy')}
                  >
                    {ALL_DAY_MODES.map((m) => (
                      <option key={m.value} value={m.value}>
                        {t(m.zh, m.en)}
                      </option>
                    ))}
                  </select>
                </div>

                <label className="sync-check" style={{ marginBottom: 14 }}>
                  <input
                    type="checkbox"
                    checked={editBlocks}
                    disabled={updateSourceAction.isPending}
                    onChange={(e) => setEditBlocks(e.target.checked)}
                  />
                  <span>{t('参与排期避让（取消勾选则仅展示，不占用空档）', 'Block scheduling (uncheck for reference only)')}</span>
                </label>
              </>
            ) : (
              <label className="sync-check" style={{ marginBottom: 14 }}>
                <input
                  type="checkbox"
                  checked={editWritable}
                  disabled={updateSourceAction.isPending}
                  onChange={(e) => setEditWritable(e.target.checked)}
                />
                <span>{t('向外部写入 GoalCal 排期改动（双向推送）', 'Push GoalCal schedule changes back (two-way)')}</span>
              </label>
            )}

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

      {confirmDeleteConn ? (
        <Modal
          title={t('断开外部日历账号', 'Disconnect calendar account')}
          onClose={() => {
            if (!deleteConnAction.isPending) setConfirmDeleteConn(null);
          }}
        >
          <p className="muted" style={{ fontSize: 13, lineHeight: 1.6, marginTop: 0 }}>
            {t(
              `断开「${confirmDeleteConn.account_label || confirmDeleteConn.username}」并清理从该账号同步进来的所有外部日程？你的 GoalCal 目标、习惯与本地排期保留不受影响。`,
              `Disconnect "${confirmDeleteConn.account_label || confirmDeleteConn.username}" and remove external events synced from it? Your GoalCal goals, habits, and blocks remain intact.`,
            )}
          </p>
          <ErrorBanner error={deleteConnAction.actionError} />
          <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, marginTop: 18 }}>
            <button
              type="button"
              className="btn"
              disabled={deleteConnAction.isPending}
              onClick={() => setConfirmDeleteConn(null)}
            >
              {t('取消', 'Cancel')}
            </button>
            <button
              type="button"
              className="btn danger"
              disabled={deleteConnAction.isPending}
              onClick={() =>
                void deleteConnAction.run({
                  id: confirmDeleteConn.id,
                  version: confirmDeleteConn.version,
                })
              }
            >
              {deleteConnAction.isPending ? t('断开中…', 'Disconnecting…') : t('确认断开', 'Confirm disconnect')}
            </button>
          </div>
        </Modal>
      ) : null}
    </>
  );
}
