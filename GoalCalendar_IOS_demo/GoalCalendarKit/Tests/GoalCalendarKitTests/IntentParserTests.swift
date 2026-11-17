import XCTest
@testable import GoalCalendarKit

final class RuleBasedIntentParserTests: XCTestCase {

    private let parser = RuleBasedIntentParser()

    // MARK: - Intent classification

    func testAddEvent() throws {
        let intent = parser.parse("明天下午三点和小组开会")
        XCTAssertEqual(intent.kind, .addEvent)
        XCTAssertEqual(intent.time?.day, .tomorrow)
        XCTAssertEqual(intent.time?.period, .afternoon)
        XCTAssertEqual(intent.time?.hour, 3)
        let title = try XCTUnwrap(intent.title)
        XCTAssertTrue(title.contains("和小组开会"), title)
    }

    func testReportState() {
        XCTAssertEqual(parser.parse("我现在好累").state, .tired)
        XCTAssertEqual(parser.parse("忙死了").state, .busy)
        XCTAssertEqual(parser.parse("完全没灵感").state, .uninspired)
        XCTAssertEqual(parser.parse("我现在好累").kind, .reportState)
    }

    func testStateBeatsEventWhenBothPresent() {
        // "明天很忙" reports a state; it is not a calendar entry.
        let intent = parser.parse("明天很忙")
        XCTAssertEqual(intent.kind, .reportState)
        XCTAssertEqual(intent.state, .busy)
    }

    func testGoalCreation() {
        XCTAssertEqual(parser.parse("我三个月内想学会 SwiftUI").kind, .createGoal)
        XCTAssertEqual(parser.parse("我想考过驾照").kind, .createGoal)
        XCTAssertEqual(parser.parse("我的目标是攒够十万").kind, .createGoal)
    }

    /// "完成报告" is a task, not a long-term goal.
    func testCompletingATaskIsNotAGoal() {
        XCTAssertEqual(parser.parse("明天下午三点完成报告").kind, .addEvent)
    }

    func testDelete() {
        XCTAssertEqual(parser.parse("删除明天下午的会").kind, .deleteEvent)
    }

    /// Regression: the command verb must not survive into the title.
    func testDeleteTitleDropsTheVerb() throws {
        let intent = parser.parse("删除明天的会")
        let title = try XCTUnwrap(intent.title)
        XCTAssertEqual(title, "会")
    }

    func testQuery() {
        let intent = parser.parse("明天有什么安排")
        XCTAssertEqual(intent.kind, .queryEvents)
        XCTAssertEqual(intent.queryRange?.day, .tomorrow)
    }

    func testConfirmation() {
        XCTAssertEqual(parser.parse("好的").kind, .confirm)
        XCTAssertEqual(parser.parse("就这个").kind, .confirm)
    }

    func testScheduleRequest() {
        XCTAssertEqual(parser.parse("下一步什么时候做").kind, .scheduleNext)
    }

    // MARK: - Time extraction details

    func testChineseNumeralHourAndHalf() {
        let expression = RuleBasedIntentParser.extractTime("明天上午九点半开会")
        XCTAssertEqual(expression?.period, .morning)
        XCTAssertEqual(expression?.hour, 9)
        XCTAssertEqual(expression?.minute, 30)
    }

    func testWeekdayExtraction() {
        let expression = RuleBasedIntentParser.extractTime("下周三见")
        XCTAssertEqual(expression?.day, .weekday)
        XCTAssertEqual(expression?.weekday, 3)
        XCTAssertEqual(expression?.weekOffset, 1)
    }

    func testMonthDayExtraction() {
        let expression = RuleBasedIntentParser.extractTime("10月20日交作业")
        XCTAssertEqual(expression?.month, 10)
        XCTAssertEqual(expression?.dayOfMonth, 20)
    }

    func testDurationExtraction() {
        let expression = RuleBasedIntentParser.extractTime("明天下午三点开会一个小时")
        XCTAssertEqual(expression?.durationMinutes, 60)
    }

