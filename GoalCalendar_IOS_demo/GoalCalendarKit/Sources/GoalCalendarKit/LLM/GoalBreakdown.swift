import Foundation

/// One micro-step proposed by the breakdown model, before it is persisted.
public struct BreakdownStep: Codable, Hashable, Sendable {
    public var title: String
    public var detail: String?
    public var estimatedMinutes: Int
    /// nil for a one-off task; set for habits like "walk 30 minutes".
    public var recurrence: StepRecurrence?

    public init(
        title: String,
        detail: String? = nil,
        estimatedMinutes: Int = 45,
        recurrence: StepRecurrence? = nil
    ) {
        self.title = title
        self.detail = detail
        self.estimatedMinutes = estimatedMinutes
        self.recurrence = recurrence
    }

    private enum CodingKeys: String, CodingKey {
        case title, detail, recurrence
        case estimatedMinutes = "estimated_minutes"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        title = ((try? container.decodeIfPresent(String.self, forKey: .title)) ?? nil) ?? ""
        detail = (try? container.decodeIfPresent(String.self, forKey: .detail)) ?? nil

        // Models sometimes emit "45" instead of 45, and sometimes omit it.
        let number = (try? container.decodeIfPresent(Int.self, forKey: .estimatedMinutes)) ?? nil
        if let number {
            estimatedMinutes = number
        } else if let text = (try? container.decodeIfPresent(String.self, forKey: .estimatedMinutes)) ?? nil,
                  let parsed = Int(text) {
            estimatedMinutes = parsed
        } else {
            estimatedMinutes = 45
        }

        // Accept both {"kind":"daily"} and the shortcut "daily".
        if let object = (try? container.decodeIfPresent(StepRecurrence.self, forKey: .recurrence)) ?? nil {
            recurrence = object
        } else if let raw = (try? container.decodeIfPresent(String.self, forKey: .recurrence)) ?? nil {
            recurrence = StepRecurrence(kind: StepRecurrence.Kind(rawValue: raw) ?? .daily)
        } else {
            recurrence = nil
        }
    }
}

public struct BreakdownResult: Sendable {
    public var steps: [BreakdownStep]
    /// Non-empty when the model needs more information before it can plan.
    public var questions: [String]
    public var usedLLM: Bool
    public var warnings: [String]
    public var rawOutput: String?

    public init(
        steps: [BreakdownStep],
        questions: [String] = [],
        usedLLM: Bool,
        warnings: [String] = [],
        rawOutput: String? = nil
    ) {
        self.steps = steps
        self.questions = questions
        self.usedLLM = usedLLM
        self.warnings = warnings
        self.rawOutput = rawOutput
    }

    public var needsClarification: Bool { !questions.isEmpty }
}

/// Sanitises whatever the model returned before it can touch the database.
public enum BreakdownValidator {

    public static let preferredRange = 8...12
    public static let acceptedRange = 5...15
    public static let maxQuestions = 5

    public static func normalize(_ steps: [BreakdownStep]) -> (steps: [BreakdownStep], warnings: [String]) {
        var warnings: [String] = []
        var seenTitles = Set<String>()
        var cleaned: [BreakdownStep] = []

        for step in steps {
            let title = step.title.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !title.isEmpty else {
                warnings.append("丢弃了一个空标题的步骤")
                continue
            }

            let key = title.lowercased()
            guard !seenTitles.contains(key) else {
                warnings.append("去掉重复步骤：\(title)")
                continue
            }
            seenTitles.insert(key)

            let minutes = min(max(step.estimatedMinutes, 15), 240)
            if minutes != step.estimatedMinutes {
                warnings.append("「\(title)」的时长 \(step.estimatedMinutes) 分钟已调整到 \(minutes) 分钟")
            }

            // Prefer the model's structured field, but fall back to the wording:
            // "每天…" is the cadence even when the model omits `recurrence`.
            var recurrence = step.recurrence
                ?? RecurrenceInference.infer(from: title + " " + (step.detail ?? ""))

            switch recurrence?.kind {
            case .daily:
                // A daily habit has no meaningful "times per week".
                recurrence?.timesPerWeek = nil
            case .weekly:
                let raw = recurrence?.timesPerWeek ?? 3
                let clamped = min(max(raw, 1), 7)
                if clamped != raw {
                    warnings.append("「\(title)」的每周次数 \(raw) 已调整到 \(clamped)")
                }
                recurrence?.timesPerWeek = clamped
            case nil:
                break
            }

            let detail = step.detail?.trimmingCharacters(in: .whitespacesAndNewlines)
            cleaned.append(BreakdownStep(
                title: title,
                detail: (detail?.isEmpty ?? true) ? nil : detail,
                estimatedMinutes: minutes,
                recurrence: recurrence
            ))
        }

        if cleaned.count > acceptedRange.upperBound {
            warnings.append("步骤过多（\(cleaned.count) 步），只保留前 \(acceptedRange.upperBound) 步")
            cleaned = Array(cleaned.prefix(acceptedRange.upperBound))
        }
        if cleaned.count < preferredRange.lowerBound {
            warnings.append("只拆出 \(cleaned.count) 步，建议 \(preferredRange.lowerBound)–\(preferredRange.upperBound) 步")
        }

        return (cleaned, warnings)
    }

