import XCTest
@testable import GoalCalendarKit

final class SplitStepTests: XCTestCase {

    private var database: Database!
    private var user: User!
    private var goal: Goal!
    private var steps: [Step]!

    private let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        return calendar
    }()

    private var now: Date {
        calendar.date(from: DateComponents(year: 2026, month: 10, day: 19, hour: 14))!
    }

    override func setUpWithError() throws {
        database = try Database.inMemory()
        user = try database.upsertUser(User(
            name: "测试", timezoneIdentifier: "Asia/Shanghai",
            workStartMinute: 9 * 60, workEndMinute: 22 * 60
        ))
        goal = try database.insertGoal(Goal(userID: user.id, title: "学会 SwiftUI"))
        steps = try database.insertSteps((1...4).map {
            Step(goalID: goal.id, seq: $0, title: "第 \($0) 步", percent: 0, estimatedMinutes: 60)
        })
    }

    func testSplitReplacesOneStepWithTwo() throws {
        let result = try XCTUnwrap(try database.splitStep(
            id: steps[1].id,
            into: ["上半", "下半"],
            at: now
        ))

        XCTAssertEqual(result.goalID, goal.id)
        XCTAssertEqual(result.steps.count, 5, "4 steps minus 1 plus 2")
        XCTAssertEqual(result.created.count, 2)
        XCTAssertEqual(result.created.first?.estimatedMinutes, 30, "60 minutes halved")

        // Ordering: the pair sits where the original was.
        XCTAssertEqual(result.steps.map(\.seq), Array(1...5))
        XCTAssertEqual(result.steps.map(\.title), ["第 1 步", "上半", "下半", "第 3 步", "第 4 步"])
    }

    func testPercentIsRebalancedToTheNewCount() throws {
        _ = try database.splitStep(id: steps[1].id, into: ["A", "B"], at: now)

        let stored = try database.steps(goalID: goal.id)
        let total = stored.reduce(0.0) { $0 + $1.percent }
        XCTAssertEqual(total, 100.0, accuracy: 0.0001)
        XCTAssertEqual(stored[0].percent, 20.0, accuracy: 0.0001)
    }

    /// Splitting must not create a second goal.
    func testSplitKeepsTheSameGoal() throws {
        _ = try database.splitStep(id: steps[0].id, into: ["A", "B"], at: now)
        XCTAssertEqual(try database.goals(userID: user.id).count, 1)
        XCTAssertTrue(try database.steps(goalID: goal.id).allSatisfy { $0.goalID == goal.id })
    }

    func testSplitUnknownStepReturnsNil() throws {
        XCTAssertNil(try database.splitStep(id: "nope", into: ["A", "B"]))
    }

    func testProgressStillAddsUpAfterSplit() throws {
        _ = try database.splitStep(id: steps[0].id, into: ["A", "B"], at: now)
        let updated = try database.steps(goalID: goal.id)

        try database.setStepStatus(id: updated[0].id, status: .done)
        let progress = try database.progress(goalID: goal.id)
        XCTAssertEqual(progress.totalSteps, 5)
        XCTAssertEqual(progress.doneSteps, 1)
        XCTAssertEqual(progress.percent, 20.0, accuracy: 0.0001)
    }
}

final class MissedFlowTests: XCTestCase {

    private var database: Database!
    private var user: User!
    private var goal: Goal!
    private var step: Step!
    private var event: CalendarEvent!

    private let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        return calendar
    }()

    private var now: Date {
        calendar.date(from: DateComponents(year: 2026, month: 10, day: 19, hour: 14))!
    }

    override func setUpWithError() throws {
        database = try Database.inMemory()
        user = try database.upsertUser(User(
            name: "测试", timezoneIdentifier: "Asia/Shanghai",
            workStartMinute: 9 * 60, workEndMinute: 22 * 60
        ))
        goal = try database.insertGoal(Goal(userID: user.id, title: "学会 SwiftUI"))
        step = try database.insertSteps([
            Step(goalID: goal.id, seq: 1, title: "第三课", percent: 100, estimatedMinutes: 60)
        ]).first

        let start = now.addingTimeInterval(-3 * 3600)
        event = try database.insertEvent(CalendarEvent(
            userID: user.id, stepID: step.id, goalID: goal.id,
            title: step.title, startAt: start, endAt: start.addingTimeInterval(1800)
        ))
    }

    private var orchestrator: ChatOrchestrator {
        ChatOrchestrator(
            database: database,
            intentParser: IntentParser(client: nil),
            planner: GoalPlanner(database: database, parser: GoalBreakdownParser(client: nil))
        )
    }

    /// A missed slot must leave the calendar, otherwise the same step ends up
    /// with one passed event and one future one and every query sees two.
    func testMarkingMissedCancelsTheOldEventAndAsksWhy() throws {
        let turn = orchestrator.markMissed(event, user: user, now: now)

        XCTAssertEqual(try database.event(id: event.id)?.status, .cancelled)
        guard case .missedChoice = try XCTUnwrap(turn.proposal).kind else {
            return XCTFail("expected missedChoice")
        }
        XCTAssertTrue(turn.reply.contains("临时有事"), turn.reply)
        XCTAssertTrue(turn.reply.contains("重"), turn.reply)
    }

    func testChoosingTooHardSplitsAndOffersANewSlot() throws {
        let turn = orchestrator.splitStepAndSchedule(event, user: user, now: now)

        let stored = try database.steps(goalID: goal.id)
        XCTAssertEqual(stored.count, 2, "one step split into two")
        XCTAssertTrue(stored.allSatisfy { $0.estimatedMinutes == 30 })

        guard case let .schedule(newStep, _, options) = try XCTUnwrap(turn.proposal).kind else {
            return XCTFail("expected a schedule proposal for the first half")
        }
        XCTAssertEqual(newStep.title, "第三课（上半）")
        XCTAssertFalse(options.isEmpty)
    }

    func testChoosingBusyOffersNewSlotsWithoutSplitting() throws {
        _ = orchestrator.markMissed(event, user: user, now: now)
        let turn = orchestrator.postponeProposal(for: event, user: user, now: now)

        XCTAssertEqual(try database.steps(goalID: goal.id).count, 1, "still one step")
        guard case .schedule = try XCTUnwrap(turn.proposal).kind else {
            return XCTFail("expected schedule proposal")
        }
    }
}
