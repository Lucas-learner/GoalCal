import { DateTime } from 'luxon';
import { describe, expect, it } from 'vitest';
import {
  calendarStrToUtcIso,
  datetimeLocalToUtcIso,
  formatTimeHHmm,
  getRangeBoundsUtc,
  isExecutionWaitingCheckin,
  isoToDatetimeLocal,
} from '../lib/datetime';

describe('Luxon user.timezone conversions', () => {
  it('converts UTC ISO <-> datetime-local accurately in user.timezone (Asia/Shanghai)', () => {
    const utcIso = '2026-11-16T11:30:00Z'; // 19:30 in Asia/Shanghai (+08:00)
    const local = isoToDatetimeLocal(utcIso, 'Asia/Shanghai');
    expect(local).toBe('2026-11-16T19:30');
    expect(formatTimeHHmm(utcIso, 'Asia/Shanghai')).toBe('19:30');

    const backToUtc = datetimeLocalToUtcIso('2026-11-16T19:30', 'Asia/Shanghai');
    expect(backToUtc).toBe('2026-11-16T11:30:00Z');
  });

  it('handles negative-offset timezones across date boundaries (America/New_York)', () => {
    const utcIso = '2026-11-17T02:15:00Z'; // 2026-11-16 21:15 in EST (-05:00)
    const local = isoToDatetimeLocal(utcIso, 'America/New_York');
    expect(local).toBe('2026-11-16T21:15');
    expect(datetimeLocalToUtcIso(local, 'America/New_York')).toBe('2026-11-17T02:15:00Z');
  });

  it('computes Monday-Sunday week bounds in user.timezone', () => {
    // 2026-11-18 is Wednesday. Week is Mon 2026-11-16 00:00 +08:00 -> Mon 2026-11-23 00:00 +08:00
    const bounds = getRangeBoundsUtc('2026-11-18', 'week', 'Asia/Shanghai');
    expect(bounds.startUtc).toBe('2026-11-15T16:00:00Z');
    expect(bounds.endUtc).toBe('2026-11-22T16:00:00Z');
    expect(bounds.label).toBe('11 月 16 日 – 22 日');
  });

  it('parses FullCalendar dateStr into UTC ISO using user.timezone', () => {
    expect(calendarStrToUtcIso('2026-11-16T10:00:00', 'Asia/Shanghai')).toBe(
      '2026-11-16T02:00:00Z',
    );
  });

  it('infers waiting checkin from expected_end_at vs current time in user.timezone', () => {
    const expected = '2026-11-16T12:00:00Z';
    const before = DateTime.fromISO('2026-11-16T19:59:00', { zone: 'Asia/Shanghai' });
    const after = DateTime.fromISO('2026-11-16T20:00:00', { zone: 'Asia/Shanghai' });
    expect(isExecutionWaitingCheckin(expected, 'Asia/Shanghai', before)).toBe(false);
    expect(isExecutionWaitingCheckin(expected, 'Asia/Shanghai', after)).toBe(true);
  });
});

// Browser wall-clock inputs must not shift silently at DST transitions.
it('rejects nonexistent and ambiguous local times', () => {
  expect(datetimeLocalToUtcIso('2026-03-08T02:30', 'America/New_York')).toBeNull();
  expect(datetimeLocalToUtcIso('2026-11-01T01:30', 'America/New_York')).toBeNull();
  expect(datetimeLocalToUtcIso('2026-11-01T03:30', 'America/New_York')).toBe('2026-11-01T08:30:00Z');
});
