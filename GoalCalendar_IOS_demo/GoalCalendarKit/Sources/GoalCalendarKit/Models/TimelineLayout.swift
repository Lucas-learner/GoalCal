import Foundation

/// Where one event sits on a day timeline.
public struct TimelineBlock: Sendable, Hashable {
    public var eventID: String
    /// Already clipped to the visible window.
    public var start: Date
    public var end: Date
    /// Zero-based horizontal slot within an overlapping cluster.
    public var column: Int
    /// How many columns that cluster needs. 1 means full width.
    public var columnCount: Int

    public init(eventID: String, start: Date, end: Date, column: Int, columnCount: Int) {
        self.eventID = eventID
        self.start = start
        self.end = end
        self.column = column
        self.columnCount = columnCount
    }
}

/// Lays events out on a single day's vertical timeline.
///
/// Two pieces of arithmetic that are easy to get subtly wrong and invisible when
/// wrong: clipping to the visible window (an event starting the previous evening
/// must not push everything off-screen), and splitting overlapping events into
/// side-by-side columns instead of letting them stack on top of each other.
public enum TimelineLayout {

    public static func blocks(
        events: [CalendarEvent],
        windowStart: Date,
        windowEnd: Date
    ) -> [TimelineBlock] {
        guard windowEnd > windowStart else { return [] }

        let visible = events
            .filter { $0.endAt > windowStart && $0.startAt < windowEnd }
            .sorted { lhs, rhs in
                if lhs.startAt == rhs.startAt { return lhs.id < rhs.id }
                return lhs.startAt < rhs.startAt
            }
        guard !visible.isEmpty else { return [] }

        var result: [TimelineBlock] = []
        var cluster: [CalendarEvent] = []
        var clusterEnd: Date?

        func flushCluster() {
            guard !cluster.isEmpty else { return }

            // Greedy column packing: put each event in the first column whose
            // last event has already finished.
            var columnEnds: [Date] = []
            var assignments: [(event: CalendarEvent, column: Int)] = []

            for event in cluster {
                if let index = columnEnds.firstIndex(where: { $0 <= event.startAt }) {
                    columnEnds[index] = event.endAt
                    assignments.append((event, index))
                } else {
                    columnEnds.append(event.endAt)
                    assignments.append((event, columnEnds.count - 1))
                }
            }

            let columnCount = columnEnds.count
            for assignment in assignments {
                result.append(TimelineBlock(
                    eventID: assignment.event.id,
                    start: max(assignment.event.startAt, windowStart),
                    end: min(assignment.event.endAt, windowEnd),
                    column: assignment.column,
                    columnCount: columnCount
                ))
            }

            cluster.removeAll()
            clusterEnd = nil
        }

        for event in visible {
            if let end = clusterEnd, event.startAt >= end {
                flushCluster()
            }
            cluster.append(event)
            clusterEnd = max(clusterEnd ?? event.endAt, event.endAt)
        }
        flushCluster()

        return result
    }
}
