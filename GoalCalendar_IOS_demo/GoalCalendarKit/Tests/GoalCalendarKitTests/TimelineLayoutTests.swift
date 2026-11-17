import XCTest
@testable import GoalCalendarKit

final class TimelineLayoutTests: XCTestCase {

    private let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        return calendar
    }()

    private func at(_ hour: Int, _ minute: Int = 0) -> Date {
        calendar.date(from: DateComponents(year: 2026, month: 10, day: 19, hour: hour, minute: minute))!
    }

    private func event(_ title: String, from: Date, to: Date) -> CalendarEvent {
        CalendarEvent(userID: "u", title: title, startAt: from, endAt: to)
    }

    private var window: (start: Date, end: Date) { (at(9), at(22)) }

    private func layout(_ events: [CalendarEvent]) -> [TimelineBlock] {
        TimelineLayout.blocks(events: events, windowStart: window.start, windowEnd: window.end)
    }

    func testSingleEventFillsTheWidth() throws {
        let blocks = layout([event("独享", from: at(10), to: at(11))])
        let block = try XCTUnwrap(blocks.first)
        XCTAssertEqual(blocks.count, 1)
        XCTAssertEqual(block.column, 0)
        XCTAssertEqual(block.columnCount, 1)
    }

    func testTwoOverlappingEventsGetSeparateColumns() {
        let blocks = layout([
            event("A", from: at(10), to: at(11)),
            event("B", from: at(10, 30), to: at(11, 30))
        ])
        XCTAssertEqual(blocks.count, 2)
        XCTAssertEqual(Set(blocks.map(\.column)), [0, 1])
        XCTAssertTrue(blocks.allSatisfy { $0.columnCount == 2 })
    }

    func testSequentialEventsShareOneColumn() {
        let blocks = layout([
            event("A", from: at(10), to: at(11)),
            event("B", from: at(11), to: at(12))
        ])
        XCTAssertEqual(blocks.count, 2)
        XCTAssertTrue(blocks.allSatisfy { $0.column == 0 && $0.columnCount == 1 })
    }

    /// A column freed by an earlier event should be reused.
    func testFreedColumnIsReusedWithinACluster() {
        // A 10:00-11:00, B 10:30-11:30, C 11:00-12:00.
        // B bridges A and C, so all three form one cluster, but C can take A's
        // column because A has ended by 11:00.
        let a = CalendarEvent(id: "A", userID: "u", title: "A", startAt: at(10), endAt: at(11))
        let b = CalendarEvent(id: "B", userID: "u", title: "B", startAt: at(10, 30), endAt: at(11, 30))
        let c = CalendarEvent(id: "C", userID: "u", title: "C", startAt: at(11), endAt: at(12))

        let blocks = layout([a, b, c])
        XCTAssertEqual(blocks.count, 3)
        XCTAssertTrue(blocks.allSatisfy { $0.columnCount == 2 })

        let columnByID = Dictionary(uniqueKeysWithValues: blocks.map { ($0.eventID, $0.column) })
        XCTAssertEqual(columnByID["A"], 0)
        XCTAssertEqual(columnByID["B"], 1)
        XCTAssertEqual(columnByID["C"], 0)
    }

    func testEventsOutsideTheWindowAreDropped() {
        let blocks = layout([
            event("太早", from: at(6), to: at(8)),
            event("太晚", from: at(22), to: at(23))
        ])
        XCTAssertTrue(blocks.isEmpty)
    }

    func testEventIsClippedToTheWindow() throws {
        // Starts the night before, ends at 10:30.
        let startsYesterday = calendar.date(byAdding: .hour, value: -4, to: at(9))!
        let blocks = layout([event("跨夜", from: startsYesterday, to: at(10, 30))])
        let block = try XCTUnwrap(blocks.first)

        XCTAssertEqual(block.start, window.start, "must be clipped to the window start")
        XCTAssertEqual(block.end, at(10, 30))
    }

    func testTouchingEventsAreNotAConflict() {
        let blocks = layout([
            event("A", from: at(10), to: at(11)),
            event("B", from: at(11), to: at(12))
        ])
        XCTAssertTrue(blocks.allSatisfy { $0.columnCount == 1 })
    }

    func testEmptyInput() {
        XCTAssertTrue(layout([]).isEmpty)
    }

    func testInvalidWindowReturnsEmpty() {
        let blocks = TimelineLayout.blocks(events: [event("A", from: at(10), to: at(11))],
                                           windowStart: at(12), windowEnd: at(9))
        XCTAssertTrue(blocks.isEmpty)
    }
}
