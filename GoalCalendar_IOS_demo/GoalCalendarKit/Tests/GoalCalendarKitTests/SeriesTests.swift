import XCTest
@testable import GoalCalendarKit

final class SeriesMatcherTests: XCTestCase {

    private let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        calendar.firstWeekday = 2
        return calendar
    }()

    private func date(_ day: Int, _ hour: Int) -> Date {
        calendar.date(from: DateComponents(year: 2026, month: 10, day: day, hour: hour))!
    }

    /// 2026-10-22 is a Thursday.
    private func thursdaySeries(title: String = "开会", seriesID: String = "S1") -> [CalendarEvent] {
        EventSeries.expand(
            userID: "u", title: title,
            start: date(22, 9), end: date(22, 10),
            weekdays: [4], weeks: 4, seriesID: seriesID, calendar: calendar
        )
    }

    func testFindsSeriesByWeekdayHourAndTitle() throws {
        let candidate = try XCTUnwrap(SeriesMatcher.bestSeries(
            in: thursdaySeries(), title: "开会", weekday: 4, hour: 9, calendar: calendar
        ))
        XCTAssertEqual(candidate.seriesID, "S1")
        XCTAssertEqual(candidate.events.count, 4)
    }

    func testTitleMismatchRejectsTheSeries() {
        XCTAssertNil(SeriesMatcher.bestSeries(
            in: thursdaySeries(title: "开会"), title: "健身", weekday: 4, hour: 9, calendar: calendar
        ))
    }

    func testWeekdayMismatchRejectsTheSeries() {
        XCTAssertNil(SeriesMatcher.bestSeries(
            in: thursdaySeries(), title: "开会", weekday: 5, hour: 9, calendar: calendar
        ))
    }

    func testHourMismatchRejectsTheSeries() {
        XCTAssertNil(SeriesMatcher.bestSeries(
            in: thursdaySeries(), title: "开会", weekday: 4, hour: 15, calendar: calendar
        ))
    }

    func testOneOffEventsAreNotCandidates() {
        let single = [CalendarEvent(userID: "u", title: "开会", startAt: date(22, 9), endAt: date(22, 10))]
        XCTAssertNil(SeriesMatcher.bestSeries(in: single, title: "开会", weekday: 4, hour: 9, calendar: calendar))
    }

    func testNoCluesMeansNoMatch() {
        XCTAssertNil(SeriesMatcher.bestSeries(in: thursdaySeries(), title: nil, weekday: nil, hour: nil, calendar: calendar))
    }

    func testPicksTheBetterScoringSeries() throws {
        let thursday = thursdaySeries(title: "开会", seriesID: "THU")
        let friday = EventSeries.expand(
            userID: "u", title: "开会",
            start: date(23, 9), end: date(23, 10),
            weekdays: [5], weeks: 4, seriesID: "FRI", calendar: calendar
        )

        let candidate = try XCTUnwrap(SeriesMatcher.bestSeries(
            in: thursday + friday, title: "开会", weekday: 4, hour: 9, calendar: calendar
        ))
        XCTAssertEqual(candidate.seriesID, "THU")
    }
}

final class SeriesEditorTests: XCTestCase {

    private let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        calendar.firstWeekday = 2
        return calendar
    }()

    private func date(_ day: Int, _ hour: Int, _ minute: Int = 0) -> Date {
        calendar.date(from: DateComponents(year: 2026, month: 10, day: day, hour: hour, minute: minute))!
    }

    private func thursdaySeries() -> [CalendarEvent] {
        EventSeries.expand(
            userID: "u", title: "开会",
            start: date(22, 9), end: date(22, 10),
            weekdays: [4], weeks: 4, seriesID: "S1", calendar: calendar
        )
    }

    func testMovesEveryOccurrenceToTheNewTimeKeepingTheDay() throws {
        let changes = SeriesEditor.reschedule(
            events: thursdaySeries(), hour: 14, minute: 0, weekday: nil, durationMinutes: 60, calendar: calendar
        )

        XCTAssertEqual(changes.count, 4)
        for change in changes {
            let oldDay = calendar.startOfDay(for: change.oldStart)
            let newDay = calendar.startOfDay(for: change.newStart)
            XCTAssertEqual(oldDay, newDay, "the day must not move when only the hour changes")

            let components = calendar.dateComponents([.hour, .minute], from: change.newStart)
            XCTAssertEqual(components.hour, 14)
            XCTAssertEqual(components.minute, 0)
            XCTAssertEqual(change.newEnd.timeIntervalSince(change.newStart), 60 * 60, accuracy: 1)
        }
    }

    func testDurationCanChange() throws {
        let changes = SeriesEditor.reschedule(
            events: thursdaySeries(), hour: 9, minute: 0, weekday: nil, durationMinutes: 30, calendar: calendar
        )
        XCTAssertEqual(changes.count, 4)
        XCTAssertEqual(changes[0].newEnd.timeIntervalSince(changes[0].newStart), 30 * 60, accuracy: 1)
    }

    func testWeekdayCanChange() throws {
        let changes = SeriesEditor.reschedule(
            events: thursdaySeries(), hour: nil, minute: nil, weekday: 5, durationMinutes: nil, calendar: calendar
        )
        XCTAssertEqual(changes.count, 4)
        for change in changes {
            XCTAssertEqual(SeriesMatcher.mondayBasedWeekday(change.newStart, calendar: calendar), 5)
            XCTAssertEqual(calendar.component(.hour, from: change.newStart), 9, "time unchanged")
        }
    }

    func testNoChangeProducesNoChanges() throws {
        let changes = SeriesEditor.reschedule(
            events: thursdaySeries(), hour: 9, minute: 0, weekday: nil, durationMinutes: 60, calendar: calendar
        )
        XCTAssertTrue(changes.isEmpty)
    }
}

