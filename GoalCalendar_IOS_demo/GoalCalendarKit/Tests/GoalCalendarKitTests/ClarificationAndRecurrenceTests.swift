import XCTest
@testable import GoalCalendarKit

/// Returns canned responses in order, so a multi-turn flow can be scripted.
final class SequencedLLMClient: LLMClient, @unchecked Sendable {
    private var responses: [String]
    private var captured: [String] = []
    private let lock = NSLock()

    init(_ responses: [String]) {
        self.responses = responses
    }

    var capturedUserMessages: [String] {
        lock.lock(); defer { lock.unlock() }
        return captured
    }

    func complete(system: String, user: String) async throws -> String {
        lock.lock(); defer { lock.unlock() }
        captured.append(user)
        return responses.isEmpty ? "{}" : responses.removeFirst()
    }
}

private func planJSON(_ titles: [String], recurrence: String = "") -> String {
    let items = titles.enumerated().map { index, title in
        #"{"title":"\#(title)","estimated_minutes":30\#(recurrence)}"#
    }
    return #"{"steps":[\#(items.joined(separator: ","))]}"#
}

final class BreakdownClarificationTests: XCTestCase {

    func testClarifyResponseBecomesQuestions() async {
        let json = #"{"need_clarification":true,"questions":["你今年多大、性别是？","目前身高体重大概多少？"]}"#
        let parser = GoalBreakdownParser(client: StaticLLMClient(response: json))
        let result = await parser.breakdown("我想在一个月减 5 斤")

        XCTAssertTrue(result.usedLLM)
        XCTAssertTrue(result.needsClarification)
        XCTAssertEqual(result.questions.count, 2)
        XCTAssertTrue(result.steps.isEmpty, "asking a question is not a plan")
    }

    func testQuestionsAreCappedAndDeduplicated() async {
        let many = (1...9).map { "\"问题 \($0)\"" } + ["\"问题 1\""]
        let json = "{\"need_clarification\":true,\"questions\":[\(many.joined(separator: ","))]}"
        let parser = GoalBreakdownParser(client: StaticLLMClient(response: json))
        let result = await parser.breakdown("目标")

        XCTAssertEqual(result.questions.count, BreakdownValidator.maxQuestions)
        XCTAssertEqual(Set(result.questions).count, result.questions.count)
    }

    /// Saying "I need to ask" without actually asking is a failure, not a plan.
    func testClarifyWithoutQuestionsFallsBackToTemplate() async {
        let json = #"{"need_clarification":true,"questions":[]}"#
        let parser = GoalBreakdownParser(client: StaticLLMClient(response: json))
        let result = await parser.breakdown("目标")

        XCTAssertFalse(result.usedLLM)
        XCTAssertFalse(result.steps.isEmpty)
    }

    func testAnswersArePassedBackIntoThePrompt() async {
        let client = SequencedLLMClient([planJSON(["a", "b", "c"])])
        let parser = GoalBreakdownParser(client: client)
        _ = await parser.breakdown("减 5 斤", context: ["30 岁女", "170cm 62kg"])

        let prompt = try! XCTUnwrap(client.capturedUserMessages.last)
        XCTAssertTrue(prompt.contains("减 5 斤"), prompt)
        XCTAssertTrue(prompt.contains("30 岁女"), prompt)
        XCTAssertTrue(prompt.contains("170cm 62kg"), prompt)
    }

    func testWithoutContextThePromptIsJustTheGoal() async {
        let client = SequencedLLMClient([planJSON(["a", "b", "c"])])
        let parser = GoalBreakdownParser(client: client)
        _ = await parser.breakdown("学 SwiftUI")

        XCTAssertEqual(client.capturedUserMessages.last, "学 SwiftUI")
    }
}

final class RecurrenceTests: XCTestCase {

    func testDailyRecurrenceDecodes() throws {
        let json = #"{"title":"每天走路 30 分钟","estimated_minutes":30,"recurrence":{"kind":"daily"}}"#
        let step = try JSONDecoder().decode(BreakdownStep.self, from: Data(json.utf8))
        XCTAssertEqual(step.recurrence?.kind, .daily)
    }

    func testWeeklyRecurrenceDecodes() throws {
        let json = #"{"title":"每周三次快走","estimated_minutes":40,"recurrence":{"kind":"weekly","times_per_week":3}}"#
        let step = try JSONDecoder().decode(BreakdownStep.self, from: Data(json.utf8))
        XCTAssertEqual(step.recurrence?.kind, .weekly)
        XCTAssertEqual(step.recurrence?.timesPerWeek, 3)
    }

    /// Models sometimes emit the shorthand string.
    func testStringShorthandDecodes() throws {
        let json = #"{"title":"每天背单词","estimated_minutes":15,"recurrence":"daily"}"#
        let step = try JSONDecoder().decode(BreakdownStep.self, from: Data(json.utf8))
        XCTAssertEqual(step.recurrence?.kind, .daily)
    }

