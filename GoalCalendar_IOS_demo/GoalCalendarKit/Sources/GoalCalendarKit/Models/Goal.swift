import Foundation

public enum GoalStatus: String, Codable, CaseIterable, Sendable {
    case active
    case completed
    case archived
}

public struct Goal: Codable, Identifiable, Hashable, Sendable {
    public var id: String
    public var userID: String
    public var title: String
    public var detail: String?
    public var status: GoalStatus
    public var targetDate: Date?
    public var createdAt: Date
    public var completedAt: Date?

    public init(
        id: String = UUID().uuidString,
        userID: String,
        title: String,
        detail: String? = nil,
        status: GoalStatus = .active,
        targetDate: Date? = nil,
        createdAt: Date = Date(),
        completedAt: Date? = nil
    ) {
        self.id = id
        self.userID = userID
        self.title = title
        self.detail = detail
        self.status = status
        self.targetDate = targetDate
        self.createdAt = createdAt
        self.completedAt = completedAt
    }
}

/// Progress is *derived*, never stored. This struct is what the UI renders and
/// what the app speaks out loud; the single source of truth stays the `steps`
/// table so the number can never drift from reality.
///
/// Progress model B: `percent = doneSteps / totalSteps * 100`.
/// Step count is elastic (the LLM aims for 8–12), so a 12-step goal shows 8.33%
/// per step rather than a misleading fixed 10%.
public struct GoalProgress: Codable, Hashable, Sendable {
    public var goalID: String
    public var totalSteps: Int
    public var doneSteps: Int
    /// 0...100
    public var percent: Double

    public init(goalID: String, totalSteps: Int, doneSteps: Int) {
        self.goalID = goalID
        self.totalSteps = totalSteps
        self.doneSteps = doneSteps
        self.percent = totalSteps == 0
            ? 0
            : (Double(doneSteps) / Double(totalSteps)) * 100.0
    }

    public var isComplete: Bool { totalSteps > 0 && doneSteps == totalSteps }

    /// Human-facing string. 8.33 rounds to "8%", 100 stays "100%".
    public var percentLabel: String {
        String(format: "%.0f%%", percent)
    }
}
