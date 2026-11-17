import Foundation

/// Result of marking an event complete: the event itself plus everything that
/// changed downstream. Returned in one shot so the UI never renders a
/// half-updated goal.
public struct EventCompletion: Sendable {
    public var event: CalendarEvent
    public var step: Step?
    public var goal: Goal?
    public var progress: GoalProgress?
}

public enum EventEditError: Error, CustomStringConvertible {
    case invalidInterval
    case notFound

    public var description: String {
        switch self {
        case .invalidInterval: return "结束时间必须晚于开始时间"
        case .notFound: return "找不到这条日程"
        }
    }
}

/// One row of a batch time change.
public struct EventTimeUpdate: Sendable, Hashable {
    public var id: String
    public var startAt: Date
    public var endAt: Date

    public init(id: String, startAt: Date, endAt: Date) {
        self.id = id
        self.startAt = startAt
        self.endAt = endAt
    }
}

/// Outcome of replacing one step with several smaller ones.
public struct SplitResult: Sendable {
    public var goalID: String
    public var created: [Step]
    public var steps: [Step]
    public var progress: GoalProgress
}

/// Result of changing a step's status directly.
public struct StepCompletion: Sendable {
    public var step: Step?
    public var goal: Goal?
    public var progress: GoalProgress?
}

/// The only code in the app allowed to talk to SQLite. Everything above this
/// (LLM parser, scheduler, views) works with plain values, which is what makes
/// the scheduling logic unit-testable without a database or a simulator.
public final class Database: @unchecked Sendable {
    private let connection: SQLiteConnection
    private let lock = NSRecursiveLock()

    public init(path: String) throws {
        self.connection = try SQLiteConnection(path: path)
        try Schema.migrate(connection)
    }

    /// Ephemeral database, used by tests.
    public static func inMemory() throws -> Database {
        try Database(path: ":memory:")
    }

    /// On-device location: Application Support/GoalCalendar/goalcalendar.sqlite
    public static func defaultURL() throws -> URL {
        let fileManager = FileManager.default
        let directory = try fileManager
            .url(for: .applicationSupportDirectory, in: .userDomainMask, appropriateFor: nil, create: true)
            .appendingPathComponent("GoalCalendar", isDirectory: true)
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        return directory.appendingPathComponent("goalcalendar.sqlite")
    }

    private func withLock<T>(_ body: () throws -> T) rethrows -> T {
        lock.lock()
        defer { lock.unlock() }
        return try body()
    }

    private func statement(_ sql: String) throws -> SQLiteStatement {
        try connection.prepare(sql)
    }
}

// MARK: - Users

public extension Database {

    @discardableResult
    func upsertUser(_ user: User) throws -> User {
        try withLock {
            let statement = try statement("""
            INSERT INTO users (id, name, timezone_identifier, work_start_minute, work_end_minute, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                timezone_identifier = excluded.timezone_identifier,
                work_start_minute = excluded.work_start_minute,
                work_end_minute = excluded.work_end_minute
            """)
            try statement.bind([
                .text(user.id),
                .text(user.name),
                .text(user.timezoneIdentifier),
                .int(Int64(user.workStartMinute)),
                .int(Int64(user.workEndMinute)),
                .text(TimeFormat.string(from: user.createdAt))
            ])
            try statement.step()
            return user
        }
    }

    func user(id: String) throws -> User? {
        try withLock {
            let statement = try statement("""
            SELECT id, name, timezone_identifier, work_start_minute, work_end_minute, created_at
            FROM users WHERE id = ?
            """)
            try statement.bind([.text(id)])
            return try statement.step() ? mapUser(statement) : nil
        }
    }

    func allUsers() throws -> [User] {
        try withLock {
            let statement = try statement("SELECT id, name, timezone_identifier, work_start_minute, work_end_minute, created_at FROM users ORDER BY created_at")
            var users: [User] = []
            while try statement.step() {
                if let user = mapUser(statement) { users.append(user) }
            }
            return users
        }
    }

    private func mapUser(_ statement: SQLiteStatement) -> User? {
        guard let id = statement.text(0),
              let name = statement.text(1),
              let timezone = statement.text(2),
              let createdAt = statement.text(5).flatMap(TimeFormat.date(from:))
        else { return nil }
        return User(
            id: id,
            name: name,
            timezoneIdentifier: timezone,
            workStartMinute: Int(statement.int(3)),
            workEndMinute: Int(statement.int(4)),
            createdAt: createdAt
        )
    }
}

