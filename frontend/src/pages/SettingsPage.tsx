import { useCallback, useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { invalidateAfterWrite, queryKeys } from '../api/queryKeys';
import type { Availability, TimeWindow, User } from '../api/types';
import { SparkleIcon } from '../components/Icons';
import { ErrorBanner, SkeletonCard } from '../components/StateFeedback';
import { useIdempotentAction } from '../lib/useIdempotentMutation';
import { useAppState } from '../state/AppStateContext';

const DAY_LABELS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
const MAX_ICS_BYTES = 5 * 1024 * 1024;

type SettingsSectionId = 'account' | 'windows' | 'preferences' | 'import';

const START_TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;
const END_TIME_RE = /^(([01]\d|2[0-3]):[0-5]\d|24:00)$/;

function toMinutes(hhmm: string): number {
  const [h, m] = hhmm.split(':').map(Number);
  return h * 60 + m;
}

function validateWindowGroup(label: string, windows: TimeWindow[]): string | null {
  for (let i = 0; i < windows.length; i++) {
    const w = windows[i];
    if (!w.days || w.days.length === 0) {
      return `${label}第 ${i + 1} 条请至少选择一天。`;
    }
    if (!START_TIME_RE.test(w.start)) {
      return `${label}第 ${i + 1} 条开始时间格式须为 HH:MM（00:00–23:59）。`;
    }
    if (!END_TIME_RE.test(w.end)) {
      return `${label}第 ${i + 1} 条结束时间格式须为 HH:MM（00:01–24:00）。`;
    }
    if (toMinutes(w.start) >= toMinutes(w.end)) {
      return `${label}第 ${i + 1} 条结束时间必须晚于开始时间（同一天内）。`;
    }
  }
  return null;
}

function formatPct(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  return `${Math.round(value * 100)}%`;
}

function WindowGroupEditor({
  groupKey,
  title,
  description,
  emptyNotice,
  windows,
  disabled,
  onChange,
}: {
  groupKey: string;
  title: string;
  description: string;
  emptyNotice: string;
  windows: TimeWindow[];
  disabled: boolean;
  onChange: (next: TimeWindow[]) => void;
}) {
  const addWindow = () => {
    onChange([...windows, { days: [0, 1, 2, 3, 4], start: '09:00', end: '12:00' }]);
  };

  const removeWindow = (idx: number) => {
    onChange(windows.filter((_, i) => i !== idx));
  };

  const updateWindow = (idx: number, patch: Partial<TimeWindow>) => {
    onChange(windows.map((w, i) => (i === idx ? { ...w, ...patch } : w)));
  };

  const toggleDay = (idx: number, day: number) => {
    const curr = windows[idx].days;
    const exists = curr.includes(day);
    const nextDays = exists
      ? curr.filter((d) => d !== day)
      : [...curr, day].sort((a, b) => a - b);
    updateWindow(idx, { days: nextDays });
  };

  return (
    <div className="window-group">
      <div className="window-group-head">
        <div>
          <div className="setting-name">{title}</div>
          <div className="setting-desc">{description}</div>
        </div>
        <button type="button" className="btn small" disabled={disabled} onClick={addWindow}>
          + 添加时段
        </button>
      </div>

      {windows.length === 0 ? (
        <div className="field-help" role="status">
          {emptyNotice}
        </div>
      ) : (
        windows.map((win, idx) => (
          <div key={`${groupKey}-${idx}`} className="window-row">
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
              <div className="day-chips" role="group" aria-label={`${title}第 ${idx + 1} 条星期`}>
                {DAY_LABELS.map((label, dayIdx) => {
                  const selected = win.days.includes(dayIdx);
                  return (
                    <button
                      key={dayIdx}
                      type="button"
                      className={`day-chip${selected ? ' selected' : ''}`}
                      aria-pressed={selected}
                      disabled={disabled}
                      onClick={() => toggleDay(idx, dayIdx)}
                    >
                      {label}
                    </button>
                  );
                })}
              </div>
              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                <button
                  type="button"
                  className="btn small"
                  disabled={disabled}
                  onClick={() => updateWindow(idx, { days: [0, 1, 2, 3, 4] })}
                >
                  工作日
                </button>
                <button
                  type="button"
                  className="btn small"
                  disabled={disabled}
                  onClick={() => updateWindow(idx, { days: [5, 6] })}
                >
                  周末
                </button>
              </div>
            </div>

            <div className="time-range-inputs">
              <label htmlFor={`${groupKey}-${idx}-start`} style={{ fontSize: 11 }}>
                开始
              </label>
              <input
                id={`${groupKey}-${idx}-start`}
                type="text"
                inputMode="numeric"
                className="input time-input-small tabular-nums"
                placeholder="09:00"
                value={win.start}
                disabled={disabled}
                onChange={(e) => updateWindow(idx, { start: e.target.value })}
              />
              <span aria-hidden="true">–</span>
              <label htmlFor={`${groupKey}-${idx}-end`} style={{ fontSize: 11 }}>
                结束
              </label>
              <input
                id={`${groupKey}-${idx}-end`}
                type="text"
                inputMode="numeric"
                className="input time-input-small tabular-nums"
                placeholder="18:00 或 24:00"
                value={win.end}
                disabled={disabled}
                onChange={(e) => updateWindow(idx, { end: e.target.value })}
              />
              <button
                type="button"
                className="btn small"
                disabled={disabled}
                onClick={() => updateWindow(idx, { end: '24:00' })}
              >
                24:00
              </button>
              <button
                type="button"
                className="btn small"
                disabled={disabled}
                onClick={() => removeWindow(idx)}
              >
                删除
              </button>
            </div>
          </div>
        ))
      )}
    </div>
  );
}

export function SettingsPage({ user }: { user: User | undefined }) {
  const queryClient = useQueryClient();
  const { openAssistant, recordMutationResult } = useAppState();
  const [section, setSection] = useState<SettingsSectionId>('account');

  const availabilityQuery = useQuery({
    queryKey: queryKeys.availability,
    queryFn: api.getAvailability,
  });

  const metricsQuery = useQuery({
    queryKey: queryKeys.metrics,
    queryFn: api.getMetrics,
  });

  const [draftAvail, setDraftAvail] = useState<Availability | null>(null);
  const [dirty, setDirty] = useState(false);
  const [validationError, setValidationError] = useState<string | null>(null);
  const [savedNotice, setSavedNotice] = useState<string | null>(null);

  // Sync from server only when not dirty (preserves edits across 409 refresh!)
  useEffect(() => {
    if (availabilityQuery.data && !dirty) {
      setDraftAvail(structuredClone(availabilityQuery.data));
    }
  }, [availabilityQuery.data, dirty]);

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
      setSavedNotice('可用时间与计划偏好已保存，后续排期将立即使用新配置。');
      recordMutationResult({ message: '设置已保存' });
      await invalidateAfterWrite(queryClient, { includeAvailability: true });
    },
    onError: async (err) => {
      setSavedNotice(null);
      if (err.status === 409) {
        await queryClient.invalidateQueries({ queryKey: queryKeys.availability });
      }
    },
  });

  // ICS import state
  const [icsFile, setIcsFile] = useState<File | null>(null);
  const [icsLocalError, setIcsLocalError] = useState<string | null>(null);
  const [icsResultMsg, setIcsResultMsg] = useState<string | null>(null);

  const importFn = useCallback(
    async (icsText: string, key: string) => api.importCalendarIcs({ ics: icsText }, key),
    [],
  );

  const importAction = useIdempotentAction(importFn, {
    onSuccess: async (res) => {
      setIcsLocalError(null);
      setIcsResultMsg(`${res.message}（共处理 ${res.count} 条日程）`);
      recordMutationResult({ message: `${res.message}（${res.count} 条）` });
      await invalidateAfterWrite(queryClient);
    },
  });

  const handleSaveAvailability = (e: React.FormEvent) => {
    e.preventDefault();
    if (!draftAvail) return;
    setSavedNotice(null);
    setValidationError(null);

    const err =
      validateWindowGroup('可用时间', draftAvail.available_windows) ??
      validateWindowGroup('保护时段', draftAvail.protected_windows) ??
      validateWindowGroup('偏好时段', draftAvail.preferred_windows);

    if (err) {
      setValidationError(err);
      return;
    }

    if (
      !Number.isFinite(draftAvail.buffer_minutes) ||
      draftAvail.buffer_minutes < 0 ||
      draftAvail.buffer_minutes > 120
    ) {
      setValidationError('日程缓冲时间须为 0 至 120 分钟。');
      return;
    }

    if (
      !Number.isFinite(draftAvail.max_goal_minutes_per_day) ||
      draftAvail.max_goal_minutes_per_day < 1 ||
      draftAvail.max_goal_minutes_per_day > 1440
    ) {
      setValidationError('每天最多投入目标时间须为 1 至 1440 分钟。');
      return;
    }

    // The version belongs to the draft the user edited; a background refresh must not overwrite concurrent edits.
    void saveAction.run(draftAvail);
  };

  const handleImportIcs = async () => {
    setIcsLocalError(null);
    setIcsResultMsg(null);
    if (!icsFile) {
      setIcsLocalError('请先选择一个 .ics 日历文件。');
      return;
    }
    if (icsFile.size > MAX_ICS_BYTES) {
      setIcsLocalError('ICS 文件大小不能超过 5 MB。');
      return;
    }
    try {
      const text = await icsFile.text();
      if (!text.includes('BEGIN:VCALENDAR')) {
        setIcsLocalError('文件内容不是有效的 ICS 日历格式（缺少 BEGIN:VCALENDAR）。');
        return;
      }
      await importAction.run(text);
    } catch {
      setIcsLocalError('读取本地文件失败，请重新选择文件。');
    }
  };

  const metrics = metricsQuery.data;
  const acceptanceRate = metrics?.acceptance_rate ?? metrics?.acceptance ?? null;
  const recoveryRate = metrics?.recovery_rate_72h ?? metrics?.recovery_72h ?? null;
  const progressRate = metrics?.progress_rate ?? metrics?.progress ?? null;

  return (
    <div className="settings-wrap">
      <div className="topbar">
        <div>
          <div className="eyebrow">账号与个人偏好</div>
          <h1>设置</h1>
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

      <p className="subtitle">让计划适应你的生活节奏，而不是要求你适应系统。</p>

      <div className="settings-layout">
        <nav className="settings-nav" aria-label="设置分组">
          {[
            { id: 'account' as const, label: '通用与账号' },
            { id: 'windows' as const, label: '可用与保护时间' },
            { id: 'preferences' as const, label: '计划偏好' },
            { id: 'import' as const, label: '日历导入与指标' },
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
            <section className="settings-section" aria-label="通用与账号">
              <div className="settings-section-title">通用与账号</div>
              <div className="settings-section-copy">
                当前登录账号与时区由服务端账号配置决定，所有页面均按该时区显示日期与日程。
              </div>
              <div className="card-flat settings-card">
                <div className="setting-row">
                  <div>
                    <div className="setting-name">当前账号</div>
                    <div className="setting-desc">用于隔离你的目标、日程与对话记录</div>
                  </div>
                  <div className="setting-control" style={{ fontSize: 12, fontWeight: 600 }}>
                    {user?.name || '—'}
                  </div>
                </div>
                <div className="setting-row">
                  <div>
                    <div className="setting-name">绑定邮箱</div>
                    <div className="setting-desc">用于登录与后续账号找回</div>
                  </div>
                  <div className="setting-control" style={{ fontSize: 12 }}>
                    {user?.email || '未绑定'}
                  </div>
                </div>
                <div className="setting-row">
                  <div>
                    <div className="setting-name">当前时区</div>
                    <div className="setting-desc">用于日历网格、时间转换、签到与排期窗口</div>
                  </div>
                  <div className="setting-control tabular-nums" style={{ fontSize: 12 }}>
                    {user?.timezone || '—'}
                  </div>
                </div>
                <div className="setting-row">
                  <div>
                    <div className="setting-name">退出登录</div>
                    <div className="setting-desc">清除当前浏览器会话并返回登录页</div>
                  </div>
                  <form method="post" action="/_goalcal/logout?next=/app/">
                    <button type="submit" className="btn">
                      退出登录
                    </button>
                  </form>
                </div>
              </div>
            </section>
          ) : null}

          {section === 'windows' || section === 'preferences' ? (
            availabilityQuery.isLoading ? (
              <SkeletonCard label="正在加载排期偏好设置" />
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

                {savedNotice ? (
                  <div className="status-banner info" role="status">
                    <div className="status-banner-body">{savedNotice}</div>
                  </div>
                ) : null}

                {section === 'windows' ? (
                  <section className="settings-section" aria-label="可用与保护时间">
                    <div className="settings-section-title">每周时间窗口（精确到分钟）</div>
                    <div className="settings-section-copy">
                      按周一至周日配置每日同一天内的 HH:MM 时段（结束时间支持至 24:00）。
                    </div>
                    <div className="card-flat availability-card">
                      <WindowGroupEditor
                        groupKey="avail"
                        title="可用时间"
                        description="只有落在可用时间内的空档才会被排入目标时间块。"
                        emptyNotice="当前未配置任何可用时间，系统不会推荐任何可排期时段（不会自动视为全天可用）。"
                        windows={draftAvail.available_windows}
                        disabled={saveAction.isPending}
                        onChange={(next) => {
                          setDirty(true);
                          setDraftAvail({ ...draftAvail, available_windows: next });
                        }}
                      />

                      <WindowGroupEditor
                        groupKey="protected"
                        title="保护时段"
                        description="如午休或固定休息时间，排期时会避开并在日历中标出。"
                        emptyNotice="当前没有设置保护时段。"
                        windows={draftAvail.protected_windows}
                        disabled={saveAction.isPending}
                        onChange={(next) => {
                          setDirty(true);
                          setDraftAvail({ ...draftAvail, protected_windows: next });
                        }}
                      />

                      <WindowGroupEditor
                        groupKey="preferred"
                        title="偏好时段"
                        description="仅用于提高这些时段候选的排序权重，不代表额外可用时间。"
                        emptyNotice="当前没有设置偏好时段，系统会按精力曲线与可用时间排序。"
                        windows={draftAvail.preferred_windows}
                        disabled={saveAction.isPending}
                        onChange={(next) => {
                          setDirty(true);
                          setDraftAvail({ ...draftAvail, preferred_windows: next });
                        }}
                      />
                    </div>
                  </section>
                ) : (
                  <section className="settings-section" aria-label="计划偏好">
                    <div className="settings-section-title">计划偏好</div>
                    <div className="settings-section-copy">
                      控制每日目标总时长与相邻日程之间的缓冲时间。
                    </div>
                    <div className="card-flat settings-card">
                      <div className="setting-row">
                        <div>
                          <label className="setting-name" htmlFor="pref-max-minutes">
                            每天最多投入目标（分钟）
                          </label>
                          <div className="setting-desc">
                            当天达到该上限后，不再安排新的目标时间块
                          </div>
                        </div>
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
                        <div>
                          <label className="setting-name" htmlFor="pref-buffer-minutes">
                            日程缓冲时间（分钟）
                          </label>
                          <div className="setting-desc">
                            相邻事件之间预留的切换间隔
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
                )}

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
                      还原为服务端配置
                    </button>
                  ) : null}
                  <button type="submit" className="btn primary" disabled={saveAction.isPending}>
                    {saveAction.isPending ? '保存中…' : '保存更改'}
                  </button>
                </div>
              </form>
            ) : null
          ) : null}

          {section === 'import' ? (
            <>
              <section className="settings-section" aria-label="ICS 日历导入">
                <div className="settings-section-title">日历导入（ICS 单向导入）</div>
                <div className="settings-section-copy">
                  上传本地 .ics 文件将日程导入为固定日程（按 UID 去重，文件上限 5 MB）。不支持双向同步。
                </div>
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
                      选择 .ics 日历文件
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
                    {importAction.isPending ? '导入中…' : '开始导入'}
                  </button>
                </div>
              </section>

              <section className="settings-section" aria-label="近 7 天效果指标">
                <div className="settings-section-title">近 7 天指标</div>
                <div className="settings-section-copy">
                  反映近期时间建议的匹配度与执行恢复情况。分母为 0 时显示“—”。
                </div>
                {metricsQuery.isLoading ? (
                  <SkeletonCard label="正在加载指标" />
                ) : metricsQuery.isError ? (
                  <ErrorBanner
                    error={metricsQuery.error}
                    onRetry={() => void metricsQuery.refetch()}
                  />
                ) : (
                  <div className="metrics-grid">
                    <div className="card-flat metric-card">
                      <div className="metric-label">建议接受率</div>
                      <div className="metric-value">{formatPct(acceptanceRate)}</div>
                    </div>
                    <div className="card-flat metric-card">
                      <div className="metric-label">72h 恢复率</div>
                      <div className="metric-value">{formatPct(recoveryRate)}</div>
                    </div>
                    <div className="card-flat metric-card">
                      <div className="metric-label">推进率</div>
                      <div className="metric-value">{formatPct(progressRate)}</div>
                    </div>
                  </div>
                )}
              </section>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}