    func testChineseNumerals() {
        XCTAssertEqual(RuleBasedIntentParser.cnNumber("三"), 3)
        XCTAssertEqual(RuleBasedIntentParser.cnNumber("十"), 10)
        XCTAssertEqual(RuleBasedIntentParser.cnNumber("十一"), 11)
        XCTAssertEqual(RuleBasedIntentParser.cnNumber("二十"), 20)
        XCTAssertEqual(RuleBasedIntentParser.cnNumber("两"), 2)
        XCTAssertNil(RuleBasedIntentParser.cnNumber("百"))
    }
}

final class IntentParserTests: XCTestCase {

    private let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        return calendar
    }()

    private var user: User {
        User(name: "测试", timezoneIdentifier: "Asia/Shanghai")
    }

    private var now: Date {
        calendar.date(from: DateComponents(year: 2026, month: 10, day: 14, hour: 10))!
    }

    func testLLMPathDecodesStructuredIntent() async throws {
        let json = """
        {"intent":"add_event","title":"开组会","time":{"day":"tomorrow","period":"afternoon","hour":3},"confidence":0.95}
        """
        let parser = IntentParser(client: StaticLLMClient(response: json))
        let result = await parser.parse("明天下午三点开组会", user: user, now: now)

        XCTAssertTrue(result.usedLLM)
        XCTAssertEqual(result.intent.kind, .addEvent)
        XCTAssertEqual(result.intent.title, "开组会")
        XCTAssertEqual(result.intent.time?.hour, 3)
        XCTAssertFalse(result.intent.degraded)
    }

    /// Models love wrapping JSON in code fences.
    func testCodeFencedJSONIsStillParsed() async throws {
        let fenced = """
        ```json
        {"intent":"report_state","state":"tired","confidence":0.9}
        ```
        """
        let parser = IntentParser(client: StaticLLMClient(response: fenced))
        let result = await parser.parse("好累", user: user, now: now)

        XCTAssertTrue(result.usedLLM)
        XCTAssertEqual(result.intent.state, .tired)
    }

    func testTransportFailureFallsBackToRules() async throws {
        let parser = IntentParser(client: StaticLLMClient(error: .transport("boom")))
        let result = await parser.parse("我现在很累", user: user, now: now)

        XCTAssertFalse(result.usedLLM)
        XCTAssertEqual(result.intent.state, .tired)
        XCTAssertNotNil(result.failure)
    }

    func testMalformedOutputFallsBackToRules() async throws {
        let parser = IntentParser(client: StaticLLMClient(response: "抱歉，我不明白"))
        let result = await parser.parse("明天下午三点开会", user: user, now: now)

        XCTAssertFalse(result.usedLLM)
        XCTAssertEqual(result.intent.kind, .addEvent)
    }

    func testLowConfidenceUnknownFallsBack() async throws {
        let json = #"{"intent":"unknown","confidence":0.2}"#
        let parser = IntentParser(client: StaticLLMClient(response: json))
        let result = await parser.parse("明天下午三点开会", user: user, now: now)

        XCTAssertFalse(result.usedLLM)
        XCTAssertEqual(result.intent.kind, .addEvent)
    }

    func testNoClientUsesRules() async throws {
        let parser = IntentParser(client: nil)
        let result = await parser.parse("明天下午三点开会", user: user, now: now)

        XCTAssertFalse(parser.isOnline)
        XCTAssertFalse(result.usedLLM)
        XCTAssertEqual(result.intent.kind, .addEvent)
    }

    /// The whole point of the split: the model supplies semantics, the resolver
    /// supplies the calendar arithmetic. Feed one into the other end to end.
    func testModelOutputResolvesToAConcreteSlot() async throws {
        let json = """
        {"intent":"add_event","title":"开组会","time":{"day":"tomorrow","period":"afternoon","hour":3},"confidence":0.95}
        """
        let parser = IntentParser(client: StaticLLMClient(response: json))
        let result = await parser.parse("明天下午三点开组会", user: user, now: now)

        let expression = try XCTUnwrap(result.intent.time)
        let resolution = try XCTUnwrap(
            TimeResolver().resolve(expression, now: now, calendar: calendar)
        )
        let components = calendar.dateComponents([.month, .day, .hour], from: resolution.interval.start)
        XCTAssertEqual(components.month, 10)
        XCTAssertEqual(components.day, 15)
        XCTAssertEqual(components.hour, 15)
    }
}
