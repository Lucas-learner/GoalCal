import { useCallback, useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { queryKeys } from '../api/queryKeys';
import type { CalendarAlarmPrefs, EmailDigestPrefs } from '../api/types';
import { useI18n } from '../i18n';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useAppState } from '../state/AppStateContext';
import { ErrorBanner, SkeletonCard } from './StateFeedback';

const ALARM_LEAD_OPTIONS: Array<{ value: number | null; zh: string; en: string }> = [
  { value: null, zh: '关闭', en: 'Off' },
  { value: 0, zh: '开始时提醒', en: 'At start time' },
  { value: 5, zh: '提前 5 分钟', en: '5 min before' },
  { value: 10, zh: '提前 10 分钟', en: '10 min before' },
  { value: 15, zh: '提前 15 分钟', en: '15 min before' },
  { value: 30, zh: '提前 30 分钟', en: '30 min before' },
  { value: 60, zh: '提前 1 小时', en: '1 hour before' },
];

const DEFAULT_ALARMS: CalendarAlarmPrefs = {
  enabled: true,
  block_minutes: 10,
  habit_minutes: 10,
  manual_minutes: null,
  version: null,
};

const DEFAULT_EMAIL_DIGESTS: EmailDigestPrefs = {
  enabled: false,
  morning_focus: false,
  morning_time: '08:30',
  evening_wrapup: true,
  evening_time: '21:00',
  weekly_habit_nudge: true,
  goal_stall_nudge: true,
  weekly_review: false,
  weekly_review_time: '19:30',
  quiet_start: '22:30',
  quiet_end: '07:30',
  daily_cap: 3,
  version: null,
};

function kindLabel(kind: string, lang: 'zh' | 'en'): string {
  const map: Record<string, [string, string]> = {
    evening_wrapup: ['晚间收口', 'Evening wrap-up'],
    morning_focus: ['今日聚焦', 'Morning focus'],
    weekly_habit_nudge: ['习惯余量', 'Weekly habit'],
    goal_stall: ['目标唤醒', 'Goal nudge'],
    weekly_review: ['每周复盘', 'Weekly review'],
  };
  const pair = map[kind];
  if (!pair) return kind;
  return lang === 'zh' ? pair[0] : pair[1];
}

function statusBadge(status: string, lang: 'zh' | 'en'): { cls: string; text: string } {
  if (status === 'sent') return { cls: 'chip sync-chip-ok', text: lang === 'zh' ? '已发送' : 'Sent' };
  if (status === 'skipped') return { cls: 'chip', text: lang === 'zh' ? '已跳过' : 'Skipped' };
  if (status === 'dead') return { cls: 'chip sync-chip-error', text: lang === 'zh' ? '失败' : 'Failed' };
  return { cls: 'chip', text: lang === 'zh' ? '待发送' : 'Pending' };
}

