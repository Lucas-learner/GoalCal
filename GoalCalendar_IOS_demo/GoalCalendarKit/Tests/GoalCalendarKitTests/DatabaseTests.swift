import XCTest
@testable import GoalCalendarKit

final class DatabaseTests: XCTestCase {

    private var database: Database!
    private var user: User!

    override func setUpWithError() throws {
        database = try Database.inMemory()
        user = try database.upsertUser(
            User(name: "测试用户", timezoneIdentifier: "Asia/Shanghai")
        )
    }

    private func makeGoal(stepCount: Int) throws -> (Goal, [Step]) {
        let goal = try database.insertGoal(Goal(userID: user.id, title: "学会 SwiftUI"))
        let steps = try database.insertSteps((1...stepCount).map { index in
            Step(goalID: goal.id, seq: index, title: "第 \(index) 步", percent: 0)
        })
        return (goal, steps)
    }

    // MARK: - Progress model B

    /// The chosen model: step count is elastic, each step is exactly 100/N.
    func testInsertStepsComputesPercentAsHundredOverN() throws {
        let (_, twelve) = try makeGoal(stepCount: 12)
        XCTAssertEqual(twelve.count, 12)
        for step in twelve {
            XCTAssertEqual(step.percent, 100.0 / 12.0, accuracy: 0.0001)
        }

        let (_, five) = try makeGoal(stepCount: 5)
        for step in five {
            XCTAssertEqual(step.percent, 20.0, accuracy: 0.0001)
        }
    }

    func testProgressSumsToOneHundredPercent() throws {
        let (goal, steps) = try makeGoal(stepCount: 12)
        XCTAssertEqual(try database.progress(goalID: goal.id).percent, 0, accuracy: 0.0001)

        for step in steps {
            try database.updateStepStatus(id: step.id, status: .done, completedAt: Date())
        }

        let progress = try database.progress(goalID: goal.id)
        XCTAssertEqual(progress.doneSteps, 12)
        XCTAssertEqual(progress.totalSteps, 12)
        XCTAssertEqual(progress.percent, 100.0, accuracy: 0.0001)
        XCTAssertTrue(progress.isComplete)
    }

    func testPartialProgressRoundsForDisplay() throws {
        let (goal, steps) = try makeGoal(stepCount: 12)
        try database.updateStepStatus(id: steps[0].id, status: .done, completedAt: Date())

        let progress = try database.progress(goalID: goal.id)
        XCTAssertEqual(progress.percent, 100.0 / 12.0, accuracy: 0.0001)
        XCTAssertEqual(progress.percentLabel, "8%")
    }

    // MARK: - Requirement #4: completion advances the goal

    func testCompletingEventAdvancesStepAndProgress() throws {
        let (goal, steps) = try makeGoal(stepCount: 10)
        let start = Date(timeIntervalSince1970: 1_800_000_000)
        let event = try database.insertEvent(CalendarEvent(
            userID: user.id,
            stepID: steps[0].id,
            goalID: goal.id,
            title: steps[0].title,
            startAt: start,
            endAt: start.addingTimeInterval(1800),
            status: .confirmed,
            source: .scheduler
        ))

        let completion = try XCTUnwrap(try database.completeEvent(id: event.id))
        XCTAssertEqual(completion.event.status, .done)
        XCTAssertEqual(completion.step?.status, .done)
        XCTAssertEqual(completion.progress?.doneSteps, 1)
        XCTAssertEqual(completion.progress?.percent ?? 0, 10.0, accuracy: 0.0001)
        XCTAssertEqual(completion.goal?.status, .active)
    }

    func testCompletingFinalStepClosesGoal() throws {
        let (goal, steps) = try makeGoal(stepCount: 2)
        let start = Date(timeIntervalSince1970: 1_800_000_000)

        for step in steps {
            let event = try database.insertEvent(CalendarEvent(
                userID: user.id, stepID: step.id, goalID: goal.id,
                title: step.title, startAt: start, endAt: start.addingTimeInterval(1800)
            ))
            _ = try database.completeEvent(id: event.id)
        }

        let closed = try XCTUnwrap(try database.goal(id: goal.id))
        XCTAssertEqual(closed.status, .completed)
        XCTAssertNotNil(closed.completedAt)
        XCTAssertEqual(try database.progress(goalID: goal.id).percent, 100.0, accuracy: 0.0001)
    }

    // MARK: - Scheduling support

