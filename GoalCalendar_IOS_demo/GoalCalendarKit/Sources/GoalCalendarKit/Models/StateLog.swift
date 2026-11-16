import Foundation

/// The three states the user can report. Kept as an enum, not free text, because
/// the scheduler branches on it deterministically.
public enum UserState: String, Codable, CaseIterable, Sendable {
    case tired
    case busy
    case uninspired

    public var label: String {
        switch self {
        case .tired: return "累"
        case .busy: return "忙"
        case .uninspired: return "没灵感"
        }
    }
}

/// An immutable record of "I felt X at time Y". We keep history rather than a
/// single mutable field so the scheduler can weight *recent* state more heavily
/// and so the user can see their own pattern later.
public struct StateLog: Codable, Identifiable, Hashable, Sendable {
    public var id: String
    public var userID: String
    public var state: UserState
    public var note: String?
    public var createdAt: Date

    public init(
        id: String = UUID().uuidString,
        userID: String,
        state: UserState,
        note: String? = nil,
        createdAt: Date = Date()
    ) {
        self.id = id
        self.userID = userID
        self.state = state
        self.note = note
        self.createdAt = createdAt
    }
}
