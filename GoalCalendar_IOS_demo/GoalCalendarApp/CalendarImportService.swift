import EventKit
import Foundation
import SwiftUI

/// Read-only import of the device calendar.
///
/// Imported rows are ordinary events (source `.external`), so the scheduler,
/// free-time search and notifications treat them like anything else. Duplicates
/// are skipped by comparing title + start time, which is crude but predictable —
/// worth revisiting once a real external identifier exists.
@MainActor
final class CalendarImportService: ObservableObject {
    enum Status: Equatable {
        case idle
        case denied
        case importing
        case imported(Int)
        case failed(String)
    }

    @Published private(set) var status: Status = .idle
    @Published private(set) var authorized = false

    private let store = EKEventStore()

    func refreshAuthorization() {
        authorized = EKEventStore.authorizationStatus(for: .event) == .fullAccess
    }

    func requestAndImport(database: Database, user: User, days: Int = 30, now: Date = Date()) async {
        store.reset()
        do {
            let granted = try await store.requestFullAccessToEvents()
            authorized = granted
            guard granted else {
                status = .denied
                return
            }
        } catch {
            status = .failed(error.localizedDescription)
            return
        }

        await importEvents(database: database, user: user, days: days, now: now)
    }

    func importEvents(database: Database, user: User, days: Int = 30, now: Date = Date()) async {
        guard authorized else {
            status = .denied
            return
        }

        status = .importing
        let end = now.addingTimeInterval(TimeInterval(days * 86_400))
        let predicate = store.predicateForEvents(withStart: now, end: end, calendars: nil)
        let external = store.events(matching: predicate)

        let existing = (try? database.events(userID: user.id, from: now, to: end)) ?? []
        let known = Set(existing.map { "\($0.title)|\(Int($0.startAt.timeIntervalSince1970 / 60))" })

        var toInsert: [CalendarEvent] = []
        for event in external {
            guard let start = event.startDate, let finish = event.endDate, finish > start else { continue }
            let title = (event.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            guard !title.isEmpty else { continue }
            guard !event.isAllDay else { continue }

            let key = "\(title)|\(Int(start.timeIntervalSince1970 / 60))"
            guard !known.contains(key) else { continue }

            toInsert.append(CalendarEvent(
                userID: user.id,
                title: title,
                notes: event.notes,
                startAt: start,
                endAt: finish,
                status: .confirmed,
                source: .external
            ))
        }

        do {
            try database.insertEvents(toInsert)
            status = .imported(toInsert.count)
        } catch {
            status = .failed(String(describing: error))
        }
    }

    var statusText: String {
        switch status {
        case .idle: return "尚未导入"
        case .denied: return "没有日历权限，请到系统设置里开启"
        case .importing: return "正在导入…"
        case .imported(let count): return count == 0 ? "没有新的日程" : "已导入 \(count) 个日程"
        case .failed(let message): return "导入失败：\(message)"
        }
    }
}