function formatLogTime(iso: string | null, timezone: string, lang: 'zh' | 'en'): string {
  if (!iso) return '—';
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

export function RemindersSection({
  timezone,
  onGoToSync,
}: {
  timezone: string;
  onGoToSync: () => void;
}) {
  const queryClient = useQueryClient();
  const { recordMutationResult } = useAppState();
  const { lang, t } = useI18n();

  const remindersQuery = useQuery({
    queryKey: queryKeys.reminders,
    queryFn: api.getReminders,
  });

  const [draftEmail, setDraftEmail] = useState<EmailDigestPrefs | null>(null);
  const [emailDirty, setEmailDirty] = useState(false);

  useEffect(() => {
    if (remindersQuery.data?.email_digests && !emailDirty) {
      setDraftEmail(structuredClone(remindersQuery.data.email_digests));
    }
  }, [remindersQuery.data?.email_digests, emailDirty]);

  const updateAlarmsFn = useCallback(
    async (
      args: {
        enabled: boolean;
        block_minutes: number | null;
        habit_minutes: number | null;
        manual_minutes: number | null;
        version?: string;
      },
      key: string,
    ) => api.updateCalendarAlarms(args, key),
    [],
  );

  const updateAlarmsAction = useIdempotentAction(updateAlarmsFn, {
    onSuccess: async (res) => {
      queryClient.setQueryData(queryKeys.reminders, (prev: typeof remindersQuery.data) =>
        prev ? { ...prev, calendar_alarms: res.alarms } : undefined,
      );
      recordMutationResult({ message: res.message });
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.reminders }),
        queryClient.invalidateQueries({ queryKey: queryKeys.calendarSync }),
      ]);
    },
    onError: async (err) => {
      if (err.status === 409) {
        await queryClient.invalidateQueries({ queryKey: queryKeys.reminders });
      }
    },
  });

  const updateEmailFn = useCallback(
    async (args: EmailDigestPrefs, key: string) => {
      const { version, ...rest } = args;
      return api.updateEmailReminders(version ? { ...rest, version } : rest, key);
    },
    [],
  );

  const updateEmailAction = useIdempotentAction(updateEmailFn, {
    onSuccess: async (res) => {
      setEmailDirty(false);
      setDraftEmail(structuredClone(res.email_digests));
      queryClient.setQueryData(queryKeys.reminders, (prev: typeof remindersQuery.data) =>
        prev ? { ...prev, email_digests: res.email_digests } : undefined,
      );
      recordMutationResult({ message: res.message });
      await queryClient.invalidateQueries({ queryKey: queryKeys.reminders });
    },
    onError: async (err) => {
      if (err.status === 409) {
        await queryClient.invalidateQueries({ queryKey: queryKeys.reminders });
      }
    },
  });

  if (remindersQuery.isLoading) {
    return <SkeletonCard label={t('正在加载提醒设置', 'Loading reminder settings')} />;
  }

  if (remindersQuery.isError) {
    return <ErrorBanner error={remindersQuery.error} onRetry={() => void remindersQuery.refetch()} />;
  }

  const alarms = remindersQuery.data?.calendar_alarms ?? DEFAULT_ALARMS;
  const calConn = remindersQuery.data?.calendar_sync_connected ?? { two_way: false, feed: false };
  const hasCalendarChannel = calConn.two_way || calConn.feed;
  const emailState = draftEmail ?? remindersQuery.data?.email_digests ?? DEFAULT_EMAIL_DIGESTS;
  const channel = remindersQuery.data?.email_channel ?? {
    smtp_configured: false,
    from_address: '',
    user_email: null,
  };
  const recentNotifications = remindersQuery.data?.recent_notifications ?? [];

  const handleAlarmsChange = (
    patch: Partial<Pick<CalendarAlarmPrefs, 'enabled' | 'block_minutes' | 'habit_minutes' | 'manual_minutes'>>,
  ) => {
    void updateAlarmsAction.run({
      enabled: patch.enabled ?? alarms.enabled,
      block_minutes: 'block_minutes' in patch ? patch.block_minutes ?? null : alarms.block_minutes,
      habit_minutes: 'habit_minutes' in patch ? patch.habit_minutes ?? null : alarms.habit_minutes,
      manual_minutes: 'manual_minutes' in patch ? patch.manual_minutes ?? null : alarms.manual_minutes,
      version: alarms.version ?? undefined,
    });
  };

  const updateDraftEmail = (patch: Partial<EmailDigestPrefs>) => {
    setEmailDirty(true);
    setDraftEmail({ ...emailState, ...patch });
  };

  const handleSaveEmail = (e: React.FormEvent) => {
    e.preventDefault();
    void updateEmailAction.run(emailState);
  };

  return (
    <>
      <section className="settings-section" aria-label={t('日历提醒闹钟', 'Calendar reminders')}>
        <div className="settings-section-title">{t('通道一：系统日历闹钟', 'Channel 1: System calendar alarms')}</div>
        <p className="settings-section-copy">
          {t(
            '借由 Apple / Google 原生双向同步或 .ics 订阅把安排同步到手机和手表，到点原生强响；已完成或已跳过会自动消除闹钟。',
            'Delivers native phone/watch alerts via Apple/Google two-way sync or .ics feeds. Completed or skipped events automatically omit alarms.',
          )}
        </p>

        <div className="card-flat settings-card">
          <ErrorBanner error={updateAlarmsAction.actionError} />

          {!hasCalendarChannel ? (
            <div className="setting-row">
              <div>
                <div className="setting-name">{t('尚未开启日历同步通道', 'No calendar channel connected yet')}</div>
                <div className="sync-hint">
                  {t(
                    '连接任意双向日历或生成 .ics 订阅链接后，未完成的日程将按下方配置自动触发系统闹钟。',
                    'Connect a two-way calendar or enable an .ics feed in Calendar Sync to receive native device alerts.',
                  )}
                </div>
              </div>
              <button type="button" className="btn small primary" onClick={onGoToSync}>
                {t('前往日历同步', 'Open Calendar Sync')}
              </button>
            </div>
          ) : (
            <div className="setting-row">
              <div>
                <div className="setting-name">{t('同步事件提醒闹钟', 'Calendar alert alarms')}</div>
                <div className="sync-hint">
                  {calConn.two_way
                    ? t('已连接双向同步 · 修改后会自动更新即将到来的日程', 'Two-way sync active · changes re-queue upcoming events')
                    : t('已开启 .ics 订阅导出 · 随日历刷新自动生效', '.ics feed active · updates on calendar refresh')}
                </div>
              </div>
              <button
                type="button"
                className={alarms.enabled ? 'btn small' : 'btn primary small'}
                disabled={updateAlarmsAction.isPending}
                onClick={() => handleAlarmsChange({ enabled: !alarms.enabled })}
              >
                {updateAlarmsAction.isPending
                  ? t('更新中…', 'Saving…')
                  : alarms.enabled
                    ? t('停用闹钟', 'Turn off')
                    : t('开启闹钟', 'Turn on')}
              </button>
            </div>
          )}

          {hasCalendarChannel && alarms.enabled
            ? (
                [
                  {
                    id: 'alarm-block-minutes',
                    field: 'block_minutes' as const,
                    label: t('目标执行块提醒', 'Goal blocks'),
                    value: alarms.block_minutes,
                  },
                  {
                    id: 'alarm-habit-minutes',
                    field: 'habit_minutes' as const,
                    label: t('习惯安排提醒', 'Habit sessions'),
                    value: alarms.habit_minutes,
                  },
                  {
                    id: 'alarm-manual-minutes',
                    field: 'manual_minutes' as const,
                    label: t('手动日程提醒', 'Manual events'),
                    value: alarms.manual_minutes,
                  },
                ] as const
              ).map((item) => {
                const hasCustomValue =
                  item.value !== null && !ALARM_LEAD_OPTIONS.some((opt) => opt.value === item.value);
                return (
                  <div key={item.id} className="setting-row">
                    <label className="setting-name" htmlFor={item.id}>
                      {item.label}
                    </label>
                    <div className="setting-control">
                      <select
                        id={item.id}
                        className="select"
                        value={item.value === null ? 'off' : String(item.value)}
                        disabled={updateAlarmsAction.isPending}
                        onChange={(e) => {
                          const raw = e.target.value;
                          handleAlarmsChange({
                            [item.field]: raw === 'off' ? null : Number(raw),
                          });
                        }}
                      >
                        {hasCustomValue ? (
                          <option value={String(item.value)}>
                            {t(`提前 ${item.value} 分钟`, `${item.value} min before`)}
                          </option>
                        ) : null}
                        {ALARM_LEAD_OPTIONS.map((opt) => (
                          <option
                            key={opt.value === null ? 'off' : opt.value}
                            value={opt.value === null ? 'off' : String(opt.value)}
                          >
                            {t(opt.zh, opt.en)}
                          </option>
                        ))}
                      </select>
                    </div>
                  </div>
                );
              })
            : null}
        </div>
      </section>

      <section className="settings-section" aria-label={t('邮件节奏提醒', 'Email digest reminders')}>
        <div className="settings-section-title">{t('通道二：邮件节奏摘要', 'Channel 2: Email rhythm digests')}</div>
        <p className="settings-section-copy">
          {t(
            '合并不轰炸，支持发送前二次校验（已完成或忙碌中自动取消/延后）。当前生效时区：',
            'Calm, merged digests with send-time guard (auto-skips if completed or busy). Active time zone: ',
          )}
          <span className="tabular-nums">{timezone}</span>
        </p>

        <form onSubmit={handleSaveEmail} noValidate>
          <div className="card-flat settings-card">
            <ErrorBanner
              error={updateEmailAction.actionError}
              onRefresh={() => {
                setEmailDirty(false);
                void remindersQuery.refetch();
              }}
            />

            {!channel.smtp_configured ? (
              <div className="status-banner info" role="status" style={{ margin: '12px 16px 4px' }}>
                <div className="status-banner-body">
                  {t(
                    '邮件发送服务暂未开启，你仍可先保存偏好，开启后将自动生效。',
                    'Email delivery is not enabled on this server yet; you can still save your preferences.',
                  )}
                </div>
              </div>
            ) : null}

            {!channel.user_email ? (
              <div className="status-banner info" role="status" style={{ margin: '12px 16px 4px' }}>
                <div className="status-banner-body">
                  {t('当前账号未绑定邮箱，邮件提醒暂时无法送达。', 'No email address is bound to this account yet.')}
                </div>
              </div>
            ) : null}

            <div className="setting-row">
              <div>
                <div className="setting-name">{t('开启邮件节奏提醒', 'Enable email digests')}</div>
                <div className="sync-hint">
                  {channel.user_email
                    ? t(`收件邮箱：${channel.user_email}`, `Delivering to: ${channel.user_email}`)
                    : t('未绑定收件邮箱', 'No recipient email')}
                </div>
              </div>
              <label className="sync-check">
                <input
                  id="email-reminders-enabled"
                  type="checkbox"
                  checked={emailState.enabled}
                  disabled={updateEmailAction.isPending}
                  onChange={(e) => updateDraftEmail({ enabled: e.target.checked })}
                />
                <span>{emailState.enabled ? t('已开启', 'On') : t('已关闭', 'Off')}</span>
              </label>
            </div>

            <div className="setting-row">
              <div>
                <div className="setting-name">{t('晚间合并收口', 'Evening merged wrap-up')}</div>
                <div className="sync-hint">
                  {t('当天仍有未签到安排时，只发 1 封温和汇总邮件。', 'One merged summary only when open items remain today.')}
                </div>
              </div>
              <div className="setting-control" style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <input
                  id="email-evening-wrapup"
                  type="checkbox"
                  aria-label={t('晚间合并收口开关', 'Evening merged wrap-up toggle')}
                  checked={emailState.evening_wrapup}
                  disabled={updateEmailAction.isPending}
                  onChange={(e) => updateDraftEmail({ evening_wrapup: e.target.checked })}
                />
                <input
                  id="email-evening-time"
                  type="time"
                  aria-label={t('晚间收口时间', 'Evening wrap-up time')}
                  className="input tabular-nums"
                  style={{ width: 112 }}
                  value={emailState.evening_time}
                  disabled={updateEmailAction.isPending || !emailState.evening_wrapup}
                  onChange={(e) => updateDraftEmail({ evening_time: e.target.value })}
                />
              </div>
            </div>

            <div className="setting-row">
              <div>
                <div className="setting-name">{t('早间今日聚焦', 'Morning focus')}</div>
                <div className="sync-hint">
                  {t('列出今日已排定的关键目标块与最小产出。', 'Lists today’s scheduled focus blocks and concrete outcomes.')}
                </div>
              </div>
              <div className="setting-control" style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <input
                  id="email-morning-focus"
                  type="checkbox"
                  aria-label={t('早间今日聚焦开关', 'Morning focus toggle')}
                  checked={emailState.morning_focus}
                  disabled={updateEmailAction.isPending}
                  onChange={(e) => updateDraftEmail({ morning_focus: e.target.checked })}
                />
                <input
                  id="email-morning-time"
                  type="time"
                  aria-label={t('早间聚焦时间', 'Morning focus time')}
                  className="input tabular-nums"
                  style={{ width: 112 }}
                  value={emailState.morning_time}
                  disabled={updateEmailAction.isPending || !emailState.morning_focus}
                  onChange={(e) => updateDraftEmail({ morning_time: e.target.value })}
                />
              </div>
            </div>

            <div className="setting-row">
              <div>
                <div className="setting-name">{t('灵活习惯周中窗口提醒', 'Weekly habit window nudge')}</div>
                <div className="sync-hint">
                  {t('本周剩余天数接近目标所差次数时提醒挑时间。', 'Nudges mid-week when remaining days approach remaining weekly target.')}
                </div>
              </div>
              <div className="setting-control">
                <input
                  id="email-weekly-habit"
                  type="checkbox"
                  aria-label={t('灵活习惯周中窗口提醒', 'Weekly habit window nudge')}
                  checked={emailState.weekly_habit_nudge}
                  disabled={updateEmailAction.isPending}
                  onChange={(e) => updateDraftEmail({ weekly_habit_nudge: e.target.checked })}
                />
              </div>
            </div>

            <div className="setting-row">
              <div>
                <div className="setting-name">{t('目标停滞温和唤醒', 'Goal stall nudge')}</div>
                <div className="sync-hint">
                  {t('进行中的目标连续 6 天未排期且无打卡时每周最多提醒 1 次。', 'Up to once weekly when an active goal is unscheduled for 6+ days.')}
                </div>
              </div>
              <div className="setting-control">
                <input
                  id="email-goal-stall"
                  type="checkbox"
                  aria-label={t('目标停滞温和唤醒', 'Goal stall nudge')}
                  checked={emailState.goal_stall_nudge}
                  disabled={updateEmailAction.isPending}
                  onChange={(e) => updateDraftEmail({ goal_stall_nudge: e.target.checked })}
                />
              </div>
            </div>

            <div className="setting-row">
              <div>
                <div className="setting-name">{t('周日复盘摘要', 'Sunday weekly review')}</div>
                <div className="sync-hint">
                  {t('周日傍晚汇总本周完成情况并提示安排下周。', 'Sunday summary of completed blocks/habits and next-week nudge.')}
                </div>
              </div>
              <div className="setting-control" style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                <input
                  id="email-weekly-review"
                  type="checkbox"
                  aria-label={t('周日复盘摘要开关', 'Sunday weekly review toggle')}
                  checked={emailState.weekly_review}
                  disabled={updateEmailAction.isPending}
                  onChange={(e) => updateDraftEmail({ weekly_review: e.target.checked })}
                />
                <input
                  id="email-weekly-review-time"
                  type="time"
                  aria-label={t('周日复盘时间', 'Weekly review time')}
                  className="input tabular-nums"
                  style={{ width: 112 }}
                  value={emailState.weekly_review_time}
                  disabled={updateEmailAction.isPending || !emailState.weekly_review}
                  onChange={(e) => updateDraftEmail({ weekly_review_time: e.target.value })}
                />
              </div>
            </div>

            <div className="setting-row">
              <div className="setting-name">{t('免打扰时段', 'Quiet hours')}</div>
              <div className="setting-control" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <input
                  id="email-quiet-start"
                  type="time"
                  aria-label={t('免打扰开始', 'Quiet hours start')}
                  className="input tabular-nums"
                  style={{ width: 108 }}
                  value={emailState.quiet_start}
                  disabled={updateEmailAction.isPending}
                  onChange={(e) => updateDraftEmail({ quiet_start: e.target.value })}
                />
                <span className="muted">–</span>
                <input
                  id="email-quiet-end"
                  type="time"
                  aria-label={t('免打扰结束', 'Quiet hours end')}
                  className="input tabular-nums"
                  style={{ width: 108 }}
                  value={emailState.quiet_end}
                  disabled={updateEmailAction.isPending}
                  onChange={(e) => updateDraftEmail({ quiet_end: e.target.value })}
                />
              </div>
            </div>

            <div className="setting-row">
              <label className="setting-name" htmlFor="email-daily-cap">
                {t('每日邮件上限（封）', 'Daily email cap')}
              </label>
              <div className="setting-control">
                <input
                  id="email-daily-cap"
                  type="number"
                  min={1}
                  max={10}
                  className="input tabular-nums"
                  style={{ width: 88 }}
                  value={emailState.daily_cap}
                  disabled={updateEmailAction.isPending}
                  onChange={(e) =>
                    updateDraftEmail({
                      daily_cap: Math.max(1, Math.min(10, Number(e.target.value) || 1)),
                    })
                  }
                />
              </div>
            </div>
          </div>

          <div className="save-row">
            <button type="submit" className="btn primary" disabled={updateEmailAction.isPending}>
              {updateEmailAction.isPending ? t('保存中…', 'Saving…') : t('保存邮件设置', 'Save email settings')}
            </button>
          </div>
        </form>
      </section>

      {recentNotifications.length > 0 ? (
        <section className="settings-section" aria-label={t('最近提醒记录', 'Recent reminders')}>
          <div className="settings-section-title">{t('最近提醒记录', 'Recent reminders')}</div>
          <div className="card-flat settings-card">
            {recentNotifications.map((item) => {
              const badge = statusBadge(item.status, lang);
              return (
                <div key={item.id} className="setting-row">
                  <div style={{ minWidth: 0 }}>
                    <div className="setting-name" style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                      <span>{kindLabel(item.kind, lang)}</span>
                      <span className={badge.cls}>{badge.text}</span>
                    </div>
                    <div className="setting-desc" style={{ marginTop: 2 }}>
                      {item.subject}
                    </div>
                  </div>
                  <div className="setting-control muted tabular-nums" style={{ fontSize: 12 }}>
                    {formatLogTime(item.sent_at || item.scheduled_for, timezone, lang)}
                  </div>
                </div>
              );
            })}
          </div>
        </section>
      ) : null}
    </>
  );
}
