import Foundation

/// What the user is asking for. The LLM picks one of these; it never returns a
/// date, and it never returns a duration it invented.
public enum IntentKind: String, Codable, CaseIterable, Sendable {
    case addEvent = "add_event"
    case deleteEvent = "delete_event"
    case queryEvents = "query_events"
    case createGoal = "create_goal"
    case reportState = "report_state"
    case scheduleNext = "schedule_next"
    case completeEvent = "complete_event"
    case rescheduleSeries = "reschedule_series"
    case deleteSeries = "delete_series"
    case confirm
    case smallTalk = "small_talk"
    case unknown
}

public enum DayReference: String, Codable, CaseIterable, Sendable {
    case today
    case tomorrow
    case dayAfterTomorrow = "day_after_tomorrow"
    case explicitDate = "explicit_date"
    case weekday
    case unspecified
}

public enum DayPeriod: String, Codable, CaseIterable, Sendable {
    case morning
    case noon
    case afternoon
    case evening
    case night
    case unspecified
}

/// Everything the LLM is allowed to tell us about a time.
///
/// Note what is *absent*: no absolute dates, no timezone conversion, no
/// conflict checks. The model reports only what the user said — "tomorrow",
/// "afternoon", "3 o'clock" — and `TimeResolver` turns that into an instant
/// using the user's own calendar.
///
/// All fields are optional and decoded leniently, because this comes from an
/// untrusted source and a malformed field must not sink the whole request.
public struct TimeExpression: Codable, Hashable, Sendable {
    /// The user's literal words, kept only for echoing back in replies.
    public var phrase: String?
    public var day: DayReference?
    /// "2026-10-20" — only when the user literally named a calendar date.
    public var explicitDate: String?
    /// Monday = 1 ... Sunday = 7.
    public var weekday: Int?
    /// 0 = this week, 1 = next week.
    public var weekOffset: Int?
    public var period: DayPeriod?
    /// As spoken: "下午3点" -> 3, "15:00" -> 15.
    public var hour: Int?
    public var minute: Int?
    /// "10月20日" — the model reports month and day only; the year is filled in
    /// by `TimeResolver`, never inferred by the model.
    public var month: Int?
    public var dayOfMonth: Int?
    public var durationMinutes: Int?

    public init(
        phrase: String? = nil,
        day: DayReference? = nil,
        explicitDate: String? = nil,
        weekday: Int? = nil,
        weekOffset: Int? = nil,
        period: DayPeriod? = nil,
        hour: Int? = nil,
        minute: Int? = nil,
        month: Int? = nil,
        dayOfMonth: Int? = nil,
        durationMinutes: Int? = nil
    ) {
        self.phrase = phrase
        self.day = day
        self.explicitDate = explicitDate
        self.weekday = weekday
        self.weekOffset = weekOffset
        self.period = period
        self.hour = hour
        self.minute = minute
        self.month = month
        self.dayOfMonth = dayOfMonth
        self.durationMinutes = durationMinutes
    }

    private enum CodingKeys: String, CodingKey {
        case phrase, day, explicitDate = "explicit_date", weekday
        case weekOffset = "week_offset", period, hour, minute
        case month, dayOfMonth = "day_of_month"
        case durationMinutes = "duration_minutes"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        phrase = (try? container.decodeIfPresent(String.self, forKey: .phrase)) ?? nil

        let dayRaw: String? = (try? container.decodeIfPresent(String.self, forKey: .day)) ?? nil
        day = dayRaw.flatMap(DayReference.init(rawValue:))

        let periodRaw: String? = (try? container.decodeIfPresent(String.self, forKey: .period)) ?? nil
        period = periodRaw.flatMap(DayPeriod.init(rawValue:))

        explicitDate = (try? container.decodeIfPresent(String.self, forKey: .explicitDate)) ?? nil
        weekday = TimeExpression.lenientInt(container, .weekday)
        weekOffset = TimeExpression.lenientInt(container, .weekOffset)
        hour = TimeExpression.lenientInt(container, .hour)
        minute = TimeExpression.lenientInt(container, .minute)
        month = TimeExpression.lenientInt(container, .month)
        dayOfMonth = TimeExpression.lenientInt(container, .dayOfMonth)
        durationMinutes = TimeExpression.lenientInt(container, .durationMinutes)
    }

    /// Models occasionally emit `"3"` instead of `3`.
    private static func lenientInt(
        _ container: KeyedDecodingContainer<CodingKeys>,
        _ key: CodingKeys
    ) -> Int? {
        if let value = (try? container.decodeIfPresent(Int.self, forKey: key)) ?? nil {
            return value
        }
        if let text = (try? container.decodeIfPresent(String.self, forKey: key)) ?? nil {
            return Int(text)
        }
        return nil
    }
}

/// The result of turning a `TimeExpression` into real instants.
public struct TimeResolution: Hashable, Sendable {
    public var interval: DateInterval
    /// Why the resolved time differs from the literal words, e.g. the user said
    /// "today 3pm" but it is already 4pm. Shown to the user, never hidden.
    public var notes: [String]

    public init(interval: DateInterval, notes: [String] = []) {
        self.interval = interval
        self.notes = notes
    }
}

