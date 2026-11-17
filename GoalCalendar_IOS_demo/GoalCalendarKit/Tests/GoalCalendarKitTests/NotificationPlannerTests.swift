import XCTest
@testable import GoalCalendarKit

final class NotificationPlannerTests: XCTestCase {

    private let user = User(
        name: "测试",
        timezoneIdentifier: "Asia/Shanghai",
        workStartMinute: 9 * 60,
        workEndMinute: 22 * 60
    )

    private var calendar: Calendar { user.calendar }

    private var now: Date {
        calendar.date(from: DateComponents(year: 2026, month: 10, day: 18, hour: 20))!
    }

    private func event(
        _ title: String,
        day: Int = 19,
        hour: Int,
        minute: Int = 0,
        durationMinutes: Int = 45,
        status: EventStatus = .confirmed
    ) -> CalendarEvent {
        let start = calendar.date(from: DateComponents(year: 2026, month: 10, day: day, hour: hour, minute: minute))!
        return CalendarEvent(
            userID: user.id,
            title: title,
            startAt: start,
            endAt: start.addingTimeInterval(TimeInterval(durationMinutes * 60)),
            status: status
        )
    }

    func testConfirmedFutureEventIsScheduledWithLeadTime() throws {
        let plan = NotificationPlanner.plan(events: [event("开会", hour: 10)], now: now, user: user)
        let item = try XCTUnwrap(plan.first)

        XCTAssertEqual(plan.count, 1)
        XCTAssertEqual(item.title, "开会")
        XCTAssertEqual(item.identifier, "event.\(plan[0].eventID)")
        // 5 minutes before 10:00 is 09:55.
        let components = calendar.dateComponents([.hour, .minute], from: item.fireDate)
        XCTAssertEqual(components.hour, 9)
        XCTAssertEqual(components.minute, 55)
    }

    func testOnlyConfirmedEventsAreScheduled() {
        let events = [
            event("确认的", hour: 10, status: .confirmed),
            event("建议的", hour: 11, status: .proposed),
            event("完成的", hour: 12, status: .done),
            event("取消的", hour: 13, status: .cancelled)
        ]
        let plan = NotificationPlanner.plan(events: events, now: now, user: user)
        XCTAssertEqual(plan.map(\.title), ["确认的"])
    }

    func testPastEventsAreSkipped() {
        // 08:00 today is already behind us at 20:00.
        let plan = NotificationPlanner.plan(events: [event("早上的会", day: 18, hour: 8)], now: now, user: user)
        XCTAssertTrue(plan.isEmpty)
    }

    func testEventTooCloseForLeadTimeIsSkipped() {
        // Fires at 19:55, but it is already 20:00.
        let plan = NotificationPlanner.plan(events: [event("马上开始", day: 18, hour: 20, minute: 5)], now: now, user: user)
        XCTAssertTrue(plan.isEmpty)
    }

    func testEventsBeyondHorizonAreSkipped() {
        let farFuture = calendar.date(byAdding: .day, value: 60, to: now)!
        let event = CalendarEvent(
            userID: user.id,
            title: "太远了",
            startAt: farFuture,
            endAt: farFuture.addingTimeInterval(1800)
        )
        let plan = NotificationPlanner.plan(events: [event], now: now, user: user, horizonDays: 30)
        XCTAssertTrue(plan.isEmpty)
    }

    /// iOS keeps only the 64 soonest; a 65th would silently never fire.
    func testPlanIsCappedAtSixtyFourKeepingTheSoonest() throws {
        let events = (0..<70).map { index in
            let start = calendar.date(byAdding: .minute, value: 60 + index * 30, to: now)!
            return CalendarEvent(
                userID: user.id,
                title: "第 \(index) 条",
                startAt: start,
                endAt: start.addingTimeInterval(1800)
            )
        }

        let plan = NotificationPlanner.plan(events: events, now: now, user: user)
        XCTAssertEqual(plan.count, NotificationPlanner.maxPendingRequests)
        XCTAssertEqual(plan.map(\.fireDate), plan.map(\.fireDate).sorted(), "must stay chronological")

        let newest = try XCTUnwrap(plan.last)
        let dropped = events.filter { $0.startAt > newest.fireDate.addingTimeInterval(600) }
        XCTAssertFalse(dropped.isEmpty, "the latest events should have been dropped, not the earliest")
    }

    func testBodyMentionsTimeAndDuration() throws {
        let plan = NotificationPlanner.plan(events: [event("写周报", hour: 15, durationMinutes: 90)], now: now, user: user)
        let body = try XCTUnwrap(plan.first).body
        XCTAssertTrue(body.contains("15:00"), body)
        XCTAssertTrue(body.contains("16:30"), body)
        XCTAssertTrue(body.contains("1 小时 30 分钟"), body)
    }

    func testIdentifiersAreUniqueAndStable() {
        let events = [event("A", hour: 10), event("B", hour: 11)]
        let first = NotificationPlanner.plan(events: events, now: now, user: user)
        let second = NotificationPlanner.plan(events: events, now: now, user: user)

        XCTAssertEqual(Set(first.map(\.identifier)).count, 2)
        XCTAssertEqual(first.map(\.identifier), second.map(\.identifier))
    }
}