// MARK: - Goals

public extension Database {

    @discardableResult
    func insertGoal(_ goal: Goal) throws -> Goal {
        try withLock { try insertGoalUnsafe(goal) }
    }

    private func insertGoalUnsafe(_ goal: Goal) throws -> Goal {
        let statement = try statement("""
        INSERT INTO goals (id, user_id, title, detail, status, target_date, created_at, completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """)
        try statement.bind([
            .text(goal.id),
            .text(goal.userID),
            .text(goal.title),
            goal.detail.map { .text($0) } ?? .null,
            .text(goal.status.rawValue),
            goal.targetDate.map { .text(TimeFormat.string(from: $0)) } ?? .null,
            .text(TimeFormat.string(from: goal.createdAt)),
            goal.completedAt.map { .text(TimeFormat.string(from: $0)) } ?? .null
        ])
        try statement.step()
        return goal
    }

    /// Creates a goal and its whole breakdown in one transaction.
    ///
    /// A goal with no steps is a dead end in this app — it can never advance, so
    /// it must never be observable. If any step fails to insert, the goal goes
    /// with it.
    @discardableResult
    func createGoal(_ goal: Goal, steps: [Step]) throws -> (goal: Goal, steps: [Step]) {
        try withLock {
            try connection.transaction {
                let insertedGoal = try insertGoalUnsafe(goal)
                let insertedSteps = steps.isEmpty ? [] : try insertStepsUnsafe(steps)
                return (insertedGoal, insertedSteps)
            }
        }
    }

    func goal(id: String) throws -> Goal? {
        try withLock {
            let statement = try statement("""
            SELECT id, user_id, title, detail, status, target_date, created_at, completed_at
            FROM goals WHERE id = ?
            """)
            try statement.bind([.text(id)])
            return try statement.step() ? mapGoal(statement) : nil
        }
    }

    func goals(userID: String, status: GoalStatus? = nil) throws -> [Goal] {
        try withLock {
            let sql: String
            if status != nil {
                sql = """
                SELECT id, user_id, title, detail, status, target_date, created_at, completed_at
                FROM goals WHERE user_id = ? AND status = ? ORDER BY created_at
                """
            } else {
                sql = """
                SELECT id, user_id, title, detail, status, target_date, created_at, completed_at
                FROM goals WHERE user_id = ? ORDER BY created_at
                """
            }
            let statement = try statement(sql)
            var values: [SQLValue] = [.text(userID)]
            if let status { values.append(.text(status.rawValue)) }
            try statement.bind(values)

            var goals: [Goal] = []
            while try statement.step() {
                if let goal = mapGoal(statement) { goals.append(goal) }
            }
            return goals
        }
    }

    func updateGoalStatus(id: String, status: GoalStatus, completedAt: Date? = nil) throws {
        try withLock {
            let statement = try statement("UPDATE goals SET status = ?, completed_at = ? WHERE id = ?")
            try statement.bind([
                .text(status.rawValue),
                completedAt.map { .text(TimeFormat.string(from: $0)) } ?? .null,
                .text(id)
            ])
            try statement.step()
        }
    }

    @discardableResult
    func deleteGoal(id: String) throws -> Bool {
        try withLock {
            let statement = try statement("DELETE FROM goals WHERE id = ?")
            try statement.bind([.text(id)])
            try statement.step()
            return connection.changes > 0
        }
    }

    private func mapGoal(_ statement: SQLiteStatement) -> Goal? {
        guard let id = statement.text(0),
              let userID = statement.text(1),
              let title = statement.text(2),
              let createdAt = statement.text(6).flatMap(TimeFormat.date(from:))
        else { return nil }
        return Goal(
            id: id,
            userID: userID,
            title: title,
            detail: statement.text(3),
            status: GoalStatus(rawValue: statement.text(4) ?? "") ?? .active,
            targetDate: statement.text(5).flatMap(TimeFormat.date(from:)),
            createdAt: createdAt,
            completedAt: statement.text(7).flatMap(TimeFormat.date(from:))
        )
    }
}

// MARK: - Steps

public extension Database {

    /// Inserts a whole breakdown atomically: a goal is either fully split into
    /// micro-steps or not split at all. `percent` is computed here (100 / N)
    /// rather than trusted from the caller, so progress can never fail to sum.
    @discardableResult
    func insertSteps(_ steps: [Step]) throws -> [Step] {
        guard !steps.isEmpty else { return [] }
        return try withLock {
            try connection.transaction {
                try insertStepsUnsafe(steps)
            }
        }
    }

