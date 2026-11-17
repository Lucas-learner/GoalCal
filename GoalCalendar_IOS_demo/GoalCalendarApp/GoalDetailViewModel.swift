import Foundation
import SwiftUI

@MainActor
final class GoalDetailViewModel: ObservableObject {
    @Published private(set) var goal: Goal?
    @Published private(set) var steps: [Step] = []
    @Published private(set) var progress: GoalProgress?
    @Published var errorMessage: String?

    /// Lets the goal list and calendar refresh after progress changes.
    var onDataChanged: (() -> Void)?

    private let database: Database
    private let goalID: String

    init(database: Database, goalID: String) {
        self.database = database
        self.goalID = goalID
        reload()
    }

    var isComplete: Bool { progress?.isComplete ?? false }

    func reload() {
        do {
            goal = try database.goal(id: goalID)
            steps = try database.steps(goalID: goalID)
            progress = try database.progress(goalID: goalID)
        } catch {
            errorMessage = String(describing: error)
        }
    }

    /// Tapping a completed step un-completes it. Being able to undo a mistaken
    /// tap matters more than preventing the accidental un-check.
    func toggle(_ step: Step) {
        let next: StepStatus = step.status == .done ? .pending : .done
        apply(stepID: step.id, status: next)
    }

    func skip(_ step: Step) {
        apply(stepID: step.id, status: step.status == .skipped ? .pending : .skipped)
    }

    private func apply(stepID: String, status: StepStatus) {
        do {
            let result = try database.setStepStatus(id: stepID, status: status)
            goal = result.goal
            progress = result.progress
            steps = try database.steps(goalID: goalID)
            onDataChanged?()
        } catch {
            errorMessage = String(describing: error)
        }
    }
}
