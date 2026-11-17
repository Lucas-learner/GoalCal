import Foundation

/// Single source of truth for how instants become text and back.
///
/// Everything stored in SQLite is UTC ISO-8601 with a fixed shape
/// (`2026-10-15T06:00:00Z`). Fixed shape matters for two reasons:
/// 1. lexical string comparison equals chronological comparison, so index-backed
///    range queries work on TEXT columns;
/// 2. the `CHECK (end_at > start_at)` constraint becomes a valid time check.
///
/// Localisation happens only at the view layer, never in storage.
public enum TimeFormat {
    private static let utc = TimeZone(secondsFromGMT: 0)!

    public static let style = Date.ISO8601FormatStyle(timeZone: utc)

    public static func string(from date: Date) -> String {
        date.formatted(style)
    }

    public static func date(from string: String) -> Date? {
        if let parsed = try? Date(string, strategy: style) {
            return parsed
        }
        // Tolerate fractional seconds that an older build may have written.
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        formatter.timeZone = utc
        return formatter.date(from: string)
    }

    /// Convenience for parsing user-visible times in a specific zone (used by
    /// the parser's fallback path, not by storage).
    public static func localDate(from string: String, in timeZone: TimeZone) -> Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        formatter.timeZone = timeZone
        return formatter.date(from: string)
    }
}

extension Date {
    /// Rounds down to the nearest multiple of `minutes` in the given calendar.
    /// The scheduler works on a fixed grid, so this keeps slots aligned.
    public func roundedDown(toMinutes minutes: Int, calendar: Calendar) -> Date {
        let components = calendar.dateComponents([.minute], from: self)
        guard let minute = components.minute else { return self }
        let remainder = minute % minutes
        guard remainder != 0 else { return self }
        return calendar.date(byAdding: .minute, value: -remainder, to: self) ?? self
    }

    /// Rounds up to the next multiple of `minutes` in the given calendar.
    public func roundedUp(toMinutes minutes: Int, calendar: Calendar) -> Date {
        let rounded = roundedDown(toMinutes: minutes, calendar: calendar)
        return rounded == self ? self : (calendar.date(byAdding: .minute, value: minutes, to: rounded) ?? self)
    }
}
