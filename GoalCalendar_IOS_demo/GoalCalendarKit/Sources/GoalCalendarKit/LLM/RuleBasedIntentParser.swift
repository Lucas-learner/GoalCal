import Foundation

/// Offline intent parser. Two jobs:
/// 1. keep the app usable with no API key and no network;
/// 2. catch the cases where the model returns something unusable.
///
/// It is intentionally shallow — it recognises the shapes of the five core
/// commands plus the three states, and nothing more. Anything cleverer belongs
/// in the LLM path.
public struct RuleBasedIntentParser: Sendable {

    public init() {}

    public func parse(_ text: String, now: Date = Date()) -> ParsedIntent {
        let input = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !input.isEmpty else {
            return ParsedIntent(kind: .unknown, confidence: 0, degraded: true, rawText: text)
        }

        let time = Self.extractTime(input)
        let state = Self.detectState(input)

        if Self.isConfirmation(input) {
            return ParsedIntent(kind: .confirm, confidence: 0.6, degraded: true, rawText: text)
        }
        if input.contains("删除") || input.contains("删掉") || input.contains("取消") {
            return ParsedIntent(
                kind: .deleteEvent,
                title: Self.extractTitle(input),
                time: time,
                confidence: 0.6,
                degraded: true,
                rawText: text
            )
        }
        if Self.isGoalCreation(input) {
            return ParsedIntent(
                kind: .createGoal,
                title: Self.extractGoalTitle(input),
                confidence: 0.6,
                degraded: true,
                rawText: text
            )
        }
        if let state {
            return ParsedIntent(kind: .reportState, state: state, confidence: 0.6, degraded: true, rawText: text)
        }
        if Self.isCompletion(input) {
            return ParsedIntent(kind: .completeEvent, time: time, confidence: 0.6, degraded: true, rawText: text)
        }
        if Self.isScheduleRequest(input) {
            return ParsedIntent(kind: .scheduleNext, confidence: 0.5, degraded: true, rawText: text)
        }
        if Self.isQuery(input) {
            return ParsedIntent(
                kind: .queryEvents,
                queryRange: time ?? TimeExpression(day: .today),
                confidence: 0.5,
                degraded: true,
                rawText: text
            )
        }
        // Defaulting everything to "add an event" turns noise into calendar
        // entries. Require either a recognisable time or an action verb.
        if time != nil || Self.looksLikeEventRequest(input) {
            return ParsedIntent(
                kind: .addEvent,
                title: Self.extractTitle(input),
                time: time,
                confidence: 0.5,
                degraded: true,
                rawText: text
            )
        }
        return ParsedIntent(kind: .unknown, confidence: 0.3, degraded: true, rawText: text)
    }

    /// Verbs that make a phrase plausibly a calendar request even without a time.
    static func looksLikeEventRequest(_ text: String) -> Bool {
        let verbs = [
            "开会", "见面", "讨论", "汇报", "提交", "参加", "面试", "体检",
            "旅行", "加班", "值班", "交付", "上线", "复盘", "打卡", "约", "聊",
            "写", "做", "去", "见", "买", "吃", "跑", "练", "学", "看", "听",
            "送", "取", "交"
        ]
        return verbs.contains { text.contains($0) }
    }

    // MARK: - Time extraction

