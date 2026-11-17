import { DateTime } from 'luxon';
import { getActiveLang, type Lang } from '../i18n';

const WEEKDAY_ZH = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
const WEEKDAY_EN = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const MONTH_EN = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export function toZoneDateTime(iso: string, zone: string): DateTime {
  return DateTime.fromISO(iso, { setZone: true }).setZone(zone);
}

export function nowInZone(zone: string): DateTime {
  return DateTime.now().setZone(zone);
}

export function todayIsoDateInZone(zone: string): string {
  return nowInZone(zone).toISODate() ?? '';
}

export function addDaysIsoDate(baseIsoDate: string, days: number, zone: string): string {
  const dt = DateTime.fromISO(baseIsoDate, { zone });
  return dt.plus({ days }).toISODate() ?? baseIsoDate;
}

export function addMonthsIsoDate(baseIsoDate: string, months: number, zone: string): string {
  const dt = DateTime.fromISO(baseIsoDate, { zone });
  return dt.plus({ months }).toISODate() ?? baseIsoDate;
}

export function formatEyebrowDate(
  isoDateOrDateTime: string,
  zone: string,
  lang: Lang = getActiveLang(),
): string {
  const dt = isoDateOrDateTime.includes('T')
    ? toZoneDateTime(isoDateOrDateTime, zone)
    : DateTime.fromISO(isoDateOrDateTime, { zone });
  if (!dt.isValid) return isoDateOrDateTime;
  if (lang === 'en') {
    const weekday = WEEKDAY_EN[dt.weekday - 1] ?? '';
    const month = MONTH_EN[dt.month - 1] ?? String(dt.month);
    return `${weekday}, ${month} ${dt.day}`;
  }
  const weekday = WEEKDAY_ZH[dt.weekday - 1] ?? '';
  return `${dt.month} 月 ${dt.day} 日 · ${weekday}`;
}

export function formatTimeHHmm(iso: string, zone: string): string {
  const dt = toZoneDateTime(iso, zone);
  return dt.isValid ? dt.toFormat('HH:mm') : iso;
}

export function formatTimeRange(startIso: string, endIso: string, zone: string): string {
  const start = toZoneDateTime(startIso, zone);
  const end = toZoneDateTime(endIso, zone);
  if (!start.isValid || !end.isValid) return '';
  if (start.hasSame(end, 'day')) {
    return `${start.toFormat('HH:mm')}–${end.toFormat('HH:mm')}`;
  }
  return `${start.toFormat('MM/dd HH:mm')}–${end.toFormat('MM/dd HH:mm')}`;
}

export function isEventOnIsoDate(startIso: string, baseDateIso: string, zone: string): boolean {
  const start = toZoneDateTime(startIso, zone);
  if (!start.isValid) return false;
  return start.toISODate() === baseDateIso;
}

export function formatRelativeEventDay(
  startIso: string,
  baseDateIso: string,
  zone: string,
  lang: Lang = getActiveLang(),
): string {
  const start = toZoneDateTime(startIso, zone).startOf('day');
  const base = DateTime.fromISO(baseDateIso, { zone }).startOf('day');
  if (!start.isValid || !base.isValid) return '';
  const diffDays = Math.round(start.diff(base, 'days').days);
  if (diffDays === 0) return '';
  if (diffDays === 1) return lang === 'en' ? 'Tomorrow' : '明天';
  if (diffDays === -1) return lang === 'en' ? 'Yesterday' : '昨天';
  if (lang === 'en') {
    const month = MONTH_EN[start.month - 1] ?? String(start.month);
    return `${month} ${start.day}`;
  }
  return `${start.month}月${start.day}日`;
}

export function formatDateTimeLabel(
  startIso: string,
  endIso: string,
  zone: string,
  lang: Lang = getActiveLang(),
): string {
  const start = toZoneDateTime(startIso, zone);
  const end = toZoneDateTime(endIso, zone);
  if (!start.isValid || !end.isValid) return startIso;

  const today = nowInZone(zone).startOf('day');
  const startDay = start.startOf('day');
  const diffDays = Math.round(startDay.diff(today, 'days').days);

  if (lang === 'en') {
    const weekday = WEEKDAY_EN[start.weekday - 1] ?? '';
    const month = MONTH_EN[start.month - 1] ?? String(start.month);
    let dayLabel = `${weekday}, ${month} ${start.day}`;
    if (diffDays === 0) dayLabel = `Today (${month} ${start.day})`;
    else if (diffDays === 1) dayLabel = `Tomorrow (${weekday}, ${month} ${start.day})`;
    else if (diffDays === -1) dayLabel = `Yesterday (${month} ${start.day})`;
    return `${dayLabel} ${formatTimeRange(startIso, endIso, zone)}`;
  }

  const weekday = WEEKDAY_ZH[start.weekday - 1] ?? '';
  let dayLabel = `${start.month} 月 ${start.day} 日 ${weekday}`;
  if (diffDays === 0) dayLabel = `今天 (${start.month}/${start.day})`;
  else if (diffDays === 1) dayLabel = `明天 (${start.month}/${start.day} ${weekday})`;
  else if (diffDays === -1) dayLabel = `昨天 (${start.month}/${start.day})`;

  return `${dayLabel} ${formatTimeRange(startIso, endIso, zone)}`;
}

export function formatShortDate(
  isoDate: string | null,
  zone: string,
  lang: Lang = getActiveLang(),
): string {
  if (!isoDate) return lang === 'en' ? 'No fixed deadline' : '没有明确期限';
  const dt = DateTime.fromISO(isoDate, { zone });
  if (!dt.isValid) return isoDate;
  if (lang === 'en') {
    const month = MONTH_EN[dt.month - 1] ?? String(dt.month);
    return `${month} ${dt.day}`;
  }
  return `${dt.month} 月 ${dt.day} 日`;
}

