import SwiftUI

struct GoalDetailView: View {
    @StateObject private var viewModel: GoalDetailViewModel
    private let onChanged: () -> Void

    init(database: Database, goalID: String, onChanged: @escaping () -> Void) {
        _viewModel = StateObject(wrappedValue: GoalDetailViewModel(database: database, goalID: goalID))
        self.onChanged = onChanged
    }

    var body: some View {
        List {
            Section {
                header
            }

            Section("步骤") {
                ForEach(viewModel.steps) { step in
                    StepRow(step: step) {
                        viewModel.toggle(step)
                        onChanged()
                    } onSkip: {
                        viewModel.skip(step)
                        onChanged()
                    }
                }
            }

            if let error = viewModel.errorMessage {
                Section {
                    Text(error).font(.footnote).foregroundStyle(.red)
                }
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("目标详情")
        .navigationBarTitleDisplayMode(.inline)
        .onAppear {
            viewModel.onDataChanged = onChanged
            viewModel.reload()
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(viewModel.goal?.title ?? "")
                .font(.headline)

            if let detail = viewModel.goal?.detail, !detail.isEmpty {
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            ProgressView(value: viewModel.progress?.percent ?? 0, total: 100)
                .tint(viewModel.isComplete ? .green : .accentColor)

            HStack {
                Text("\(viewModel.progress?.doneSteps ?? 0) / \(viewModel.progress?.totalSteps ?? 0) 步已完成")
                Spacer()
                Text(viewModel.progress?.percentLabel ?? "0%")
                    .monospacedDigit()
            }
            .font(.caption)
            .foregroundStyle(.secondary)

            if viewModel.isComplete {
                Label("目标已完成", systemImage: "checkmark.seal.fill")
                    .font(.caption)
                    .foregroundStyle(.green)
            }
        }
        .padding(.vertical, 4)
    }
}

private struct StepRow: View {
    let step: Step
    let onToggle: () -> Void
    let onSkip: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Button(action: onToggle) {
                Image(systemName: icon)
                    .font(.title3)
                    .foregroundStyle(iconColor)
            }
            .buttonStyle(.plain)

            VStack(alignment: .leading, spacing: 3) {
                Text(step.title)
                    .font(.callout)
                    .strikethrough(step.status == .done)
                    .foregroundStyle(step.status == .done ? .secondary : .primary)

                HStack(spacing: 8) {
                    Text(String(format: "%.1f%%", step.percent))
                    Text("约 \(step.estimatedMinutes) 分钟")
                    if let recurrence = step.recurrence {
                        Text(recurrence.label).foregroundStyle(Color.accentColor)
                    }
                    Text(step.status.label)
                }
                .font(.caption2)
                .foregroundStyle(.tertiary)

                if let detail = step.detail, !detail.isEmpty {
                    Text(detail)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }

            Spacer(minLength: 4)

            Button(action: onSkip) {
                Image(systemName: step.status == .skipped ? "arrow.uturn.backward.circle" : "forward.circle")
                    .font(.body)
                    .foregroundStyle(.tertiary)
            }
            .buttonStyle(.plain)
        }
        .padding(.vertical, 2)
    }

    private var icon: String {
        switch step.status {
        case .done: return "checkmark.circle.fill"
        case .skipped: return "minus.circle"
        default: return "circle"
        }
    }

    private var iconColor: Color {
        switch step.status {
        case .done: return .green
        case .skipped: return .orange
        default: return .secondary
        }
    }
}