    public static func normalizeQuestions(_ questions: [String]) -> [String] {
        var seen = Set<String>()
        var result: [String] = []
        for question in questions {
            let trimmed = question.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty, !seen.contains(trimmed) else { continue }
            seen.insert(trimmed)
            result.append(trimmed)
            if result.count >= maxQuestions { break }
        }
        return result
    }
}

/// Turns a goal sentence into an ordered list of micro-steps — or, when the
/// request is underspecified, into the questions needed to plan properly.
public struct GoalBreakdownParser: Sendable {

    private let client: LLMClient?

    public init(client: LLMClient?) {
        self.client = client
    }

    public var isOnline: Bool { client != nil }

    /// - Parameter context: answers the user already gave to earlier questions.
    public func breakdown(_ goalText: String, context: [String] = []) async -> BreakdownResult {
        let template = Self.template(for: goalText)

        guard let client else {
            return BreakdownResult(
                steps: template,
                usedLLM: false,
                warnings: ["未配置模型，已生成通用模板，请手动改成更具体的步骤"]
            )
        }

        do {
            let prompt = Self.userMessage(goalText, context: context)
            let raw = try await client.complete(system: PromptBuilder.breakdownSystem, user: prompt)

            guard let json = JSONExtraction.firstJSON(in: raw),
                  let data = json.data(using: .utf8)
            else {
                throw LLMError.malformedJSON(raw)
            }

            let response = try Self.decodeResponse(from: data)
            let questions = BreakdownValidator.normalizeQuestions(response.questions ?? [])

            // Asking is a valid outcome, not a failure. Anything else here would
            // force the model to invent numbers it does not have.
            if response.needClarification == true || (response.steps == nil && !questions.isEmpty) {
                guard !questions.isEmpty else { throw LLMError.malformedJSON(raw) }
                return BreakdownResult(steps: [], questions: questions, usedLLM: true, rawOutput: raw)
            }

            guard let rawSteps = response.steps else { throw LLMError.malformedJSON(raw) }
            let (steps, warnings) = BreakdownValidator.normalize(rawSteps)
            guard steps.count >= 3 else { throw LLMError.malformedJSON(raw) }

            return BreakdownResult(steps: steps, usedLLM: true, warnings: warnings, rawOutput: raw)
        } catch {
            let message = (error as? LLMError)?.description ?? error.localizedDescription
            return BreakdownResult(
                steps: template,
                usedLLM: false,
                warnings: ["模型拆解失败：\(message)", "已生成通用模板，请手动调整"]
            )
        }
    }

    static func userMessage(_ goalText: String, context: [String]) -> String {
        guard !context.isEmpty else { return goalText }
        let answers = context.map { "· \($0)" }.joined(separator: "\n")
        return """
        目标：\(goalText)

        用户补充的信息：
        \(answers)
        """
    }

    private struct RawResponse: Decodable {
        var needClarification: Bool?
        var questions: [String]?
        var steps: [BreakdownStep]?

        enum CodingKeys: String, CodingKey {
            case needClarification = "need_clarification"
            case questions, steps
        }
    }

    private static func decodeResponse(from data: Data) throws -> RawResponse {
        if let response = try? JSONDecoder().decode(RawResponse.self, from: data) {
            return response
        }
        // Some providers drop the wrapper and return a bare array.
        let steps = try JSONDecoder().decode([BreakdownStep].self, from: data)
        return RawResponse(steps: steps)
    }

    /// Used when the model is unavailable. Deliberately generic.
    static func template(for goalText: String) -> [BreakdownStep] {
        let subject = goalText.count > 16 ? String(goalText.prefix(16)) + "…" : goalText
        return [
            BreakdownStep(title: "写清「\(subject)」的完成标准", detail: "用一句话定义做成什么样算成功，且可验证", estimatedMinutes: 30),
            BreakdownStep(title: "列出达成目标需要的 3–5 个里程碑", detail: "粗粒度即可，不求精确", estimatedMinutes: 45),
            BreakdownStep(title: "为第一个里程碑准备所需资源", detail: "工具、账号、材料、需要找的人", estimatedMinutes: 45),
            BreakdownStep(title: "完成第一个里程碑的最小可交付", detail: "先做出能跑通的最简版本", estimatedMinutes: 90),
            BreakdownStep(title: "复盘并调整后续计划", detail: "看哪一步估时不准，修正后面的步骤", estimatedMinutes: 30)
        ]
    }
}

/// Pulls the first JSON value out of model output. Handles code fences and
/// leading prose, and accepts either an object or an array.
enum JSONExtraction {
    static func firstJSON(in text: String) -> String? {
        let objectStart = text.firstIndex(of: "{")
        let arrayStart = text.firstIndex(of: "[")

        let start: String.Index
        let closer: Character
        switch (objectStart, arrayStart) {
        case let (object?, array?):
            if object < array {
                (start, closer) = (object, "}")
            } else {
                (start, closer) = (array, "]")
            }
        case let (object?, nil):
            (start, closer) = (object, "}")
        case let (nil, array?):
            (start, closer) = (array, "]")
        case (nil, nil):
            return nil
        }

        guard let end = text.lastIndex(of: closer), start < end else { return nil }
        return String(text[start...end])
    }
}
