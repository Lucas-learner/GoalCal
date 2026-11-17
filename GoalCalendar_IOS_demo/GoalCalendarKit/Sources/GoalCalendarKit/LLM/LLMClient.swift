import Foundation

public protocol LLMClient: Sendable {
    /// Sends a system + user message and returns the assistant's raw text.
    /// Intentionally minimal: no calendar access, no tools, no date context.
    func complete(system: String, user: String) async throws -> String
}

public enum LLMError: Error, CustomStringConvertible {
    case notConfigured
    case transport(String)
    case http(status: Int, body: String)
    case emptyResponse
    case malformedJSON(String)

    public var description: String {
        switch self {
        case .notConfigured:
            return "未配置 LLM。请设置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL 环境变量。"
        case .transport(let message):
            return "网络请求失败：\(message)"
        case .http(let status, let body):
            return "LLM 返回 HTTP \(status)：\(body.prefix(300))"
        case .emptyResponse:
            return "LLM 返回为空"
        case .malformedJSON(let text):
            return "无法从 LLM 输出中提取 JSON：\(text.prefix(200))"
        }
    }
}

public struct LLMConfiguration: Sendable {
    public var baseURL: URL
    public var apiKey: String
    public var model: String
    public var temperature: Double
    public var timeout: TimeInterval
    /// Most OpenAI-compatible providers support `response_format`, but not all.
    /// On a 400 we retry once without it instead of failing the request.
    public var supportsJSONMode: Bool

    public init(
        baseURL: URL,
        apiKey: String,
        model: String,
        temperature: Double = 0.2,
        // Intent parsing answers in ~2s, but a 12-step goal breakdown generates
        // far more tokens and has been measured at ~30s. A 30s timeout would
        // silently degrade good breakdowns into the fallback template.
        timeout: TimeInterval = 90,
        supportsJSONMode: Bool = true
    ) {
        self.baseURL = baseURL
        self.apiKey = apiKey
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.supportsJSONMode = supportsJSONMode
    }

    /// Reads configuration from the environment so no secret ever lives in the
    /// repository or the app bundle.
    public static func load(
        from environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> LLMConfiguration? {
        guard
            let base = environment["LLM_BASE_URL"], let url = URL(string: base),
            let key = environment["LLM_API_KEY"], !key.isEmpty,
            let model = environment["LLM_MODEL"], !model.isEmpty
        else { return nil }
        return LLMConfiguration(baseURL: url, apiKey: key, model: model)
    }
}

public struct OpenAICompatibleClient: LLMClient {
    private let configuration: LLMConfiguration
    private let session: URLSession

    public init(configuration: LLMConfiguration, session: URLSession = .shared) {
        self.configuration = configuration
        self.session = session
    }

    public func complete(system: String, user: String) async throws -> String {
        let first = try await send(system: system, user: user, jsonMode: configuration.supportsJSONMode)

        // A provider that rejects `response_format` is common enough to handle
        // quietly: retry bare rather than surfacing a confusing 400 to the user.
        if first.status == 400, configuration.supportsJSONMode {
            let retry = try await send(system: system, user: user, jsonMode: false)
            return try Self.extractContent(from: retry.data, status: retry.status)
        }
        return try Self.extractContent(from: first.data, status: first.status)
    }

    private func send(system: String, user: String, jsonMode: Bool) async throws -> (status: Int, data: Data) {
        var request = URLRequest(url: configuration.baseURL.appendingPathComponent("chat/completions"))
        request.httpMethod = "POST"
        request.timeoutInterval = configuration.timeout
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(configuration.apiKey)", forHTTPHeaderField: "Authorization")

        var payload: [String: Any] = [
            "model": configuration.model,
            "temperature": configuration.temperature,
            "messages": [
                ["role": "system", "content": system],
                ["role": "user", "content": user]
            ]
        ]
        if jsonMode {
            payload["response_format"] = ["type": "json_object"]
        }
        request.httpBody = try JSONSerialization.data(withJSONObject: payload)

        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                throw LLMError.emptyResponse
            }
            return (http.statusCode, data)
        } catch let error as LLMError {
            throw error
        } catch {
            throw LLMError.transport(error.localizedDescription)
        }
    }

    private static func extractContent(from data: Data, status: Int) throws -> String {
        guard (200..<300).contains(status) else {
            throw LLMError.http(status: status, body: String(data: data, encoding: .utf8) ?? "")
        }
        struct Response: Decodable {
            struct Choice: Decodable {
                struct Message: Decodable { let content: String? }
                let message: Message
            }
            let choices: [Choice]
        }
        guard let decoded = try? JSONDecoder().decode(Response.self, from: data),
              let content = decoded.choices.first?.message.content,
              !content.isEmpty
        else {
            throw LLMError.emptyResponse
        }
        return content
    }
}

/// Deterministic stand-in for tests and for measuring scheduler behaviour without
/// burning API calls.
public struct StaticLLMClient: LLMClient {
    public let response: String
    public let error: LLMError?

    public init(response: String = "{}", error: LLMError? = nil) {
        self.response = response
        self.error = error
    }

    public func complete(system: String, user: String) async throws -> String {
        if let error { throw error }
        return response
    }
}
