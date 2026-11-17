import Foundation

/// Recovers a recurrence cadence from the step's own wording.
///
/// The model reliably writes "每天早餐加一份蛋白质" but unreliably emits the
/// matching structured field, and a plan that silently loses its cadence is worse
/// than no plan. So the text is treated as the source of truth and the structure
/// is derived deterministically — the same division of labour used everywhere
/// else in this app.
public enum RecurrenceInference {

    public static func infer(from text: String) -> StepRecurrence? {
        if text.contains("每天") || text.contains("每日") || text.contains("天天") {
            return StepRecurrence(kind: .daily)
        }

        guard let weeklyRange = text.range(of: "每周") else { return nil }

        // Look for a count right after 每周: "每周 3 次", "每周两次训练".
        let tail = text[weeklyRange.upperBound...]
        if let count = leadingCount(in: tail) {
            return StepRecurrence(kind: .weekly, timesPerWeek: min(max(count, 1), 7))
        }

        // "每周复盘一次" has no count; treat it as once a week.
        return StepRecurrence(kind: .weekly, timesPerWeek: 1)
    }

    /// Parses an optional count at the start of the fragment, skipping separators.
    private static func leadingCount(in text: Substring) -> Int? {
        var characters = Substring(text)
        while let first = characters.first, first == " " || first == "　" {
            characters = characters.dropFirst()
        }
        guard let first = characters.first else { return nil }

        if let digit = first.wholeNumberValue {
            let remaining = characters
                .drop(while: { $0.isNumber })
                .drop(while: { $0 == " " || $0 == "　" })
            guard remaining.hasPrefix("次") || remaining.hasPrefix("练") || remaining.hasPrefix("回") else {
                return nil
            }
            return digit
        }

        // Chinese numerals: 两/二/三... seven at most for a weekly cadence.
        let numerals: [Character: Int] = [
            "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7
        ]
        guard let value = numerals[first] else { return nil }
        let remaining = characters.dropFirst()
        let rest = remaining.drop(while: { $0 == " " || $0 == "　" })
        guard rest.hasPrefix("次") || rest.hasPrefix("练") || rest.hasPrefix("回") else { return nil }
        return value
    }
}
