import XCTest
@testable import GoalCalendarKit

final class BreakdownValidatorTests: XCTestCase {

    private func make(_ count: Int, minutes: Int = 45) -> [BreakdownStep] {
        (1...count).map { BreakdownStep(title: "第 \($0) 步", detail: nil, estimatedMinutes: minutes) }
    }

    func testPreferredCountPassesThrough() {
        let (steps, warnings) = BreakdownValidator.normalize(make(10))
        XCTAssertEqual(steps.count, 10)
        XCTAssertTrue(warnings.isEmpty, "\(warnings)")
    }

    func testTooManyStepsAreTrimmed() {
        let (steps, warnings) = BreakdownValidator.normalize(make(20))
        XCTAssertEqual(steps.count, BreakdownValidator.acceptedRange.upperBound)
        XCTAssertFalse(warnings.isEmpty)
    }

    func testTooFewStepsIsFlaggedButKept() {
        let (steps, warnings) = BreakdownValidator.normalize(make(4))
        XCTAssertEqual(steps.count, 4)
        XCTAssertFalse(warnings.isEmpty)
    }

    func testEmptyTitlesAreDropped() {
        let input = [
            BreakdownStep(title: "  ", estimatedMinutes: 45),
            BreakdownStep(title: "真的步骤", estimatedMinutes: 45)
        ]
        let (steps, warnings) = BreakdownValidator.normalize(input)
        XCTAssertEqual(steps.count, 1)
        XCTAssertEqual(steps[0].title, "真的步骤")
        XCTAssertFalse(warnings.isEmpty)
    }

    func testDuplicateTitlesAreDropped() {
        let input = [
            BreakdownStep(title: "写计划", estimatedMinutes: 45),
            BreakdownStep(title: "写计划", estimatedMinutes: 60),
            BreakdownStep(title: "写计划 ", estimatedMinutes: 30)
        ]
        let (steps, _) = BreakdownValidator.normalize(input)
        XCTAssertEqual(steps.count, 1)
    }

    func testEstimatedMinutesAreClamped() {
        // Ten steps so the count itself stays inside the preferred range and does
        // not add its own warning.
        var input = (1...8).map { BreakdownStep(title: "正常 \($0)", estimatedMinutes: 45) }
        input.append(BreakdownStep(title: "太短", estimatedMinutes: 1))
        input.append(BreakdownStep(title: "太长", estimatedMinutes: 600))

        let (steps, warnings) = BreakdownValidator.normalize(input)
        XCTAssertEqual(steps.count, 10)
        XCTAssertEqual(steps[8].estimatedMinutes, 15)
        XCTAssertEqual(steps[9].estimatedMinutes, 240)
        XCTAssertEqual(warnings.count, 2, "\(warnings)")
    }

    func testBlankDetailBecomesNil() {
        let (steps, _) = BreakdownValidator.normalize([
            BreakdownStep(title: "步骤", detail: "   ", estimatedMinutes: 45)
        ])
        XCTAssertNil(steps[0].detail)
    }

    func testTitlesAreTrimmed() {
        let (steps, _) = BreakdownValidator.normalize([
            BreakdownStep(title: "  前后有空格  ", estimatedMinutes: 45)
        ])
        XCTAssertEqual(steps[0].title, "前后有空格")
    }
}

final class GoalBreakdownParserTests: XCTestCase {

    private let user = User(name: "测试", timezoneIdentifier: "Asia/Shanghai")

    private func json(steps: Int) -> String {
        let items = (1...steps).map {
            #"{"title":"步骤 \#($0)","detail":"做完的标志 \#($0)","estimated_minutes":45}"#
        }
        return #"{"steps":[\#(items.joined(separator: ","))]}"#
    }

    func testWellFormedBreakdown() async throws {
        let parser = GoalBreakdownParser(client: StaticLLMClient(response: json(steps: 10)))
        let result = await parser.breakdown("三个月内学会 SwiftUI")

        XCTAssertTrue(result.usedLLM)
        XCTAssertEqual(result.steps.count, 10)
        XCTAssertEqual(result.steps[0].title, "步骤 1")
        XCTAssertEqual(result.steps[0].estimatedMinutes, 45)
        XCTAssertTrue(result.warnings.isEmpty, "\(result.warnings)")
    }

    func testCodeFencedOutput() async {
        let fenced = "```json\n" + json(steps: 9) + "\n```"
        let parser = GoalBreakdownParser(client: StaticLLMClient(response: fenced))
        let result = await parser.breakdown("目标")
        XCTAssertTrue(result.usedLLM)
        XCTAssertEqual(result.steps.count, 9)
    }

    func testBareArrayOutput() async {
        let bare = #"[{"title":"a","estimated_minutes":30},{"title":"b","estimated_minutes":30},{"title":"c","estimated_minutes":30}]"#
        let parser = GoalBreakdownParser(client: StaticLLMClient(response: bare))
        let result = await parser.breakdown("目标")
        XCTAssertTrue(result.usedLLM)
        XCTAssertEqual(result.steps.count, 3)
    }

    func testMissingEstimatedMinutesDefaultsTo45() async throws {
        let payload = #"{"steps":[{"title":"a"},{"title":"b"},{"title":"c"}]}"#
        let parser = GoalBreakdownParser(client: StaticLLMClient(response: payload))
        let result = await parser.breakdown("目标")
        XCTAssertEqual(result.steps.first?.estimatedMinutes, 45)
    }

