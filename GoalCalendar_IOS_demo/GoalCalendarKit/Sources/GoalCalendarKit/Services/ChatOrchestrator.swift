import Foundation

/// Human-facing date formatting for chat replies. Kept separate from the
/// scheduler's own labels so the two can evolve independently.
public struct DisplayFormat: Sendable {
    public let calendar: Calendar

    public init(calendar: Calendar) {
        self.calendar = calendar
    }

    public init(user: User) {
        self.calendar = user.calendar
    }

    public func day(_ date: Date, now: Date) -> String {
        let today = calendar.startOfDay(for: now)
        let target = calendar.startOfDay(for: date)
        let days = calendar.dateComponents([.day], from: today, to: target).day ?? 0
        switch days {
        case 0: return "今天"
        case 1: return "明天"
        case 2: return "后天"
        case -1: return "昨天"
        default:
            let components = calendar.dateComponents([.month, .day, .weekday], from: date)
            let names = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]
            let index = min(max((components.weekday ?? 1) - 1, 0), 6)
            return "\(components.month ?? 1)月\(components.day ?? 1)日 \(names[index])"
        }
    }

    public func time(_ date: Date) -> String {
        let components = calendar.dateComponents([.hour, .minute], from: date)
        return String(format: "%02d:%02d", components.hour ?? 0, components.minute ?? 0)
    }

    public func range(_ interval: DateInterval, now: Date) -> String {
        if calendar.isDate(interval.start, inSameDayAs: interval.end) {
            return "\(day(interval.start, now: now)) \(time(interval.start))–\(time(interval.end))"
        }
        return "\(day(interval.start, now: now)) \(time(interval.start)) – \(day(interval.end, now: now)) \(time(interval.end))"
    }

    public func minutes(_ value: Int) -> String {
        guard value >= 60 else { return "\(value) 分钟" }
        let hours = value / 60
        let rest = value % 60
        return rest == 0 ? "\(hours) 小时" : "\(hours) 小时 \(rest) 分钟"
    }
}

/// Something the assistant is proposing but has NOT done yet.
///
/// Every calendar or goal mutation goes through one of these. The type exists so
/// that "the model suggested X" and "X is now true" can never be confused: a
/// `ProposedAction` is inert data until `ChatOrchestrator.commit` is called.
public struct ProposedAction: Identifiable, Sendable {
    public enum Kind: Sendable {
        case addEvent(CalendarEvent)
        case createGoal(GoalDraft)
        case schedule(step: Step, goal: Goal, options: [SlotSuggestion])
        case deleteEvents([CalendarEvent])
        case completeEvents([CalendarEvent])
        /// "Did you finish this one?" — raised proactively, not in reply to a message.
        case checkin(event: CalendarEvent, stepTitle: String)
        /// Follow-up when the answer was "not done": was it timing or difficulty?
        case missedChoice(event: CalendarEvent, stepTitle: String)
        /// Reschedule every occurrence of a repeating event at once.
        case rescheduleSeries(title: String, changes: [SeriesEditor.Change])
        case deleteSeries(title: String, events: [CalendarEvent])
    }

    public let id: UUID
    public var kind: Kind

    public init(id: UUID = UUID(), kind: Kind) {
        self.id = id
        self.kind = kind
    }
}

/// A goal the assistant is still gathering information for.
///
/// Carried in `ChatTurn` rather than stored on the orchestrator so the whole
/// flow stays a pure function of its inputs — the caller threads it back in.
public struct PendingGoal: Sendable, Hashable {
    public var goalText: String
    public var questions: [String]
    public var answers: [String]
    /// How many question rounds have already happened.
    public var rounds: Int

    public init(goalText: String, questions: [String], answers: [String] = [], rounds: Int = 0) {
        self.goalText = goalText
        self.questions = questions
        self.answers = answers
        self.rounds = rounds
    }
}

public struct ChatTurn: Sendable {
    public var reply: String
    public var proposal: ProposedAction?
    /// Non-fatal problems worth showing, e.g. "model unavailable, used rules".
    public var notes: [String]
    public var usedLLM: Bool
    /// Non-nil means the next user message answers these questions.
    public var pendingGoal: PendingGoal?

