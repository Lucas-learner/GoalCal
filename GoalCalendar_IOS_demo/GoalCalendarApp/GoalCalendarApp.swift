import SwiftUI

@main
struct GoalCalendarApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(model)
                // The UI copy is Chinese-only, so pin the locale: system
                // DatePickers would otherwise render "Oct 18, 2026" on a device
                // whose app bundle declares no localisation.
                .environment(\.locale, Locale(identifier: "zh_CN"))
                .task { model.bootstrap() }
        }
    }
}
