import Foundation

/// A breakdown that has been generated but not yet written anywhere.
///
/// The split from `PlannedGoal` is what makes the confirmation gate possible:
/// generating a 30-second, 12-step breakdown and *committing* it are two separate
/// user-visible steps. Nothing touches the database until the user says yes.
public struct GoalDraft: Sendable, Hashable {
    public var title: String
    public var steps: [BreakdownStep]
    /// Non-empty when the model needs more information before planning.
    public var questions: [String]
    public var usedLLM: Bool
    public var warnings: [String]
    public var rawOutput: String?

    public init(
        title: String,
        steps: [BreakdownStep],
        questions: [String] = [],
        usedLLM: Bool,
        warnings: [String] = [],
        rawOutput: String? = nil
    ) {
        self.title = title
        self.steps = steps
        self.questions = questions
        self.usedLLM = usedLLM
        self.warnings = warnings
        self.rawOutput = rawOutput
    }

    public var needsClarification: Bool { !questions.isEmpty }

    public var percentPerStep: Double {
        steps.isEmpty ? 0 : 100.0 / Double(steps.count)
    }
}

public struct PlannedGoal: Sendable {
    public var goal: Goal
    public var steps: [Step]
    public var progress: GoalProgress
    public var usedLLM: Bool
    public var warnings: [String]
    /// Model output kept for prompt tuning.
    public var rawOutput: String?

    public init(
        goal: Goal,
        steps: [Step],
        progress: GoalProgress,
        usedLLM: Bool,
        warnings: [String] = [],
        rawOutput: String? = nil
    ) {
        self.goal = goal
        self.steps = steps
        self.progress = progress
        self.usedLLM = usedLLM
        self.warnings = warnings
        self.rawOutput = rawOutput
    }
}

public enum PlannerError: Error, CustomStringConvertible {
    case emptyGoal
    case noSteps

    public var description: String {
        switch self {
        case .emptyGoal: return "目标描述为空"
        case .noSteps: return "没有生成任何可执行的步骤"
        }
    }
}

/// Turns a goal sentence into stored steps.
///
/// The only type that knows both the model and SQLite, which keeps
/// `GoalBreakdownParser` testable without a database.
public struct GoalPlanner: Sendable {

    private let database: Database
    private let parser: GoalBreakdownParser

    public init(database: Database, parser: GoalBreakdownParser) {
        self.database = database
        self.parser = parser
    }

    /// Generates the breakdown only. No writes.
    public func draft(_ goalText: String, context: [String] = []) async -> GoalDraft {
        let text = goalText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            return GoalDraft(title: text, steps: [], usedLLM: false, warnings: ["目标描述为空"])
        }
        let breakdown = await parser.breakdown(text, context: context)
        return GoalDraft(
            title: text,
            steps: breakdown.steps,
            questions: breakdown.questions,
            usedLLM: breakdown.usedLLM,
            warnings: breakdown.warnings,
            rawOutput: breakdown.rawOutput
        )
    }

    /// Writes a draft the user has approved.
    @discardableResult
    public func commit(_ draft: GoalDraft, user: User, now: Date = Date()) throws -> PlannedGoal {
        guard !draft.title.isEmpty else { throw PlannerError.emptyGoal }
        guard !draft.steps.isEmpty else { throw PlannerError.noSteps }

        let goal = Goal(userID: user.id, title: draft.title, createdAt: now)
        // Ordering comes from array position, not from the model's own numbering.
        let steps = draft.steps.enumerated().map { index, item in
            Step(
                goalID: goal.id,
                seq: index + 1,
                title: item.title,
                detail: item.detail,
                percent: 0,                      // recomputed as 100/N on insert
                estimatedMinutes: item.estimatedMinutes,
                recurrence: item.recurrence,
                createdAt: now
            )
        }

        let created = try database.createGoal(goal, steps: steps)
        let progress = try database.progress(goalID: created.goal.id)

        return PlannedGoal(
            goal: created.goal,
            steps: created.steps,
            progress: progress,
            usedLLM: draft.usedLLM,
            warnings: draft.warnings,
            rawOutput: draft.rawOutput
        )
    }

    /// Convenience for one-shot callers (the demo): draft then immediately commit.
    public func plan(_ goalText: String, user: User, now: Date = Date()) async throws -> PlannedGoal {
        let draft = await draft(goalText)
        guard !draft.steps.isEmpty else { throw PlannerError.noSteps }
        return try commit(draft, user: user, now: now)
    }
}
