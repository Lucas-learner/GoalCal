import XCTest
@testable import GoalCalendarKit

final class ChatOrchestratorTests: XCTestCase {

    private var database: Database!
    private var user: User!

    private let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        return calendar
    }()

    private var now: Date {
        calendar.date(from: DateComponents(year: 2026, month: 10, day: 14, hour: 10))!
    }

    override func setUpWithError() throws {
        database = try Database.inMemory()
        user = try database.upsertUser(User(
            name: "测试",
            timezoneIdentifier: "Asia/Shanghai",
            workStartMinute: 9 * 60,
            workEndMinute: 22 * 60
        ))
    }

    /// Rules-only orchestrator: intent parsing is deterministic here, which is
    /// what the confirmation-gate tests need.
    private func makeOrchestrator(breakdownSteps: Int = 10) -> ChatOrchestrator {
        let items = (1...breakdownSteps).map {
            #"{"title":"步骤 \#($0)","detail":null,"estimated_minutes":45}"#
        }
        let payload = #"{"steps":[\#(items.joined(separator: ","))]}"#
        let planner = GoalPlanner(
            database: database,
            parser: GoalBreakdownParser(client: StaticLLMClient(response: payload))
        )
        return ChatOrchestrator(
            database: database,
            intentParser: IntentParser(client: nil),
            planner: planner
        )
    }

    private func allEvents() throws -> [CalendarEvent] {
        try database.events(
            userID: user.id,
            from: now.addingTimeInterval(-86_400 * 7),
            to: now.addingTimeInterval(86_400 * 60),
            includingProposed: true
        )
    }

    private func makeGoalWithSteps(count: Int = 10) throws -> (Goal, [Step]) {
        let goal = try database.insertGoal(Goal(userID: user.id, title: "学会 SwiftUI"))
        let steps = try database.insertSteps((1...count).map {
            Step(goalID: goal.id, seq: $0, title: "第 \($0) 步", percent: 0, estimatedMinutes: 45)
        })
        return (goal, steps)
    }

    // MARK: - The confirmation gate

    /// The single most important property of this layer: parsing and replying
    /// must never write to the calendar.
    func testAddEventDoesNotWriteAnything() async throws {
        let turn = await makeOrchestrator().handle("明天下午三点和小组开会", user: user, now: now)

        XCTAssertNotNil(turn.proposal)
        XCTAssertTrue(try allEvents().isEmpty, "handle() must not create events")

        guard case .addEvent(let draft) = try XCTUnwrap(turn.proposal).kind else {
            return XCTFail("expected addEvent proposal")
        }
        XCTAssertEqual(draft.status, .proposed)
        XCTAssertEqual(draft.title, "和小组开会")
        let hour = calendar.component(.hour, from: draft.startAt)
        XCTAssertEqual(hour, 15)
    }

    func testCommitWritesTheEventAsConfirmed() async throws {
        let orchestrator = makeOrchestrator()
        let turn = await orchestrator.handle("明天下午三点和小组开会", user: user, now: now)
        let proposal = try XCTUnwrap(turn.proposal)

        let result = try orchestrator.commit(proposal, user: user, now: now)
        XCTAssertEqual(result.event?.status, .confirmed)

        let written = try allEvents()
        XCTAssertEqual(written.count, 1)
        XCTAssertEqual(written[0].title, "和小组开会")
    }

    func testEventWithoutTimeAsksForOneInsteadOfGuessing() async throws {
        let turn = await makeOrchestrator().handle("帮我加一个开会", user: user, now: now)
        XCTAssertNil(turn.proposal)
        XCTAssertTrue(turn.reply.contains("什么时候"))
    }

    // MARK: - State → scheduling

    func testReportStateLogsStateAndProposesTimes() async throws {
        try makeGoalWithSteps()
        let orchestrator = makeOrchestrator()

        let turn = await orchestrator.handle("我现在好累", user: user, now: now)

        XCTAssertEqual(try database.latestStateLog(userID: user.id)?.state, .tired)
        XCTAssertTrue(try allEvents().isEmpty, "scheduling proposal must not write events")

        guard case let .schedule(step, _, options) = try XCTUnwrap(turn.proposal).kind else {
            return XCTFail("expected schedule proposal")
        }
        XCTAssertEqual(step.seq, 1)
        XCTAssertFalse(options.isEmpty)
    }

    func testCommittingAScheduleCreatesTheEventAndMarksTheStep() async throws {
        let (_, steps) = try makeGoalWithSteps()
        let orchestrator = makeOrchestrator()
        let turn = await orchestrator.handle("我现在好累", user: user, now: now)
        let proposal = try XCTUnwrap(turn.proposal)

        guard case let .schedule(_, _, options) = proposal.kind else {
            return XCTFail("expected schedule proposal")
        }
        let chosen = try XCTUnwrap(options.first)
        let result = try orchestrator.commit(proposal, option: chosen, user: user, now: now)

        XCTAssertEqual(result.event?.startAt, chosen.start)
        XCTAssertEqual(try database.step(id: steps[0].id)?.status, .scheduled)
    }

    func testReportStateWithoutGoalsDoesNotPropose() async throws {
        let turn = await makeOrchestrator().handle("我现在好累", user: user, now: now)
        XCTAssertNil(turn.proposal)
        XCTAssertTrue(turn.reply.contains("没有进行中的目标"))
    }

    // MARK: - Goal creation

    func testCreateGoalProposesWithoutWriting() async throws {
        let turn = await makeOrchestrator(breakdownSteps: 9)
            .handle("我三个月内想学会 SwiftUI", user: user, now: now)

        XCTAssertTrue(try database.goals(userID: user.id).isEmpty, "proposal must not create goals")

        guard case let .createGoal(draft) = try XCTUnwrap(turn.proposal).kind else {
            return XCTFail("expected createGoal proposal")
        }
        XCTAssertEqual(draft.steps.count, 9)
        XCTAssertEqual(draft.percentPerStep, 100.0 / 9.0, accuracy: 0.0001)
    }

    func testCommittingGoalCreatesItWithSteps() async throws {
        let orchestrator = makeOrchestrator(breakdownSteps: 10)
        let turn = await orchestrator.handle("我三个月内想学会 SwiftUI", user: user, now: now)
        let proposal = try XCTUnwrap(turn.proposal)

        let result = try orchestrator.commit(proposal, user: user, now: now)
        XCTAssertEqual(result.plannedGoal?.steps.count, 10)

        let goals = try database.goals(userID: user.id)
        XCTAssertEqual(goals.count, 1)
        XCTAssertEqual(try database.steps(goalID: goals[0].id).count, 10)
    }

    // MARK: - Query / delete / complete

    func testQueryListsEventsForTheDay() async throws {
        try database.insertEvent(CalendarEvent(
            userID: user.id,
            title: "站会",
            startAt: calendar.date(from: DateComponents(year: 2026, month: 10, day: 15, hour: 9))!,
            endAt: calendar.date(from: DateComponents(year: 2026, month: 10, day: 15, hour: 10))!
        ))

        let turn = await makeOrchestrator().handle("明天有什么安排", user: user, now: now)
        XCTAssertTrue(turn.reply.contains("站会"), turn.reply)
        XCTAssertNil(turn.proposal)
    }

    func testDeleteRequiresConfirmationThenRemoves() async throws {
        let target = try database.insertEvent(CalendarEvent(
            userID: user.id,
            title: "开组会",
            startAt: calendar.date(from: DateComponents(year: 2026, month: 10, day: 15, hour: 15))!,
            endAt: calendar.date(from: DateComponents(year: 2026, month: 10, day: 15, hour: 16))!
        ))

        let orchestrator = makeOrchestrator()
        let turn = await orchestrator.handle("删除明天的会", user: user, now: now)
        let proposal = try XCTUnwrap(turn.proposal)
        XCTAssertNotNil(try database.event(id: target.id), "delete must not happen before confirmation")

        _ = try orchestrator.commit(proposal, user: user, now: now)
        XCTAssertNil(try database.event(id: target.id))
    }

    func testCompletingAnEventAdvancesProgressEndToEnd() async throws {
        let (goal, steps) = try makeGoalWithSteps(count: 10)
        let orchestrator = makeOrchestrator()

        // 1. user reports state -> gets a schedule proposal
        let scheduleTurn = await orchestrator.handle("我现在不忙，下一步什么时候做", user: user, now: now)
        let scheduleProposal = try XCTUnwrap(scheduleTurn.proposal)
        guard case let .schedule(_, _, options) = scheduleProposal.kind else {
            return XCTFail("expected schedule proposal")
        }
        _ = try orchestrator.commit(scheduleProposal, option: options.first, user: user, now: now)

        // 2. user says it is done
        let completeTurn = await orchestrator.handle("完成了", user: user, now: now)
        let completeProposal = try XCTUnwrap(completeTurn.proposal)
        let result = try orchestrator.commit(completeProposal, user: user, now: now)

        XCTAssertEqual(try database.step(id: steps[0].id)?.status, .done)
        XCTAssertEqual(result.progress?.doneSteps, 1)
        XCTAssertEqual(result.progress?.percent ?? 0, 10.0, accuracy: 0.0001)
        XCTAssertEqual(try database.goal(id: goal.id)?.status, .active)
    }

    func testUnknownInputGivesExamples() async throws {
        let turn = await makeOrchestrator().handle("嗯嗯呵呵", user: user, now: now)
        XCTAssertNil(turn.proposal)
        XCTAssertTrue(turn.reply.contains("明天下午三点开会"), turn.reply)
    }
}