    /// Assumes the caller already opened a transaction.
    private func insertStepsUnsafe(_ steps: [Step]) throws -> [Step] {
        let percent = 100.0 / Double(steps.count)
        let statement = try statement("""
        INSERT INTO steps (id, goal_id, seq, title, detail, percent, estimated_minutes, recurrence_kind, recurrence_times_per_week, status, created_at, completed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """)
        for step in steps {
            try statement.bind([
                .text(step.id),
                .text(step.goalID),
                .int(Int64(step.seq)),
                .text(step.title),
                step.detail.map { .text($0) } ?? .null,
                .double(percent),
                .int(Int64(step.estimatedMinutes)),
                step.recurrence.map { .text($0.kind.rawValue) } ?? .null,
                step.recurrence?.timesPerWeek.map { .int(Int64($0)) } ?? .null,
                .text(step.status.rawValue),
                .text(TimeFormat.string(from: step.createdAt)),
                step.completedAt.map { .text(TimeFormat.string(from: $0)) } ?? .null
            ])
            try statement.step()
        }
        return steps.map { step in
            var copy = step
            copy.percent = percent
            return copy
        }
    }

    func steps(goalID: String) throws -> [Step] {
        try withLock {
            let statement = try statement("""
            SELECT id, goal_id, seq, title, detail, percent, estimated_minutes, status, created_at, completed_at, recurrence_kind, recurrence_times_per_week
            FROM steps WHERE goal_id = ? ORDER BY seq
            """)
            try statement.bind([.text(goalID)])
            var steps: [Step] = []
            while try statement.step() {
                if let step = mapStep(statement) { steps.append(step) }
            }
            return steps
        }
    }

    func step(id: String) throws -> Step? {
        try withLock {
            let statement = try statement("""
            SELECT id, goal_id, seq, title, detail, percent, estimated_minutes, status, created_at, completed_at, recurrence_kind, recurrence_times_per_week
            FROM steps WHERE id = ?
            """)
            try statement.bind([.text(id)])
            return try statement.step() ? mapStep(statement) : nil
        }
    }

    /// Every actionable micro-step across all of one user's active goals,
    /// ordered by goal age then step order. This is the scheduler's work queue.
    func actionableSteps(userID: String) throws -> [Step] {
        try withLock {
            let statement = try statement("""
            SELECT s.id, s.goal_id, s.seq, s.title, s.detail, s.percent, s.estimated_minutes, s.status, s.created_at, s.completed_at, s.recurrence_kind, s.recurrence_times_per_week
            FROM steps s
            JOIN goals g ON g.id = s.goal_id
            WHERE g.user_id = ? AND g.status = 'active' AND s.status IN ('pending', 'scheduled')
            ORDER BY g.created_at, s.seq
            """)
            try statement.bind([.text(userID)])
            var steps: [Step] = []
            while try statement.step() {
                if let step = mapStep(statement) { steps.append(step) }
            }
            return steps
        }
    }

