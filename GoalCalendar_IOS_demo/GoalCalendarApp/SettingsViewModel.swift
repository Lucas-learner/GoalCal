import Foundation
import SwiftUI

@MainActor
final class SettingsViewModel: ObservableObject {
    @Published private(set) var goalCount = 0
    @Published private(set) var completedPercent: Double = 0
    @Published var workStartHour: Int = 9
    @Published var workEndHour: Int = 22
    @Published var errorMessage: String?

    let importService = CalendarImportService()

    private let database: Database
    private var user: User

    init(database: Database, user: User) {
        self.database = database
        self.user = user
        self.workStartHour = user.workStartMinute / 60
        self.workEndHour = user.workEndMinute / 60
        refresh()
        importService.refreshAuthorization()
    }

    var displayName: String { user.name }

    var workWindow: String {
        String(format: "%02d:00 – %02d:00", workStartHour, workEndHour)
    }

    func refresh() {
        do {
            let goals = try database.goals(userID: user.id)
            goalCount = goals.count

            // Aggregate, not the average of percentages: a 10-step goal and a
            // 2-step goal should not count equally.
            var done = 0
            var total = 0
            for goal in goals {
                let progress = try database.progress(goalID: goal.id)
                done += progress.doneSteps
                total += progress.totalSteps
            }
            completedPercent = total == 0 ? 0 : Double(done) / Double(total) * 100
        } catch {
            errorMessage = String(describing: error)
        }
    }

    func saveWorkHours() {
        guard workEndHour > workStartHour else {
            errorMessage = "结束时间必须晚于开始时间"
            return
        }
        user.workStartMinute = workStartHour * 60
        user.workEndMinute = workEndHour * 60
        do {
            try database.upsertUser(user)
            errorMessage = nil
        } catch {
            errorMessage = String(describing: error)
        }
    }

    func importCalendar() {
        Task {
            await importService.requestAndImport(database: database, user: user)
            refresh()
        }
    }
}
