import Foundation

/// Finds the repeating series a request refers to.
///
/// Matching is done here, in code, from structured clues the parser extracted —
/// never by asking the model which rows to touch. A wrong guess would silently
/// rewrite someone's whole calendar, so the rule is: match conservatively, and
/// return nothing rather than something plausible-but-wrong.
public enum SeriesMatcher {

    public struct Candidate: Sendable, Hashable {
        public var seriesID: String
        public var events: [CalendarEvent]
        public var score: Int
    }

    public static func bestSeries(
        in events: [CalendarEvent],
        title: String?,
        weekday: Int?,
        hour: Int?,
        calendar: Calendar
    ) -> Candidate? {
        let keyword = title?.trimmingCharacters(in: .whitespacesAndNewlines)
        let grouped = Dictionary(grouping: events.filter { $0.seriesID != nil }, by: { $0.seriesID! })
        guard !grouped.isEmpty else { return nil }

        // With no clue at all there is nothing safe to match on.
        let hasDayClue = weekday != nil
        let hasTimeClue = hour != nil
        let hasTitleClue = !(keyword?.isEmpty ?? true)
        guard hasDayClue || hasTimeClue || hasTitleClue else { return nil }

        var best: Candidate?

        for (seriesID, occurrences) in grouped {
            let ordered = occurrences.sorted { $0.startAt < $1.startAt }
            var score = 0

            if hasTitleClue, let keyword {
                guard ordered.contains(where: { $0.title.localizedCaseInsensitiveContains(keyword) }) else {
                    continue
                }
                score += 3
            }

            if let weekday {
                let matchesDay = ordered.contains {
                    SeriesMatcher.mondayBasedWeekday($0.startAt, calendar: calendar) == weekday
                }
                guard matchesDay else { continue }
                score += 2
            }

            if let hour {
                let matchesHour = ordered.contains {
                    calendar.component(.hour, from: $0.startAt) == hour
                }
                guard matchesHour else { continue }
                score += 2
            }

            guard score > 0 else { continue }

            if let current = best {
                if score > current.score {
                    best = Candidate(seriesID: seriesID, events: ordered, score: score)
                } else if score == current.score,
                          let first = ordered.first,
                          let bestFirst = current.events.first,
                          first.startAt < bestFirst.startAt {
                    best = Candidate(seriesID: seriesID, events: ordered, score: score)
                }
            } else {
                best = Candidate(seriesID: seriesID, events: ordered, score: score)
            }
        }

        return best
    }

    public static func mondayBasedWeekday(_ date: Date, calendar: Calendar) -> Int {
        (calendar.component(.weekday, from: date) + 5) % 7 + 1
    }
}
