import SwiftUI

struct RootView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        // Chat is first because almost everything in this app starts with a
        // sentence. Note this view holds no @State: SwiftUI's property-wrapper
        // macros cannot be expanded in this build sandbox, so all mutable state
        // lives in ObservableObject view models instead.
        TabView {
            CalendarView()
                .tabItem { Label("日历", systemImage: "calendar") }

            ChatView()
                .tabItem { Label("对话", systemImage: "bubble.left.and.bubble.right") }

            GoalListView()
                .tabItem { Label("目标", systemImage: "target") }

            SettingsView()
                .tabItem { Label("设置", systemImage: "gearshape") }
        }
    }
}
