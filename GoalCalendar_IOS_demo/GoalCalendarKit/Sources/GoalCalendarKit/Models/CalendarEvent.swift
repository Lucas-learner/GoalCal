import Foundation

public enum EventStatus: String, Codable, CaseIterable, Sendable {
    /// Produced by the scheduler or the LLM, shown to the user, NOT yet written
    /// to the calendar as a commitment.
    case proposed
    /// User confirmed. This is a real commitment and blocks other scheduling.
    case confirmed
    /// The user did the work.
    case done
    case cancelled
}

/// User-chosen highlight colour. Deliberately three options — a full palette
/// turns "make this important" into a colour-picking exercise.
public enum EventColor: String, Codable, CaseIterable, Sendable {
    case blue
    case red
    case yellow

    public var label: String {
        switch self {
        case .blue: return "蓝"
        case .red: return "红"
        case .yellow: return "黄"
        }
    }
}

public enum EventSource: String, Codable, CaseIterable, Sendable {
    case chat            // "add 明天下午三点开会"
    case goalBreakdown = "goal_breakdown"
    case scheduler       // "我现在很累，下一步什么时候做"
    case manual
    /// Imported from the device calendar.
    case external
}

/// A block on the user's calendar. Named `CalendarEvent` rather than `Event` to
/// avoid ambiguity with SwiftUI/Combine usages elsewhere.
///
/// `stepID` is optional: a goal-linked event advances progress, a plain event
/// (dentist appointment) is just a time block. Both occupy the same timeline and
/// both must be respected when looking for free slots.
public struct CalendarEvent: Codable, Identifiable, Hashable, Sendable {
    public var id: String
    public var userID: String
    public var stepID: String?
    public var goalID: String?
    /// Shared by every occurrence of one repeating event; nil for one-offs.
    public var seriesID: String?
    /// nil means the default colour.
    public var color: EventColor?
    public var title: String
    public var notes: String?
    public var startAt: Date
    public var endAt: Date
    public var status: EventStatus
    public var source: EventSource
    public var createdAt: Date

    public init(
        id: String = UUID().uuidString,
        userID: String,
        stepID: String? = nil,
        goalID: String? = nil,
        seriesID: String? = nil,
        color: EventColor? = nil,
        title: String,
        notes: String? = nil,
        startAt: Date,
        endAt: Date,
        status: EventStatus = .confirmed,
        source: EventSource = .manual,
        createdAt: Date = Date()
    ) {
        self.id = id
        self.userID = userID
        self.stepID = stepID
        self.goalID = goalID
        self.seriesID = seriesID
        self.color = color
        self.title = title
        self.notes = notes
        self.startAt = startAt
        self.endAt = endAt
        self.status = status
        self.source = source
        self.createdAt = createdAt
    }

    public var duration: TimeInterval { endAt.timeIntervalSince(startAt) }

    /// The span this event occupies. Handy for interval math in the scheduler.
    public var interval: DateInterval { DateInterval(start: startAt, end: endAt) }

    /// Only these statuses occupy time. `proposed` and `cancelled` do not.
    public var blocksTime: Bool { status == .confirmed || status == .done }
}