    /// Replaces one step with several smaller ones, keeping the goal intact.
    ///
    /// Used when the user says a step was too hard. The replacement stays inside
    /// the same goal, so progress and scheduling keep pointing at one goal rather
    /// than spawning a second one.
    @discardableResult
    func splitStep(id: String, into titles: [String], at date: Date = Date()) throws -> SplitResult? {
        guard !titles.isEmpty else { return nil }

        return try withLock {
            try connection.transaction {
                guard let original = try step(id: id) else { return nil }
                let goalID = original.goalID
                let existing = try steps(goalID: goalID)
                guard let originalIndex = existing.firstIndex(where: { $0.id == id }) else { return nil }

                // Park every seq far away first: renumbering in place would trip
                // the UNIQUE(goal_id, seq) constraint mid-flight.
                let park = try statement("UPDATE steps SET seq = seq + 1000 WHERE goal_id = ?")
                try park.bind([.text(goalID)])
                try park.step()

                let remove = try statement("DELETE FROM steps WHERE id = ?")
                try remove.bind([.text(id)])
                try remove.step()

                let minutes = max(15, original.estimatedMinutes / max(1, titles.count))
                let replacements = titles.map {
                    Step(
                        goalID: goalID,
                        seq: 0,
                        title: $0,
                        detail: nil,
                        percent: 0,
                        estimatedMinutes: minutes,
                        createdAt: date
                    )
                }

                var ordered = existing.filter { $0.id != id }
                ordered.insert(contentsOf: replacements, at: min(originalIndex, ordered.count))

                let percent = 100.0 / Double(ordered.count)
                let renumber = try statement("UPDATE steps SET seq = ?, percent = ? WHERE id = ?")
                let insert = try statement("""
                INSERT INTO steps (id, goal_id, seq, title, detail, percent, estimated_minutes, recurrence_kind, recurrence_times_per_week, status, created_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
                """)
                let newIDs = Set(replacements.map(\.id))

                for (index, step) in ordered.enumerated() {
                    let seq = Int64(index + 1)
                    if newIDs.contains(step.id) {
                        try insert.bind([
                            .text(step.id),
                            .text(goalID),
                            .int(seq),
                            .text(step.title),
                            .null,
                            .double(percent),
                            .int(Int64(step.estimatedMinutes)),
                            .text(StepStatus.pending.rawValue),
                            .text(TimeFormat.string(from: date)),
                            .null
                        ])
                        try insert.step()
                    } else {
                        try renumber.bind([.int(seq), .double(percent), .text(step.id)])
                        try renumber.step()
                    }
                }

                return SplitResult(
                    goalID: goalID,
                    created: replacements,
                    steps: try steps(goalID: goalID),
                    progress: try progress(goalID: goalID)
                )
            }
        }
    }

    func updateStepStatus(id: String, status: StepStatus, completedAt: Date? = nil) throws {
        try withLock {
            let statement = try statement("UPDATE steps SET status = ?, completed_at = ? WHERE id = ?")
            try statement.bind([
                .text(status.rawValue),
                completedAt.map { .text(TimeFormat.string(from: $0)) } ?? .null,
                .text(id)
            ])
            try statement.step()
        }
    }

    private func mapStep(_ statement: SQLiteStatement) -> Step? {
        guard let id = statement.text(0),
              let goalID = statement.text(1),
              let title = statement.text(3),
              let createdAt = statement.text(8).flatMap(TimeFormat.date(from:))
        else { return nil }
        return Step(
            id: id,
            goalID: goalID,
            seq: Int(statement.int(2)),
            title: title,
            detail: statement.text(4),
            percent: statement.double(5),
            estimatedMinutes: Int(statement.int(6)),
            recurrence: {
                guard let kindRaw = statement.text(10) else { return nil }
                let times = statement.isNull(11) ? nil : Int(statement.int(11))
                return StepRecurrence(
                    kind: StepRecurrence.Kind(rawValue: kindRaw) ?? .daily,
                    timesPerWeek: times
                )
            }(),
            status: StepStatus(rawValue: statement.text(7) ?? "") ?? .pending,
            createdAt: createdAt,
            completedAt: statement.text(9).flatMap(TimeFormat.date(from:))
        )
    }
}

// MARK: - Progress

public extension Database {

    /// Reads the derived view. Never recompute progress anywhere else.
    func progress(goalID: String) throws -> GoalProgress {
        try withLock {
            let statement = try statement("""
            SELECT goal_id, total_steps, done_steps, percent FROM goal_progress WHERE goal_id = ?
            """)
            try statement.bind([.text(goalID)])
            guard try statement.step() else {
                return GoalProgress(goalID: goalID, totalSteps: 0, doneSteps: 0)
            }
            return GoalProgress(
                goalID: statement.text(0) ?? goalID,
                totalSteps: Int(statement.int(1)),
                doneSteps: Int(statement.int(2))
            )
        }
    }
}

// MARK: - Events

public extension Database {

    @discardableResult
    func insertEvent(_ event: CalendarEvent) throws -> CalendarEvent {
        try withLock {
            let statement = try statement("""
            INSERT INTO events (id, user_id, step_id, goal_id, title, notes, start_at, end_at, status, source, created_at, series_id, color)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """)
            try statement.bind([
                .text(event.id),
                .text(event.userID),
                event.stepID.map { .text($0) } ?? .null,
                event.goalID.map { .text($0) } ?? .null,
                .text(event.title),
                event.notes.map { .text($0) } ?? .null,
                .text(TimeFormat.string(from: event.startAt)),
                .text(TimeFormat.string(from: event.endAt)),
                .text(event.status.rawValue),
                .text(event.source.rawValue),
                .text(TimeFormat.string(from: event.createdAt)),
                event.seriesID.map { .text($0) } ?? .null,
                event.color.map { .text($0.rawValue) } ?? .null
            ])
            try statement.step()
            return event
        }
    }