    func testValidatorClearsTimesPerWeekForDaily() {
        let input = [
            BreakdownStep(title: "每天喝水", estimatedMinutes: 5,
                          recurrence: StepRecurrence(kind: .daily, timesPerWeek: 5))
        ]
        let (steps, _) = BreakdownValidator.normalize(input)
        XCTAssertNil(steps[0].recurrence?.timesPerWeek, "a daily habit has no weekly count")
    }

    func testValidatorClampsWeeklyCount() {
        let input = [
            BreakdownStep(title: "太少", estimatedMinutes: 30,
                          recurrence: StepRecurrence(kind: .weekly, timesPerWeek: 0)),
            BreakdownStep(title: "太多", estimatedMinutes: 30,
                          recurrence: StepRecurrence(kind: .weekly, timesPerWeek: 99))
        ]
        let (steps, warnings) = BreakdownValidator.normalize(input)
        XCTAssertEqual(steps[0].recurrence?.timesPerWeek, 1)
        XCTAssertEqual(steps[1].recurrence?.timesPerWeek, 7)
        XCTAssertEqual(warnings.filter { $0.contains("每周次数") }.count, 2, "\(warnings)")
    }

    func testLabels() {
        XCTAssertEqual(StepRecurrence(kind: .daily).label, "每天")
        XCTAssertEqual(StepRecurrence(kind: .weekly, timesPerWeek: 3).label, "每周 3 次")
    }

    // MARK: - Persistence

    func testRecurrenceSurvivesTheDatabaseRoundTrip() throws {
        let database = try Database.inMemory()
        let user = try database.upsertUser(User(name: "测试"))
        let goal = try database.insertGoal(Goal(userID: user.id, title: "减重"))

        try database.insertSteps([
            Step(goalID: goal.id, seq: 1, title: "每天走路", percent: 0,
                 estimatedMinutes: 30, recurrence: StepRecurrence(kind: .daily)),
            Step(goalID: goal.id, seq: 2, title: "每周三次快走", percent: 0,
                 estimatedMinutes: 40, recurrence: StepRecurrence(kind: .weekly, timesPerWeek: 3)),
            Step(goalID: goal.id, seq: 3, title: "一次性买鞋", percent: 0)
        ])

        let stored = try database.steps(goalID: goal.id)
        XCTAssertEqual(stored[0].recurrence?.kind, .daily)
        XCTAssertNil(stored[0].recurrence?.timesPerWeek)
        XCTAssertEqual(stored[1].recurrence?.kind, .weekly)
        XCTAssertEqual(stored[1].recurrence?.timesPerWeek, 3)
        XCTAssertNil(stored[2].recurrence)
    }
}

final class ClarificationFlowTests: XCTestCase {

    private var database: Database!
    private var user: User!

    override func setUpWithError() throws {
        database = try Database.inMemory()
        user = try database.upsertUser(User(name: "测试", timezoneIdentifier: "Asia/Shanghai"))
    }

    private func orchestrator(responses: [String]) -> ChatOrchestrator {
        let client = SequencedLLMClient(responses)
        return ChatOrchestrator(
            database: database,
            intentParser: IntentParser(client: nil),
            planner: GoalPlanner(database: database, parser: GoalBreakdownParser(client: client))
        )
    }

    /// The model asks; the app must not create a goal yet.
    func testQuestionsAreAskedBeforeAnyPlanExists() async throws {
        let clarify = #"{"need_clarification":true,"questions":["你多大？","身高体重？"]}"#
        let chat = orchestrator(responses: [clarify])

        let turn = await chat.handle("我的目标是一个月减 5 斤", user: user)

        XCTAssertNil(turn.proposal)
        XCTAssertNotNil(turn.pendingGoal)
        XCTAssertEqual(turn.pendingGoal?.questions.count, 2)
        XCTAssertTrue(turn.reply.contains("你多大"), turn.reply)
        XCTAssertTrue(try database.goals(userID: user.id).isEmpty, "must not create a goal while asking")
    }

    func testAnsweringTheQuestionsProducesAPlan() async throws {
        let clarify = #"{"need_clarification":true,"questions":["你多大？"]}"#
        let chat = orchestrator(responses: [clarify, planJSON(["每天走路 30 分钟", "每周三次快走", "每周记录体重"])])

        let first = await chat.handle("我的目标是一个月减 5 斤", user: user)
        let pending = try XCTUnwrap(first.pendingGoal)

        let second = await chat.handle("30 岁，170cm，62kg", user: user, pendingGoal: pending)
        XCTAssertNotNil(second.proposal)
        XCTAssertNil(second.pendingGoal, "questions are done")

        guard case let .createGoal(draft) = try XCTUnwrap(second.proposal).kind else {
            return XCTFail("expected createGoal")
        }
        XCTAssertEqual(draft.steps.count, 3)
        XCTAssertEqual(draft.title, "一个月减 5 斤")
    }

