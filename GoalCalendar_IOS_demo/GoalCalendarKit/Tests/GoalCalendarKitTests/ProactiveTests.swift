import XCTest
@testable import GoalCalendarKit

final class ProactiveTests: XCTestCase {

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
        steps = try database.insertSteps((1...3).map {
            Step(goalID: goal.id, seq: $0, title: "第 \($0) 步", percent: 0, estimatedMinutes: 45)
        })
    }

    private var orchestrator: ChatOrchestrator {
        ChatOrchestrator(
            database: database,
            intentParser: IntentParser(client: nil),
            planner: GoalPlanner(database: database, parser: GoalBreakdownParser(client: nil))
        )
    }

    // MARK: - Overdue check-in

    func testOverdueStepRaisesACheckin() throws {
        let start = now.addingTimeInterval(-3 * 3600)
        _ = try database.insertEvent(CalendarEvent(
            userID: user.id, stepID: steps[0].id, goalID: goal.id,
            title: steps[0].title, startAt: start, endAt: start.addingTimeInterval(1800)
        ))

        let turn = try XCTUnwrap(orchestrator.proactiveTurn(user: user, now: now))
        guard case let .checkin(event, stepTitle) = try XCTUnwrap(turn.proposal).kind else {
            return XCTFail("expected checkin")
        }
        XCTAssertEqual(event.stepID, steps[0].id)
        XCTAssertEqual(stepTitle, "第 1 步")
        XCTAssertTrue(turn.reply.contains("完成了吗"), turn.reply)
    }

    func testCompletedStepIsNotChased() throws {
        let start = now.addingTimeInterval(-3 * 3600)
        _ = try database.insertEvent(CalendarEvent(
            userID: user.id, stepID: steps[0].id, goalID: goal.id,
            title: steps[0].title, startAt: start, endAt: start.addingTimeInterval(1800)
        ))
        try database.setStepStatus(id: steps[0].id, status: .done)

        // Nothing overdue, but there are idle steps, so we fall through to a suggestion.
        let turn = orchestrator.proactiveTurn(user: user, now: now)
        if let proposal = turn?.proposal, case .checkin = proposal.kind {
            XCTFail("a completed step must not be chased")
        }
    }

    // MARK: - Idle suggestion

    func testUnscheduledStepGetsAFreeSlotSuggestion() throws {
        let turn = try XCTUnwrap(orchestrator.proactiveTurn(user: user, now: now))
        guard case let .schedule(step, _, options) = try XCTUnwrap(turn.proposal).kind else {
            return XCTFail("expected schedule suggestion")
        }
        XCTAssertEqual(step.id, steps[0].id)
        XCTAssertFalse(options.isEmpty)
        // The prompt must name a concrete day and time, not just "later".
        let first = try XCTUnwrap(options.first)
        let hourMinute = String(
            format: "%02d:%02d",
            calendar.component(.hour, from: first.start),
            calendar.component(.minute, from: first.start)
        )
        XCTAssertTrue(turn.reply.contains(hourMinute), turn.reply)
        XCTAssertTrue(
            turn.reply.contains("今天") || turn.reply.contains("明天") || turn.reply.contains("月"),
            turn.reply
        )
    }

    func testAlreadyScheduledStepIsNotSuggestedAgain() throws {
        let start = now.addingTimeInterval(3 * 3600)
        _ = try database.insertEvent(CalendarEvent(
            userID: user.id, stepID: steps[0].id, goalID: goal.id,
            title: steps[0].title, startAt: start, endAt: start.addingTimeInterval(1800)
        ))
        try database.updateStepStatus(id: steps[0].id, status: .scheduled)

        // Step 2 is still idle, so a suggestion is fine — but it must not be step 1.
        if let proposal = orchestrator.proactiveTurn(user: user, now: now)?.proposal,
           case let .schedule(step, _, _) = proposal.kind {
            XCTAssertNotEqual(step.id, steps[0].id, "step 1 already has a future event")
        }
    }

    func testNothingToSayWhenAllStepsAreDone() throws {
        for step in steps {
            try database.setStepStatus(id: step.id, status: .done)
        }
        XCTAssertNil(orchestrator.proactiveTurn(user: user, now: now))
    }

    func testNoGoalsMeansNoProactiveMessage() throws {
        let emptyDatabase = try Database.inMemory()
        let emptyUser = try emptyDatabase.upsertUser(User(name: "空"))
        let chat = ChatOrchestrator(
            database: emptyDatabase,
            intentParser: IntentParser(client: nil),
            planner: GoalPlanner(database: emptyDatabase, parser: GoalBreakdownParser(client: nil))
        )
        XCTAssertNil(chat.proactiveTurn(user: emptyUser, now: now))
    }

    // MARK: - Postpone

    func testPostponeOffersNewSlotsForTheSameStep() throws {
        let start = now.addingTimeInterval(-3 * 3600)
        let event = try database.insertEvent(CalendarEvent(
            userID: user.id, stepID: steps[0].id, goalID: goal.id,
            title: steps[0].title, startAt: start, endAt: start.addingTimeInterval(1800)
        ))

        let turn = orchestrator.postponeProposal(for: event, user: user, now: now)
        guard case let .schedule(step, _, options) = try XCTUnwrap(turn.proposal).kind else {
            return XCTFail("expected a new-time proposal")
        }
        XCTAssertEqual(step.id, steps[0].id)
        XCTAssertFalse(options.isEmpty)
        XCTAssertTrue(options.allSatisfy { $0.start > now }, "new slots must be in the future")
    }

    // MARK: - Check-in commit

    func testCompletingACheckinAdvancesProgress() throws {
        let start = now.addingTimeInterval(-3 * 3600)
        let event = try database.insertEvent(CalendarEvent(
            userID: user.id, stepID: steps[0].id, goalID: goal.id,
            title: steps[0].title, startAt: start, endAt: start.addingTimeInterval(1800)
        ))

        let result = try orchestrator.commit(
            ProposedAction(kind: .checkin(event: event, stepTitle: steps[0].title)),
            user: user
        )
        XCTAssertEqual(result.event?.status, .done)
        XCTAssertEqual(result.progress?.doneSteps, 1)
    }
}