    /// Inserts a batch of events atomically. Used for recurring series: a
    /// half-created series would look like the user's calendar lost days.
    @discardableResult
    func insertEvents(_ events: [CalendarEvent]) throws -> [CalendarEvent] {
        guard !events.isEmpty else { return [] }
        return try withLock {
            try connection.transaction {
                let insert = try statement("""
                INSERT INTO events (id, user_id, step_id, goal_id, title, notes, start_at, end_at, status, source, created_at, series_id, color)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """)
                for event in events {
                    try insert.bind([
                        .text(event.id),
                        .text(event.userID),
                        event.stepID.map { .text($0) } ?? .null,
                        event.goalID.map { .text($0) } ?? .null,
                        .text(event.title),
                        event.notes.map { .text($0) } ?? .null,
                        .text(TimeFormat.string(from: event.startAt)),
                        .text(TimeFormat.string(from: event.endAt)),
                        .text(event.status.rawValue),
                        .text(event.source.rawValue),
                        .text(TimeFormat.string(from: event.createdAt)),
                        event.seriesID.map { .text($0) } ?? .null,
                        event.color.map { .text($0.rawValue) } ?? .null
                    ])
                    try insert.step()
                }
                return events
            }
        }
    }

    func event(id: String) throws -> CalendarEvent? {
        try withLock {
            let statement = try statement("""
            SELECT id, user_id, step_id, goal_id, title, notes, start_at, end_at, status, source, created_at, series_id, color
            FROM events WHERE id = ?
            """)
            try statement.bind([.text(id)])
            return try statement.step() ? mapEvent(statement) : nil
        }
    }

    /// Events in a window, for rendering the calendar.
    func events(userID: String, from: Date, to: Date, includingProposed: Bool = false) throws -> [CalendarEvent] {
        try withLock {
            let statuses = includingProposed
                ? "('proposed', 'confirmed', 'done')"
                : "('confirmed', 'done')"
            let statement = try statement("""
            SELECT id, user_id, step_id, goal_id, title, notes, start_at, end_at, status, source, created_at, series_id, color
            FROM events
            WHERE user_id = ? AND start_at < ? AND end_at > ? AND status IN \(statuses)
            ORDER BY start_at
            """)
            try statement.bind([
                .text(userID),
                .text(TimeFormat.string(from: to)),
                .text(TimeFormat.string(from: from))
            ])
            var events: [CalendarEvent] = []
            while try statement.step() {
                if let event = mapEvent(statement) { events.append(event) }
            }
            return events
        }
    }

    /// Only what actually occupies time. The scheduler uses this to find holes;
    /// `proposed` blocks deliberately do not count, otherwise a rejected
    /// suggestion would poison future scheduling.
    func blockingEvents(userID: String, from: Date, to: Date) throws -> [CalendarEvent] {
        try events(userID: userID, from: from, to: to, includingProposed: false)
    }

    func updateEventStatus(id: String, status: EventStatus) throws {
        try withLock {
            let statement = try statement("UPDATE events SET status = ? WHERE id = ?")
            try statement.bind([.text(status.rawValue), .text(id)])
            try statement.step()
        }
    }

    @discardableResult
    func deleteEvent(id: String) throws -> Bool {
        try withLock {
            let statement = try statement("DELETE FROM events WHERE id = ?")
            try statement.bind([.text(id)])
            try statement.step()
            return connection.changes > 0
        }
    }

    /// The single atomic operation behind requirement #4: completing an event
    /// advances its step, and a fully completed step set closes the goal.
    /// One transaction so the UI can never observe a step marked done while the
    /// goal still shows the old percentage.
    @discardableResult
    func completeEvent(id: String, at date: Date = Date()) throws -> EventCompletion? {
        try withLock {
            try connection.transaction {
                guard let event = try event(id: id) else { return nil }

                let markEvent = try statement("UPDATE events SET status = 'done' WHERE id = ?")
                try markEvent.bind([.text(event.id)])
                try markEvent.step()

                var updatedEvent = event
                updatedEvent.status = .done

                guard let stepID = event.stepID else {
                    return EventCompletion(event: updatedEvent, step: nil, goal: nil, progress: nil)
                }

                let markStep = try statement("UPDATE steps SET status = 'done', completed_at = ? WHERE id = ?")
                try markStep.bind([.text(TimeFormat.string(from: date)), .text(stepID)])
                try markStep.step()

                let updatedStep = try step(id: stepID)
                guard let goalID = updatedStep?.goalID else {
                    return EventCompletion(event: updatedEvent, step: updatedStep, goal: nil, progress: nil)
                }

                try syncGoalStatus(goalID: goalID, at: date)

                return EventCompletion(
                    event: updatedEvent,
                    step: try step(id: stepID),
                    goal: try goal(id: goalID),
                    progress: try progress(goalID: goalID)
                )
            }
        }
    }