    /// Proposed blocks must not occupy time; otherwise a rejected suggestion
    /// would permanently poison the free-slot search.
    func testBlockingEventsExcludeProposed() throws {
        let start = Date(timeIntervalSince1970: 1_800_000_000)
        _ = try database.insertEvent(CalendarEvent(
            userID: user.id, title: "confirmed", startAt: start,
            endAt: start.addingTimeInterval(1800), status: .confirmed
        ))
        _ = try database.insertEvent(CalendarEvent(
            userID: user.id, title: "proposed",
            startAt: start.addingTimeInterval(3600),
            endAt: start.addingTimeInterval(5400), status: .proposed
        ))

        let blocking = try database.blockingEvents(
            userID: user.id,
            from: start.addingTimeInterval(-3600),
            to: start.addingTimeInterval(7200)
        )
        XCTAssertEqual(blocking.count, 1)
        XCTAssertEqual(blocking[0].title, "confirmed")

        let all = try database.events(
            userID: user.id,
            from: start.addingTimeInterval(-3600),
            to: start.addingTimeInterval(7200),
            includingProposed: true
        )
        XCTAssertEqual(all.count, 2)
    }

    func testWindowQueryIsHalfOpenOnBoundaries() throws {
        let start = Date(timeIntervalSince1970: 1_800_000_000)
        _ = try database.insertEvent(CalendarEvent(
            userID: user.id, title: "inside", startAt: start,
            endAt: start.addingTimeInterval(1800)
        ))

        // Event starts exactly at `from` -> included.
        XCTAssertEqual(try database.blockingEvents(
            userID: user.id, from: start, to: start.addingTimeInterval(3600)
        ).count, 1)

        // Event ends exactly at `from` -> excluded (no overlap).
        XCTAssertEqual(try database.blockingEvents(
            userID: user.id, from: start.addingTimeInterval(1800), to: start.addingTimeInterval(3600)
        ).count, 0)
    }

    func testActionableStepsOnlyIncludesActiveGoals() throws {
        let (goal, steps) = try makeGoal(stepCount: 3)
        XCTAssertEqual(try database.actionableSteps(userID: user.id).count, 3)

        try database.updateStepStatus(id: steps[0].id, status: .done, completedAt: Date())
        try database.updateStepStatus(id: steps[1].id, status: .skipped)
        XCTAssertEqual(try database.actionableSteps(userID: user.id).count, 1)

        try database.updateGoalStatus(id: goal.id, status: .archived)
        XCTAssertEqual(try database.actionableSteps(userID: user.id).count, 0)
    }

    // MARK: - State logs

    func testLatestStateLogReturnsMostRecent() throws {
        let now = Date()
        try database.appendStateLog(StateLog(userID: user.id, state: .busy, createdAt: now.addingTimeInterval(-60)))
        try database.appendStateLog(StateLog(userID: user.id, state: .tired, createdAt: now))

        XCTAssertEqual(try database.latestStateLog(userID: user.id)?.state, .tired)
        XCTAssertEqual(try database.recentStateLogs(userID: user.id, limit: 5).count, 2)
        XCTAssertEqual(UserState.uninspired.label, "没灵感")
    }
}

final class TimeFormatTests: XCTestCase {

    func testRoundTripIsLosslessToTheSecond() throws {
        let original = Date(timeIntervalSince1970: 1_800_000_000)
        let text = TimeFormat.string(from: original)
        XCTAssertEqual(text, "2027-01-15T08:00:00Z")
        let parsed = try XCTUnwrap(TimeFormat.date(from: text))
        XCTAssertEqual(parsed.timeIntervalSince1970, original.timeIntervalSince1970, accuracy: 1)
    }

    /// Fixed-width UTC strings must sort chronologically as plain text,
    /// otherwise the index-backed range queries in Database are wrong.
    func testLexicalOrderMatchesChronologicalOrder() {
        let dates = (0..<50).map { Date(timeIntervalSince1970: 1_700_000_000 + Double($0) * 86_400) }
        let texts = dates.map(TimeFormat.string(from:))
        XCTAssertEqual(texts, texts.sorted())
        XCTAssertEqual(texts.sorted(), dates.map(TimeFormat.string(from:)))
    }

    func testRoundsToGrid() throws {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0)!

        let base = Date(timeIntervalSince1970: 1_800_000_000) // :00
        let messy = base.addingTimeInterval(17 * 60)         // :17

        XCTAssertEqual(
            messy.roundedDown(toMinutes: 30, calendar: calendar).timeIntervalSince1970,
            base.timeIntervalSince1970,
            accuracy: 1
        )
        XCTAssertEqual(
            messy.roundedUp(toMinutes: 30, calendar: calendar).timeIntervalSince1970,
            base.addingTimeInterval(30 * 60).timeIntervalSince1970,
            accuracy: 1
        )
    }
}
