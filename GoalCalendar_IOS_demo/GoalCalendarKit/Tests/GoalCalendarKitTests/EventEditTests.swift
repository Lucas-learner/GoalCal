import XCTest
@testable import GoalCalendarKit

final class EventEditTests: XCTestCase {

    private var database: Database!
    private var user: User!

    private let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        return calendar
    }()

    private func date(_ day: Int, _ hour: Int, _ minute: Int = 0) -> Date {
        calendar.date(from: DateComponents(year: 2026, month: 10, day: day, hour: hour, minute: minute))!
    }

    override func setUpWithError() throws {
        database = try Database.inMemory()
        user = try database.upsertUser(User(name: "测试", timezoneIdentifier: "Asia/Shanghai"))
    }

    private func makeLinkedEvent() throws -> (Goal, Step, CalendarEvent) {
        let goal = try database.insertGoal(Goal(userID: user.id, title: "学会 SwiftUI"))
        let steps = try database.insertSteps((1...4).map {
            Step(goalID: goal.id, seq: $0, title: "第 \($0) 步", percent: 0, estimatedMinutes: 45)
        })
        let event = try database.insertEvent(CalendarEvent(
            userID: user.id,
            stepID: steps[0].id,
            goalID: goal.id,
            title: steps[0].title,
            startAt: date(19, 10),
            endAt: date(19, 10, 45),
            status: .confirmed,
            source: .scheduler
        ))
        return (goal, steps[0], event)
    }

    func testMovingAnEventChangesItsTime() throws {
        let (_, _, event) = try makeLinkedEvent()

        let moved = try database.updateEvent(id: event.id, startAt: date(20, 14), endAt: date(20, 15))
        let unwrapped = try XCTUnwrap(moved)
        XCTAssertEqual(unwrapped.startAt, date(20, 14))
        XCTAssertEqual(unwrapped.endAt, date(20, 15))

        let stored = try XCTUnwrap(try database.event(id: event.id))
        XCTAssertEqual(stored.startAt, date(20, 14))
    }

    /// The whole reason to move rather than recreate.
    func testMovingPreservesStepAndGoalLinkage() throws {
        let (goal, step, event) = try makeLinkedEvent()

        let moved = try XCTUnwrap(try database.updateEvent(id: event.id, startAt: date(21, 9), endAt: date(21, 9, 45)))
        XCTAssertEqual(moved.stepID, step.id)
        XCTAssertEqual(moved.goalID, goal.id)

        // And it still advances progress when completed.
        let completion = try XCTUnwrap(try database.completeEvent(id: event.id))
        XCTAssertEqual(completion.progress?.doneSteps, 1)
    }

    func testInvalidIntervalIsRejectedAndLeavesTheRowUntouched() throws {
        let (_, _, event) = try makeLinkedEvent()

        XCTAssertThrowsError(
            try database.updateEvent(id: event.id, startAt: date(20, 15), endAt: date(20, 14))
        )
        XCTAssertThrowsError(
            try database.updateEvent(id: event.id, startAt: date(20, 15), endAt: date(20, 15))
        )

        let stored = try XCTUnwrap(try database.event(id: event.id))
        XCTAssertEqual(stored.startAt, date(19, 10), "a rejected edit must not partially apply")
    }

    func testMovingAnUnknownEventReturnsNil() throws {
        XCTAssertNil(try database.updateEvent(id: "nope", startAt: date(20, 9), endAt: date(20, 10)))
    }

    // MARK: - Conflict detection

    func testOverlapQueryFindsAConflictingEvent() throws {
        let (_, _, event) = try makeLinkedEvent()
        _ = try database.insertEvent(CalendarEvent(
            userID: user.id,
            title: "已有会议",
            startAt: date(20, 9),
            endAt: date(20, 11)
        ))

        let conflicts = try database.eventsOverlapping(
            userID: user.id,
            start: date(20, 10),
            end: date(20, 10, 45),
            excluding: event.id
        )
        XCTAssertEqual(conflicts.count, 1)
        XCTAssertEqual(conflicts[0].title, "已有会议")
    }

    func testOverlapQueryExcludesTheEventBeingEdited() throws {
        let (_, _, event) = try makeLinkedEvent()

        let conflicts = try database.eventsOverlapping(
            userID: user.id,
            start: event.startAt,
            end: event.endAt,
            excluding: event.id
        )
        XCTAssertTrue(conflicts.isEmpty, "an event must not conflict with itself")
    }

    func testProposedEventsDoNotCountAsConflicts() throws {
        _ = try database.insertEvent(CalendarEvent(
            userID: user.id,
            title: "还没确认的",
            startAt: date(20, 9),
            endAt: date(20, 11),
            status: .proposed
        ))

        let conflicts = try database.eventsOverlapping(userID: user.id, start: date(20, 10), end: date(20, 10, 45))
        XCTAssertTrue(conflicts.isEmpty)
    }

    func testTouchingBoundariesAreNotAConflict() throws {
        _ = try database.insertEvent(CalendarEvent(
            userID: user.id,
            title: "紧挨着",
            startAt: date(20, 11),
            endAt: date(20, 12)
        ))

        // Ends exactly when the other starts.
        let conflicts = try database.eventsOverlapping(userID: user.id, start: date(20, 10), end: date(20, 11))
        XCTAssertTrue(conflicts.isEmpty)
    }
}