    public init(
        reply: String,
        proposal: ProposedAction? = nil,
        notes: [String] = [],
        usedLLM: Bool = false,
        pendingGoal: PendingGoal? = nil
    ) {
        self.reply = reply
        self.proposal = proposal
        self.notes = notes
        self.usedLLM = usedLLM
        self.pendingGoal = pendingGoal
    }
}

public struct CommitResult: Sendable {
    public var message: String
    public var event: CalendarEvent?
    public var plannedGoal: PlannedGoal?
    public var progress: GoalProgress?

    public init(
        message: String,
        event: CalendarEvent? = nil,
        plannedGoal: PlannedGoal? = nil,
        progress: GoalProgress? = nil
    ) {
        self.message = message
        self.event = event
        self.plannedGoal = plannedGoal
        self.progress = progress
    }
}

public enum ChatError: Error, CustomStringConvertible {
    case nothingToSchedule

    public var description: String {
        switch self {
        case .nothingToSchedule: return "没有可用的时间段"
        }
    }
}

/// Turns one user utterance into a reply plus, at most, one proposal.
///
/// Deliberately free of SwiftUI and of any write on the read path: `handle` may
/// log a reported state (the user literally told us how they feel) but never
/// touches the calendar or creates goals. That only happens in `commit`.
public struct ChatOrchestrator: Sendable {

    private let database: Database
    private let intentParser: IntentParser
    private let planner: GoalPlanner
    private let scheduler: Scheduler
    private let resolver: TimeResolver
    private let preferences: SchedulingPreferences

    public init(
        database: Database,
        intentParser: IntentParser,
        planner: GoalPlanner,
        scheduler: Scheduler = Scheduler(),
        resolver: TimeResolver = TimeResolver(),
        preferences: SchedulingPreferences = SchedulingPreferences()
    ) {
        self.database = database
        self.intentParser = intentParser
        self.planner = planner
        self.scheduler = scheduler
        self.resolver = resolver
        self.preferences = preferences
    }

    public var isOnline: Bool { intentParser.isOnline }

    // MARK: - Read path

    /// How many rounds of questions before we give up and hand over a starter plan.
    public static let maxClarificationRounds = 2

    public func handle(
        _ text: String,
        user: User,
        now: Date = Date(),
        pendingGoal: PendingGoal? = nil
    ) async -> ChatTurn {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return ChatTurn(reply: "说点什么吧。")
        }

        // While questions are outstanding, treat the message as an answer rather
        // than running intent detection on it.
        if let pendingGoal {
            return await answer(pendingGoal, with: trimmed, user: user, now: now)
        }

        let parsed = await intentParser.parse(trimmed, user: user, now: now)
        var notes = parsed.usedLLM ? [] : (parsed.failure.map { ["未连接模型：\($0)"] } ?? [])

        var turn: ChatTurn
        switch parsed.intent.kind {
        case .addEvent:
            turn = handleAddEvent(parsed.intent, user: user, now: now)
        case .createGoal:
            turn = await handleCreateGoal(parsed.intent, text: trimmed, user: user, now: now)
        case .reportState:
            turn = handleReportState(parsed.intent, user: user, now: now)
        case .scheduleNext:
            turn = handleScheduleNext(user: user, now: now)
        case .queryEvents:
            turn = handleQuery(parsed.intent, user: user, now: now)
        case .deleteEvent:
            turn = handleDelete(parsed.intent, user: user, now: now)
        case .completeEvent:
            turn = handleComplete(parsed.intent, user: user, now: now)
        case .rescheduleSeries:
            turn = handleSeriesReschedule(parsed.intent, user: user, now: now)
        case .deleteSeries:
            turn = handleSeriesDelete(parsed.intent, user: user, now: now)
        case .confirm:
            turn = ChatTurn(reply: "好的。如果你在等我确认什么，点上面的确认按钮。")
        case .smallTalk, .unknown:
            turn = ChatTurn(reply: "没太听懂。你可以说：\n· 明天下午三点开会\n· 我想三个月内学会 SwiftUI\n· 我现在很累")
        }

