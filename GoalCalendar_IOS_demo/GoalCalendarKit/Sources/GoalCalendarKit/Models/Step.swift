import Foundation

public enum StepStatus: String, Codable, CaseIterable, Sendable {
    case pending      // not scheduled yet
    case scheduled    // has a confirmed event on the calendar
    case done         // user marked the event complete
    case skipped      // user abandoned this micro-step

    public var label: String {
        switch self {
        case .pending: return "待安排"
        case .scheduled: return "已排期"
        case .done: return "已完成"
        case .skipped: return "已跳过"
        }
    }
}

/// How often a step repeats.
///
/// The distinction that matters for planning: one-off steps ("register the
/// domain") finish once, while habit steps ("walk 30 minutes") are executed on a
/// cadence. Without this, a coach-style plan can only ever describe setup work.
public struct StepRecurrence: Codable, Hashable, Sendable {
    public enum Kind: String, Codable, CaseIterable, Sendable {
        case daily
        case weekly
    }

    public var kind: Kind
    /// Meaningful for `.weekly`; clamped to 1...7.
    public var timesPerWeek: Int?

    public init(kind: Kind, timesPerWeek: Int? = nil) {
        self.kind = kind
        self.timesPerWeek = timesPerWeek
    }

    public var label: String {
        switch kind {
        case .daily:
            return "每天"
        case .weekly:
            return "每周 \(min(max(timesPerWeek ?? 1, 1), 7)) 次"
        }
    }

    private enum CodingKeys: String, CodingKey {
        case kind
        case timesPerWeek = "times_per_week"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let raw = (try? container.decodeIfPresent(String.self, forKey: .kind)) ?? nil ?? ""
        kind = Kind(rawValue: raw) ?? .daily
        timesPerWeek = (try? container.decodeIfPresent(Int.self, forKey: .timesPerWeek)) ?? nil
    }
}

/// One micro-step of a goal. `percent` is 100 / (number of steps in the goal),
/// computed once at breakdown time and frozen, so later edits can't silently
/// change historical progress.
public struct Step: Codable, Identifiable, Hashable, Sendable {
    public var id: String
    public var goalID: String
    /// 1-based display order.
    public var seq: Int
    public var title: String
    public var detail: String?
    /// 100 / N. For a 12-step goal this is 8.333...
    public var percent: Double
    public var estimatedMinutes: Int
    /// nil for a one-off task.
    public var recurrence: StepRecurrence?
    public var status: StepStatus
    public var createdAt: Date
    public var completedAt: Date?

    public init(
        id: String = UUID().uuidString,
        goalID: String,
        seq: Int,
        title: String,
        detail: String? = nil,
        percent: Double,
        estimatedMinutes: Int = 30,
        recurrence: StepRecurrence? = nil,
        status: StepStatus = .pending,
        createdAt: Date = Date(),
        completedAt: Date? = nil
    ) {
        self.id = id
        self.goalID = goalID
        self.seq = seq
        self.title = title
        self.detail = detail
        self.percent = percent
        self.estimatedMinutes = estimatedMinutes
        self.recurrence = recurrence
        self.status = status
        self.createdAt = createdAt
        self.completedAt = completedAt
    }
}
