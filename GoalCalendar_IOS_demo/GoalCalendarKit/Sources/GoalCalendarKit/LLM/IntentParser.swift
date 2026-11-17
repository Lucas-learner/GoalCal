import Foundation

public struct ParseResult: Sendable {
    public var intent: ParsedIntent
    /// False when the rule engine produced this because the LLM was missing or
    /// returned something unusable. The UI shows this so the user knows.
    public var usedLLM: Bool
    public var failure: String?
    /// The model's unprocessed text. Kept for prompt tuning: when a field comes
    /// back wrong, the only way to fix the prompt is to see what it actually said.
    public var rawOutput: String?

    public init(intent: ParsedIntent, usedLLM: Bool, failure: String? = nil, rawOutput: String? = nil) {
        self.intent = intent
        self.usedLLM = usedLLM
        self.failure = failure
        self.rawOutput = rawOutput
    }
}

/// Orchestrates: try the model, fall back to rules.
///
/// The fallback is not a nicety. A calendar that stops accepting input because
/// the network dropped is worse than one that understands a narrower range of
/// phrases, so a parse failure must never surface as an error to the user.
public struct IntentParser: Sendable {
    private let client: LLMClient?
    private let rules: RuleBasedIntentParser

    public init(client: LLMClient?, rules: RuleBasedIntentParser = RuleBasedIntentParser()) {
        self.client = client
        self.rules = rules
    }

    public var isOnline: Bool { client != nil }

    public func parse(_ text: String, user: User, now: Date = Date()) async -> ParseResult {
        let fallback = rules.parse(text, now: now)
        guard let client else {
            return ParseResult(intent: fallback, usedLLM: false, failure: LLMError.notConfigured.description)
        }

        do {
            let raw = try await client.complete(system: PromptBuilder.system, user: PromptBuilder.user(text))
            guard let jsonText = Self.extractJSONObject(from: raw),
                  let data = jsonText.data(using: .utf8)
            else {
                throw LLMError.malformedJSON(raw)
            }
            var intent = try JSONDecoder().decode(ParsedIntent.self, from: data)
            intent.rawText = text
            intent.degraded = false

            // A confident `unknown` is still an answer; a timid one means the
            // model did not understand and the rules are more likely right.
            if intent.kind == .unknown, intent.confidence < 0.5 {
                return ParseResult(intent: fallback, usedLLM: false, failure: "模型未能识别意图")
            }
            if intent.kind == .addEvent, intent.title == nil {
                intent.title = fallback.title
            }
            return ParseResult(intent: intent, usedLLM: true, rawOutput: raw)
        } catch {
            let message = (error as? LLMError)?.description ?? error.localizedDescription
            return ParseResult(intent: fallback, usedLLM: false, failure: message)
        }
    }

    /// Models wrap JSON in code fences or prose often enough that pulling the
    /// outermost braces is more reliable than string trimming.
    static func extractJSONObject(from text: String) -> String? {
        guard let start = text.firstIndex(of: "{"),
              let end = text.lastIndex(of: "}"),
              start < end
        else { return nil }
        return String(text[start...end])
    }
}
