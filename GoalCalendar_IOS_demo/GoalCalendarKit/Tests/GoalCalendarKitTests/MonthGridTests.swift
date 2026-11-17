import XCTest
@testable import GoalCalendarKit

final class MonthGridTests: XCTestCase {

    private let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        calendar.firstWeekday = 2   // Monday
        return calendar
    }()

    private func date(_ year: Int, _ month: Int, _ day: Int, _ hour: Int = 12) -> Date {
        calendar.date(from: DateComponents(year: year, month: month, day: day, hour: hour))!
    }

    private func weekday(of date: Date) -> Int {
        (calendar.component(.weekday, from: date) + 5) % 7 + 1   // Monday = 1
    }

    /// October 2026 starts on a Thursday, so the grid must be padded by 3.
    func testOctober2026StartsWithThreeBlanks() {
        let cells = MonthGrid.cells(for: date(2026, 10, 15), calendar: calendar)
        XCTAssertEqual(cells.filter { $0.date == nil }.count, 3)
        XCTAssertEqual(cells.filter { $0.date != nil }.count, 31)

        let firstDay = try! XCTUnwrap(cells.first { $0.date != nil })
        XCTAssertEqual(firstDay.dayNumber, 1)
        XCTAssertEqual(weekday(of: firstDay.date!), 4, "1 Oct 2026 is a Thursday")
    }

    /// Every date must sit in the column matching its real weekday.
    func testEveryDateLandsUnderItsOwnWeekday() {
        for month in 1...12 {
            let cells = MonthGrid.cells(for: date(2026, month, 1), calendar: calendar)
            for (index, cell) in cells.enumerated() {
                guard let cellDate = cell.date else { continue }
                let column = index % 7 + 1
                XCTAssertEqual(column, weekday(of: cellDate),
                               "month \(month) day \(cell.dayNumber) landed in the wrong column")
            }
        }
    }

    func testFebruary2026Has28Days() {
        let cells = MonthGrid.cells(for: date(2026, 2, 10), calendar: calendar)
        XCTAssertEqual(cells.filter { $0.date != nil }.count, 28)
        XCTAssertEqual(cells.map(\.dayNumber).filter { $0 > 0 }.max(), 28)
    }

    func testLeapYearFebruaryHas29Days() {
        let cells = MonthGrid.cells(for: date(2028, 2, 10), calendar: calendar)
        XCTAssertEqual(cells.filter { $0.date != nil }.count, 29)
    }

    /// A month starting exactly on Monday needs no padding.
    func testMonthStartingOnMondayHasNoBlanks() {
        // 2026-06-01 is a Monday.
        let firstOfJune = date(2026, 6, 1)
        let cells = MonthGrid.cells(for: firstOfJune, calendar: calendar)
        XCTAssertEqual(weekday(of: firstOfJune), 1)
        XCTAssertEqual(
            cells.first?.date,
            calendar.startOfDay(for: firstOfJune),
            "first cell should be day 1, not a blank"
        )
    }

    func testCellsAreDayAligned() {
        let cells = MonthGrid.cells(for: date(2026, 10, 7), calendar: calendar)
        for cell in cells.compactMap(\.date) {
            let components = calendar.dateComponents([.hour, .minute, .second], from: cell)
            XCTAssertEqual(components.hour, 0)
            XCTAssertEqual(components.minute, 0)
            XCTAssertEqual(components.second, 0)
        }
    }
}

final class WeekGridTests: XCTestCase {

    private let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        calendar.firstWeekday = 2
        return calendar
    }()

    private func date(_ year: Int, _ month: Int, _ day: Int) -> Date {
        calendar.date(from: DateComponents(year: year, month: month, day: day, hour: 12))!
    }

    /// 2026-10-21 is a Wednesday; the week runs Mon 19 → Sun 25.
    func testMidweekResolvesToMondayThroughSunday() {
        let days = WeekGrid.days(containing: date(2026, 10, 21), calendar: calendar)
        XCTAssertEqual(days.count, 7)
        XCTAssertEqual(calendar.component(.day, from: days.first!), 19)
        XCTAssertEqual(calendar.component(.day, from: days.last!), 25)
    }

    /// From a Sunday, the week must start on the *preceding* Monday.
    func testSundayBelongsToTheWeekThatStartedSixDaysEarlier() throws {
        let days = WeekGrid.days(containing: date(2026, 10, 25), calendar: calendar)
        XCTAssertEqual(calendar.component(.day, from: try XCTUnwrap(days.first)), 19)
        XCTAssertEqual(calendar.component(.day, from: try XCTUnwrap(days.last)), 25)
    }

    func testMondayIsTheStartOfItsOwnWeek() throws {
        let days = WeekGrid.days(containing: date(2026, 10, 19), calendar: calendar)
        XCTAssertEqual(calendar.component(.day, from: try XCTUnwrap(days.first)), 19)
    }

    /// A week can span two months; the loader must cover both.
    func testWeekCanStraddleTwoMonths() throws {
        // 2026-10-01 is a Thursday, so that week starts Mon 2026-09-28.
        let days = WeekGrid.days(containing: date(2026, 10, 1), calendar: calendar)
        let first = try XCTUnwrap(days.first)
        let last = try XCTUnwrap(days.last)
        XCTAssertEqual(calendar.component(.month, from: first), 9)
        XCTAssertEqual(calendar.component(.day, from: first), 28)
        XCTAssertEqual(calendar.component(.month, from: last), 10)
        XCTAssertEqual(calendar.component(.day, from: last), 4)
    }

    func testTitleWithinOneMonth() {
        let days = WeekGrid.days(containing: date(2026, 10, 21), calendar: calendar)
        XCTAssertEqual(WeekGrid.title(for: days, calendar: calendar), "10月19日 – 25日")
    }

    func testTitleAcrossMonths() {
        let days = WeekGrid.days(containing: date(2026, 10, 1), calendar: calendar)
        XCTAssertEqual(WeekGrid.title(for: days, calendar: calendar), "9月28日 – 10月4日")
    }

    func testDaysAreDayAligned() {
        for day in WeekGrid.days(containing: date(2026, 10, 21), calendar: calendar) {
            let components = calendar.dateComponents([.hour, .minute], from: day)
            XCTAssertEqual(components.hour, 0)
            XCTAssertEqual(components.minute, 0)
        }
    }
}
