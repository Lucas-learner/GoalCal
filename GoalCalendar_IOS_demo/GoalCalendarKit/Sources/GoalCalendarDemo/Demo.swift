import Foundation
import GoalCalendarKit

// A terminal walkthrough of the logic layer.
//
//   ./scripts/demo.sh          rules only (works offline)
//   ./scripts/demo.sh --llm    also calls the real model and compares
//
// Needs Config/env.local for the LLM path; see scripts/setup-llm.sh.

@main
struct Demo {

    static let calendar: Calendar = {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Shanghai")!
        calendar.firstWeekday = 2
        return calendar
    }()

    static let stamp: DateFormatter = {
        let formatter = DateFormatter()
        formatter.calendar = calendar
        formatter.timeZone = calendar.timeZone
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = "M月d日 EEE HH:mm"
        return formatter
    }()

    static let user = User(
        name: "演示用户",
        timezoneIdentifier: "Asia/Shanghai",
        workStartMinute: 9 * 60,
        workEndMinute: 22 * 60
    )

    static let rules = RuleBasedIntentParser()
    static let resolver = TimeResolver()

    static func main() async {
        let arguments = Array(CommandLine.arguments.dropFirst())
        let useLLM = arguments.contains("--llm")
        let showRaw = arguments.contains("--raw")

        func value(after flag: String) -> String? {
            guard let index = arguments.firstIndex(of: flag), index + 1 < arguments.count else { return nil }
            return arguments[index + 1]
        }
        let modelOverride = value(after: "--model")
        let goalOverride = value(after: "--goal")
        let answersRaw = value(after: "--answers")
        let answers = answersRaw?.split(separator: "|").map(String.init) ?? []
        let now = Date()

        separator("1. 意图解析 + 时间归一化（规则）")
        print("当前时间：\(label(now))")

        let utterances = [
            "明天下午三点和小组开会",
            "我现在好累，下一步什么时候做",
            "下周三上午九点半交作业",
            "删除明天的会",
            "我三个月内想学会 SwiftUI"
        ]

        for utterance in utterances {
            let intent = rules.parse(utterance, now: now)
            print("\n用户：\(utterance)")
            print("  \(describe(intent))")
            if intent.kind == .addEvent || intent.kind == .queryEvents,
               let expression = intent.time,
               let resolution = resolver.resolve(expression, user: user, now: now) {
                print("  时间 = \(label(resolution.interval.start)) → \(label(resolution.interval.end))")
                for note in resolution.notes { print("  提示 = \(note)") }
            }
        }

        schedulingSections(now: now)

        if useLLM {
            await llmComparison(now: now, showRaw: showRaw)
        } else {
            separator("4. 真实模型对比")
            print("未启用。先执行 ./scripts/setup-llm.sh 写入配置，")
            print("再运行 ./scripts/demo.sh --llm")
        }

        await goalBreakdownSection(useLLM: useLLM, showRaw: showRaw, modelOverride: modelOverride, goalOverride: goalOverride, answers: answers)

        if let chatLine = value(after: "--chat") {
            await chatSection(line: chatLine, useLLM: useLLM, modelOverride: modelOverride)
        }

        print("")
    }

    // MARK: - Goal breakdown