    /// Marks a step directly, without going through a calendar event.
    ///
    /// Users legitimately finish work they never scheduled; the progress model
    /// must not require an event to exist for a step to count.
    @discardableResult
    func setStepStatus(id: String, status: StepStatus, at date: Date = Date()) throws -> StepCompletion {
        try withLock {
            try connection.transaction {
                let update = try statement("UPDATE steps SET status = ?, completed_at = ? WHERE id = ?")
                try update.bind([
                    .text(status.rawValue),
                    status == .done ? .text(TimeFormat.string(from: date)) : .null,
                    .text(id)
                ])
                try update.step()

                guard let updated = try step(id: id) else {
                    return StepCompletion(step: nil, goal: nil, progress: nil)
                }

                try syncGoalStatus(goalID: updated.goalID, at: date)

                return StepCompletion(
                    step: try step(id: id),
                    goal: try goal(id: updated.goalID),
                    progress: try progress(goalID: updated.goalID)
                )
            }
        }
    }

    /// Keeps the goal's status consistent with its steps.
    ///
    /// Closing on the last step is the point of the whole app. Re-opening when a
    /// step is un-done matters just as much: progress that cannot go backwards
    /// makes a mistaken tap permanent.
    private func syncGoalStatus(goalID: String, at date: Date) throws {
        let counts = try statement("""
        SELECT COUNT(*) AS total, SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done
        FROM steps WHERE goal_id = ?
        """)
        try counts.bind([.text(goalID)])

        var total = 0
        var done = 0
        if try counts.step() {
            total = Int(counts.int(0))
            done = Int(counts.int(1))
        }

        if total > 0 && total == done {
            let close = try statement("UPDATE goals SET status = 'completed', completed_at = ? WHERE id = ? AND status = 'active'")
            try close.bind([.text(TimeFormat.string(from: date)), .text(goalID)])
            try close.step()
        } else {
            let reopen = try statement("UPDATE goals SET status = 'active', completed_at = NULL WHERE id = ? AND status = 'completed'")
            try reopen.bind([.text(goalID)])
            try reopen.step()
        }
    }

    /// Applies a batch of time changes in one transaction, so a rescheduled
    /// series can never end up half-moved.
    func updateEventTimes(_ updates: [EventTimeUpdate]) throws {
        guard !updates.isEmpty else { return }
        try withLock {
            try connection.transaction {
                let update = try statement("UPDATE events SET start_at = ?, end_at = ? WHERE id = ?")
                for item in updates {
                    try update.bind([
                        .text(TimeFormat.string(from: item.startAt)),
                        .text(TimeFormat.string(from: item.endAt)),
                        .text(item.id)
                    ])
                    try update.step()
                }
            }
        }
    }

    /// Deletes a specific set of events in one transaction.
    @discardableResult
    func deleteEvents(ids: [String]) throws -> Int {
        guard !ids.isEmpty else { return 0 }
        return try withLock {
            try connection.transaction {
                let delete = try statement("DELETE FROM events WHERE id = ?")
                var removed = 0
                for id in ids {
                    try delete.bind([.text(id)])
                    try delete.step()
                    removed += Int(connection.changes)
                }
                return removed
            }
        }
    }

    /// Removes every occurrence of a repeating event.
    @discardableResult
    func deleteSeries(id: String) throws -> Int {
        try withLock {
            let delete = try statement("DELETE FROM events WHERE series_id = ?")
            try delete.bind([.text(id)])
            try delete.step()
            return Int(connection.changes)
        }
    }

