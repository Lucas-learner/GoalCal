import SwiftUI
import UIKit

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        if let viewModel = model.settings {
            SettingsScreen(viewModel: viewModel, notifications: model.notifications)
        } else {
            ProgressView("正在初始化…")
        }
    }
}

private struct SettingsScreen: View {
    @ObservedObject var viewModel: SettingsViewModel
    @ObservedObject var notifications: NotificationManager

    var body: some View {
        NavigationStack {
            List {
                hoursSection
                notificationSection
                importSection
                dataSection

                if let error = viewModel.errorMessage {
                    Section {
                        Text(error).font(.footnote).foregroundStyle(.red)
                    }
                }
            }
            .listStyle(.insetGrouped)
            .navigationTitle("设置")
            .navigationBarTitleDisplayMode(.inline)
            .onAppear { viewModel.refresh() }
        }
    }

    // MARK: - Work hours

    private var hoursSection: some View {
        Section {
            HStack {
                Text("每日可用起点")
                Spacer()
                Picker("", selection: $viewModel.workStartHour) {
                    ForEach(0..<24, id: \.self) { hour in
                        Text(String(format: "%02d:00", hour)).tag(hour)
                    }
                }
                .labelsHidden()
                .pickerStyle(.menu)
                .onChange(of: viewModel.workStartHour) { _, _ in viewModel.saveWorkHours() }
            }

            HStack {
                Text("每日可用终点")
                Spacer()
                Picker("", selection: $viewModel.workEndHour) {
                    ForEach(0..<24, id: \.self) { hour in
                        Text(String(format: "%02d:00", hour)).tag(hour)
                    }
                }
                .labelsHidden()
                .pickerStyle(.menu)
                .onChange(of: viewModel.workEndHour) { _, _ in viewModel.saveWorkHours() }
            }
        } header: {
            Text("可用时段")
        } footer: {
            Text("排程只在这段时间内找空档。时区跟随系统设置。")
        }
    }

    // MARK: - Notifications

    private var notificationSection: some View {
        Section {
            Button {
                Task {
                    if notifications.authorization == .notDetermined {
                        await notifications.requestAuthorizationIfNeeded()
                    } else if let url = URL(string: UIApplication.openSettingsURLString) {
                        _ = await UIApplication.shared.open(url)
                    }
                }
            } label: {
                HStack {
                    Text("通知").foregroundStyle(.primary)
                    Spacer()
                    Text(notificationStatus)
                        .foregroundStyle(.secondary)
                    Image(systemName: "chevron.right")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                }
            }
        } header: {
            Text("提醒")
        } footer: {
            Text("事件开始前 5 分钟提醒。点这一行可以跳到系统设置里修改通知权限。")
        }
    }

    private var notificationStatus: String {
        if notifications.isDenied { return "已关闭" }
        if notifications.isEnabled { return "已开启（\(notifications.scheduledCount) 个提醒）" }
        return "未授权"
    }

    // MARK: - Import

    private var importSection: some View {
        Section {
            Button {
                viewModel.importCalendar()
            } label: {
                Label("从系统日历导入未来 30 天", systemImage: "square.and.arrow.down")
            }

            Text(viewModel.importService.statusText)
                .font(.caption)
                .foregroundStyle(.secondary)

            if viewModel.importService.status == .denied {
                Button("打开系统设置") {
                    if let url = URL(string: UIApplication.openSettingsURLString) {
                        Task { _ = await UIApplication.shared.open(url) }
                    }
                }
                .font(.caption)
            }
        } header: {
            Text("导入")
        } footer: {
            Text("只读导入，不会修改你的系统日历。导入的事件按标题和开始时间去重。")
        }
    }

    // MARK: - Data

    private var dataSection: some View {
        Section {
            HStack {
                Text("目标完成状态")
                Spacer()
                Text(String(format: "%.0f%%", viewModel.completedPercent))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
            ProgressView(value: viewModel.completedPercent, total: 100)

            LabeledContent("目标数量", value: "\(viewModel.goalCount)")
        } header: {
            Text("数据")
        } footer: {
            Text("按所有目标的步骤总数加权，不是各目标百分比的平均。")
        }
    }
}