final class SeriesPersistenceTests: XCTestCase {

    private let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        return calendar
    }()

    private func date(_ day: Int, _ hour: Int) -> Date {
        calendar.date(from: DateComponents(year: 2026, month: 10, day: day, hour: hour))!
    }

    private func seed() throws -> (Database, User, [CalendarEvent]) {
        let database = try Database.inMemory()
        let user = try database.upsertUser(User(name: "测试", timezoneIdentifier: "Asia/Shanghai"))
        let events = EventSeries.expand(
            userID: user.id, title: "开会",
            start: date(22, 9), end: date(22, 10),
            weekdays: [4], weeks: 4, calendar: calendar
        )
        try database.insertEvents(events)
        return (database, user, events)
    }

    func testSeriesIDRoundTrips() throws {
        let (database, user, events) = try seed()
        let seriesID = try XCTUnwrap(events.first?.seriesID)

        let stored = try database.events(seriesID: seriesID)
        XCTAssertEqual(stored.count, 4)
        XCTAssertTrue(stored.allSatisfy { $0.seriesID == seriesID })

        let oneOff = try database.events(userID: user.id, from: date(21, 0), to: date(23, 0))
        XCTAssertTrue(oneOff.allSatisfy { $0.seriesID == seriesID })
    }

    func testDeleteSeriesRemovesEveryOccurrence() throws {
        let (database, user, events) = try seed()
        let seriesID = try XCTUnwrap(events.first?.seriesID)

        let removed = try database.deleteSeries(id: seriesID)
        XCTAssertEqual(removed, 4)
        XCTAssertTrue(try database.events(seriesID: seriesID).isEmpty)

        let remaining = try database.events(userID: user.id, from: date(21, 0), to: date(30, 0))
        XCTAssertTrue(remaining.isEmpty)
    }

    func testBatchTimeUpdateIsApplied() throws {
        let (database, _, events) = try seed()
        let updates = events.map {
            EventTimeUpdate(id: $0.id, startAt: $0.startAt.addingTimeInterval(5 * 3600),
                            endAt: $0.endAt.addingTimeInterval(5 * 3600))
        }
        try database.updateEventTimes(updates)

        let seriesID = try XCTUnwrap(events.first?.seriesID)
        let stored = try database.events(seriesID: seriesID)
        XCTAssertEqual(stored.count, 4)
        XCTAssertTrue(stored.allSatisfy { calendar.component(.hour, from: $0.startAt) == 14 })
    }
}

final class SeriesChatTests: XCTestCase {

    private let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        calendar.firstWeekday = 2
        return calendar
    }()

    private var database: Database!
    private var user: User!

    private var now: Date {
        calendar.date(from: DateComponents(year: 2026, month: 10, day: 19, hour: 8))!
    }

    override func setUpWithError() throws {
        database = try Database.inMemory()
        user = try database.upsertUser(User(name: "测试", timezoneIdentifier: "Asia/Shanghai"))

        let start = calendar.date(from: DateComponents(year: 2026, month: 10, day: 22, hour: 9))!
        let events = EventSeries.expand(
            userID: user.id, title: "开会",
            start: start, end: start.addingTimeInterval(3600),
            weekdays: [4], weeks: 4, calendar: calendar
        )
        try database.insertEvents(events)
    }

    private func orchestrator(intentJSON: String) -> ChatOrchestrator {
        ChatOrchestrator(
            database: database,
            intentParser: IntentParser(client: StaticLLMClient(response: intentJSON)),
            planner: GoalPlanner(database: database, parser: GoalBreakdownParser(client: nil))
        )
    }

    func testRescheduleProposalChangesNothingUntilConfirmed() async throws {
        let json = #"{"intent":"reschedule_series","series_match":{"weekday":4,"hour":9,"title":"开会"},"series_target":{"hour":14,"minute":0,"duration_minutes":60},"confidence":0.9}"#
        let chat = orchestrator(intentJSON: json)

        let turn = await chat.handle("将每周四上午9-10开会全部更改到下午14-15点", user: user, now: now)
        let proposal = try XCTUnwrap(turn.proposal)
        guard case let .rescheduleSeries(_, changes) = proposal.kind else {
            return XCTFail("expected rescheduleSeries")
        }
        XCTAssertEqual(changes.count, 4)

        // Still 09:00 everywhere.
        let window = try database.events(userID: user.id, from: now, to: now.addingTimeInterval(90 * 86_400))
        let seriesID = try XCTUnwrap(window.first?.seriesID)
        let before = try database.events(seriesID: seriesID)
        XCTAssertTrue(before.allSatisfy { calendar.component(.hour, from: $0.startAt) == 9 })

        _ = try chat.commit(proposal, user: user, now: now)

        let after = try database.events(seriesID: seriesID)
        XCTAssertEqual(after.count, 4)
        XCTAssertTrue(after.allSatisfy { calendar.component(.hour, from: $0.startAt) == 14 })
    }

    func testDeleteSeriesProposalThenCommit() async throws {
        let json = #"{"intent":"delete_series","series_match":{"weekday":4,"title":"开会"},"confidence":0.9}"#
        let chat = orchestrator(intentJSON: json)

        let turn = await chat.handle("把每周四的会全部删掉", user: user, now: now)
        let proposal = try XCTUnwrap(turn.proposal)

        let before = try database.events(userID: user.id, from: now, to: now.addingTimeInterval(90 * 86_400))
        XCTAssertEqual(before.count, 4, "nothing deleted before confirmation")

        _ = try chat.commit(proposal, user: user, now: now)
        let after = try database.events(userID: user.id, from: now, to: now.addingTimeInterval(90 * 86_400))
        XCTAssertTrue(after.isEmpty)
    }

    func testUnmatchedSeriesAsksForClarity() async throws {
        let json = #"{"intent":"reschedule_series","series_match":{"weekday":2,"hour":15,"title":"健身"},"series_target":{"hour":16},"confidence":0.9}"#
        let chat = orchestrator(intentJSON: json)

        let turn = await chat.handle("把健身改到 16 点", user: user, now: now)
        XCTAssertNil(turn.proposal)
        XCTAssertTrue(turn.reply.contains("没找到"), turn.reply)
    }
}

