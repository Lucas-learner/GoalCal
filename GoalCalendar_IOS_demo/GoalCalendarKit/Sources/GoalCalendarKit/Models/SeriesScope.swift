import Foundation

/// Which occurrences of a repeating event an edit applies to.
public enum SeriesScope: String, CaseIterable, Identifiable, Sendable {
    case onlyThis = "only_this"
    case thisAndFuture = "this_and_future"
    case all

    public var id: String { rawValue }

    public var label: String {
        switch self {
        case .onlyThis: return "这一次"
        case .thisAndFuture: return "这次及以后"
        case .all: return "全部"
        }
    }
}

public enum SeriesScopeFilter {
    /// Selects the occurrences an edit should touch.
    ///
    /// Because occurrences are stored as concrete rows, "this and following" is
    /// just a date comparison — no exception table, no rule rewriting.
    public static func events(
        _ all: [CalendarEvent],
        scope: SeriesScope,
        pivot: CalendarEvent
    ) -> [CalendarEvent] {
        switch scope {
        case .onlyThis:
            return all.filter { $0.id == pivot.id }
        case .thisAndFuture:
            return all.filter { $0.startAt >= pivot.startAt }
        case .all:
            return all
        }
    }
}

public extension SeriesEditor {

    /// Shifts a set of occurrences by a whole number of days plus a number of
    /// minutes.
    ///
    /// Expressed as calendar arithmetic rather than a raw time offset so a
    /// "+1 day" edit keeps the same wall-clock time across a DST boundary.
    static func shift(
        events: [CalendarEvent],
        byDays days: Int,
        byMinutes minutes: Int,
        durationMinutes: Int? = nil,
        calendar: Calendar
    ) -> [EventTimeUpdate] {
        events.compactMap { event in
            guard let shiftedDay = calendar.date(byAdding: .day, value: days, to: event.startAt),
                  let shiftedStart = calendar.date(byAdding: .minute, value: minutes, to: shiftedDay)
            else { return nil }

            let duration = TimeInterval(
                (durationMinutes ?? Int((event.endAt.timeIntervalSince(event.startAt) / 60).rounded())) * 60
            )
            let shiftedEnd = shiftedStart.addingTimeInterval(max(duration, 15 * 60))

            // Skip only when nothing at all changed — a duration-only edit still counts.
            let startChanged = abs(shiftedStart.timeIntervalSince(event.startAt)) >= 60
            let endChanged = abs(shiftedEnd.timeIntervalSince(event.endAt)) >= 60
            guard startChanged || endChanged else { return nil }

            return EventTimeUpdate(id: event.id, startAt: shiftedStart, endAt: shiftedEnd)
        }
    }

    /// Whole-day and minute deltas between two instants, in calendar terms.
    static func deltas(from old: Date, to new: Date, calendar: Calendar) -> (days: Int, minutes: Int) {
        let oldDay = calendar.startOfDay(for: old)
        let newDay = calendar.startOfDay(for: new)
        let days = calendar.dateComponents([.day], from: oldDay, to: newDay).day ?? 0

        let oldTime = calendar.dateComponents([.hour, .minute], from: old)
        let newTime = calendar.dateComponents([.hour, .minute], from: new)
        let oldMinutes = (oldTime.hour ?? 0) * 60 + (oldTime.minute ?? 0)
        let newMinutes = (newTime.hour ?? 0) * 60 + (newTime.minute ?? 0)

        return (days, newMinutes - oldMinutes)
    }
}
