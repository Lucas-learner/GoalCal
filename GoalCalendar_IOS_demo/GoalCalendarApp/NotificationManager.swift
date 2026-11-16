import Foundation
import UserNotifications

/// Thin wrapper around `UNUserNotificationCenter`.
///
/// All the deciding happens in `NotificationPlanner` (pure, tested). This type
/// only asks for permission and mirrors the plan into the system, which keeps the
/// interesting rules testable without a simulator.
/// iOS suppresses local notifications while the app is in the foreground unless a
/// delegate explicitly opts in. Without this, a reminder that fires while the user
/// is inside the app does nothing at all — which reads as "reminders are broken".
final class ForegroundNotificationDelegate: NSObject, UNUserNotificationCenterDelegate {
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .sound, .list]
    }
}

@MainActor
final class NotificationManager: ObservableObject {
    @Published private(set) var authorization: UNAuthorizationStatus = .notDetermined
    @Published private(set) var scheduledCount: Int = 0
    @Published private(set) var lastError: String?

    private let center = UNUserNotificationCenter.current()
    private let foregroundDelegate = ForegroundNotificationDelegate()
    private static let identifierPrefix = "event."

    init() {
        // Must be set before the first notification fires.
        center.delegate = foregroundDelegate
    }

    var isEnabled: Bool {
        authorization == .authorized || authorization == .provisional
    }

    var isDenied: Bool { authorization == .denied }

    func refreshAuthorization() async {
        let settings = await center.notificationSettings()
        authorization = settings.authorizationStatus
    }

    /// Asks once. If the user has already answered, this is a no-op — iOS only
    /// shows the prompt a single time per install.
    func requestAuthorizationIfNeeded() async {
        await refreshAuthorization()
        guard authorization == .notDetermined else { return }
        do {
            _ = try await center.requestAuthorization(options: [.alert, .sound, .badge])
        } catch {
            lastError = String(describing: error)
        }
        await refreshAuthorization()
    }

    func reschedule(events: [CalendarEvent], user: User, now: Date = Date()) async {
        await refreshAuthorization()

        guard isEnabled else {
            scheduledCount = 0
            return
        }

        // Replace only this app's reminders. Wiping every pending request would
        // break any notification type added later.
        let pending = await center.pendingNotificationRequests()
        let ours = pending.map(\.identifier).filter { $0.hasPrefix(Self.identifierPrefix) }
        if !ours.isEmpty {
            center.removePendingNotificationRequests(withIdentifiers: ours)
        }

        let plan = NotificationPlanner.plan(events: events, now: now, user: user)
        for item in plan {
            let content = UNMutableNotificationContent()
            content.title = item.title
            content.body = item.body
            content.sound = .default

            // Wall-clock components, not a relative interval: a 10:00 meeting
            // should remind at 09:50 local time even across a DST change or an
            // app relaunch.
            var components = user.calendar.dateComponents(
                [.year, .month, .day, .hour, .minute],
                from: item.fireDate
            )
            components.second = 0

            let request = UNNotificationRequest(
                identifier: item.identifier,
                content: content,
                trigger: UNCalendarNotificationTrigger(dateMatching: components, repeats: false)
            )
            do {
                try await center.add(request)
            } catch {
                lastError = String(describing: error)
            }
        }
        scheduledCount = plan.count
    }
}