/// Clues for locating which repeating series the user means.
public struct SeriesMatch: Codable, Hashable, Sendable {
    /// Monday = 1 ... Sunday = 7.
    public var weekday: Int?
    /// 24-hour clock: this is matched against stored times, not spoken as 12h.
    public var hour: Int?
    public var minute: Int?
    public var title: String?

    public init(weekday: Int? = nil, hour: Int? = nil, minute: Int? = nil, title: String? = nil) {
        self.weekday = weekday
        self.hour = hour
        self.minute = minute
        self.title = title
    }

    private enum CodingKeys: String, CodingKey {
        case weekday, hour, minute, title
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        weekday = (try? container.decodeIfPresent(Int.self, forKey: .weekday)) ?? nil
        hour = (try? container.decodeIfPresent(Int.self, forKey: .hour)) ?? nil
        minute = (try? container.decodeIfPresent(Int.self, forKey: .minute)) ?? nil
        title = (try? container.decodeIfPresent(String.self, forKey: .title)) ?? nil
    }
}

/// What a repeating series should become.
public struct SeriesTarget: Codable, Hashable, Sendable {
    public var hour: Int?
    public var minute: Int?
    public var weekday: Int?
    public var durationMinutes: Int?

    public init(hour: Int? = nil, minute: Int? = nil, weekday: Int? = nil, durationMinutes: Int? = nil) {
        self.hour = hour
        self.minute = minute
        self.weekday = weekday
        self.durationMinutes = durationMinutes
    }

    private enum CodingKeys: String, CodingKey {
        case hour, minute, weekday
        case durationMinutes = "duration_minutes"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        hour = (try? container.decodeIfPresent(Int.self, forKey: .hour)) ?? nil
        minute = (try? container.decodeIfPresent(Int.self, forKey: .minute)) ?? nil
        weekday = (try? container.decodeIfPresent(Int.self, forKey: .weekday)) ?? nil
        durationMinutes = (try? container.decodeIfPresent(Int.self, forKey: .durationMinutes)) ?? nil
    }
}

/// The full parse of one user utterance.
///
/// Decode-only on purpose: an intent is something the app *receives* from the
/// model and from the rule engine. Nothing should ever encode one back out.
public struct ParsedIntent: Decodable, Hashable, Sendable {
    public var kind: IntentKind
    public var title: String?
    public var time: TimeExpression?
    /// Separate range for "我明天有什么安排" so it does not collide with `time`.
    public var queryRange: TimeExpression?
    public var goalTitle: String?
    public var state: UserState?
    /// 1-based step index when the user says "做第三步".
    public var stepOrdinal: Int?
    public var seriesMatch: SeriesMatch?
    public var seriesTarget: SeriesTarget?
    public var confidence: Double
    /// True when the rule engine produced this instead of the LLM.
    public var degraded: Bool
    public var rawText: String?

    public init(
        kind: IntentKind,
        title: String? = nil,
        time: TimeExpression? = nil,
        queryRange: TimeExpression? = nil,
        goalTitle: String? = nil,
        state: UserState? = nil,
        stepOrdinal: Int? = nil,
        seriesMatch: SeriesMatch? = nil,
        seriesTarget: SeriesTarget? = nil,
        confidence: Double = 1.0,
        degraded: Bool = false,
        rawText: String? = nil
    ) {
        self.kind = kind
        self.title = title
        self.time = time
        self.queryRange = queryRange
        self.goalTitle = goalTitle
        self.state = state
        self.stepOrdinal = stepOrdinal
        self.seriesMatch = seriesMatch
        self.seriesTarget = seriesTarget
        self.confidence = confidence
        self.degraded = degraded
        self.rawText = rawText
    }

    private enum CodingKeys: String, CodingKey {
        case intent, title, time
        case queryRange = "query_range"
        case goalTitle = "goal_title"
        case state, confidence
        case stepOrdinal = "step_ordinal"
        case seriesMatch = "series_match"
        case seriesTarget = "series_target"
        case rawText = "raw_text"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let raw: String? = (try? container.decodeIfPresent(String.self, forKey: .intent)) ?? nil
        kind = raw.flatMap(IntentKind.init(rawValue:)) ?? .unknown
        title = (try? container.decodeIfPresent(String.self, forKey: .title)) ?? nil
        time = (try? container.decodeIfPresent(TimeExpression.self, forKey: .time)) ?? nil
        queryRange = (try? container.decodeIfPresent(TimeExpression.self, forKey: .queryRange)) ?? nil
        goalTitle = (try? container.decodeIfPresent(String.self, forKey: .goalTitle)) ?? nil
        let stateRaw: String? = (try? container.decodeIfPresent(String.self, forKey: .state)) ?? nil
        state = stateRaw.flatMap(UserState.init(rawValue:))
        stepOrdinal = (try? container.decodeIfPresent(Int.self, forKey: .stepOrdinal)) ?? nil
        seriesMatch = (try? container.decodeIfPresent(SeriesMatch.self, forKey: .seriesMatch)) ?? nil
        seriesTarget = (try? container.decodeIfPresent(SeriesTarget.self, forKey: .seriesTarget)) ?? nil
        confidence = (try? container.decodeIfPresent(Double.self, forKey: .confidence)) ?? nil ?? 0.5
        degraded = false
        rawText = (try? container.decodeIfPresent(String.self, forKey: .rawText)) ?? nil
    }
}