    static func goalBreakdownSection(
        useLLM: Bool,
        showRaw: Bool,
        modelOverride: String?,
        goalOverride: String?,
        answers: [String]
    ) async {
        separator("5. 目标拆解 → 落库 → 进度")

        let text = goalOverride ?? "三个月内学会 SwiftUI 并上架一个 App"

        let client: LLMClient? = {
            guard useLLM, var configuration = LLMConfiguration.load() else { return nil }
            if let modelOverride { configuration.model = modelOverride }
            return OpenAICompatibleClient(configuration: configuration)
        }()
        let parser = GoalBreakdownParser(client: client)

        guard let database = try? Database.inMemory(),
              let demoUser = try? database.upsertUser(user) else {
            print("数据库初始化失败")
            return
        }
        let planner = GoalPlanner(database: database, parser: parser)

        print("目标：\(text)")
        let started = Date()
        do {
            var draft = await planner.draft(text)

            if draft.needsClarification {
                print("\n模型要求先补充信息（不再硬拆）：")
                for (index, question) in draft.questions.enumerated() {
                    print("  \(index + 1). \(question)")
                }
                guard !answers.isEmpty else {
                    print("\n（用 --answers \"回答1|回答2\" 模拟用户回答）")
                    return
                }
                print("\n模拟用户回答：")
                for answer in answers { print("  · \(answer)") }
                draft = await planner.draft(text, context: answers)
                if draft.needsClarification {
                    print("\n还需要更多信息：")
                    for (index, question) in draft.questions.enumerated() {
                        print("  \(index + 1). \(question)")
                    }
                    return
                }
            }

            let planned = try planner.commit(draft, user: demoUser)
            let elapsed = Date().timeIntervalSince(started)

            if planned.usedLLM {
                print(String(format: "模型拆解完成，耗时 %.1f 秒", elapsed))
            } else {
                print("未走模型，使用通用模板")
            }

            print("\n拆出 \(planned.steps.count) 步，每步占 "
                  + String(format: "%.2f%%", 100.0 / Double(planned.steps.count)) + "：\n")

            for step in planned.steps {
                let badge = step.recurrence.map { " · \($0.label)" } ?? ""
                print(String(format: "%2d. %@（约 %d 分钟%@）", step.seq, step.title, step.estimatedMinutes, badge))
                if let detail = step.detail {
                    print("     ↳ \(detail)")
                }
            }

            let totalMinutes = planned.steps.reduce(0) { $0 + $1.estimatedMinutes }
            print("")
            print("总预计耗时：\(totalMinutes) 分钟（"
                  + String(format: "%.1f", Double(totalMinutes) / 60) + " 小时）")
            print("当前进度：\(planned.progress.doneSteps)/\(planned.progress.totalSteps) 步，"
                  + "\(planned.progress.percentLabel)")

            if !planned.warnings.isEmpty {
                print("\n提示：")
                for warning in planned.warnings { print("  · \(warning)") }
            }
            if showRaw, let raw = planned.rawOutput {
                print("\n原始输出：\(raw.prefix(700))")
            }
        } catch {
            print("拆解失败：\(error)")
        }
    }

    // MARK: - Scheduling

    static func schedulingSections(now: Date) {
        separator("2. 状态感知排程")

        let goal = Goal(userID: user.id, title: "三个月内做出自己的 iOS App")
        let step = Step(
            goalID: goal.id,
            seq: 7,
            title: "接入 LLM，跑通对话式添加事件",
            percent: 10,
            estimatedMinutes: 45
        )

        let tomorrow = calendar.date(byAdding: .day, value: 1, to: calendar.startOfDay(for: now))!
        let meetingStart = calendar.date(byAdding: .hour, value: 10, to: tomorrow)!
        let meeting = CalendarEvent(
            userID: user.id,
            title: "已有会议",
            startAt: meetingStart,
            endAt: meetingStart.addingTimeInterval(2 * 3600)
        )
        let tired = StateLog(userID: user.id, state: .tired, createdAt: now)

        print("待做步骤：\(step.title)（约 \(step.estimatedMinutes) 分钟）")
        print("已有占用：\(label(meeting.startAt)) → \(label(meeting.endAt))")
        print("当前状态：累")

        let suggestions = Scheduler().suggest(SchedulerInput(
            user: user,
            now: now,
            candidates: [SchedulerCandidate(step: step, goal: goal)],
            busyEvents: [meeting],
            recentStates: [tired]
        ))

        if suggestions.isEmpty {
            print("\n未来 14 天没有合适的空档。")
        } else {
            for (index, suggestion) in suggestions.enumerated() {
                print("\n候选 \(index + 1)  \(label(suggestion.start)) → \(label(suggestion.end))"
                      + String(format: "（评分 %.1f）", suggestion.score))
                for reason in suggestion.reasons { print("  · \(reason)") }
            }
        }

        separator("3. 同样一步，换了状态之后")
        for state in [UserState.busy, .uninspired] {
            let log = StateLog(userID: user.id, state: state, createdAt: now)
            let options = Scheduler().suggest(SchedulerInput(
                user: user,
                now: now,
                candidates: [SchedulerCandidate(step: step, goal: goal)],
                busyEvents: [meeting],
                recentStates: [log]
            ))
            if let first = options.first {
                print("状态「\(state.label)」→ \(label(first.start))"
                      + String(format: "（评分 %.1f）", first.score))
            } else {
                print("状态「\(state.label)」→ 无候选")
            }
        }
    }

    // MARK: - Chat-driven series edit