    /// Answers must reach the model, otherwise the follow-up is pointless.
    func testAnswersAreForwardedToTheModel() async throws {
        let clarify = #"{"need_clarification":true,"questions":["你多大？"]}"#
        let client = SequencedLLMClient([clarify, planJSON(["a", "b", "c"])])
        let chat = ChatOrchestrator(
            database: database,
            intentParser: IntentParser(client: nil),
            planner: GoalPlanner(database: database, parser: GoalBreakdownParser(client: client))
        )

        let first = await chat.handle("我的目标是减 5 斤", user: user)
        _ = await chat.handle("30 岁女，170cm", user: user, pendingGoal: first.pendingGoal)

        let lastPrompt = try XCTUnwrap(client.capturedUserMessages.last)
        XCTAssertTrue(lastPrompt.contains("30 岁女"), lastPrompt)
    }

    func testCancelAbandonsThePendingGoal() async throws {
        let clarify = #"{"need_clarification":true,"questions":["你多大？"]}"#
        let chat = orchestrator(responses: [clarify, clarify])

        let first = await chat.handle("我的目标是减 5 斤", user: user)
        let second = await chat.handle("算了，先不弄了", user: user, pendingGoal: first.pendingGoal)

        XCTAssertNil(second.pendingGoal)
        XCTAssertNil(second.proposal)
        XCTAssertTrue(try database.goals(userID: user.id).isEmpty)
    }

    /// After the cap, a rough plan is better than an endless interview.
    func testQuestionLoopIsCappedAndFallsBackToATemplate() async throws {
        let clarify = #"{"need_clarification":true,"questions":["还是不够，再说点？"]}"#
        let chat = orchestrator(responses: [clarify, clarify, clarify, clarify])

        var turn = await chat.handle("我的目标是减 5 斤", user: user)
        var rounds = 0
        while let pending = turn.pendingGoal, rounds < 5 {
            rounds += 1
            turn = await chat.handle("补充信息 \(rounds)", user: user, pendingGoal: pending)
        }

        XCTAssertNotNil(turn.proposal, "should eventually hand over a plan")
        XCTAssertNil(turn.pendingGoal)
        XCTAssertFalse(turn.notes.isEmpty)
        XCTAssertLessThanOrEqual(rounds, ChatOrchestrator.maxClarificationRounds + 1)
    }
}

final class RecurrenceInferenceTests: XCTestCase {

    func testDailyWording() {
        XCTAssertEqual(RecurrenceInference.infer(from: "每天早餐加一份蛋白质")?.kind, .daily)
        XCTAssertEqual(RecurrenceInference.infer(from: "每日复盘 5 分钟")?.kind, .daily)
    }

    func testWeeklyWithArabicDigit() {
        let result = RecurrenceInference.infer(from: "每周 3 次全身力量训练")
        XCTAssertEqual(result?.kind, .weekly)
        XCTAssertEqual(result?.timesPerWeek, 3)
    }

    func testWeeklyWithChineseNumeral() {
        XCTAssertEqual(RecurrenceInference.infer(from: "每周三次快走")?.timesPerWeek, 3)
        XCTAssertEqual(RecurrenceInference.infer(from: "每周两次网球")?.timesPerWeek, 2)
    }

    func testWeeklyWithoutCountDefaultsToOne() {
        XCTAssertEqual(RecurrenceInference.infer(from: "每周固定称重一次")?.timesPerWeek, 1)
    }

    func testOneOffStepHasNoRecurrence() {
        XCTAssertNil(RecurrenceInference.infer(from: "测一次基线数据"))
        XCTAssertNil(RecurrenceInference.infer(from: "买一双跑鞋"))
    }

    /// "每天" must win over a later 每周 mention.
    func testDailyWinsWhenBothWordsAppear() {
        XCTAssertEqual(
            RecurrenceInference.infer(from: "每天拉伸，每周复盘一次")?.kind,
            .daily
        )
    }

    /// The model often writes the cadence in prose but omits the field.
    func testValidatorBackfillsMissingRecurrenceFromTheTitle() {
        let input = [
            BreakdownStep(title: "每天早餐加一份蛋白质", estimatedMinutes: 15),
            BreakdownStep(title: "每周三次力量训练", estimatedMinutes: 45),
            BreakdownStep(title: "测一次基线", estimatedMinutes: 15)
        ]
        let (steps, _) = BreakdownValidator.normalize(input)

        XCTAssertEqual(steps[0].recurrence?.kind, .daily)
        XCTAssertEqual(steps[1].recurrence?.timesPerWeek, 3)
        XCTAssertNil(steps[2].recurrence)
    }

    /// End to end: inferred cadence must survive into the stored step.
    func testInferredRecurrenceIsPersistedByThePlanner() async throws {
        let payload = #"{"steps":[{"title":"每天走路 30 分钟","estimated_minutes":30},{"title":"每周两次力量","estimated_minutes":30},{"title":"测一次基线","estimated_minutes":15}]}"#
        let database = try Database.inMemory()
        let user = try database.upsertUser(User(name: "测试"))
        let planner = GoalPlanner(
            database: database,
            parser: GoalBreakdownParser(client: StaticLLMClient(response: payload))
        )

        let planned = try await planner.plan("减重", user: user)
        let stored = try database.steps(goalID: planned.goal.id)

        XCTAssertEqual(stored[0].recurrence?.kind, .daily)
        XCTAssertEqual(stored[1].recurrence?.timesPerWeek, 2)
        XCTAssertNil(stored[2].recurrence)
    }
}