    func events(seriesID: String) throws -> [CalendarEvent] {
        try withLock {
            let select = try statement("""
            SELECT id, user_id, step_id, goal_id, title, notes, start_at, end_at, status, source, created_at, series_id, color
            FROM events WHERE series_id = ? ORDER BY start_at
            """)
            try select.bind([.text(seriesID)])
            var results: [CalendarEvent] = []
            while try select.step() {
                if let event = mapEvent(select) { results.append(event) }
            }
            return results
        }
    }

    /// Recolours one event. Used by the swatches in the detail sheet.
    func updateEventColor(id: String, color: EventColor?) throws {
        try withLock {
            let update = try statement("UPDATE events SET color = ? WHERE id = ?")
            try update.bind([
                color.map { .text($0.rawValue) } ?? .null,
                .text(id)
            ])
            try update.step()
        }
    }

    /// Moves an event without recreating it.
    ///
    /// Recreating is not an acceptable alternative: a new row would lose
    /// `step_id`, silently detaching the calendar entry from the goal step it
    /// advances. Rescheduling must preserve that link.
    @discardableResult
    func updateEvent(id: String, startAt: Date, endAt: Date) throws -> CalendarEvent? {
        guard endAt > startAt else { throw EventEditError.invalidInterval }
        return try withLock {
            let update = try statement("UPDATE events SET start_at = ?, end_at = ? WHERE id = ?")
            try update.bind([
                .text(TimeFormat.string(from: startAt)),
                .text(TimeFormat.string(from: endAt)),
                .text(id)
            ])
            try update.step()
            return try event(id: id)
        }
    }

    /// Events that actually occupy time inside `[start, end)`, optionally
    /// ignoring one event (the one being moved).
    func eventsOverlapping(
        userID: String,
        start: Date,
        end: Date,
        excluding eventID: String? = nil
    ) throws -> [CalendarEvent] {
        let overlapping = try events(userID: userID, from: start, to: end)
        guard let eventID else { return overlapping }
        return overlapping.filter { $0.id != eventID }
    }

    private func mapEvent(_ statement: SQLiteStatement) -> CalendarEvent? {
        guard let id = statement.text(0),
              let userID = statement.text(1),
              let title = statement.text(4),
              let startText = statement.text(6),
              let endText = statement.text(7),
              let startAt = TimeFormat.date(from: startText),
              let endAt = TimeFormat.date(from: endText),
              let createdAt = statement.text(10).flatMap(TimeFormat.date(from:))
        else { return nil }
        return CalendarEvent(
            id: id,
            userID: userID,
            stepID: statement.text(2),
            goalID: statement.text(3),
            seriesID: statement.text(11),
            color: statement.text(12).flatMap(EventColor.init(rawValue:)),
            title: title,
            notes: statement.text(5),
            startAt: startAt,
            endAt: endAt,
            status: EventStatus(rawValue: statement.text(8) ?? "") ?? .confirmed,
            source: EventSource(rawValue: statement.text(9) ?? "") ?? .manual,
            createdAt: createdAt
        )
    }
}

// MARK: - State logs

public extension Database {

    @discardableResult
    func appendStateLog(_ log: StateLog) throws -> StateLog {
        try withLock {
            let statement = try statement("""
            INSERT INTO state_logs (id, user_id, state, note, created_at) VALUES (?, ?, ?, ?, ?)
            """)
            try statement.bind([
                .text(log.id),
                .text(log.userID),
                .text(log.state.rawValue),
                log.note.map { .text($0) } ?? .null,
                .text(TimeFormat.string(from: log.createdAt))
            ])
            try statement.step()
            return log
        }
    }

    func latestStateLog(userID: String) throws -> StateLog? {
        try recentStateLogs(userID: userID, limit: 1).first
    }

    func recentStateLogs(userID: String, limit: Int = 20) throws -> [StateLog] {
        try withLock {
            let statement = try statement("""
            SELECT id, user_id, state, note, created_at
            FROM state_logs WHERE user_id = ?
            ORDER BY created_at DESC LIMIT ?
            """)
            try statement.bind([.text(userID), .int(Int64(limit))])
            var logs: [StateLog] = []
            while try statement.step() {
                guard let id = statement.text(0),
                      let stateRaw = statement.text(2),
                      let createdAt = statement.text(4).flatMap(TimeFormat.date(from:))
                else { continue }
                logs.append(StateLog(
                    id: id,
                    userID: statement.text(1) ?? userID,
                    state: UserState(rawValue: stateRaw) ?? .tired,
                    note: statement.text(3),
                    createdAt: createdAt
                ))
            }
            return logs
        }
    }
}
