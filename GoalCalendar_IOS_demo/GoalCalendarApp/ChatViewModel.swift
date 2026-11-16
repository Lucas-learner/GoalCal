import Foundation
import SwiftUI

struct ChatMessage: Identifiable {
    enum Kind {
        case user
        case assistant
        case proposal
        case note
    }

    let id = UUID()
    var kind: Kind
    var text: String
    var action: ProposedAction?
    /// Proposals stop accepting taps once handled, so a double-tap cannot
    /// write the same event twice.
    var resolved: Bool = false
    var notes: [String] = []
}

@MainActor
final class ChatViewModel: ObservableObject {
    @Published private(set) var messages: [ChatMessage] = []
    @Published var input: String = ""
    @Published private(set) var isBusy = false
    @Published private(set) var statusText: String = ""
    @Published private(set) var isOnline: Bool
    /// True while the assistant is waiting for answers to its questions.
    @Published private(set) var awaitingAnswers = false

    private var pendingGoal: PendingGoal?
    /// Proactive outreach should happen once per launch, not on every tab switch.
    private var hasRunProactive = false

    /// Lets the goal list refresh after a chat-driven write.
    var onDataChanged: (() -> Void)?

    private let user: User
    private let orchestrator: ChatOrchestrator

    init(database: Database, user: User) {
        self.user = user

        let configuration = LLMConfiguration.load()
        let client: LLMClient? = configuration.map { OpenAICompatibleClient(configuration: $0) }
        isOnline = client != nil

        self.orchestrator = ChatOrchestrator(
            database: database,
            intentParser: IntentParser(client: client),
            planner: GoalPlanner(database: database, parser: GoalBreakdownParser(client: client))
        )

        let mode = client == nil ? "当前为离线模式：只能识别固定句式。" : ""
        messages = [ChatMessage(
            kind: .assistant,
            text: """
            你可以这样跟我说：

            · 明天下午三点和小组成员开会
            · 我想在三个月内学会 SwiftUI
            · 我现在好累，下一步什么时候做
            · 明天有什么安排

            我会先把方案给你看，你确认之后才会写进日历。
            """,
            notes: mode.isEmpty ? [] : [mode]
        )]
    }

    /// Asks the assistant to check in on overdue work or offer free slots.
    /// Runs once per launch, when the chat first appears.
    func runProactiveCheckIfNeeded() {
        guard !hasRunProactive, !isBusy else { return }
        hasRunProactive = true

        guard let turn = orchestrator.proactiveTurn(user: user) else { return }
        messages.append(ChatMessage(
            kind: turn.proposal == nil ? .assistant : .proposal,
            text: turn.reply,
            action: turn.proposal,
            notes: turn.notes
        ))
    }

    /// "Not done" on a check-in: cancel the missed slot, then ask why.
    func markNotDone(_ messageID: UUID) {
        guard let event = checkinEvent(in: messageID) else { return }
        resolve(messageID)
        append(orchestrator.markMissed(event, user: user))
    }

    /// "It was just a busy day" — keep the step, offer the next free slots.
    func markJustBusy(_ messageID: UUID) {
        guard let event = missedEvent(in: messageID) else { return }
        resolve(messageID)
        append(orchestrator.postponeProposal(for: event, user: user))
    }

    /// "It is too hard" — split the step and offer to schedule the first half.
    func markTooHard(_ messageID: UUID) {
        guard let event = missedEvent(in: messageID) else { return }
        resolve(messageID)
        append(orchestrator.splitStepAndSchedule(event, user: user))
    }

    private func checkinEvent(in messageID: UUID) -> CalendarEvent? {
        guard let index = messages.firstIndex(where: { $0.id == messageID }),
              let action = messages[index].action,
              case let .checkin(event, _) = action.kind,
              !messages[index].resolved
        else { return nil }
        return event
    }

    private func missedEvent(in messageID: UUID) -> CalendarEvent? {
        guard let index = messages.firstIndex(where: { $0.id == messageID }),
              let action = messages[index].action,
              case let .missedChoice(event, _) = action.kind,
              !messages[index].resolved
        else { return nil }
        return event
    }

    private func resolve(_ messageID: UUID) {
        if let index = messages.firstIndex(where: { $0.id == messageID }) {
            messages[index].resolved = true
        }
    }

    private func append(_ turn: ChatTurn) {
        messages.append(ChatMessage(
            kind: turn.proposal == nil ? .assistant : .proposal,
            text: turn.reply,
            action: turn.proposal,
            notes: turn.notes
        ))
    }

    func send() {
        let text = input.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !isBusy else { return }

        input = ""
        messages.append(ChatMessage(kind: .user, text: text))
        isBusy = true
        statusText = "正在理解…"

        // Goal breakdown takes ~30s, so the spinner must say something useful
        // rather than looking frozen.
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 6_000_000_000)
            guard let self, self.isBusy else { return }
            self.statusText = "还在处理……拆解目标通常需要 30 秒左右"
        }

        Task { [weak self] in
            guard let self else { return }
            let turn = await self.orchestrator.handle(
                text,
                user: self.user,
                pendingGoal: self.pendingGoal
            )
            self.pendingGoal = turn.pendingGoal
            self.awaitingAnswers = turn.pendingGoal != nil
            self.isBusy = false
            self.statusText = ""

            self.messages.append(ChatMessage(
                kind: turn.proposal == nil ? .assistant : .proposal,
                text: turn.reply,
                action: turn.proposal,
                notes: turn.notes
            ))
        }
    }

    func confirm(_ messageID: UUID, option: SlotSuggestion? = nil) {
        guard let index = messages.firstIndex(where: { $0.id == messageID }),
              let action = messages[index].action,
              !messages[index].resolved
        else { return }

        messages[index].resolved = true
        isBusy = true
        statusText = "写入中…"

        Task { [weak self] in
            guard let self else { return }
            defer {
                self.isBusy = false
                self.statusText = ""
            }
            do {
                let result = try self.orchestrator.commit(action, option: option, user: self.user)
                self.messages.append(ChatMessage(kind: .assistant, text: result.message))
                self.onDataChanged?()
            } catch {
                self.messages.append(ChatMessage(kind: .assistant, text: "操作失败：\(error)"))
            }
        }
    }

    func cancel(_ messageID: UUID) {
        guard let index = messages.firstIndex(where: { $0.id == messageID }),
              !messages[index].resolved
        else { return }

        messages[index].resolved = true
        messages.append(ChatMessage(kind: .assistant, text: "好，已取消，没有写入任何东西。"))
    }
}
