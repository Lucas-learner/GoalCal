import Foundation

/// Lays out a month as a 7-column grid.
///
/// Small, pure, and separate from SwiftUI so the weekday alignment can be tested.
/// Off-by-one here is invisible in code review and obvious to users — on a grid
/// that starts on the wrong column, every date is under the wrong weekday.
public enum MonthGrid {

    public struct Cell: Sendable, Hashable, Identifiable {
        /// Stable within one month's grid; used as a SwiftUI identity.
        public let id: String
        /// nil for the leading blanks that pad the first week.
        public let date: Date?
        public let dayNumber: Int

        public init(id: String, date: Date?, dayNumber: Int) {
            self.id = id
            self.date = date
            self.dayNumber = dayNumber
        }
    }

    /// - Parameter month: any instant inside the month; it is normalised first.
    /// - Returns: leading blank cells plus one cell per day, Monday-first.
    public static func cells(for month: Date, calendar: Calendar) -> [Cell] {
        let start = startOfMonth(month, calendar: calendar)
        guard let dayRange = calendar.range(of: .day, in: .month, for: start) else { return [] }

        // Calendar.component(.weekday) is Sunday = 1; convert to Monday = 1.
        let sundayBased = calendar.component(.weekday, from: start)
        let mondayBased = (sundayBased + 5) % 7 + 1
        let leadingBlanks = mondayBased - 1

        var cells: [Cell] = (0..<leadingBlanks).map { index in
            Cell(id: "blank-\(index)", date: nil, dayNumber: 0)
        }

        for offset in 0..<dayRange.count {
            guard let date = calendar.date(byAdding: .day, value: offset, to: start) else { continue }
            cells.append(Cell(
                id: "day-\(offset)",
                date: calendar.startOfDay(for: date),
                dayNumber: offset + 1
            ))
        }
        return cells
    }

    public static func startOfMonth(_ date: Date, calendar: Calendar) -> Date {
        let components = calendar.dateComponents([.year, .month], from: date)
        return calendar.date(from: components) ?? date
    }
}
