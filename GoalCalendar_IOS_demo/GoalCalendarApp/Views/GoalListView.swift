import SwiftUI

struct GoalListView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        NavigationStack {
            List {
                if let error = model.errorMessage {
                    Section {
                        Text(error)
                            .font(.footnote)
                            .foregroundStyle(.red)
                    }
                }

                ForEach(model.goals) { goal in
                    Section {
                        if let database = model.database {
                            NavigationLink {
                                GoalDetailView(
                                    database: database,
                                    goalID: goal.id,
                                    onChanged: { model.refreshAll() }
                                )
                            } label: {
                                GoalRow(goal: goal)
                            }
                        } else {
                            GoalRow(goal: goal)
                        }
                    }
                }
            }
            .listStyle(.insetGrouped)
            .navigationTitle("我的目标")
            .overlay {
                if model.goals.isEmpty && model.errorMessage == nil {
                    ContentUnavailableView(
                        "还没有目标",
                        systemImage: "target",
                        description: Text("试着说一句：我想在三个月内学会 SwiftUI")
                    )
                }
            }
            .refreshable { model.reload() }
            .onAppear { model.reload() }
        }
    }
}

private struct GoalRow: View {
    @EnvironmentObject private var model: AppModel
    let goal: Goal

    private var progress: GoalProgress {
        model.progressByGoal[goal.id] ?? GoalProgress(goalID: goal.id, totalSteps: 0, doneSteps: 0)
    }

    private var steps: [Step] {
        model.stepsByGoal[goal.id] ?? []
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                Text(goal.title)
                    .font(.headline)
                Spacer()
                Text(progress.percentLabel)
                    .font(.subheadline.monospacedDigit())
                    .foregroundStyle(.secondary)
            }

            if let detail = goal.detail {
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            ProgressView(value: progress.percent, total: 100)
                .tint(progress.isComplete ? .green : .accentColor)

            HStack(spacing: 6) {
                Image(systemName: "checklist")
                Text("\(progress.doneSteps) / \(progress.totalSteps) 步已完成")
                Text("·")
                Text("每步约 \(steps.first?.estimatedMinutes ?? 30) 分钟")
            }
            .font(.caption2)
            .foregroundStyle(.tertiary)

            if !steps.isEmpty {
                StepStrip(steps: steps)
            }
        }
        .padding(.vertical, 6)
    }
}

/// Ten-ish small cells, one per micro-step, filling in as work completes.
/// Reads as "how much of this goal is actually done" at a glance.
private struct StepStrip: View {
    let steps: [Step]

    var body: some View {
        HStack(spacing: 3) {
            ForEach(steps) { step in
                RoundedRectangle(cornerRadius: 2)
                    .fill(color(for: step.status))
                    .frame(height: 18)
                    .overlay {
                        if step.status == .done {
                            Image(systemName: "checkmark")
                                .font(.system(size: 8, weight: .bold))
                                .foregroundStyle(.white)
                        }
                    }
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("共 \(steps.count) 步，已完成 \(steps.filter { $0.status == .done }.count) 步")
    }

    private func color(for status: StepStatus) -> Color {
        switch status {
        case .done: return .green
        case .scheduled: return .accentColor
        case .pending: return Color.secondary.opacity(0.18)
        case .skipped: return Color.orange.opacity(0.3)
        }
    }
}
