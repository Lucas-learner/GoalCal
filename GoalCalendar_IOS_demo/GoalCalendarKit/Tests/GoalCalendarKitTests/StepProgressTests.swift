import XCTest
@testable import GoalCalendarKit

/// Direct step completion, without a calendar event.
final class StepProgressTests: XCTestCase {

    private var database: Database!
    private var user: User!
    private var goal: Goal!
    private var steps: [Step]!

    override func setUpWithError() throws {
        database = try Database.inMemory()
        user = try database.upsertUser(User(name: "测试", timezoneIdentifier: "Asia/Shanghai"))
        goal = try database.insertGoal(Goal(userID: user.id, title: "学会 SwiftUI"))
        steps = try database.insertSteps((1...4).map {
            Step(goalID: goal.id, seq: $0, title: "第 \($0) 步", percent: 0, estimatedMinutes: 45)
        })
    }

    func testCompletingAStepAdvancesProgressWithoutAnEvent() throws {
        let result = try database.setStepStatus(id: steps[0].id, status: .done)

        XCTAssertEqual(result.step?.status, .done)
        XCTAssertNotNil(result.step?.completedAt)
        XCTAssertEqual(result.progress?.doneSteps, 1)
        XCTAssertEqual(result.progress?.percent ?? 0, 25.0, accuracy: 0.0001)
        XCTAssertEqual(result.goal?.status, .active)
        XCTAssertTrue(try database.events(
            userID: user.id,
            from: Date().addingTimeInterval(-86_400),
            to: Date().addingTimeInterval(86_400),
            includingProposed: true
        ).isEmpty, "no event should have been created")
    }

    func testCompletingTheLastStepClosesTheGoal() throws {
        for step in steps.dropLast() {
            try database.setStepStatus(id: step.id, status: .done)
        }
        XCTAssertEqual(try database.goal(id: goal.id)?.status, .active)

        let last = try database.setStepStatus(id: steps[3].id, status: .done)
        XCTAssertEqual(last.goal?.status, .completed)
        XCTAssertNotNil(last.goal?.completedAt)
        XCTAssertEqual(last.progress?.isComplete, true)
    }

    /// Progress that cannot go backwards makes a mistaken tap permanent.
    func testUncompletingAStepReopensTheGoal() throws {
        for step in steps {
            try database.setStepStatus(id: step.id, status: .done)
        }
        XCTAssertEqual(try database.goal(id: goal.id)?.status, .completed)

        let reopened = try database.setStepStatus(id: steps[1].id, status: .pending)
        XCTAssertEqual(reopened.goal?.status, .active)
        XCTAssertNil(reopened.goal?.completedAt)
        XCTAssertEqual(reopened.progress?.doneSteps, 3)
        XCTAssertEqual(reopened.progress?.percent ?? 0, 75.0, accuracy: 0.0001)
    }

    func testSkippingDoesNotAdvanceProgress() throws {
        let result = try database.setStepStatus(id: steps[0].id, status: .skipped)
        XCTAssertEqual(result.step?.status, .skipped)
        XCTAssertEqual(result.progress?.doneSteps, 0)
    }

    func testMissingStepReturnsEmptyResult() throws {
        let result = try database.setStepStatus(id: "does-not-exist", status: .done)
        XCTAssertNil(result.step)
        XCTAssertNil(result.goal)
    }
}
