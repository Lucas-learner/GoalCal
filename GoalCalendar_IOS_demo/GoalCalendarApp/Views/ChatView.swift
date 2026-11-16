import SwiftUI

struct ChatView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        if let chat = model.chat {
            ChatScreen(chat: chat)
        } else {
            ProgressView("正在初始化…")
        }
    }
}

struct ChatScreen: View {
    @ObservedObject var chat: ChatViewModel

    var body: some View {
        NavigationStack {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        ForEach(chat.messages) { message in
                            MessageRow(message: message, chat: chat)
                                .id(message.id)
                        }
                        if chat.isBusy {
                            HStack(spacing: 8) {
                                ProgressView()
                                Text(chat.statusText)
                                    .font(.footnote)
                                    .foregroundStyle(.secondary)
                            }
                            .id("busy")
                        }
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 12)
                }
                .onChange(of: chat.messages.count) { _, _ in
                    withAnimation { proxy.scrollTo(chat.messages.last?.id, anchor: .bottom) }
                }
                .onChange(of: chat.isBusy) { _, busy in
                    if busy { withAnimation { proxy.scrollTo("busy", anchor: .bottom) } }
                }
            }
            .safeAreaInset(edge: .bottom) { inputBar }
            .onAppear { chat.runProactiveCheckIfNeeded() }
            .navigationTitle("对话")
            .navigationBarTitleDisplayMode(.inline)
        }
    }

    private var inputBar: some View {
        HStack(alignment: .bottom, spacing: 10) {
            TextField(chat.awaitingAnswers ? "回答上面的问题…" : "说点什么…", text: $chat.input, axis: .vertical)
                .lineLimit(1...4)
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 20))
                .submitLabel(.send)
                .onSubmit { chat.send() }

            Button {
                chat.send()
            } label: {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.system(size: 30))
            }
            .disabled(
                chat.input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || chat.isBusy
            )
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(.bar)
    }
}

private struct MessageRow: View {
    let message: ChatMessage
    @ObservedObject var chat: ChatViewModel

    var body: some View {
        switch message.kind {
        case .user:
            HStack {
                Spacer(minLength: 40)
                Text(message.text)
                    .font(.callout)
                    .foregroundStyle(.white)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background(Color.accentColor, in: RoundedRectangle(cornerRadius: 16))
            }
        case .assistant:
            HStack {
                VStack(alignment: .leading, spacing: 6) {
                    Text(message.text)
                        .font(.callout)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 10)
                        .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 16))
                    ForEach(Array(message.notes.enumerated()), id: \.offset) { _, note in
                        Text(note)
                            .font(.caption2)
                            .foregroundStyle(.orange)
                    }
                }
                Spacer(minLength: 40)
            }
        case .proposal:
            HStack {
                ProposalCard(message: message, chat: chat)
                Spacer(minLength: 0)
            }
        case .note:
            Text(message.text)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }
}

private struct ProposalCard: View {
    let message: ChatMessage
    @ObservedObject var chat: ChatViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(message.text)
                .font(.callout)

