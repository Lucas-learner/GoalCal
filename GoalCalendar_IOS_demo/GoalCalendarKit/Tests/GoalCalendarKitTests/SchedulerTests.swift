import XCTest
@testable import GoalCalendarKit

final class FreeTimeTests: XCTestCase {

    private var user: User {
        User(name: "T", timezoneIdentifier: "Asia/Shanghai", workStartMinute: 9 * 60, workEndMinute: 22 * 60)
    }

    private let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        return calendar
    }()

    private func date(_ hour: Int, _ minute: Int = 0, day: Int = 14) -> Date {
        calendar.date(from: DateComponents(year: 2026, month: 10, day: day, hour: hour, minute: minute))!
    }

    private func hourMinute(_ date: Date) -> (Int, Int) {
        let components = calendar.dateComponents([.hour, .minute], from: date)
        return (components.hour ?? -1, components.minute ?? -1)
    }

    func testEmptyDayIsTheWholeWorkWindow() {
        let slots = FreeTime.slots(user: user, busy: [], from: date(0), to: date(23))
        XCTAssertEqual(slots.count, 1)
        XCTAssertEqual(hourMinute(slots[0].start).0, 9)
        XCTAssertEqual(hourMinute(slots[0].end).0, 22)
    }

    func testBusyEventSplitsTheDay() {
        let busy = [DateInterval(start: date(10), end: date(11))]
        let slots = FreeTime.slots(user: user, busy: busy, from: date(0), to: date(23))
        XCTAssertEqual(slots.count, 2)
        XCTAssertEqual(hourMinute(slots[0].start).0, 9)
        XCTAssertEqual(hourMinute(slots[0].end).0, 10)
        XCTAssertEqual(hourMinute(slots[1].start).0, 11)
        XCTAssertEqual(hourMinute(slots[1].end).0, 22)
    }

    func testOverlappingBusyEventsMerge() {
        let busy = [
            DateInterval(start: date(10), end: date(12)),
            DateInterval(start: date(11), end: date(13))
        ]
        let slots = FreeTime.slots(user: user, busy: busy, from: date(0), to: date(23))
        XCTAssertEqual(slots.count, 2)
        XCTAssertEqual(hourMinute(slots[1].start).0, 13)
    }

    func testEventOutsideWorkWindowDoesNotSplit() {
        let busy = [DateInterval(start: date(7), end: date(8))]
        let slots = FreeTime.slots(user: user, busy: busy, from: date(0), to: date(23))
        XCTAssertEqual(slots.count, 1)
    }

    func testRangeIsClipped() {
        let slots = FreeTime.slots(user: user, busy: [], from: date(13), to: date(15))
        XCTAssertEqual(slots.count, 1)
        XCTAssertEqual(hourMinute(slots[0].start).0, 13)
        XCTAssertEqual(hourMinute(slots[0].end).0, 15)
    }

    func testMultipleDays() {
        let slots = FreeTime.slots(user: user, busy: [], from: date(0, day: 14), to: date(23, day: 15))
        XCTAssertEqual(slots.count, 2)
    }
}

final class SchedulerTests: XCTestCase {

