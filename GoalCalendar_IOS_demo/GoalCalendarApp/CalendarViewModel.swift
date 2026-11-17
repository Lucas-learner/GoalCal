import Foundation
import SwiftUI

enum CalendarMode: String, CaseIterable, Identifiable {
    case month
    case week
    case day

    var id: String { rawValue }

    var label: String {
        switch self {
        case .month: return "月"
        case .week: return "周"
        case .day: return "日"
        }
    }
}

struct MonthCell: Identifiable {
    let id: String
    /// nil for the blank cells that align the first day to its weekday column.
    let date: Date?
    let dayNumber: Int
    let isToday: Bool
    let eventCount: Int
    /// Colours of the first few events, for the month grid dots.
    let colors: [EventColor]
}

@MainActor
final class CalendarViewModel: ObservableObject {
    @Published var mode: CalendarMode = .month
    @Published private(set) var monthAnchor: Date
    @Published private(set) var selectedDay: Date
    @Published private(set) var eventsByDay: [Date: [CalendarEvent]] = [:]
    @Published private(set) var goals: [Goal] = []
    @Published private(set) var progressByGoal: [String: GoalProgress] = [:]
    /// Drives the event detail sheet. `nil` means no sheet.
    @Published var presentedEvent: CalendarEvent?
    @Published private(set) var editingStart: Date = Date()
    @Published private(set) var editingEnd: Date = Date()
    @Published private(set) var editingConflicts: [CalendarEvent] = []
    /// Which occurrences an edit applies to. Only meaningful for series events.
    @Published var editScope: SeriesScope = .onlyThis
    @Published private(set) var seriesOccurrenceCount = 0
    @Published var errorMessage: String?

    // Manual event composer state. All of it lives here because SwiftUI property
    // wrapper macros cannot compile in this build sandbox.
    @Published var isComposing = false
    @Published var draftTitle = ""
    @Published private(set) var draftStart = Date()
    @Published private(set) var draftEnd = Date()
    @Published var draftRepeatsWeekly = false
    @Published var draftWeekdays: Set<Int> = []
    @Published var draftWeeks = 8
    @Published var draftColor: EventColor = .blue
    @Published var composeError: String?
    private var draftDuration: TimeInterval = 3600

    /// Duration is remembered so moving the start also moves the end.
    private var editingDuration: TimeInterval = 1800
    /// Lets the goal list refresh after an event is completed or deleted.
    var onDataChanged: (() -> Void)?

    private let database: Database
    private let calendar: Calendar
    private let userID: String
    private let workStartMinute: Int
    private let workEndMinute: Int

    init(database: Database, user: User, now: Date = Date()) {
        self.database = database
        self.calendar = user.calendar
        self.userID = user.id
        self.workStartMinute = user.workStartMinute
        self.workEndMinute = user.workEndMinute
        self.monthAnchor = MonthGrid.startOfMonth(now, calendar: user.calendar)
        self.selectedDay = user.calendar.startOfDay(for: now)
        reload()
    }

    // MARK: - Derived display data

    var monthTitle: String {
        let components = calendar.dateComponents([.year, .month], from: monthAnchor)
        return "\(components.year ?? 0)年\(components.month ?? 0)月"
    }

    var weekdaySymbols: [String] {
        ["一", "二", "三", "四", "五", "六", "日"]
    }

    var cells: [MonthCell] {
        MonthGrid.cells(for: monthAnchor, calendar: calendar).map { cell in
            let count = cell.date.map { eventsByDay[$0]?.count ?? 0 } ?? 0
            return MonthCell(
                id: cell.id,
                date: cell.date,
                dayNumber: cell.dayNumber,
                isToday: cell.date.map { calendar.isDate($0, inSameDayAs: Date()) } ?? false,
                eventCount: count,
                colors: cell.date
                    .map { (eventsByDay[$0] ?? []).prefix(3).map { $0.color ?? .blue } } ?? []
            )
        }
    }

    var selectedDayEvents: [CalendarEvent] {
        (eventsByDay[selectedDay] ?? []).sorted { $0.startAt < $1.startAt }
    }