final class SeriesScopeTests: XCTestCase {

    private let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        calendar.firstWeekday = 2
        return calendar
    }()

    private func date(_ day: Int, _ hour: Int) -> Date {
        calendar.date(from: DateComponents(year: 2026, month: 10, day: day, hour: hour))!
    }

    private func series() -> [CalendarEvent] {
        EventSeries.expand(
            userID: "u", title: "开会",
            start: date(22, 9), end: date(22, 10),
            weekdays: [4], weeks: 4, seriesID: "S1", calendar: calendar
        )
    }

    func testOnlyThisSelectsOneOccurrence() throws {
        let all = series()
        let pivot = all[1]
        let selected = SeriesScopeFilter.events(all, scope: .onlyThis, pivot: pivot)
        XCTAssertEqual(selected.count, 1)
        XCTAssertEqual(selected.first?.id, pivot.id)
    }

    func testThisAndFutureIncludesThePivotAndLater() throws {
        let all = series()
        let pivot = all[2]
        let selected = SeriesScopeFilter.events(all, scope: .thisAndFuture, pivot: pivot)
        XCTAssertEqual(selected.count, 2, "the third and fourth occurrences")
        XCTAssertTrue(selected.allSatisfy { $0.startAt >= pivot.startAt })
    }

    func testAllSelectsEverything() throws {
        let all = series()
        let selected = SeriesScopeFilter.events(all, scope: .all, pivot: all[0])
        XCTAssertEqual(selected.count, 4)
    }

    func testDeltasForASameDayTimeChange() {
        let deltas = SeriesEditor.deltas(from: date(22, 9), to: date(22, 14), calendar: calendar)
        XCTAssertEqual(deltas.days, 0)
        XCTAssertEqual(deltas.minutes, 5 * 60)
    }

    func testDeltasForADayChange() {
        let deltas = SeriesEditor.deltas(from: date(22, 9), to: date(23, 9), calendar: calendar)
        XCTAssertEqual(deltas.days, 1)
        XCTAssertEqual(deltas.minutes, 0)
    }

    func testShiftKeepsWallClockTimeAcrossOccurrences() {
        let updates = SeriesEditor.shift(
            events: series(), byDays: 1, byMinutes: 5 * 60, durationMinutes: nil, calendar: calendar
        )
        XCTAssertEqual(updates.count, 4)

        for update in updates {
            let components = calendar.dateComponents([.hour, .minute], from: update.startAt)
            XCTAssertEqual(components.hour, 14)
            XCTAssertEqual(components.minute, 0)
            XCTAssertEqual(update.endAt.timeIntervalSince(update.startAt), 3600, accuracy: 1)
        }
    }

    func testShiftCanOverrideDuration() {
        let updates = SeriesEditor.shift(
            events: series(), byDays: 0, byMinutes: 0, durationMinutes: 30, calendar: calendar
        )
        XCTAssertEqual(updates.count, 4)
        XCTAssertEqual(updates[0].endAt.timeIntervalSince(updates[0].startAt), 1800, accuracy: 1)
    }

    func testZeroShiftProducesNoUpdates() {
        let updates = SeriesEditor.shift(
            events: series(), byDays: 0, byMinutes: 0, durationMinutes: nil, calendar: calendar
        )
        XCTAssertTrue(updates.isEmpty)
    }
}