    static func extractTime(_ text: String) -> TimeExpression? {
        var expression = TimeExpression()
        var found = false

        if let groups = firstMatch(Patterns.monthDay, in: text) {
            expression.month = int(groups, 0)
            expression.dayOfMonth = int(groups, 1)
            found = true
        }
        if let groups = firstMatch(Patterns.dayWord, in: text), let word = groups.first ?? nil {
            expression.day = dayReference(word)
            found = true
        }
        if let groups = firstMatch(Patterns.weekday, in: text),
           let number = weekdayNumber(groups.last ?? nil) {
            expression.weekday = number
            expression.weekOffset = (groups.first ?? nil) == "下" ? 1 : 0
            expression.day = .weekday
            found = true
        }
        if let groups = firstMatch(Patterns.clock, in: text) {
            if let periodWord = groups[0] {
                expression.period = dayPeriod(periodWord)
            }
            expression.hour = groups[1].flatMap(cnNumber)
            if let minuteText = groups.count > 2 ? groups[2] : nil {
                if minuteText == "半" {
                    expression.minute = 30
                } else {
                    expression.minute = cnNumber(minuteText.replacingOccurrences(of: "分", with: ""))
                }
            }
            found = true
        }
        if let groups = firstMatch(Patterns.duration, in: text), let amount = groups[0].flatMap(cnNumber) {
            let unit = groups[1] ?? ""
            expression.durationMinutes = unit.contains("分") ? amount : amount * 60
            found = true
        }

        return found ? expression : nil
    }