    func testTransportFailureFallsBackToTemplate() async {
        let parser = GoalBreakdownParser(client: StaticLLMClient(error: .transport("boom")))
        let result = await parser.breakdown("三个月内学会 SwiftUI")

        XCTAssertFalse(result.usedLLM)
        XCTAssertEqual(result.steps.count, 5)
        XCTAssertFalse(result.warnings.isEmpty)
        XCTAssertTrue(result.steps[0].title.contains("完成标准"))
    }

    func testGarbageFallsBackToTemplate() async {
        let parser = GoalBreakdownParser(client: StaticLLMClient(response: "抱歉，我无法完成"))
        let result = await parser.breakdown("目标")
        XCTAssertFalse(result.usedLLM)
        XCTAssertEqual(result.steps.count, 5)
    }

    /// Two steps is not a plan; the template is more useful than a stub.
    func testTooFewStepsFallsBack() async {
        let payload = #"{"steps":[{"title":"a"},{"title":"b"}]}"#
        let parser = GoalBreakdownParser(client: StaticLLMClient(response: payload))
        let result = await parser.breakdown("目标")
        XCTAssertFalse(result.usedLLM)
    }

    func testNoClientUsesTemplate() async {
        let parser = GoalBreakdownParser(client: nil)
        let result = await parser.breakdown("三个月内学会 SwiftUI")
        XCTAssertFalse(result.usedLLM)
        XCTAssertFalse(result.steps.isEmpty)
    }
}

final class GoalPlannerTests: XCTestCase {

    private var database: Database!
    private var user: User!

    override func setUpWithError() throws {
        database = try Database.inMemory()
        user = try database.upsertUser(User(name: "测试", timezoneIdentifier: "Asia/Shanghai"))
    }

    private func planner(steps: Int) -> GoalPlanner {
        let items = (1...steps).map {
            #"{"title":"步骤 \#($0)","detail":null,"estimated_minutes":45}"#
        }
        let payload = #"{"steps":[\#(items.joined(separator: ","))]}"#
        return GoalPlanner(
            database: database,
            parser: GoalBreakdownParser(client: StaticLLMClient(response: payload))
        )
    }

    func testPlanPersistsGoalAndSteps() async throws {
        let planned = try await planner(steps: 10).plan("三个月内学会 SwiftUI", user: user)

        XCTAssertEqual(planned.goal.title, "三个月内学会 SwiftUI")
        XCTAssertEqual(planned.steps.count, 10)
        XCTAssertTrue(planned.usedLLM)
        XCTAssertEqual(planned.progress.totalSteps, 10)
        XCTAssertEqual(planned.progress.doneSteps, 0)
        XCTAssertEqual(planned.progress.percent, 0, accuracy: 0.0001)

        XCTAssertEqual(try database.steps(goalID: planned.goal.id).count, 10)
        XCTAssertEqual(try database.goals(userID: user.id).count, 1)
    }

    func testSeqIsAssignedByPosition() async throws {
        let planned = try await planner(steps: 8).plan("目标", user: user)
        XCTAssertEqual(planned.steps.map(\.seq), Array(1...8))
    }

    /// Progress model B: percent is 100/N, so it always sums to 100.
    func testPercentSumsToOneHundred() async throws {
        let planned = try await planner(steps: 12).plan("目标", user: user)
        let steps = try database.steps(goalID: planned.goal.id)
        let total = steps.reduce(0.0) { $0 + $1.percent }
        XCTAssertEqual(total, 100.0, accuracy: 0.0001)
        XCTAssertEqual(steps[0].percent, 100.0 / 12.0, accuracy: 0.0001)
    }

    func testEmptyGoalTextThrows() async {
        do {
            _ = try await planner(steps: 10).plan("   ", user: user)
            XCTFail("expected throw")
        } catch {
            XCTAssertTrue(error is PlannerError)
        }
    }

    /// A goal without its steps is unusable, so a partial write must not survive.
    func testCreateGoalRollsBackWhenAStepFails() throws {
        let goal = Goal(userID: user.id, title: "会失败的目标")
        let duplicatedSeq = [
            Step(goalID: goal.id, seq: 1, title: "a", percent: 0),
            Step(goalID: goal.id, seq: 1, title: "b", percent: 0)
        ]

        XCTAssertThrowsError(try database.createGoal(goal, steps: duplicatedSeq))
        XCTAssertNil(try database.goal(id: goal.id), "goal should have been rolled back")
        XCTAssertTrue(try database.steps(goalID: goal.id).isEmpty)
    }

    func testDegradedPlanStillCreatesAUsableGoal() async throws {
        let planner = GoalPlanner(
            database: database,
            parser: GoalBreakdownParser(client: StaticLLMClient(error: .transport("offline")))
        )
        let planned = try await planner.plan("三个月内学会 SwiftUI", user: user)

        XCTAssertFalse(planned.usedLLM)
        XCTAssertFalse(planned.steps.isEmpty)
        XCTAssertFalse(planned.warnings.isEmpty)
        XCTAssertEqual(try database.steps(goalID: planned.goal.id).count, planned.steps.count)
    }
}