    var selectedDayTitle: String {
        let components = calendar.dateComponents([.month, .day, .weekday], from: selectedDay)
        let names = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]
        let index = min(max((components.weekday ?? 1) - 1, 0), 6)
        return "\(components.month ?? 1)月\(components.day ?? 1)日 \(names[index])"
    }

    func goalTitle(for event: CalendarEvent) -> String? {
        guard let goalID = event.goalID else { return nil }
        return goals.first { $0.id == goalID }?.title
    }

    /// The user's timezone, not the device's: the grid labels must match the
    /// calendar the events were stored against.
    var timeZone: TimeZone { calendar.timeZone }

    var weekDays: [Date] {
        WeekGrid.days(containing: selectedDay, calendar: calendar)
    }

    var weekTitle: String {
        WeekGrid.title(for: weekDays, calendar: calendar)
    }

    func moveWeek(by weeks: Int) {
        guard let moved = calendar.date(byAdding: .day, value: weeks * 7, to: selectedDay) else { return }
        selectedDay = calendar.startOfDay(for: moved)
        monthAnchor = MonthGrid.startOfMonth(selectedDay, calendar: calendar)
        reload()
    }

    var workStartHour: Int { workStartMinute / 60 }
    var workEndHour: Int { workEndMinute / 60 }

    func window(for day: Date) -> (start: Date, end: Date) {
        let start = calendar.date(byAdding: .minute, value: workStartMinute, to: day) ?? day
        let end = calendar.date(byAdding: .minute, value: workEndMinute, to: day)
            ?? day.addingTimeInterval(13 * 3600)
        return (start, max(start, end))
    }

    func events(on day: Date) -> [CalendarEvent] {
        (eventsByDay[calendar.startOfDay(for: day)] ?? []).sorted { $0.startAt < $1.startAt }
    }

    var eventsByID: [String: CalendarEvent] {
        Dictionary(uniqueKeysWithValues: selectedDayEvents.map { ($0.id, $0) })
    }

    /// The visible span of the day timeline, taken from the user's work window.
    var dayWindow: (start: Date, end: Date) {
        let start = calendar.date(byAdding: .minute, value: workStartMinute, to: selectedDay) ?? selectedDay
        let end = calendar.date(byAdding: .minute, value: workEndMinute, to: selectedDay)
            ?? selectedDay.addingTimeInterval(13 * 3600)
        return (start, max(start, end))
    }

    var timelineBlocks: [TimelineBlock] {
        let window = dayWindow
        return TimelineLayout.blocks(
            events: selectedDayEvents,
            windowStart: window.start,
            windowEnd: window.end
        )
    }

    /// Visible span for an arbitrary day, used by the week columns.
    func dayWindow(for day: Date) -> (start: Date, end: Date) {
        window(for: calendar.startOfDay(for: day))
    }

    func blocks(on day: Date) -> [TimelineBlock] {
        let span = dayWindow(for: day)
        return TimelineLayout.blocks(
            events: events(on: day),
            windowStart: span.start,
            windowEnd: span.end
        )
    }

    func moveDay(by days: Int) {
        guard let moved = calendar.date(byAdding: .day, value: days, to: selectedDay) else { return }
        selectedDay = calendar.startOfDay(for: moved)
        monthAnchor = MonthGrid.startOfMonth(selectedDay, calendar: calendar)
    }

    func isSelected(_ cell: MonthCell) -> Bool {
        guard let date = cell.date else { return false }
        return calendar.isDate(date, inSameDayAs: selectedDay)
    }

    func timeLabel(_ date: Date) -> String {
        let components = calendar.dateComponents([.hour, .minute], from: date)
        return String(format: "%02d:%02d", components.hour ?? 0, components.minute ?? 0)
    }

    // MARK: - Interaction

    func select(_ day: Date) {
        selectedDay = calendar.startOfDay(for: day)
    }

    func moveMonth(by months: Int) {
        guard let shifted = calendar.date(byAdding: .month, value: months, to: monthAnchor) else { return }
        monthAnchor = MonthGrid.startOfMonth(shifted, calendar: calendar)
    }

    func goToToday() {
        monthAnchor = MonthGrid.startOfMonth(Date(), calendar: calendar)
        selectedDay = calendar.startOfDay(for: Date())
    }

    // MARK: - Loading

    func reload() {
        loadEvents()
        loadGoals()
    }

    // MARK: - Manual event creation

    func beginComposing() {
        let base = calendar.date(byAdding: .minute, value: 9 * 60, to: selectedDay) ?? Date()
        draftColor = .blue
        draftTitle = ""
        draftStart = base
        draftEnd = base.addingTimeInterval(3600)
        draftDuration = 3600
        draftRepeatsWeekly = false
        draftWeekdays = [Self.mondayBasedWeekday(base, calendar: calendar)]
        draftWeeks = 8
        composeError = nil
        isComposing = true
    }

    /// Moving the start keeps the duration; the user edits one date, not two.
    func setDraftStart(_ date: Date) {
        draftStart = date
        draftEnd = date.addingTimeInterval(draftDuration)
    }

    func setDraftEnd(_ date: Date) {
        draftEnd = max(date, draftStart.addingTimeInterval(15 * 60))
        draftDuration = draftEnd.timeIntervalSince(draftStart)
    }

    func toggleDraftWeekday(_ weekday: Int) {
        if draftWeekdays.contains(weekday) {
            draftWeekdays.remove(weekday)
        } else {
            draftWeekdays.insert(weekday)
        }
    }

    /// Live preview of how many rows the series will create.
    var draftOccurrenceCount: Int {
        guard draftRepeatsWeekly else { return 1 }
        return EventSeries.expand(
            userID: userID,
            title: draftTitle,
            start: draftStart,
            end: draftEnd,
            weekdays: draftWeekdays,
            weeks: draftWeeks,
            calendar: calendar
        ).count
    }

    /// Existing events that would overlap what the composer is about to create.
    /// A warning only — the user may genuinely want the overlap.
    var draftConflicts: [CalendarEvent] {
        guard draftEnd > draftStart else { return [] }

        var spans: [(start: Date, end: Date)] = [(draftStart, draftEnd)]
        if draftRepeatsWeekly {
            let preview = EventSeries.expand(
                userID: userID, title: draftTitle, start: draftStart, end: draftEnd,
                weekdays: draftWeekdays, weeks: draftWeeks, calendar: calendar
            )
            // Check a bounded slice: the preview must stay snappy.
            spans = preview.prefix(24).map { ($0.startAt, $0.endAt) }
        }

        var conflicts: [CalendarEvent] = []
        for span in spans {
            let overlapping = (try? database.eventsOverlapping(
                userID: userID, start: span.start, end: span.end
            )) ?? []
            for event in overlapping where !conflicts.contains(where: { $0.id == event.id }) {
                conflicts.append(event)
            }
        }
        return conflicts
    }

    func saveComposed() {
        let title = draftTitle.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !title.isEmpty else {
            composeError = "标题不能为空"
            return
        }
        guard draftEnd > draftStart else {
            composeError = "结束时间必须晚于开始时间"
            return
        }

        let events: [CalendarEvent]
        if draftRepeatsWeekly {
            events = EventSeries.expand(
                userID: userID,
                title: title,
                start: draftStart,
                end: draftEnd,
                weekdays: draftWeekdays,
                weeks: draftWeeks,
                calendar: calendar
            )
            guard !events.isEmpty else {
                composeError = "没有生成任何事件，检查星期选择和持续周数"
                return
            }
        } else {
            events = [CalendarEvent(
                userID: userID,
                color: draftColor,
                title: title,
                startAt: draftStart,
                endAt: draftEnd,
                status: .confirmed,
                source: .manual
            )]
        }

        do {
            try database.insertEvents(events)
            composeError = nil
            isComposing = false
            selectedDay = calendar.startOfDay(for: draftStart)
            monthAnchor = MonthGrid.startOfMonth(selectedDay, calendar: calendar)
            reload()
            onDataChanged?()
        } catch {
            composeError = String(describing: error)
        }
    }

    static func mondayBasedWeekday(_ date: Date, calendar: Calendar) -> Int {
        (calendar.component(.weekday, from: date) + 5) % 7 + 1
    }

    // MARK: - Event time editing

    func beginEditing(_ event: CalendarEvent) {
        presentedEvent = event
        editingStart = event.startAt
        editingEnd = event.endAt
        editingDuration = event.endAt.timeIntervalSince(event.startAt)
        errorMessage = nil
        editScope = .onlyThis
        if let seriesID = event.seriesID {
            seriesOccurrenceCount = (try? database.events(seriesID: seriesID))?.count ?? 0
        } else {
            seriesOccurrenceCount = 0
        }
        refreshConflicts()
    }

    /// Moving the start keeps the duration, so the user edits one date, not two.
    func setEditingStart(_ date: Date) {
        editingStart = date
        editingEnd = date.addingTimeInterval(editingDuration)
        refreshConflicts()
    }

    func setEditingEnd(_ date: Date) {
        editingEnd = max(date, editingStart.addingTimeInterval(15 * 60))
        editingDuration = editingEnd.timeIntervalSince(editingStart)
        refreshConflicts()
    }

    /// Recolouring is applied immediately — it is trivially reversible.
    func setEventColor(_ color: EventColor) {
        guard let event = presentedEvent else { return }
        do {
            try database.updateEventColor(id: event.id, color: color)
            var updated = event
            updated.color = color
            presentedEvent = updated
            reload()
            onDataChanged?()
        } catch {
            errorMessage = String(describing: error)
        }
    }

    func saveEventTime() {
        guard let event = presentedEvent else { return }
        do {
            if let seriesID = event.seriesID, editScope != .onlyThis {
                let all = try database.events(seriesID: seriesID)
                let selected = SeriesScopeFilter.events(all, scope: editScope, pivot: event)
                let deltas = SeriesEditor.deltas(
                    from: event.startAt, to: editingStart, calendar: calendar
                )
                let minutes = Int((editingEnd.timeIntervalSince(editingStart) / 60).rounded())
                let updates = SeriesEditor.shift(
                    events: selected,
                    byDays: deltas.days,
                    byMinutes: deltas.minutes,
                    durationMinutes: minutes,
                    calendar: calendar
                )
                try database.updateEventTimes(updates)
            } else {
                _ = try database.updateEvent(id: event.id, startAt: editingStart, endAt: editingEnd)
            }
            errorMessage = nil
            reload()
            onDataChanged?()
            presentedEvent = nil
        } catch {
            errorMessage = (error as? EventEditError)?.description ?? String(describing: error)
        }
    }

    private func refreshConflicts() {
        guard let event = presentedEvent else {
            editingConflicts = []
            return
        }
        editingConflicts = (try? database.eventsOverlapping(
            userID: userID,
            start: editingStart,
            end: editingEnd,
            excluding: event.id
        )) ?? []
    }

    // MARK: - Event actions

    func complete(_ event: CalendarEvent) {
        do {
            _ = try database.completeEvent(id: event.id)
            reload()
            onDataChanged?()
        } catch {
            reload()
        }
        presentedEvent = nil
    }

    func delete(_ event: CalendarEvent) {
        do {
            if let seriesID = event.seriesID, editScope != .onlyThis {
                let all = try database.events(seriesID: seriesID)
                let selected = SeriesScopeFilter.events(all, scope: editScope, pivot: event)
                try database.deleteEvents(ids: selected.map(\.id))
            } else {
                _ = try database.deleteEvent(id: event.id)
            }
            reload()
            onDataChanged?()
        } catch {
            reload()
        }
        presentedEvent = nil
    }

    private func loadEvents() {
        guard let monthEnd = calendar.date(byAdding: .month, value: 1, to: monthAnchor) else {
            eventsByDay = [:]
            return
        }
        // Include proposed slots so the user can see what is waiting for approval.
        // Pad by a week on both sides: the selected week may start before or end
        // after the displayed month.
        let from = calendar.date(byAdding: .day, value: -7, to: monthAnchor) ?? monthAnchor
        let to = calendar.date(byAdding: .day, value: 7, to: monthEnd) ?? monthEnd
        let events = (try? database.events(
            userID: userID,
            from: from,
            to: to,
            includingProposed: true
        )) ?? []

        var grouped: [Date: [CalendarEvent]] = [:]
        for event in events {
            let day = calendar.startOfDay(for: event.startAt)
            grouped[day, default: []].append(event)
        }
        eventsByDay = grouped
    }

    private func loadGoals() {
        let all = (try? database.goals(userID: userID)) ?? []
        goals = all

        var progress: [String: GoalProgress] = [:]
        for goal in all {
            progress[goal.id] = try? database.progress(goalID: goal.id)
        }
        progressByGoal = progress
    }

}
