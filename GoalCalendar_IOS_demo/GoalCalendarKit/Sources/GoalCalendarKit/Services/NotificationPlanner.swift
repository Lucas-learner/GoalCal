import Foundation

/// One reminder to hand to `UNUserNotificationCenter`.
public struct NotificationPlanItem: Sendable, Hashable {
    public var identifier: String
    public var eventID: String
    public var fireDate: Date
    public var title: String
    public var body: String

    public init(identifier: String, eventID: String, fireDate: Date, title: String, body: String) {
        self.identifier = identifier
        self.eventID = eventID
        self.fireDate = fireDate
        self.title = title
        self.body = body
    }
}

/// Decides which reminders to schedule.
///
/// Pure and testable, because the interesting rules are all edge cases:
/// only confirmed events, never in the past, and **iOS silently keeps only the
/// 64 soonest pending notifications**. Anything beyond that is dropped by the
/// system without an error, so the cap has to be applied here — otherwise the
/// 65th reminder would look scheduled but never fire.
public enum NotificationPlanner {

    public static let maxPendingRequests = 64

    public static func plan(
        events: [CalendarEvent],
        now: Date,
        user: User,
        leadMinutes: Int = 5,
        horizonDays: Int = 30
    ) -> [NotificationPlanItem] {
        let format = DisplayFormat(user: user)
        let lead = TimeInterval(max(0, leadMinutes) * 60)
        let horizonEnd = now.addingTimeInterval(TimeInterval(horizonDays * 86_400))

        var items: [NotificationPlanItem] = []

        for event in events {
            // Proposed slots are not commitments yet; done/cancelled are history.
            guard event.status == .confirmed else { continue }
            guard event.startAt > now, event.startAt <= horizonEnd else { continue }

            let fireDate = event.startAt.addingTimeInterval(-lead)
            guard fireDate > now else { continue }

            let duration = Int((event.endAt.timeIntervalSince(event.startAt) / 60).rounded())
            let body = "\(leadMinutes) 分钟后开始 · \(format.time(event.startAt))–\(format.time(event.endAt)) · \(format.minutes(duration))"

            items.append(NotificationPlanItem(
                identifier: "event.\(event.id)",
                eventID: event.id,
                fireDate: fireDate,
                title: event.title,
                body: body
            ))
        }

        items.sort { $0.fireDate < $1.fireDate }
        return Array(items.prefix(maxPendingRequests))
    }
}
