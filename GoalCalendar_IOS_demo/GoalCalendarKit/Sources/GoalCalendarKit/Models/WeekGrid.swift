import Foundation

/// The seven days of the week containing a given date.
///
/// Separated from the view so the boundary cases are testable: a week that
/// straddles two months, and the Sunday case (where a naive "days until Monday"
/// calculation returns the wrong week).
public enum WeekGrid {

    /// Monday-first, matching how the app reads dates everywhere else.
    public static func days(containing date: Date, calendar: Calendar) -> [Date] {
        let day = calendar.startOfDay(for: date)
        let sundayBased = calendar.component(.weekday, from: day)
        let mondayBased = (sundayBased + 5) % 7 + 1

        guard let monday = calendar.date(byAdding: .day, value: -(mondayBased - 1), to: day) else {
            return []
        }

        return (0..<7).compactMap { calendar.date(byAdding: .day, value: $0, to: monday) }
    }

    public static func title(for days: [Date], calendar: Calendar) -> String {
        guard let first = days.first, let last = days.last else { return "" }
        let a = calendar.dateComponents([.month, .day], from: first)
        let b = calendar.dateComponents([.month, .day], from: last)
        if a.month == b.month {
            return "\(a.month ?? 1)月\(a.day ?? 1)日 – \(b.day ?? 1)日"
        }
        return "\(a.month ?? 1)月\(a.day ?? 1)日 – \(b.month ?? 1)月\(b.day ?? 1)日"
    }
}
