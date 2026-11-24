import type { TimeWindow } from '../api/types';
import { getLocalizedDayLabels, useI18n } from '../i18n';

const START_TIME_RE = /^([01]\d|2[0-3]):[0-5]\d$/;
const END_TIME_RE = /^(([01]\d|2[0-3]):[0-5]\d|24:00)$/;

export function toMinutes(hhmm: string): number {
  const [h, m] = hhmm.split(':').map(Number);
  return h * 60 + m;
}

export function validateWindowGroup(
  label: string,
  windows: TimeWindow[],
  t: (zh: string, en: string) => string,
  minWindows = 0,
): string | null {
  if (windows.length < minWindows) {
    return t(`${label}至少保留 ${minWindows} 个时段。`, `${label}: keep at least ${minWindows} window.`);
  }
  for (let i = 0; i < windows.length; i++) {
    const w = windows[i];
    if (!w.days || w.days.length === 0) {
      return t(`${label}第 ${i + 1} 条请至少选择一天。`, `${label} #${i + 1}: select at least one day.`);
    }
    if (!START_TIME_RE.test(w.start)) {
      return t(
        `${label}第 ${i + 1} 条开始时间格式须为 HH:MM（00:00–23:59）。`,
        `${label} #${i + 1}: start time must be HH:MM (00:00–23:59).`,
      );
    }
    if (!END_TIME_RE.test(w.end)) {
      return t(
        `${label}第 ${i + 1} 条结束时间格式须为 HH:MM（00:01–24:00）。`,
        `${label} #${i + 1}: end time must be HH:MM (00:01–24:00).`,
      );
    }
    if (toMinutes(w.start) >= toMinutes(w.end)) {
      return t(
        `${label}第 ${i + 1} 条结束时间必须晚于开始时间（同一天内）。`,
        `${label} #${i + 1}: end time must be after start time on the same day.`,
      );
    }
  }
  return null;
}

function formatHHMM(minutes: number): string {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

const START_TIME_OPTIONS: string[] = Array.from({ length: 48 }, (_, i) => formatHHMM(i * 30));
const END_TIME_OPTIONS: string[] = Array.from({ length: 48 }, (_, i) => formatHHMM((i + 1) * 30));

function buildTimeOptions(base: string[], current: string): string[] {
  if (!current || base.includes(current) || !END_TIME_RE.test(current)) {
    return base;
  }
  return [...base, current].sort((a, b) => toMinutes(a) - toMinutes(b));
}

export function WindowGroupEditor({
  groupKey,
  title,
  emptyNotice,
  defaultWindow,
  windows,
  minWindows = 0,
  disabled,
  onChange,
}: {
  groupKey: string;
  title: string;
  emptyNotice: string;
  defaultWindow: TimeWindow;
  windows: TimeWindow[];
  minWindows?: number;
  disabled: boolean;
  onChange: (next: TimeWindow[]) => void;
}) {
  const { lang, t } = useI18n();
  const fullDayLabels = getLocalizedDayLabels(lang);
  const shortDayLabels = lang === 'en' ? ['M', 'T', 'W', 'T', 'F', 'S', 'S'] : ['一', '二', '三', '四', '五', '六', '日'];

  const addWindow = () => {
    onChange([...windows, { days: [...defaultWindow.days], start: defaultWindow.start, end: defaultWindow.end }]);
  };

  const removeWindow = (idx: number) => {
    if (windows.length <= minWindows) return;
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
        <div className="setting-name">{title}</div>
        <button
          type="button"
          className="btn small"
          aria-label={t(`添加${title}`, `Add ${title}`)}
          disabled={disabled}
          onClick={addWindow}
        >
          {t('+ 添加', '+ Add')}
        </button>
      </div>

      {windows.length === 0 ? (
        <div className="window-empty-text" role="status">
          {emptyNotice}
        </div>
      ) : (
        <div className="window-lines">
          {windows.map((win, idx) => {
            const startOptions = buildTimeOptions(START_TIME_OPTIONS, win.start);
            const endOptions = buildTimeOptions(END_TIME_OPTIONS, win.end);
            return (
              <div key={`${groupKey}-${idx}`} className="window-line">
                <div
                  className="day-pills"
                  role="group"
                  aria-label={t(`${title}第 ${idx + 1} 条星期`, `${title} #${idx + 1} days`)}
                >
                  {shortDayLabels.map((shortLabel, dayIdx) => {
                    const selected = win.days.includes(dayIdx);
                    return (
                      <button
                        key={dayIdx}
                        type="button"
                        className={`day-pill${selected ? ' selected' : ''}`}
                        aria-label={fullDayLabels[dayIdx]}
                        title={fullDayLabels[dayIdx]}
                        aria-pressed={selected}
                        disabled={disabled}
                        onClick={() => toggleDay(idx, dayIdx)}
                      >
                        {shortLabel}
                      </button>
                    );
                  })}
                </div>

                <div className="window-line-controls">
                  <label className="sr-only" htmlFor={`${groupKey}-${idx}-start`}>
                    {t(`${title}第 ${idx + 1} 条开始时间`, `${title} #${idx + 1} start time`)}
                  </label>
                  <select
                    id={`${groupKey}-${idx}-start`}
                    className="select time-select tabular-nums"
                    value={win.start}
                    disabled={disabled}
                    onChange={(e) => updateWindow(idx, { start: e.target.value })}
                  >
                    {startOptions.map((opt) => (
                      <option key={opt} value={opt}>
                        {opt}
                      </option>
                    ))}
                  </select>

                  <span className="window-time-sep" aria-hidden="true">
                    –
                  </span>

                  <label className="sr-only" htmlFor={`${groupKey}-${idx}-end`}>
                    {t(`${title}第 ${idx + 1} 条结束时间`, `${title} #${idx + 1} end time`)}
                  </label>
                  <select
                    id={`${groupKey}-${idx}-end`}
                    className="select time-select tabular-nums"
                    value={win.end}
                    disabled={disabled}
                    onChange={(e) => updateWindow(idx, { end: e.target.value })}
                  >
                    {endOptions.map((opt) => (
                      <option key={opt} value={opt}>
                        {opt}
                      </option>
                    ))}
                  </select>

                  <button
                    type="button"
                    className="window-remove-btn"
                    aria-label={t(`删除${title}第 ${idx + 1} 条`, `Remove ${title} #${idx + 1}`)}
                    title={
                      windows.length <= minWindows
                        ? t('至少保留 1 个时段', 'Keep at least 1 window')
                        : t('删除', 'Remove')
                    }
                    disabled={disabled || windows.length <= minWindows}
                    onClick={() => removeWindow(idx)}
                  >
                    <svg viewBox="0 0 16 16" aria-hidden="true">
                      <path d="M4.5 4.5 11.5 11.5M11.5 4.5 4.5 11.5" />
                    </svg>
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