export function daysUntilDeadline(deadlineIso: string | null, zone: string, nowIso?: string): number | null {
  if (!deadlineIso) return null;
  const target = DateTime.fromISO(deadlineIso, { zone }).startOf('day');
  const today = (nowIso ? DateTime.fromISO(nowIso, { zone }) : nowInZone(zone)).startOf('day');
  if (!target.isValid || !today.isValid) return null;
  return Math.round(target.diff(today, 'days').days);
}

export function durationMinutes(startIso: string, endIso: string): number {
  const start = DateTime.fromISO(startIso, { setZone: true });
  const end = DateTime.fromISO(endIso, { setZone: true });
  if (!start.isValid || !end.isValid) return 0;
  return Math.max(0, Math.round(end.diff(start, 'minutes').minutes));
}

/**
 * Convert an aware ISO string to a `datetime-local` input value (YYYY-MM-DDTHH:mm) in user.timezone.
 */
export function isoToDatetimeLocal(iso: string, zone: string): string {
  const dt = toZoneDateTime(iso, zone);
  return dt.isValid ? dt.toFormat("yyyy-MM-dd'T'HH:mm") : '';
}

/**
 * Interpret a `datetime-local` input value (YYYY-MM-DDTHH:mm) in user.timezone and output UTC ISO with Z.
 */
export function datetimeLocalToUtcIso(localValue: string, zone: string): string | null {
  if (!localValue) return null;
  const dt = DateTime.fromFormat(localValue, "yyyy-MM-dd'T'HH:mm", { zone });
  // Reject nonexistent or ambiguous wall times instead of silently shifting them.
  if (!dt.isValid || dt.toFormat("yyyy-MM-dd'T'HH:mm") !== localValue || dt.getPossibleOffsets().length !== 1) {
    return null;
  }
  return dt.toUTC().toISO({ suppressMilliseconds: true });
}

/**
 * Interpret a FullCalendar dateStr / startStr / endStr in user.timezone and return UTC ISO with Z.
 */
export function calendarStrToUtcIso(str: string, zone: string): string | null {
  if (!str) return null;
  const dt = DateTime.fromISO(str, { zone });
  if (!dt.isValid) return null;
  return dt.toUTC().toISO({ suppressMilliseconds: true });
}

/**
 * Compute start/end UTC ISO bounds for a given week or day in user.timezone.
 */
export function getRangeBoundsUtc(
  anchorIsoDate: string,
  mode: 'week' | 'day',
  zone: string,
): { startUtc: string; endUtc: string; label: string } {
  const anchor = DateTime.fromISO(anchorIsoDate, { zone }).startOf('day');
  if (mode === 'day') {
    const start = anchor;
    const end = anchor.plus({ days: 1 });
    const weekday = WEEKDAY_ZH[start.weekday - 1] ?? '';
    return {
      startUtc: start.toUTC().toISO({ suppressMilliseconds: true })!,
      endUtc: end.toUTC().toISO({ suppressMilliseconds: true })!,
      label: `${start.month} 月 ${start.day} 日 · ${weekday}`,
    };
  }
  // Week starts Monday (weekday 1..7)
  const weekStart = anchor.minus({ days: anchor.weekday - 1 });
  const weekEnd = weekStart.plus({ days: 7 });
  const lastDay = weekEnd.minus({ days: 1 });
  const label =
    weekStart.month === lastDay.month
      ? `${weekStart.month} 月 ${weekStart.day} 日 – ${lastDay.day} 日`
      : `${weekStart.month} 月 ${weekStart.day} 日 – ${lastDay.month} 月 ${lastDay.day} 日`;
  return {
    startUtc: weekStart.toUTC().toISO({ suppressMilliseconds: true })!,
    endUtc: weekEnd.toUTC().toISO({ suppressMilliseconds: true })!,
    label,
  };
}

/**
 * Infer whether a running execution is past expected_end_at in user.timezone.
 */
export function isExecutionWaitingCheckin(
  expectedEndIso: string,
  zone: string,
  nowOverride?: DateTime,
): boolean {
  const now = nowOverride ?? nowInZone(zone);
  const expectedEnd = toZoneDateTime(expectedEndIso, zone);
  if (!expectedEnd.isValid) return false;
  return now.toMillis() >= expectedEnd.toMillis();
}

export function isIsoExpired(expiresAtIso: string, zone: string, nowOverride?: DateTime): boolean {
  const now = nowOverride ?? nowInZone(zone);
  const expires = toZoneDateTime(expiresAtIso, zone);
  if (!expires.isValid) return false;
  return now.toMillis() >= expires.toMillis();
}

export function formatWeekRangeLabel(startStr: string, endStr: string, mode: 'week' | 'day', zone: string): string {
  const start = DateTime.fromISO(startStr, { zone });
  const end = DateTime.fromISO(endStr, { zone });
  if (!start.isValid || !end.isValid) return '';
  if (mode === 'day') {
    const weekday = WEEKDAY_ZH[start.weekday - 1] ?? '';
    return `${start.month} 月 ${start.day} 日 · ${weekday}`;
  }
  const inclusiveEnd = end.minus({ days: 1 });
  if (start.month === inclusiveEnd.month) {
    return `${start.month} 月 ${start.day} 日 – ${inclusiveEnd.day} 日`;
  }
  return `${start.month} 月 ${start.day} 日 – ${inclusiveEnd.month} 月 ${inclusiveEnd.day} 日`;
}
