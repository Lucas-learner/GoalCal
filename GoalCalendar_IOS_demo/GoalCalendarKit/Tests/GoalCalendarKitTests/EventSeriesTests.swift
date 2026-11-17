import XCTest
@testable import GoalCalendarKit

final class EventSeriesTests: XCTestCase {

    private let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        calendar.firstWeekday = 2
        return calendar
    }()

    private func date(_ day: Int, _ hour: Int, _ minute: Int = 0) -> Date {
        calendar.date(from: DateComponents(year: 2026, month: 10, day: day, hour: hour, minute: minute))!
    }

    private let monday = 1
    private let wednesday = 3

    func testTwoWeekdaysForFourWeeksProducesEightOccurrences() {
        // 2026-10-19 is a Monday.
        let events = EventSeries.expand(
            userID: "u",
            title: "小组站会",
            start: date(19, 9),
            end: date(19, 11),
            weekdays: [monday, wednesday],
            weeks: 4,
            calendar: calendar
        )

        XCTAssertEqual(events.count, 8)
        XCTAssertTrue(events.allSatisfy { $0.status == .confirmed })
        XCTAssertTrue(events.allSatisfy { $0.source == .manual })
        XCTAssertEqual(events.first?.startAt, date(19, 9))
        // Week 4's Wednesday is 2026-11-11.
        let lastExpected = calendar.date(from: DateComponents(year: 2026, month: 11, day: 11, hour: 9))!
        XCTAssertEqual(events.last?.startAt, lastExpected)
    }

    func testEachOccurrenceKeepsWallClockTimeAndDuration() {
        let events = EventSeries.expand(
            userID: "u", title: "课",
            start: date(19, 14, 30), end: date(19, 16),
            weekdays: [monday], weeks: 3, calendar: calendar
        )

        for event in events {
            let components = calendar.dateComponents([.hour, .minute], from: event.startAt)
            XCTAssertEqual(components.hour, 14)
            XCTAssertEqual(components.minute, 30)
            XCTAssertEqual(event.endAt.timeIntervalSince(event.startAt), 90 * 60, accuracy: 1)
        }
    }

    /// Days earlier in the first week than the chosen start are not part of it.
    func testOccurrencesBeforeTheStartAreSkipped() {
        // Start on a Wednesday, but include Monday: week 1's Monday is dropped.
        let events = EventSeries.expand(
            userID: "u", title: "课",
            start: date(21, 9), end: date(21, 10),
            weekdays: [monday, wednesday], weeks: 4, calendar: calendar
        )

        XCTAssertEqual(events.count, 7, "1 in week 1 + 2 in each of weeks 2–4")
        XCTAssertEqual(events.first?.startAt, date(21, 9))
    }

    func testSingleWeekdayOverManyWeeks() {
        let events = EventSeries.expand(
            userID: "u", title: "网球",
            start: date(20, 19), end: date(20, 21),
            weekdays: [2], weeks: 12, calendar: calendar
        )

        XCTAssertEqual(events.count, 12)
        // Every occurrence is a Tuesday.
        for event in events {
            let sundayBased = calendar.component(.weekday, from: event.startAt)
            XCTAssertEqual((sundayBased + 5) % 7 + 1, 2)
        }
    }

    func testInvalidInputsProduceNothing() {
        XCTAssertTrue(EventSeries.expand(userID: "u", title: "x", start: date(19, 9), end: date(19, 8),
                                         weekdays: [monday], weeks: 2, calendar: calendar).isEmpty)
        XCTAssertTrue(EventSeries.expand(userID: "u", title: "x", start: date(19, 9), end: date(19, 10),
                                         weekdays: [monday], weeks: 0, calendar: calendar).isEmpty)
        XCTAssertTrue(EventSeries.expand(userID: "u", title: "x", start: date(19, 9), end: date(19, 10),
                                         weekdays: [], weeks: 3, calendar: calendar).isEmpty)
    }

    /// A typo like 520 weeks must not flood the calendar.
    func testOccurrenceCap() {
        let events = EventSeries.expand(
            userID: "u", title: "长跑",
            start: date(19, 7), end: date(19, 8),
            weekdays: [1, 2, 3, 4, 5, 6, 7], weeks: 100, calendar: calendar
        )
        XCTAssertEqual(events.count, EventSeries.maxOccurrences)
    }

    func testSeriesSurvivesTheDatabaseBatchInsert() throws {
        let database = try Database.inMemory()
        let user = try database.upsertUser(User(name: "测试"))

        let events = EventSeries.expand(
            userID: user.id, title: "值班",
            start: date(19, 9), end: date(19, 18),
            weekdays: [monday], weeks: 4, calendar: calendar
        )
        try database.insertEvents(events)

        let windowStart = date(18, 0)
        let windowEnd = calendar.date(byAdding: .day, value: 40, to: windowStart)!
        let stored = try database.events(userID: user.id, from: windowStart, to: windowEnd)
        XCTAssertEqual(stored.count, 4)
        XCTAssertTrue(stored.allSatisfy { $0.title == "值班" })
    }
}
