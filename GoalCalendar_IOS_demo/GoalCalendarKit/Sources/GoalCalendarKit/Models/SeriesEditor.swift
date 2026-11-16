import Foundation

/// Computes the new times for every occurrence of a series.
///
/// Wall-clock semantics: the user says "move these to 14:00", so each occurrence
/// keeps its calendar day and gets 14:00 local time. Adding a fixed time offset
/// instead would drift across a DST boundary.
public enum SeriesEditor {

    public struct Change: Sendable, Hashable {
        public var eventID: String
        public var title: String
        public var oldStart: Date
        public var oldEnd: Date
        public var newStart: Date
        public var newEnd: Date

        public init(
            eventID: String,
            title: String,
            oldStart: Date,
            oldEnd: Date,
            newStart: Date,
            newEnd: Date
        ) {
            self.eventID = eventID
            self.title = title
            self.oldStart = oldStart
            self.oldEnd = oldEnd
            self.newStart = newStart
            self.newEnd = newEnd
        }
    }

    public static func reschedule(
        events: [CalendarEvent],
        hour: Int?,
        minute: Int?,
        weekday: Int?,
        durationMinutes: Int?,
        calendar: Calendar
    ) -> [Change] {
        guard !events.isEmpty else { return [] }

        var changes: [Change] = []

        for event in events.sorted(by: { $0.startAt < $1.startAt }) {
            var day = calendar.startOfDay(for: event.startAt)

            // Move within the same Monday-based week, e.g. Thursday -> Friday.
            if let weekday, (1...7).contains(weekday) {
                let current = SeriesMatcher.mondayBasedWeekday(day, calendar: calendar)
                if current != weekday,
                   let shifted = calendar.date(byAdding: .day, value: weekday - current, to: day) {
                    day = shifted
                }
            }

            let originalStart = calendar.dateComponents([.hour, .minute], from: event.startAt)
            let targetHour = hour ?? originalStart.hour ?? 0
            let targetMinute = minute ?? originalStart.minute ?? 0

            var components = calendar.dateComponents([.year, .month, .day], from: day)
            components.hour = min(max(targetHour, 0), 23)
            components.minute = min(max(targetMinute, 0), 59)
            components.second = 0
            guard let newStart = calendar.date(from: components) else { continue }

            let duration = TimeInterval(
                (durationMinutes ?? Int((event.endAt.timeIntervalSince(event.startAt) / 60).rounded())) * 60
            )
            let newEnd = newStart.addingTimeInterval(max(duration, 15 * 60))

            if isSameMinute(newStart, event.startAt) && newEnd == event.endAt { continue }

            changes.append(Change(
                eventID: event.id,
                title: event.title,
                oldStart: event.startAt,
                oldEnd: event.endAt,
                newStart: newStart,
                newEnd: newEnd
            ))
        }

        return changes
    }

    private static func isSameMinute(_ lhs: Date, _ rhs: Date) -> Bool {
        abs(lhs.timeIntervalSince(rhs)) < 60
    }
}
