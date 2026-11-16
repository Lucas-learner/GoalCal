import Foundation

/// Expands "every Monday and Wednesday, 09:00–11:00, for 8 weeks" into concrete
/// events.
///
/// A recurring commitment is stored as one row per occurrence rather than as a
/// rule. The scheduler, the free-time finder and the notification planner all
/// already work on concrete intervals, so materialising up front keeps them
/// unchanged — and makes "delete just this Thursday's session" trivial.
public enum EventSeries {

    /// Hard stop so a typo like "520 weeks" cannot flood the database.
    public static let maxOccurrences = 200

    public static func expand(
        userID: String,
        title: String,
        start: Date,
        end: Date,
        weekdays: Set<Int>,
        weeks: Int,
        seriesID: String = UUID().uuidString,
        color: EventColor? = nil,
        calendar: Calendar
    ) -> [CalendarEvent] {
        guard end > start, weeks >= 1, !weekdays.isEmpty else { return [] }

        let duration = end.timeIntervalSince(start)
        let time = calendar.dateComponents([.hour, .minute], from: start)
        let startDay = calendar.startOfDay(for: start)

        // Monday of the week containing the first occurrence.
        let sundayBased = calendar.component(.weekday, from: startDay)
        let mondayBased = (sundayBased + 5) % 7 + 1
        guard let weekStart = calendar.date(byAdding: .day, value: -(mondayBased - 1), to: startDay) else {
            return []
        }

        var events: [CalendarEvent] = []

        for weekIndex in 0..<weeks {
            for weekday in weekdays.sorted() where (1...7).contains(weekday) {
                guard let day = calendar.date(
                    byAdding: .day,
                    value: (weekday - 1) + weekIndex * 7,
                    to: weekStart
                ) else { continue }

                var components = calendar.dateComponents([.year, .month, .day], from: day)
                components.hour = time.hour
                components.minute = time.minute
                components.second = 0

                guard let occurrenceStart = calendar.date(from: components) else { continue }

                // Days in the first week that fall before the chosen start are
                // not part of the series.
                guard occurrenceStart >= start else { continue }

                events.append(CalendarEvent(
                    userID: userID,
                    seriesID: seriesID,
                    color: color,
                    title: title,
                    startAt: occurrenceStart,
                    endAt: occurrenceStart.addingTimeInterval(duration),
                    status: .confirmed,
                    source: .manual
                ))

                if events.count >= maxOccurrences { return events }
            }
        }

        return events
    }
}
