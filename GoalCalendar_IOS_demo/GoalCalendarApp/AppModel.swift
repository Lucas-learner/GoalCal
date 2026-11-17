import Foundation
import SwiftUI

/// Owns the database handle and the data the UI renders.
///
/// Deliberately thin: it holds no scheduling or parsing logic. Those live in
/// GoalCalendarKit so they can be tested without a running app.
@MainActor
final class AppModel: ObservableObject {
    @Published private(set) var user: User?
    @Published private(set) var goals: [Goal] = []
    @Published private(set) var progressByGoal: [String: GoalProgress] = [:]
    @Published private(set) var stepsByGoal: [String: [Step]] = [:]
    @Published private(set) var eventCount: Int = 0
    @Published var errorMessage: String?
    @Published private(set) var chat: ChatViewModel?
    @Published private(set) var calendar: CalendarViewModel?
    @Published private(set) var settings: SettingsViewModel?
    let notifications = NotificationManager()

    private(set) var database: Database?

    func bootstrap() {
        do {
            let url = try Database.defaultURL()
            let database = try Database(path: url.path)
            self.database = database

            let user: User
            if let existing = try database.allUsers().first {
                user = existing
            } else {
                user = try database.upsertUser(
                    User(name: "我", timezoneIdentifier: TimeZone.current.identifier)
                )
            }
            self.user = user

            try seedDemoContentIfNeeded(database: database, user: user)
            reload()

            let chat = ChatViewModel(database: database, user: user)
            let calendar = CalendarViewModel(database: database, user: user)
            chat.onDataChanged = { [weak self] in self?.refreshAll() }
            calendar.onDataChanged = { [weak self] in self?.refreshAll() }
            self.chat = chat
            self.calendar = calendar
            self.settings = SettingsViewModel(database: database, user: user)

            Task {
                await notifications.requestAuthorizationIfNeeded()
                await syncNotifications()
            }
        } catch {
            errorMessage = String(describing: error)
        }
    }

    /// Single refresh entry point, so a write from any screen updates the others.
    func refreshAll() {
        reload()
        calendar?.reload()
        Task { await syncNotifications() }
    }

    /// Mirrors the next 30 days of confirmed events into pending notifications.
    func syncNotifications() async {
        guard let database, let user else { return }
        let now = Date()
        let horizon = now.addingTimeInterval(30 * 86_400)
        let events = (try? database.events(userID: user.id, from: now, to: horizon)) ?? []
        await notifications.reschedule(events: events, user: user, now: now)
    }

    func reload() {
        guard let database, let user else { return }
        do {
            let goals = try database.goals(userID: user.id)
            self.goals = goals

            var progress: [String: GoalProgress] = [:]
            var steps: [String: [Step]] = [:]
            for goal in goals {
                progress[goal.id] = try database.progress(goalID: goal.id)
                steps[goal.id] = try database.steps(goalID: goal.id)
            }
            self.progressByGoal = progress
            self.stepsByGoal = steps
        } catch {
            errorMessage = String(describing: error)
        }
    }

    /// Placeholder content so the first launch shows something real.
    /// To be replaced by the LLM breakdown flow.
    private func seedDemoContentIfNeeded(database: Database, user: User) throws {
        guard try database.goals(userID: user.id).isEmpty else { return }

        let goal = try database.insertGoal(
            Goal(userID: user.id, title: "三个月内做出自己的 iOS App", detail: "从零学 SwiftUI，最终上架 App Store")
        )

        let titles = [
            "装好 Xcode，跑通 Hello World",
            "搞懂 @State / @Binding / @Observable 的差别",
            "用 List + NavigationStack 做出一个三级页面",
            "接一次真实网络请求并处理加载与错误状态",
            "把数据落地到 SwiftData 或 SQLite",
            "做出本项目的第一屏（目标列表）",
            "接入 LLM，跑通对话式添加事件",
            "实现 10% 微步骤拆解",
            "实现状态感知的排程推荐",
            "在真机上安装并用满一周"
        ]

        let steps = try database.insertSteps(titles.enumerated().map { index, title in
            Step(goalID: goal.id, seq: index + 1, title: title, percent: 0, estimatedMinutes: 45)
        })

        // Two scheduled blocks so the calendar has something to show before the
        // user has talked to the assistant.
        let calendar = user.calendar
        let today = calendar.startOfDay(for: Date())
        let demo: [(stepIndex: Int, dayOffset: Int, hour: Int)] = [
            (0, 1, 10),
            (1, 0, 21)
        ]
        for item in demo {
            guard item.stepIndex < steps.count,
                  let day = calendar.date(byAdding: .day, value: item.dayOffset, to: today),
                  let start = calendar.date(byAdding: .hour, value: item.hour, to: day),
                  let end = calendar.date(byAdding: .minute, value: 45, to: start)
            else { continue }

            let step = steps[item.stepIndex]
            try database.insertEvent(CalendarEvent(
                userID: user.id,
                stepID: step.id,
                goalID: goal.id,
                title: step.title,
                startAt: start,
                endAt: end,
                status: .confirmed,
                source: .scheduler
            ))
            try database.updateStepStatus(id: step.id, status: .scheduled)
        }

        // A repeating commitment, so the series UI is discoverable on first launch.
        let seriesStart = calendar.date(byAdding: .hour, value: 20, to: today) ?? today
        let series = EventSeries.expand(
            userID: user.id,
            title: "网球",
            start: seriesStart,
            end: seriesStart.addingTimeInterval(3600),
            weekdays: [2, 4],
            weeks: 4,
            calendar: calendar
        )
        try database.insertEvents(series)
    }
}