    /// Best-effort title: the sentence minus the time words and leading filler.
    static func extractTitle(_ text: String) -> String? {
        var result = text
        for pattern in [Patterns.monthDay, Patterns.weekday, Patterns.dayWord, Patterns.clock, Patterns.duration] {
            result = replacingMatches(pattern, in: result, with: "")
        }
        let leadingFillers = [
            "帮我", "麻烦", "请", "把", "给我", "我要", "我想", "记得", "安排",
            "添加", "新增", "加一个", "记一下",
            // Verb of the command itself must not leak into the title.
            "删除", "删掉", "取消",
            // Leftover possessive after removing "明天的": "的会" -> "会".
            "的"
        ]
        var changed = true
        while changed {
            changed = false
            for filler in leadingFillers where result.hasPrefix(filler) {
                result = String(result.dropFirst(filler.count))
                changed = true
            }
            result = result.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        result = result.trimmingCharacters(in: CharacterSet(charactersIn: "，。,.!！?？、 吧呢啊哦呀"))
        return result.isEmpty ? nil : result
    }

    private static func extractGoalTitle(_ text: String) -> String? {
        var result = text
        for filler in ["我", "想要", "想在", "我要", "目标是", "目标", "帮我"] {
            result = result.replacingOccurrences(of: filler, with: "")
        }
        // "我的目标是X" leaves a dangling possessive after removing 我/目标是.
        while result.hasPrefix("的") {
            result.removeFirst()
        }
        result = result.trimmingCharacters(in: CharacterSet(charactersIn: "，。,.!！?？、 "))
        return result.isEmpty ? text : result
    }

    // MARK: - Signals

    static func detectState(_ text: String) -> UserState? {
        if text.contains("没灵感") || text.contains("卡住") || text.contains("写不出") || text.contains("没思路") {
            return .uninspired
        }
        if text.contains("累") || text.contains("疲惫") || text.contains("没精神") || text.contains("困") {
            return .tired
        }
        if text.contains("忙") || text.contains("没空") || text.contains("没时间") {
            return .busy
        }
        return nil
    }

    static func isGoalCreation(_ text: String) -> Bool {
        if text.contains("目标") { return true }
        // Verbs that essentially only appear in long-term goals.
        let strong = ["学会", "上架", "转行", "考过", "坚持", "攒够", "读完", "达到"]
        if strong.contains(where: { text.contains($0) }) { return true }
        // "完成" alone is a task ("完成报告"), so it needs a time horizon to
        // count as a goal ("三个月内完成这本书").
        let horizons = ["个月内", "周内", "年内", "天内", "长期", "三个月", "半年"]
        if text.contains("完成"), horizons.contains(where: { text.contains($0) }) { return true }
        return false
    }

    static func isCompletion(_ text: String) -> Bool {
        ["完成了", "做完了", "搞定了", "做完了"].contains { text.contains($0) }
    }

    static func isQuery(_ text: String) -> Bool {
        ["有什么", "查一下", "查看", "看看", "有哪些", "有无安排", "有没有安排"].contains { text.contains($0) }
    }

    static func isScheduleRequest(_ text: String) -> Bool {
        text.contains("下一步") || text.contains("什么时候做") || text.contains("什么时候安排")
    }

    static func isConfirmation(_ text: String) -> Bool {
        let normalized = text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let exact = ["好", "好的", "可以", "行", "嗯", "确认", "就这样", "就这个", "ok", "okay", "是的", "对"]
        if exact.contains(normalized) { return true }
        return normalized.hasPrefix("就这个") || normalized.hasPrefix("确认")
    }

    // MARK: - Word maps

    private static func dayReference(_ word: String) -> DayReference {
        switch word {
        case "明天", "明日": return .tomorrow
        case "大后天", "后天": return .dayAfterTomorrow
        default: return .today
        }
    }

    private static func dayPeriod(_ word: String) -> DayPeriod {
        switch word {
        case "上午", "早上", "早晨", "凌晨": return .morning
        case "中午": return .noon
        case "下午": return .afternoon
        case "傍晚", "晚上", "夜里": return .evening
        default: return .unspecified
        }
    }

    private static func weekdayNumber(_ word: String?) -> Int? {
        guard let word else { return nil }
        switch word {
        case "一": return 1
        case "二": return 2
        case "三": return 3
        case "四": return 4
        case "五": return 5
        case "六": return 6
        case "日", "天": return 7
        default: return nil
        }
    }

    static func cnNumber(_ text: String) -> Int? {
        if let value = Int(text) { return value }
        let digits: [Character: Int] = [
            "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
            "六": 6, "七": 7, "八": 8, "九": 9
        ]
        if text == "十" { return 10 }
        if let tenIndex = text.firstIndex(of: "十") {
            let tensPart = text[text.startIndex..<tenIndex]
            let onesPart = text[text.index(after: tenIndex)...]
            let tens = tensPart.isEmpty ? 1 : tensPart.first.flatMap { digits[$0] }
            let ones = onesPart.isEmpty ? 0 : onesPart.first.flatMap { digits[$0] }
            guard let tens, let ones else { return nil }
            return tens * 10 + ones
        }
        if text.count == 1, let first = text.first {
            return digits[first]
        }
        return nil
    }

    private static func int(_ groups: [String?], _ index: Int) -> Int? {
        guard index < groups.count, let text = groups[index] else { return nil }
        return Int(text)
    }

    // MARK: - Regex plumbing

    private enum Patterns {
        static let monthDay = regex("([0-9]{1,2})月([0-9]{1,2})[日号]")
        static let dayWord = regex("(今天|今日|明天|明日|大后天|后天)")
        static let weekday = regex("(下|这|本)?(?:周|星期|礼拜)([一二三四五六日天])")
        static let clock = regex("(上午|早上|早晨|中午|下午|傍晚|晚上|夜里|凌晨)?([0-9]{1,2}|[一二三四五六七八九十两]{1,3})[点时](半|[0-9]{1,2}分)?")
        static let duration = regex("([0-9]{1,3}|[一二三四五六七八九十两]{1,3})个?(小时|钟头|分钟)")

        private static func regex(_ pattern: String) -> NSRegularExpression {
            // Patterns are literals covered by tests; a failure here is a
            // programming error, not a runtime input problem.
            try! NSRegularExpression(pattern: pattern)
        }
    }

    private static func firstMatch(_ pattern: NSRegularExpression, in text: String) -> [String?]? {
        let range = NSRange(text.startIndex..<text.endIndex, in: text)
        guard let match = pattern.firstMatch(in: text, options: [], range: range) else { return nil }
        var groups: [String?] = []
        for index in 1..<match.numberOfRanges {
            let groupRange = match.range(at: index)
            if groupRange.location == NSNotFound {
                groups.append(nil)
            } else {
                groups.append(Range(groupRange, in: text).map { String(text[$0]) })
            }
        }
        return groups
    }

    private static func replacingMatches(_ pattern: NSRegularExpression, in text: String, with template: String) -> String {
        let range = NSRange(text.startIndex..<text.endIndex, in: text)
        return pattern.stringByReplacingMatches(in: text, options: [], range: range, withTemplate: template)
    }
}