            if let action = message.action {
                content(for: action.kind)

                if message.resolved {
                    Label("已处理", systemImage: "checkmark.circle")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else if case .checkin = action.kind {
                    HStack(spacing: 10) {
                        Button("完成了") { chat.confirm(message.id) }
                            .buttonStyle(.borderedProminent)
                            .controlSize(.small)
                        Button("没完成") { chat.markNotDone(message.id) }
                            .buttonStyle(.bordered)
                            .controlSize(.small)
                    }
                } else if case .missedChoice = action.kind {
                    HStack(spacing: 10) {
                        Button("临时有事") { chat.markJustBusy(message.id) }
                            .buttonStyle(.borderedProminent)
                            .controlSize(.small)
                        Button("太难了") { chat.markTooHard(message.id) }
                            .buttonStyle(.bordered)
                            .controlSize(.small)
                    }
                } else if !isSchedule(action.kind) {
                    HStack(spacing: 10) {
                        Button("确认") { chat.confirm(message.id) }
                            .buttonStyle(.borderedProminent)
                            .controlSize(.small)
                        Button("取消") { chat.cancel(message.id) }
                            .buttonStyle(.bordered)
                            .controlSize(.small)
                    }
                }
            }

            ForEach(Array(message.notes.enumerated()), id: \.offset) { _, note in
                Text(note)
                    .font(.caption2)
                    .foregroundStyle(.orange)
            }
        }
        .padding(14)
        .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 16))
    }

    private func isSchedule(_ kind: ProposedAction.Kind) -> Bool {
        if case .schedule = kind { return true }
        return false
    }

    @ViewBuilder
    private func content(for kind: ProposedAction.Kind) -> some View {
        switch kind {
        case .addEvent(let event):
            VStack(alignment: .leading, spacing: 4) {
                Text(event.title).font(.subheadline.weight(.semibold))
                Text(AppDateFormat.dateTime(event.startAt))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

        case .createGoal(let draft):
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    Text("\(draft.steps.count) 步").font(.caption.weight(.semibold))
                    Text(String(format: "每步 %.1f%%", draft.percentPerStep))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                ForEach(Array(draft.steps.enumerated()), id: \.offset) { index, step in
                    HStack(alignment: .top, spacing: 8) {
                        Text("\(index + 1)")
                            .font(.caption2.monospacedDigit())
                            .foregroundStyle(.secondary)
                            .frame(width: 18, alignment: .trailing)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(step.title).font(.caption)
                            HStack(spacing: 6) {
                                Text("约 \(step.estimatedMinutes) 分钟")
                                if let recurrence = step.recurrence {
                                    Text(recurrence.label)
                                        .foregroundStyle(Color.accentColor)
                                }
                            }
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                        }
                    }
                }
            }

        case let .schedule(step, _, options):
            VStack(alignment: .leading, spacing: 8) {
                Text(step.title).font(.subheadline.weight(.semibold))
                ForEach(Array(options.enumerated()), id: \.offset) { _, option in
                    Button {
                        chat.confirm(message.id, option: option)
                    } label: {
                        HStack(spacing: 10) {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(AppDateFormat.dateTime(option.start))
                                    .font(.caption.weight(.semibold))
                                if let reason = option.reasons.first {
                                    Text(reason)
                                        .font(.caption2)
                                        .foregroundStyle(.secondary)
                                        .multilineTextAlignment(.leading)
                                }
                            }
                            Spacer(minLength: 4)
                            if !message.resolved {
                                Text("选这个")
                                    .font(.caption2.weight(.semibold))
                                    .foregroundStyle(Color.accentColor)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10)
                        .background(Color(.tertiarySystemBackground), in: RoundedRectangle(cornerRadius: 10))
                        .overlay(
                            RoundedRectangle(cornerRadius: 10)
                                .strokeBorder(Color.accentColor.opacity(message.resolved ? 0 : 0.35))
                        )
                    }
                    .buttonStyle(.plain)
                    .disabled(message.resolved)
                }
                if !message.resolved {
                    Button("取消") { chat.cancel(message.id) }
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                }
            }

        case .deleteEvents(let events):
            VStack(alignment: .leading, spacing: 4) {
                ForEach(events) { event in
                    Text("· \(AppDateFormat.dateTime(event.startAt)) \(event.title)")
                        .font(.caption)
                }
            }

        case .completeEvents(let events):
            VStack(alignment: .leading, spacing: 4) {
                ForEach(events) { event in
                    Text("· \(AppDateFormat.dateTime(event.startAt)) \(event.title)")
                        .font(.caption)
                }
            }

        case let .missedChoice(_, stepTitle):
            Text(stepTitle)
                .font(.subheadline.weight(.semibold))

        case let .checkin(event, stepTitle):
            VStack(alignment: .leading, spacing: 4) {
                Text(stepTitle).font(.subheadline.weight(.semibold))
                Text(AppDateFormat.dateTime(event.startAt))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

        case let .rescheduleSeries(_, changes):
            VStack(alignment: .leading, spacing: 4) {
                ForEach(Array(changes.prefix(6).enumerated()), id: \.offset) { _, change in
                    Text("· \(AppDateFormat.dateTime(change.oldStart)) → \(AppDateFormat.dateTime(change.newStart))")
                        .font(.caption)
                }
                if changes.count > 6 {
                    Text("… 其余 \(changes.count - 6) 次同理")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }

        case let .deleteSeries(_, events):
            VStack(alignment: .leading, spacing: 4) {
                Text("共 \(events.count) 次").font(.caption)
                ForEach(events.prefix(3)) { event in
                    Text("· \(AppDateFormat.dateTime(event.startAt)) \(event.title)")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }
}
