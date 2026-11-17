import XCTest
@testable import GoalCalendarKit

/// 2026-10-14 is a Wednesday in Asia/Shanghai — the fixtures lean on that.
final class TimeResolverTests: XCTestCase {

    private let resolver = TimeResolver()

    private let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        calendar.firstWeekday = 2
        return calendar
    }()

    private var now: Date { date(2026, 10, 14, 10) }

    private func date(_ year: Int, _ month: Int, _ day: Int, _ hour: Int, _ minute: Int = 0) -> Date {
        calendar.date(from: DateComponents(year: year, month: month, day: day, hour: hour, minute: minute))!
    }

    private func assertDate(
        _ value: Date,
        _ year: Int, _ month: Int, _ day: Int, _ hour: Int, _ minute: Int = 0,
        file: StaticString = #filePath, line: UInt = #line
    ) {
        let components = calendar.dateComponents([.year, .month, .day, .hour, .minute], from: value)
        XCTAssertEqual(components.year, year, "year", file: file, line: line)
        XCTAssertEqual(components.month, month, "month", file: file, line: line)
        XCTAssertEqual(components.day, day, "day", file: file, line: line)
        XCTAssertEqual(components.hour, hour, "hour", file: file, line: line)
        XCTAssertEqual(components.minute, minute, "minute", file: file, line: line)
    }

    private func resolve(_ expression: TimeExpression, at moment: Date? = nil) -> TimeResolution? {
        resolver.resolve(expression, now: moment ?? now, calendar: calendar)
    }

    // MARK: - Day + period + hour

    func testTomorrowAfternoonThree() throws {
        let resolution = try XCTUnwrap(resolve(TimeExpression(day: .tomorrow, period: .afternoon, hour: 3)))
        assertDate(resolution.interval.start, 2026, 10, 15, 15)
    }

    func testDayAfterTomorrowMorningHalfPastNine() throws {
        let resolution = try XCTUnwrap(resolve(TimeExpression(day: .dayAfterTomorrow, period: .morning, hour: 9, minute: 30)))
        assertDate(resolution.interval.start, 2026, 10, 16, 9, 30)
    }

    func testEveningEightBecomesTwenty() throws {
        let resolution = try XCTUnwrap(resolve(TimeExpression(day: .today, period: .evening, hour: 8)))
        assertDate(resolution.interval.start, 2026, 10, 14, 20)
    }

    /// Bare "3点" in speech means 15:00; early morning is signalled explicitly.
    func testBareEarlyHourIsAssumedAfternoon() throws {
        let resolution = try XCTUnwrap(resolve(TimeExpression(hour: 3)))
        assertDate(resolution.interval.start, 2026, 10, 14, 15)
    }

    func testNightTwoStaysEarlyMorning() throws {
        let resolution = try XCTUnwrap(resolve(TimeExpression(day: .tomorrow, period: .night, hour: 2)))
        assertDate(resolution.interval.start, 2026, 10, 15, 2)
    }

    func testEveningOneIsStillEarlyMorning() throws {
        let resolution = try XCTUnwrap(resolve(TimeExpression(day: .tomorrow, period: .evening, hour: 1)))
        assertDate(resolution.interval.start, 2026, 10, 15, 1)
    }

    func testMidnightRollsToNextDay() throws {
        let resolution = try XCTUnwrap(resolve(TimeExpression(day: .tomorrow, period: .evening, hour: 12)))
        assertDate(resolution.interval.start, 2026, 10, 16, 0)
    }

    func testAfternoonTwoIsFourteen() throws {
        let resolution = try XCTUnwrap(resolve(TimeExpression(day: .tomorrow, period: .afternoon, hour: 2)))
        assertDate(resolution.interval.start, 2026, 10, 15, 14)
    }

    func testNoonWithoutHour() throws {
        let resolution = try XCTUnwrap(resolve(TimeExpression(day: .tomorrow, period: .noon)))
        assertDate(resolution.interval.start, 2026, 10, 15, 12)
    }

    func testAlreadyResolvedTwentyFourHourValueIsNotShifted() throws {
        let resolution = try XCTUnwrap(resolve(TimeExpression(day: .tomorrow, period: .afternoon, hour: 15)))
        assertDate(resolution.interval.start, 2026, 10, 15, 15)
    }

    // MARK: - Weekdays (the arithmetic the LLM is forbidden from doing)

    func testSameWeekdayIsToday() throws {
        let resolution = try XCTUnwrap(resolve(TimeExpression(day: .weekday, weekday: 3, weekOffset: 0, period: .morning, hour: 9)))
        assertDate(resolution.interval.start, 2026, 10, 14, 9)
    }

    func testUpcomingSaturday() throws {
        let resolution = try XCTUnwrap(resolve(TimeExpression(day: .weekday, weekday: 6, weekOffset: 0, period: .morning, hour: 9)))
        assertDate(resolution.interval.start, 2026, 10, 17, 9)
    }

    func testNextMondaySkipsAhead() throws {
        let resolution = try XCTUnwrap(resolve(TimeExpression(day: .weekday, weekday: 1, weekOffset: 0, period: .morning, hour: 9)))
        assertDate(resolution.interval.start, 2026, 10, 19, 9)
    }

    func testNextWeekIsSevenDaysLater() throws {
        let thisWeek = try XCTUnwrap(resolve(TimeExpression(day: .weekday, weekday: 3, weekOffset: 0, period: .morning, hour: 9)))
        let nextWeek = try XCTUnwrap(resolve(TimeExpression(day: .weekday, weekday: 3, weekOffset: 1, period: .morning, hour: 9)))
        XCTAssertEqual(
            nextWeek.interval.start.timeIntervalSince(thisWeek.interval.start),
            7 * 86_400,
            accuracy: 1
        )
    }

    /// Regression: from a Sunday, "下周三" is three days out. Computing a raw
    /// forward delta lands a week too late.
    func testNextWednesdayFromSunday() throws {
        let sunday = date(2026, 10, 18, 10)
        let resolution = try XCTUnwrap(
            resolve(TimeExpression(day: .weekday, weekday: 3, weekOffset: 1, period: .morning, hour: 9), at: sunday)
        )
        assertDate(resolution.interval.start, 2026, 10, 21, 9)
    }

    func testThisFridayFromSunday() throws {
        let sunday = date(2026, 10, 18, 10)
        let resolution = try XCTUnwrap(
            resolve(TimeExpression(day: .weekday, weekday: 5, weekOffset: 0, period: .morning, hour: 9), at: sunday)
        )
        assertDate(resolution.interval.start, 2026, 10, 23, 9)
    }

    func testTodayItselfMatchesItsWeekday() throws {
        let sunday = date(2026, 10, 18, 10)
        let resolution = try XCTUnwrap(
            resolve(TimeExpression(day: .weekday, weekday: 7, weekOffset: 0, period: .morning, hour: 9), at: sunday)
        )
        assertDate(resolution.interval.start, 2026, 10, 18, 9)
    }

    // MARK: - Calendar dates without a year

    func testMonthAndDayUseCurrentYear() throws {
        let resolution = try XCTUnwrap(resolve(TimeExpression(period: .morning, hour: 9, month: 10, dayOfMonth: 20)))
        assertDate(resolution.interval.start, 2026, 10, 20, 9)
    }

    func testPastMonthRollsToNextYear() throws {
        let resolution = try XCTUnwrap(resolve(TimeExpression(period: .morning, hour: 9, month: 1, dayOfMonth: 5)))
        assertDate(resolution.interval.start, 2027, 1, 5, 9)
    }

    func testExplicitISODate() throws {
        let resolution = try XCTUnwrap(resolve(TimeExpression(explicitDate: "2026-12-25", period: .morning, hour: 10)))
        assertDate(resolution.interval.start, 2026, 12, 25, 10)
    }

    // MARK: - Guard rails

    func testEmptyExpressionResolvesToNil() {
        XCTAssertNil(resolve(TimeExpression()))
    }

    /// Saying "today 3pm" at 4pm must not create an event in the past; it rolls
    /// forward and tells the user.
    func testPastTimeTodayRollsToTomorrowWithNote() throws {
        let resolution = try XCTUnwrap(
            resolve(TimeExpression(day: .today, period: .afternoon, hour: 3), at: date(2026, 10, 14, 16))
        )
        assertDate(resolution.interval.start, 2026, 10, 15, 15)
        XCTAssertFalse(resolution.notes.isEmpty)
    }

    func testDurationIsApplied() throws {
        let resolution = try XCTUnwrap(
            resolve(TimeExpression(day: .tomorrow, period: .morning, hour: 9, durationMinutes: 90))
        )
        XCTAssertEqual(resolution.interval.duration, 90 * 60, accuracy: 1)
    }

    func testDefaultDurationIsOneHour() throws {
        let resolution = try XCTUnwrap(resolve(TimeExpression(day: .tomorrow, period: .morning, hour: 9)))
        XCTAssertEqual(resolution.interval.duration, 60 * 60, accuracy: 1)
    }
}
