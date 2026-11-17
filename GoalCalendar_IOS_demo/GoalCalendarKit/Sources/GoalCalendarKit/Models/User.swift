import Foundation

/// A person using the app. The work window and timezone live here because every
/// scheduling decision is made in the user's local time, never in UTC.
public struct User: Codable, Identifiable, Hashable, Sendable {
    public var id: String
    public var name: String
    /// IANA identifier, e.g. "Asia/Shanghai". Stored so scheduling survives the
    /// user travelling, and so we never depend on the device's current zone.
    public var timezoneIdentifier: String
    /// Minutes from local midnight. 9*60 = 09:00.
    public var workStartMinute: Int
    /// Exclusive end of the available window. 22*60 = 22:00.
    public var workEndMinute: Int
    public var createdAt: Date

    public init(
        id: String = UUID().uuidString,
        name: String,
        timezoneIdentifier: String = TimeZone.current.identifier,
        workStartMinute: Int = 9 * 60,
        workEndMinute: Int = 22 * 60,
        createdAt: Date = Date()
    ) {
        self.id = id
        self.name = name
        self.timezoneIdentifier = timezoneIdentifier
        self.workStartMinute = workStartMinute
        self.workEndMinute = workEndMinute
        self.createdAt = createdAt
    }

    public var timeZone: TimeZone {
        TimeZone(identifier: timezoneIdentifier) ?? .current
    }

    /// Local-time calendar configured with this user's timezone. All date math
    /// in the app goes through a calendar like this one.
    public var calendar: Calendar {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = timeZone
        cal.firstWeekday = 2   // Monday
        return cal
    }
}
