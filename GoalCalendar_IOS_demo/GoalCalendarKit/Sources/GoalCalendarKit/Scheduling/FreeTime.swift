import Foundation

/// Computes the holes in a user's calendar.
///
/// Pure and timezone-aware: the work window (09:00–22:00 by default) is applied
/// in the *user's* timezone, day by day, so a user in Asia/Shanghai gets the same
/// answers regardless of where the device thinks it is.
public enum FreeTime {

    public static func slots(user: User, events: [CalendarEvent], from start: Date, to end: Date) -> [DateInterval] {
        let busy = events
            .filter { $0.blocksTime }
            .map { DateInterval(start: $0.startAt, end: $0.endAt) }
        return slots(user: user, busy: busy, from: start, to: end)
    }

    public static func slots(user: User, busy: [DateInterval], from start: Date, to end: Date) -> [DateInterval] {
        guard end > start else { return [] }
        let calendar = user.calendar
        var results: [DateInterval] = []
        var dayCursor = calendar.startOfDay(for: start)
        let lastDay = calendar.startOfDay(for: end)

        while dayCursor <= lastDay {
            defer {
                dayCursor = calendar.date(byAdding: .day, value: 1, to: dayCursor)
                    ?? dayCursor.addingTimeInterval(86_400)
            }
            guard
                let windowStart = calendar.date(byAdding: .minute, value: user.workStartMinute, to: dayCursor),
                let windowEnd = calendar.date(byAdding: .minute, value: user.workEndMinute, to: dayCursor)
            else { continue }

            let lower = max(windowStart, start)
            let upper = min(windowEnd, end)
            guard lower < upper else { continue }

            let overlapping = busy
                // Tolerate inverted intervals rather than dropping them: a bad
                // row should still block time, not silently open a hole.
                .map { DateInterval(start: min($0.start, $0.end), end: max($0.start, $0.end)) }
                .filter { $0.end > lower && $0.start < upper }
                .sorted { $0.start < $1.start }

            var cursor = lower
            for interval in overlapping {
                if interval.start > cursor {
                    results.append(DateInterval(start: cursor, end: min(interval.start, upper)))
                }
                cursor = max(cursor, interval.end)
                if cursor >= upper { break }
            }
            if cursor < upper {
                results.append(DateInterval(start: cursor, end: upper))
            }
        }
        return results.filter { $0.duration > 0 }
    }
}
