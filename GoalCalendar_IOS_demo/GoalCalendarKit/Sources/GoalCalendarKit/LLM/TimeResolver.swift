import Foundation

/// Turns semantic time fields into absolute instants. This is the only place in
/// the app where "tomorrow" becomes a date.
///
/// Why this exists separately from the LLM: date arithmetic is deterministic and
/// must be correct every time. Asking a language model to compute "next
/// Wednesday from today" invites silent off-by-one-day bugs that are invisible
/// in testing and catastrophic in a calendar. The model's job stops at "the user
/// said Wednesday"; everything past that is code.
public struct TimeResolver: Sendable {

    public init() {}

    /// Resolves against a `User`'s timezone and calendar.
    public func resolve(
        _ expression: TimeExpression,
        user: User,
        now: Date,
        defaultDurationMinutes: Int = 60
    ) -> TimeResolution? {
        resolve(
            expression,
            now: now,
            calendar: user.calendar,
            defaultDurationMinutes: defaultDurationMinutes
        )
    }

    public func resolve(
        _ expression: TimeExpression,
        now: Date,
        calendar: Calendar,
        defaultDurationMinutes: Int = 60
    ) -> TimeResolution? {
        let hasDay = expression.day != nil
            || expression.weekday != nil
            || expression.explicitDate != nil
            || (expression.month != nil && expression.dayOfMonth != nil)
        let hasTime = expression.hour != nil || expression.period != nil
        guard hasDay || hasTime else { return nil }

        var notes: [String] = []
        guard let baseDay = resolveDay(expression, now: now, calendar: calendar) else {
            return nil
        }

        let period = expression.period ?? .unspecified
        var minute = expression.minute ?? 0
        var dayShift = 0
        let hour: Int

        if let rawHour = expression.hour {
            switch period {
            case .afternoon:
                hour = rawHour < 12 ? rawHour + 12 : rawHour
            case .evening, .night:
                // "晚上8点" means 20:00, but "夜里2点" means 02:00. The hour
                // itself disambiguates; blanket +12 is wrong half the night.
                if rawHour == 12 {
                    hour = 0
                    dayShift = 1
                } else if rawHour <= 5 {
                    hour = rawHour
                } else if rawHour < 12 {
                    hour = rawHour + 12
                } else {
                    hour = rawHour
                }
            case .noon:
                hour = 12
                minute = 0
            case .morning:
                hour = rawHour
            case .unspecified:
                // Bare "3点" almost always means 15:00 in speech. Early-morning
                // intent is signalled with 凌晨/早上, which carries a period.
                hour = rawHour <= 6 ? rawHour + 12 : rawHour
            }
        } else {
            switch period {
            case .morning: hour = 9
            case .noon: hour = 12
            case .afternoon: hour = 14
            case .evening: hour = 19
            case .night: hour = 21
            case .unspecified: hour = 9
            }
        }

        var components = calendar.dateComponents([.year, .month, .day], from: baseDay)
        components.hour = min(max(hour, 0), 23)
        components.minute = min(max(minute, 0), 59)
        components.second = 0
        guard var start = calendar.date(from: components) else { return nil }

        if dayShift != 0, let shifted = calendar.date(byAdding: .day, value: dayShift, to: start) {
            start = shifted
        }

        // "今天下午3点" said at 4pm: silently landing in the past would create an
        // event nobody can complete. Roll forward and say so.
        if start < now {
            let anchoredToToday: Bool
            switch expression.day {
            case .some(.today), .some(.unspecified), .none:
                anchoredToToday = expression.weekday == nil && expression.explicitDate == nil
            default:
                anchoredToToday = false
            }
            if anchoredToToday, let next = calendar.date(byAdding: .day, value: 1, to: start) {
                start = next
                notes.append("你提到的时间今天已经过去了，顺延到明天同一时刻")
            }
        }

        let duration = max(15, expression.durationMinutes ?? defaultDurationMinutes)
        let end = start.addingTimeInterval(TimeInterval(duration * 60))
        guard end > start else { return nil }
        return TimeResolution(interval: DateInterval(start: start, end: end), notes: notes)
    }

    private func resolveDay(_ expression: TimeExpression, now: Date, calendar: Calendar) -> Date? {
        let today = calendar.startOfDay(for: now)

        if let explicit = expression.explicitDate, let parsed = Self.parseISODate(explicit, calendar: calendar) {
            return parsed
        }
        // "10月20日" with no year: pick the upcoming occurrence. Rolling the year
        // forward is arithmetic, so it belongs here and not in the prompt.
        if let month = expression.month, let dayOfMonth = expression.dayOfMonth {
            let currentYear = calendar.component(.year, from: today)
            if let candidate = calendar.date(from: DateComponents(year: currentYear, month: month, day: dayOfMonth)) {
                if candidate >= today { return candidate }
                return calendar.date(from: DateComponents(year: currentYear + 1, month: month, day: dayOfMonth))
            }
        }
        if let weekday = expression.weekday {
            return resolveWeekday(weekday, weekOffset: expression.weekOffset ?? 0, today: today, calendar: calendar)
        }
        switch expression.day {
        case .tomorrow:
            return calendar.date(byAdding: .day, value: 1, to: today)
        case .dayAfterTomorrow:
            return calendar.date(byAdding: .day, value: 2, to: today)
        case .today, .unspecified, .none:
            return today
        case .explicitDate, .weekday:
            return today
        }
    }

    /// `weekday` is Monday = 1 ... Sunday = 7, matching how people say 周一…周日`.
    ///
    /// Anchored to the Monday of the week containing `today`. Computing a raw
    /// forward delta is subtly wrong on Sundays: from a Sunday, "下周三" is three
    /// days away, not ten, because the week already rolled over.
    func resolveWeekday(_ weekday: Int, weekOffset: Int, today: Date, calendar: Calendar) -> Date? {
        guard (1...7).contains(weekday) else { return nil }
        // Calendar.component(.weekday) is Sunday = 1; shift to Monday = 1.
        let sundayBased = calendar.component(.weekday, from: today)
        let mondayBased = (sundayBased + 5) % 7 + 1

        guard let monday = calendar.date(byAdding: .day, value: -(mondayBased - 1), to: today) else { return nil }
        let offset = max(0, weekOffset) * 7
        guard var target = calendar.date(
            byAdding: .day,
            value: (weekday - 1) + offset,
            to: monday
        ) else { return nil }

        // A day named in the current week that already passed means the next one.
        if weekOffset == 0, target < today {
            target = calendar.date(byAdding: .day, value: 7, to: target) ?? target
        }
        return target
    }

    static func parseISODate(_ text: String, calendar: Calendar) -> Date? {
        let parts = text.split(separator: "-")
        guard parts.count == 3,
              let year = Int(parts[0]),
              let month = Int(parts[1]),
              let day = Int(parts[2]),
              (1...12).contains(month),
              (1...31).contains(day)
        else { return nil }
        return calendar.date(from: DateComponents(year: year, month: month, day: day))
    }
}