    static func chatSection(line: String, useLLM: Bool, modelOverride: String?) async {
        separator("6. 对话式修改重复事件")

        let database = try! Database.inMemory()
        let user = try! database.upsertUser(User(name: "演示", timezoneIdentifier: "Asia/Shanghai"))
        let cal = user.calendar

        let today = cal.startOfDay(for: Date())
        var startDay = today
        for _ in 0..<7 {
            if SeriesMatcher.mondayBasedWeekday(startDay, calendar: cal) == 4 { break }
            startDay = cal.date(byAdding: .day, value: 1, to: startDay)!
        }
        let seriesStart = cal.date(byAdding: .hour, value: 9, to: startDay)!
        let seeded = EventSeries.expand(
            userID: user.id, title: "开会",
            start: seriesStart, end: seriesStart.addingTimeInterval(3600),
            weekdays: [4], weeks: 4, calendar: cal
        )
        try! database.insertEvents(seeded)
        print("预置：每周四 09:00–10:00「开会」，共 \(seeded.count) 次")

        let client: LLMClient? = {
            guard useLLM, var configuration = LLMConfiguration.load() else { return nil }
            if let modelOverride { configuration.model = modelOverride }
            return OpenAICompatibleClient(configuration: configuration)
        }()
        let orchestrator = ChatOrchestrator(
            database: database,
            intentParser: IntentParser(client: client),
            planner: GoalPlanner(database: database, parser: GoalBreakdownParser(client: client))
        )

        print("\n用户：\(line)")
        let turn = await orchestrator.handle(line, user: user)
        print("助手：\(turn.reply)")
        for note in turn.notes { print("  ⚠️ \(note)") }

        guard let proposal = turn.proposal else { return }

        do {
            let result = try orchestrator.commit(proposal, user: user)
            print("\n确认后：\(result.message)")

            let end = cal.date(byAdding: .day, value: 60, to: today)!
            let after = try database.events(userID: user.id, from: today, to: end)
            for event in after.sorted(by: { $0.startAt < $1.startAt }) {
                print("  · \(label(event.startAt)) – \(label(event.endAt))")
            }
        } catch {
            print("提交失败：\(error)")
        }
    }

    // MARK: - LLM comparison

    static func llmComparison(now: Date, showRaw: Bool) async {
        separator("4. 真实模型对比")

        guard let configuration = LLMConfiguration.load() else {
            print("⚠️ 未检测到 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL")
            print("   先运行 ./scripts/setup-llm.sh")
            return
        }
        print("模型：\(configuration.model)")
        print("地址：\(configuration.baseURL.absoluteString)")
        print("")

        let parser = IntentParser(client: OpenAICompatibleClient(configuration: configuration))
        let samples = [
            "明天下午三点和小组开会",
            "我现在好累，下一个 10% 什么时候做比较好",
            "这周六有什么安排",
            "我三个月内想学会 SwiftUI",
            "后天上午十点半跟导师汇报"
        ]

        var disagreements = 0
        for text in samples {
            let ruleIntent = rules.parse(text, now: now)
            let started = Date()
            let result = await parser.parse(text, user: user, now: now)
            let elapsed = Date().timeIntervalSince(started) * 1000

            print("用户：\(text)")
            print("  规则 → \(describe(ruleIntent))")
            print("  模型 → \(describe(result.intent))"
                  + String(format: "  [%.0f ms]", elapsed))

            if let failure = result.failure {
                print("  ⚠️ 降级原因：\(failure)")
            }
            if showRaw, let raw = result.rawOutput {
                print("  原始输出：\(raw.replacingOccurrences(of: "\n", with: " "))")
            }
            if result.usedLLM, ruleIntent.kind != result.intent.kind {
                disagreements += 1
                print("  ↳ 分类不一致")
            }

            if let expression = result.intent.time,
               let resolution = resolver.resolve(expression, user: user, now: now) {
                print("  解析时间 → \(label(resolution.interval.start))")
            }
            print("")
        }

        print("分类不一致：\(disagreements) / \(samples.count)")
    }

    // MARK: - Helpers

    static func describe(_ intent: ParsedIntent) -> String {
        var parts = [intent.kind.rawValue]
        if let title = intent.title { parts.append("title=\(title)") }
        if let state = intent.state { parts.append("state=\(state.rawValue)") }
        return parts.joined(separator: " | ")
    }

    static func label(_ date: Date) -> String { stamp.string(from: date) }

    static func separator(_ title: String) {
        print("\n" + String(repeating: "─", count: 52))
        print(title)
        print(String(repeating: "─", count: 52))
    }
}