        turn.notes.append(contentsOf: notes)
        turn.usedLLM = parsed.usedLLM
        return turn
    }

    private func handleAddEvent(_ intent: ParsedIntent, user: User, now: Date) -> ChatTurn {
        let title = intent.title?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard !title.isEmpty else {
            return ChatTurn(reply: "我听到你要加一件事，但没听清是什么。再说一遍？")
        }
        guard let expression = intent.time,
              let resolution = resolver.resolve(expression, user: user, now: now) else {
            return ChatTurn(reply: "「\(title)」安排在什么时候？比如「明天下午三点」。")
        }

        let draft = CalendarEvent(
            userID: user.id,
            title: title,
            notes: nil,
            startAt: resolution.interval.start,
            endAt: resolution.interval.end,
            status: .proposed,
            source: .chat
        )
        let format = DisplayFormat(user: user)
        var reply = "我记成：\(format.range(resolution.interval, now: now)) \(title)。确认后写入日历。"
        reply += resolution.notes.map { "\n" + $0 }.joined()
        return ChatTurn(reply: reply, proposal: ProposedAction(kind: .addEvent(draft)))
    }

    private func handleCreateGoal(_ intent: ParsedIntent, text: String, user: User, now: Date) async -> ChatTurn {
        let goalText = intent.title?.trimmingCharacters(in: .whitespacesAndNewlines) ?? text
        let draft = await planner.draft(goalText)
        return goalTurn(for: draft)
    }

    /// Turns an answer to the previous questions into either more questions or a plan.
    private func answer(_ pending: PendingGoal, with text: String, user: User, now: Date) async -> ChatTurn {
        if text.contains("算了") || text.contains("取消") || text.contains("不拆") {
            return ChatTurn(reply: "好，先不拆了。想清楚一点随时告诉我。")
        }

        let answers = pending.answers + [text]
        let draft = await planner.draft(pending.goalText, context: answers)

        if draft.needsClarification, pending.rounds + 1 < Self.maxClarificationRounds {
            let next = PendingGoal(
                goalText: pending.goalText,
                questions: draft.questions,
                answers: answers,
                rounds: pending.rounds + 1
            )
            return ChatTurn(
                reply: Self.questionText(draft.questions),
                notes: draft.warnings,
                usedLLM: draft.usedLLM,
                pendingGoal: next
            )
        }

        if draft.needsClarification {
            // Out of question rounds: a starter plan beats an endless interview.
            let fallback = GoalDraft(
                title: pending.goalText,
                steps: GoalBreakdownParser.template(for: pending.goalText),
                usedLLM: false,
                warnings: ["问了 \(Self.maxClarificationRounds) 轮信息仍不够，先用通用模板起个头，你可以逐条改"]
            )
            return goalTurn(for: fallback)
        }

        return goalTurn(for: draft)
    }

    static func questionText(_ questions: [String]) -> String {
        let numbered = questions.enumerated()
            .map { "\($0.offset + 1). \($0.element)" }
            .joined(separator: "\n")
        return "在排计划之前，我需要先确认几件事：\n\n\(numbered)\n\n直接回我一段就行，也可以分几条说。"
    }

    private func goalTurn(for draft: GoalDraft) -> ChatTurn {
        if draft.needsClarification {
            let pending = PendingGoal(
                goalText: draft.title,
                questions: draft.questions,
                answers: [],
                rounds: 0
            )
            return ChatTurn(
                reply: Self.questionText(draft.questions),
                notes: draft.warnings,
                usedLLM: draft.usedLLM,
                pendingGoal: pending
            )
        }

        guard !draft.steps.isEmpty else {
            return ChatTurn(reply: "没能把「\(draft.title)」拆成可执行的步骤，换个说法再试试？", notes: draft.warnings)
        }

        var reply: String
        if draft.usedLLM {
            reply = "我把「\(draft.title)」拆成了 \(draft.steps.count) 步，"
            reply += String(format: "每步占 %.1f%%。", draft.percentPerStep)
            reply += "\n确认后建立目标，我再帮你把第一步排进日历。"
        } else {
            reply = "这次没连上模型，先用通用模板拆了 \(draft.steps.count) 步。"
            reply += "\n确认后可以逐条改；也可以等网络恢复后重新拆解。"
        }

        return ChatTurn(
            reply: reply,
            proposal: ProposedAction(kind: .createGoal(draft)),
            notes: draft.warnings
        )
    }

    private func handleReportState(_ intent: ParsedIntent, user: User, now: Date) -> ChatTurn {
        guard let state = intent.state else {
            return ChatTurn(reply: "你现在是什么状态？累、忙、还是没灵感？")
        }

        // The user's own report is not a calendar mutation, so it is recorded
        // immediately — the scheduler needs it to answer this very turn.
        try? database.appendStateLog(StateLog(userID: user.id, state: state, createdAt: now))

        guard let context = nextActionable(user: user) else {
            return ChatTurn(reply: "记下了：\(state.label)。你现在没有进行中的目标，说说你想达成什么？")
        }

        let options = suggestions(for: context, user: user, state: state, now: now)
        guard !options.isEmpty else {
            return ChatTurn(
                reply: "记下了：\(state.label)。「\(context.step.title)」在接下来 \(preferences.horizonDays) 天没有合适空档，先挪一下已有安排？"
            )
        }

        let reply = "记下了：\(state.label)。下一步是「\(context.step.title)」，约 \(context.step.estimatedMinutes) 分钟。我挑了几个时间："
        return ChatTurn(
            reply: reply,
            proposal: ProposedAction(kind: .schedule(step: context.step, goal: context.goal, options: options))
        )
    }

    private func handleScheduleNext(user: User, now: Date) -> ChatTurn {
        guard let context = nextActionable(user: user) else {
            return ChatTurn(reply: "你现在没有进行中的目标，说说你想达成什么？")
        }
        let state = (try? database.latestStateLog(userID: user.id))?.state
        let options = suggestions(for: context, user: user, state: state, now: now)
        guard !options.isEmpty else {
            return ChatTurn(reply: "「\(context.step.title)」在接下来 \(preferences.horizonDays) 天没有合适空档。")
        }
        return ChatTurn(
            reply: "下一步是「\(context.step.title)」，约 \(context.step.estimatedMinutes) 分钟。",
            proposal: ProposedAction(kind: .schedule(step: context.step, goal: context.goal, options: options))
        )
    }

    private func handleQuery(_ intent: ParsedIntent, user: User, now: Date) -> ChatTurn {
        let format = DisplayFormat(user: user)
        let expression = intent.queryRange ?? intent.time

        guard let expression,
              let resolution = resolver.resolve(expression, user: user, now: now) else {
            return ChatTurn(reply: "想查哪一天？比如「明天有什么安排」。")
        }

        let dayStart = user.calendar.startOfDay(for: resolution.interval.start)
        guard let dayEnd = user.calendar.date(byAdding: .day, value: 1, to: dayStart) else {
            return ChatTurn(reply: "时间范围没算出来，换个说法？")
        }

        let events = (try? database.events(userID: user.id, from: dayStart, to: dayEnd)) ?? []
        guard !events.isEmpty else {
            return ChatTurn(reply: "\(format.day(dayStart, now: now))没有安排。")
        }

        var lines = ["\(format.day(dayStart, now: now))有 \(events.count) 件事："]
        for event in events.sorted(by: { $0.startAt < $1.startAt }) {
            lines.append("· \(format.time(event.startAt)) \(event.title)")
        }
        return ChatTurn(reply: lines.joined(separator: "\n"))
    }

    private func handleDelete(_ intent: ParsedIntent, user: User, now: Date) -> ChatTurn {
        let matches = candidateEvents(intent, user: user, now: now)
        guard !matches.isEmpty else {
            return ChatTurn(reply: "没找到要删除的日程。要不说清楚点，比如「删除明天下午的会」？")
        }
        let format = DisplayFormat(user: user)
        let list = matches.map { "· \(format.day($0.startAt, now: now)) \(format.time($0.startAt)) \($0.title)" }
        return ChatTurn(
            reply: "找到 \(matches.count) 条，确认后删除：\n" + list.joined(separator: "\n"),
            proposal: ProposedAction(kind: .deleteEvents(matches))
        )
    }

    private func handleComplete(_ intent: ParsedIntent, user: User, now: Date) -> ChatTurn {
        let matches = candidateEvents(intent, user: user, now: now).filter { $0.status == .confirmed }
        guard !matches.isEmpty else {
            return ChatTurn(reply: "没找到可以标记完成的事件。")
        }
        let format = DisplayFormat(user: user)
        let list = matches.map { "· \(format.day($0.startAt, now: now)) \(format.time($0.startAt)) \($0.title)" }
        return ChatTurn(
            reply: "确认这些已经做完了？完成后目标进度会自动推进：\n" + list.joined(separator: "\n"),
            proposal: ProposedAction(kind: .completeEvents(matches))
        )
    }

    // MARK: - Proactive outreach

    /// Called when the chat opens, not in response to anything the user said.
    ///
    /// Two jobs, in priority order: chase an overdue step, otherwise offer to put
    /// an unscheduled step on the calendar. Returns nil when there is nothing
    /// worth interrupting the user for.
    public func proactiveTurn(user: User, now: Date = Date()) -> ChatTurn? {
        overdueCheckin(user: user, now: now) ?? idleSuggestion(user: user, now: now)
    }

    private func overdueCheckin(user: User, now: Date) -> ChatTurn? {
        let windowStart = now.addingTimeInterval(-7 * 86_400)
        let events = (try? database.events(userID: user.id, from: windowStart, to: now)) ?? []

        let overdue = events
            .filter { $0.stepID != nil && $0.status == .confirmed && $0.endAt <= now }
            .sorted { $0.endAt > $1.endAt }

        for event in overdue {
            guard let stepID = event.stepID,
                  let step = (try? database.step(id: stepID)) ?? nil
            else { continue }
            guard step.status != .done, step.status != .skipped else { continue }

            let format = DisplayFormat(user: user)
            let reply = "「\(step.title)」原定 \(format.day(event.startAt, now: now)) "
                + "\(format.time(event.startAt))–\(format.time(event.endAt))，完成了吗？"
            return ChatTurn(
                reply: reply,
                proposal: ProposedAction(kind: .checkin(event: event, stepTitle: step.title))
            )
        }
        return nil
    }

    private func idleSuggestion(user: User, now: Date) -> ChatTurn? {
        // Skip anything already sitting on the calendar, but keep looking: the
        // highest-priority step being scheduled is not a reason to say nothing.
        let horizonEnd = now.addingTimeInterval(TimeInterval(preferences.horizonDays * 86_400))
        let upcoming = (try? database.events(userID: user.id, from: now, to: horizonEnd)) ?? []
        let alreadyScheduled = Set(upcoming.compactMap(\.stepID))

        let steps = (try? database.actionableSteps(userID: user.id)) ?? []
        for step in steps where !alreadyScheduled.contains(step.id) {
            guard let goal = (try? database.goal(id: step.goalID)) ?? nil else { continue }

            let options = suggestions(for: (step: step, goal: goal), user: user, state: nil, now: now)
            guard !options.isEmpty else { continue }

            let format = DisplayFormat(user: user)
            var reply = "「\(step.title)」还没有排时间，我看了下你的空档："
            if let first = options.first {
                reply += "\n最早可以放在 \(format.day(first.start, now: now)) \(format.time(first.start))。"
            }
            return ChatTurn(
                reply: reply,
                proposal: ProposedAction(kind: .schedule(step: step, goal: goal, options: options))
            )
        }
        return nil
    }

    /// The user said a scheduled step was not done.
    ///
    /// Takes it off the calendar first (otherwise the same step ends up with a
    /// passed event and a future one, and every later query sees two), then asks
    /// whether the cause was the day or the size of the step.
    public func markMissed(_ event: CalendarEvent, user: User, now: Date = Date()) -> ChatTurn {
        try? database.updateEventStatus(id: event.id, status: .cancelled)

        return ChatTurn(
            reply: "这次没做成，我先把它从日历上撤掉了。是临时有事，还是这一步本身有点重？",
            proposal: ProposedAction(kind: .missedChoice(event: event, stepTitle: event.title))
        )
    }

    /// "Too hard" — replace the step with two smaller ones and offer to schedule
    /// the first half. Stays inside the same goal on purpose.
    public func splitStepAndSchedule(_ event: CalendarEvent, user: User, now: Date = Date()) -> ChatTurn {
        guard let stepID = event.stepID,
              let step = (try? database.step(id: stepID)) ?? nil,
              let goal = (try? database.goal(id: step.goalID)) ?? nil
        else {
            return ChatTurn(reply: "没找到这个任务对应的目标。")
        }

        let titles = ["\(step.title)（上半）", "\(step.title)（下半）"]
        guard let result = try? database.splitStep(id: stepID, into: titles, at: now),
              let first = result.created.first
        else {
            return ChatTurn(reply: "拆分没有成功，稍后再试一次？")
        }

        var reply = "已经把它拆成两个更小的步骤，各约 \(first.estimatedMinutes) 分钟：\n"
        reply += "· \(titles[0])\n· \(titles[1])\n先做上半部分？"

        let options = suggestions(for: (step: first, goal: goal), user: user, state: nil, now: now)
        guard !options.isEmpty else {
            return ChatTurn(reply: reply + "\n不过接下来没有合适空档，先挪一下已有安排？")
        }
        return ChatTurn(
            reply: reply,
            proposal: ProposedAction(kind: .schedule(step: first, goal: goal, options: options))
        )
    }

    /// Builds a new-time proposal for a step the user did not finish.
    public func postponeProposal(for event: CalendarEvent, user: User, now: Date = Date()) -> ChatTurn {
        guard let stepID = event.stepID,
              let step = (try? database.step(id: stepID)) ?? nil,
              let goal = (try? database.goal(id: step.goalID)) ?? nil
        else {
            return ChatTurn(reply: "没找到这个任务对应的目标，没法重新排。")
        }

        let options = suggestions(for: (step: step, goal: goal), user: user, state: nil, now: now)
        guard !options.isEmpty else {
            return ChatTurn(reply: "接下来 \(preferences.horizonDays) 天没有合适的空档，先挪一下已有安排？")
        }

        let format = DisplayFormat(user: user)
        var reply = "那就换个时间。我重新找了几个空档："
        if let first = options.first {
            reply += "\n最早：\(format.day(first.start, now: now)) \(format.time(first.start))。"
        }
        return ChatTurn(
            reply: reply,
            proposal: ProposedAction(kind: .schedule(step: step, goal: goal, options: options))
        )
    }

    // MARK: - Series operations

    /// A wide window so a long-running weekly series is fully visible.
    private func seriesScopedEvents(user: User, now: Date) -> [CalendarEvent] {
        let start = now.addingTimeInterval(-7 * 86_400)
        let end = now.addingTimeInterval(180 * 86_400)
        return (try? database.events(userID: user.id, from: start, to: end)) ?? []
    }

    private func handleSeriesReschedule(_ intent: ParsedIntent, user: User, now: Date) -> ChatTurn {
        let match = intent.seriesMatch
        guard let candidate = SeriesMatcher.bestSeries(
            in: seriesScopedEvents(user: user, now: now),
            title: match?.title,
            weekday: match?.weekday,
            hour: match?.hour,
            calendar: user.calendar
        ) else {
            return ChatTurn(reply: "没找到匹配的重复事件。说得再具体一点，比如「每周四上午 9 点开会」。")
        }

        let target = intent.seriesTarget ?? SeriesTarget()
        guard target.hour != nil || target.minute != nil || target.weekday != nil || target.durationMinutes != nil else {
            return ChatTurn(reply: "要把这组事件改成什么时间？比如「改到下午 2 点」。")
        }

        let changes = SeriesEditor.reschedule(
            events: candidate.events,
            hour: target.hour,
            minute: target.minute,
            weekday: target.weekday,
            durationMinutes: target.durationMinutes,
            calendar: user.calendar
        )
        guard !changes.isEmpty else {
            return ChatTurn(reply: "时间没有变化，没有需要修改的地方。")
        }

        let format = DisplayFormat(user: user)
        let title = candidate.events.first?.title ?? "这组事件"
        var lines = ["找到 \(changes.count) 次「\(title)」，确认后整体调整："]
        for change in changes.prefix(5) {
            lines.append("· \(format.day(change.oldStart, now: now)) \(format.time(change.oldStart)) → \(format.day(change.newStart, now: now)) \(format.time(change.newStart))")
        }
        if changes.count > 5 {
            lines.append("… 其余 \(changes.count - 5) 次同理")
        }
        return ChatTurn(
            reply: lines.joined(separator: "\n"),
            proposal: ProposedAction(kind: .rescheduleSeries(title: title, changes: changes))
        )
    }

    private func handleSeriesDelete(_ intent: ParsedIntent, user: User, now: Date) -> ChatTurn {
        let match = intent.seriesMatch
        guard let candidate = SeriesMatcher.bestSeries(
            in: seriesScopedEvents(user: user, now: now),
            title: match?.title,
            weekday: match?.weekday,
            hour: match?.hour,
            calendar: user.calendar
        ) else {
            return ChatTurn(reply: "没找到匹配的重复事件。")
        }

        let format = DisplayFormat(user: user)
        let title = candidate.events.first?.title ?? "这组事件"
        let first = candidate.events.first
        var reply = "找到 \(candidate.events.count) 次「\(title)」，确认后全部删除"
        if let first {
            reply += "（第一次是 \(format.day(first.startAt, now: now)) \(format.time(first.startAt))）"
        }
        return ChatTurn(
            reply: reply + "。",
            proposal: ProposedAction(kind: .deleteSeries(title: title, events: candidate.events))
        )
    }

    // MARK: - Write path (only after explicit confirmation)

    public func commit(
        _ action: ProposedAction,
        option: SlotSuggestion? = nil,
        user: User,
        now: Date = Date()
    ) throws -> CommitResult {
        let format = DisplayFormat(user: user)

        switch action.kind {
        case .addEvent(var event):
            event.status = .confirmed
            let saved = try database.insertEvent(event)
            return CommitResult(
                message: "已加入日历：\(format.range(saved.interval, now: now)) \(saved.title)",
                event: saved
            )

        case .createGoal(let draft):
            let planned = try planner.commit(draft, user: user, now: now)
            return CommitResult(
                message: "目标已建立：「\(planned.goal.title)」，共 \(planned.steps.count) 步。",
                plannedGoal: planned,
                progress: planned.progress
            )

        case let .schedule(step, goal, options):
            guard let slot = option ?? options.first else { throw ChatError.nothingToSchedule }
            let event = CalendarEvent(
                userID: user.id,
                stepID: step.id,
                goalID: goal.id,
                title: step.title,
                notes: nil,
                startAt: slot.start,
                endAt: slot.end,
                status: .confirmed,
                source: .scheduler
            )
            let saved = try database.insertEvent(event)
            try database.updateStepStatus(id: step.id, status: .scheduled)
            return CommitResult(
                message: "已排进日历：\(format.range(saved.interval, now: now)) 「\(saved.title)」",
                event: saved,
                progress: try database.progress(goalID: goal.id)
            )

        case .missedChoice:
            return CommitResult(message: "请先选择「临时有事」或「太难了」。")

        case let .checkin(event, _):
            guard let completion = try database.completeEvent(id: event.id, at: now) else {
                return CommitResult(message: "没有找到这条事件。")
            }
            var message = "已标记完成：\(completion.event.title)"
            if let progress = completion.progress {
                message += "，目标进度 \(progress.doneSteps)/\(progress.totalSteps)（\(progress.percentLabel)）"
            }
            return CommitResult(message: message, event: completion.event, progress: completion.progress)

        case let .rescheduleSeries(_, changes):
            try database.updateEventTimes(changes.map {
                EventTimeUpdate(id: $0.eventID, startAt: $0.newStart, endAt: $0.newEnd)
            })
            return CommitResult(message: "已整体调整 \(changes.count) 次事件。")

        case let .deleteSeries(title, events):
            guard let seriesID = events.first?.seriesID else {
                return CommitResult(message: "这组事件没有系列标识，未做删除。")
            }
            let removed = (try? database.deleteSeries(id: seriesID)) ?? 0
            return CommitResult(message: "已删除「\(title)」的 \(removed) 次事件。")

        case .deleteEvents(let events):
            var removed = 0
            for event in events where (try? database.deleteEvent(id: event.id)) == true {
                removed += 1
            }
            return CommitResult(message: "已删除 \(removed) 条日程。")

        case .completeEvents(let events):
            var last: EventCompletion?
            for event in events {
                if let completion = try database.completeEvent(id: event.id, at: now) {
                    last = completion
                }
            }
            guard let last else {
                return CommitResult(message: "没有更新任何事件。")
            }
            var message = "已标记完成：\(last.event.title)"
            if let progress = last.progress {
                message += "，目标进度 \(progress.doneSteps)/\(progress.totalSteps)（\(progress.percentLabel)）"
                if progress.isComplete { message += "，目标已完成 🎉" }
            }
            return CommitResult(message: message, event: last.event, progress: last.progress)
        }
    }

    // MARK: - Helpers

    private func nextActionable(user: User) -> (step: Step, goal: Goal)? {
        let steps = (try? database.actionableSteps(userID: user.id)) ?? []
        for step in steps {
            if let goal = try? database.goal(id: step.goalID) {
                return (step, goal)
            }
        }
        return nil
    }

    private func suggestions(
        for context: (step: Step, goal: Goal),
        user: User,
        state: UserState?,
        now: Date
    ) -> [SlotSuggestion] {
        let horizonEnd = now.addingTimeInterval(TimeInterval(preferences.horizonDays * 86_400))
        let busy = (try? database.blockingEvents(userID: user.id, from: now, to: horizonEnd)) ?? []
        let states = state.map { [StateLog(userID: user.id, state: $0, createdAt: now)] }
            ?? ((try? database.recentStateLogs(userID: user.id, limit: 1)) ?? [])

        return scheduler.suggest(SchedulerInput(
            user: user,
            now: now,
            candidates: [SchedulerCandidate(step: context.step, goal: context.goal)],
            busyEvents: busy,
            recentStates: states,
            preferences: preferences
        ))
    }

    /// Events that a delete/complete instruction could plausibly mean.
    private func candidateEvents(_ intent: ParsedIntent, user: User, now: Date) -> [CalendarEvent] {
        let calendar = user.calendar
        let window: DateInterval

        if let expression = intent.time ?? intent.queryRange,
           let resolution = resolver.resolve(expression, user: user, now: now) {
            let start = calendar.startOfDay(for: resolution.interval.start)
            let end = calendar.date(byAdding: .day, value: 1, to: start) ?? start.addingTimeInterval(86_400)
            window = DateInterval(start: start, end: end)
        } else {
            window = DateInterval(
                start: calendar.date(byAdding: .day, value: -1, to: now) ?? now,
                end: calendar.date(byAdding: .day, value: 30, to: now) ?? now
            )
        }

        let events = (try? database.events(userID: user.id, from: window.start, to: window.end)) ?? []

        guard let title = intent.title?.trimmingCharacters(in: .whitespacesAndNewlines), !title.isEmpty else {
            return events
        }
        return events.filter {
            $0.title.localizedCaseInsensitiveContains(title)
                || title.localizedCaseInsensitiveContains($0.title)
        }
    }
}