    private let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        return calendar
    }()

    private var user: User!
    private var goal: Goal!

    private var now: Date { date(2026, 10, 14, 10) }

    override func setUpWithError() throws {
        user = User(
            name: "测试",
            timezoneIdentifier: "Asia/Shanghai",
            workStartMinute: 9 * 60,
            workEndMinute: 22 * 60
        )
        goal = Goal(userID: user.id, title: "学会 SwiftUI")
    }

    private func date(_ year: Int, _ month: Int, _ day: Int, _ hour: Int, _ minute: Int = 0) -> Date {
        calendar.date(from: DateComponents(year: year, month: month, day: day, hour: hour, minute: minute))!
    }

    private func candidate(minutes: Int, seq: Int = 1) -> SchedulerCandidate {
        SchedulerCandidate(
            step: Step(goalID: goal.id, seq: seq, title: "第 \(seq) 步", percent: 10, estimatedMinutes: minutes),
            goal: goal
        )
    }

    private func makeInput(
        candidates: [SchedulerCandidate],
        busy: [CalendarEvent] = [],
        states: [StateLog] = [],
        at moment: Date? = nil,
        preferences: SchedulingPreferences = SchedulingPreferences()
    ) -> SchedulerInput {
        SchedulerInput(
            user: user,
            now: moment ?? now,
            candidates: candidates,
            busyEvents: busy,
            recentStates: states,
            preferences: preferences
        )
    }

    // MARK: - Basics

    func testSuggestionIsInsideWorkWindowOnTheGrid() throws {
        let suggestions = Scheduler().suggest(makeInput(candidates: [candidate(minutes: 45)]))
        let first = try XCTUnwrap(suggestions.first)

        XCTAssertEqual(first.end.timeIntervalSince(first.start), 45 * 60, accuracy: 1)
        XCTAssertEqual(calendar.component(.minute, from: first.start) % 30, 0)

        let hour = calendar.component(.hour, from: first.start)
        XCTAssertTrue((9..<22).contains(hour), "start hour \(hour) outside work window")
        XCTAssertLessThanOrEqual(first.end, date(2026, 10, 14, 22))
        XCTAssertGreaterThanOrEqual(first.start.timeIntervalSince(now), 30 * 60 - 1)
        XCTAssertFalse(first.reasons.isEmpty)
    }

    func testSuggestionsNeverOverlapBusyEvents() throws {
        let busy = [CalendarEvent(
            userID: user.id,
            title: "已有安排",
            startAt: date(2026, 10, 14, 10),
            endAt: date(2026, 10, 14, 12)
        )]
        let suggestions = Scheduler().suggest(makeInput(candidates: [candidate(minutes: 45)], busy: busy))
        XCTAssertFalse(suggestions.isEmpty)
        for suggestion in suggestions {
            XCTAssertFalse(suggestion.interval.intersects(busy[0].interval), "overlaps busy event")
        }
    }

    func testExactFitInAShortGap() throws {
        let busy = [
            CalendarEvent(userID: user.id, title: "a", startAt: date(2026, 10, 14, 10), endAt: date(2026, 10, 14, 11)),
            CalendarEvent(userID: user.id, title: "b", startAt: date(2026, 10, 14, 11, 30), endAt: date(2026, 10, 14, 22))
        ]
        let suggestions = Scheduler().suggest(makeInput(candidates: [candidate(minutes: 30)], busy: busy))
        let first = try XCTUnwrap(suggestions.first)
        XCTAssertEqual(first.start, date(2026, 10, 14, 11))
        XCTAssertEqual(first.end, date(2026, 10, 14, 11, 30))
    }

    func testReturnsNothingWhenTheHorizonIsFull() {
        let wall = CalendarEvent(
            userID: user.id,
            title: "满",
            startAt: date(2026, 10, 13, 0),
            endAt: date(2026, 10, 30, 0)
        )
        let suggestions = Scheduler().suggest(makeInput(candidates: [candidate(minutes: 60)], busy: [wall]))
        XCTAssertTrue(suggestions.isEmpty)
    }

    func testFallsThroughToTheNextCandidateThatFits() throws {
        let huge = candidate(minutes: 800, seq: 1)   // no single day can hold 13h20m
        let small = candidate(minutes: 30, seq: 2)
        let suggestions = Scheduler().suggest(makeInput(candidates: [huge, small]))
        let first = try XCTUnwrap(suggestions.first)
        XCTAssertEqual(first.stepID, small.step.id)
    }

    func testAlternativesAreAtLeastTwoHoursApart() {
        let suggestions = Scheduler().suggest(makeInput(candidates: [candidate(minutes: 45)]))
        XCTAssertLessThanOrEqual(suggestions.count, 3)
        for (index, a) in suggestions.enumerated() {
            for b in suggestions.dropFirst(index + 1) {
                XCTAssertGreaterThanOrEqual(abs(a.start.timeIntervalSince(b.start)), 2 * 3600)
            }
        }
    }

    // MARK: - State aware

    /// "累" should pull the next step into a morning slot instead of tonight.
    func testTiredPrefersMorning() throws {
        let evening = date(2026, 10, 13, 20)
        let state = StateLog(userID: user.id, state: .tired, createdAt: evening)

        let suggestions = Scheduler().suggest(
            makeInput(candidates: [candidate(minutes: 45)], states: [state], at: evening)
        )
        let first = try XCTUnwrap(suggestions.first)

        XCTAssertTrue(calendar.isDate(first.start, inSameDayAs: date(2026, 10, 14, 9)))
        let hour = calendar.component(.hour, from: first.start)
        XCTAssertTrue((9...11).contains(hour), "tired user got hour \(hour)")
    }

    /// "忙" should compress the wait: take today rather than next week.
    func testBusyPrefersSoonest() throws {
        let state = StateLog(userID: user.id, state: .busy, createdAt: now)
        let suggestions = Scheduler().suggest(makeInput(candidates: [candidate(minutes: 45)], states: [state]))
        let first = try XCTUnwrap(suggestions.first)
        XCTAssertTrue(calendar.isDate(first.start, inSameDayAs: now))
    }

    func testStaleStateIsIgnored() {
        let old = StateLog(userID: user.id, state: .tired, createdAt: now.addingTimeInterval(-72 * 3600))
        XCTAssertNil(Scheduler.activeState([old], now: now))
    }

    func testFreshStateIsUsed() {
        let fresh = StateLog(userID: user.id, state: .uninspired, createdAt: now.addingTimeInterval(-3600))
        XCTAssertEqual(Scheduler.activeState([fresh], now: now), .uninspired)
    }

    func testUninspiredPrefersLongerBlocks() throws {
        let state = StateLog(userID: user.id, state: .uninspired, createdAt: now)
        let suggestions = Scheduler().suggest(
            makeInput(candidates: [candidate(minutes: 60)], states: [state])
        )
        let first = try XCTUnwrap(suggestions.first)
        // The whole 09:00-22:00 day is free, so a long block is available and
        // the suggestion should sit in the daytime window.
        let hour = calendar.component(.hour, from: first.start)
        XCTAssertTrue((10...16).contains(hour), "uninspired user got hour \(hour)")
    }

    // MARK: - Reasons are code generated

    func testReasonsMentionTheRealDurationAndFreeWindow() throws {
        let suggestions = Scheduler().suggest(makeInput(candidates: [candidate(minutes: 45)]))
        let first = try XCTUnwrap(suggestions.first)
        let joined = first.reasons.joined(separator: "|")
        XCTAssertTrue(joined.contains("45"), joined)
        XCTAssertTrue(joined.contains("分钟"), joined)
    }
}
