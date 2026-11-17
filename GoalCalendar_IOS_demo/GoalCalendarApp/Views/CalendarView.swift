import SwiftUI
import UIKit

struct CalendarView: View {
    @EnvironmentObject private var model: AppModel

    var body: some View {
        if let viewModel = model.calendar {
            CalendarScreen(viewModel: viewModel, notifications: model.notifications)
        } else {
            ProgressView("正在初始化…")
        }
    }
}

private struct CalendarScreen: View {
    @ObservedObject var viewModel: CalendarViewModel
    @ObservedObject var notifications: NotificationManager

    private let columns = Array(repeating: GridItem(.flexible(), spacing: 4), count: 7)

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    modePicker

                    switch viewModel.mode {
                    case .month:
                        monthHeader
                        weekdayHeader
                        monthGrid
                        agendaSection
                    case .week:
                        weekHeader
                        weekTimeline
                    case .day:
                        dayHeader
                        timelineSection
                    }

                    goalsSection
                    notificationSection
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
                .sheet(isPresented: $viewModel.isComposing) {
                    EventComposerSheet(viewModel: viewModel)
                        .presentationDetents([.medium, .large])
                }
            }
            .navigationTitle("日历")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button {
                        viewModel.beginComposing()
                    } label: {
                        Image(systemName: "plus")
                    }
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button("今天") { viewModel.goToToday() }
                        .font(.caption)
                }
            }
            .sheet(item: $viewModel.presentedEvent) { event in
                EventDetailSheet(event: event, viewModel: viewModel)
                    .presentationDetents([.medium, .large])
            }
        }
    }

    private var modePicker: some View {
        Picker("视图", selection: Binding(
            get: { viewModel.mode },
            set: { viewModel.mode = $0 }
        )) {
            ForEach(CalendarMode.allCases) { mode in
                Text(mode.label).tag(mode)
            }
        }
        .pickerStyle(.segmented)
    }

    private var weekHeader: some View {
        HStack {
            Button { viewModel.moveWeek(by: -1) } label: {
                Image(systemName: "chevron.left").font(.subheadline.weight(.semibold))
            }
            Spacer()
            Text(viewModel.weekTitle).font(.headline)
            Spacer()
            Button { viewModel.moveWeek(by: 1) } label: {
                Image(systemName: "chevron.right").font(.subheadline.weight(.semibold))
            }
        }
    }

    private var weekTimeline: some View {
        WeekTimeline(
            days: viewModel.weekDays,
            eventsForDay: { viewModel.events(on: $0) },
            windowForDay: { viewModel.window(for: $0) },
            startHour: viewModel.workStartHour,
            endHour: viewModel.workEndHour,
            timeZone: viewModel.timeZone,
            onSelectDay: { day in
                viewModel.select(day)
                viewModel.mode = .day
            },
            onSelectEvent: { viewModel.beginEditing($0) }
        )
    }

    private var dayHeader: some View {
        HStack {
            Button { viewModel.moveDay(by: -1) } label: {
                Image(systemName: "chevron.left").font(.subheadline.weight(.semibold))
            }
            Spacer()
            Text(viewModel.selectedDayTitle).font(.headline)
            Spacer()
            Button { viewModel.moveDay(by: 1) } label: {
                Image(systemName: "chevron.right").font(.subheadline.weight(.semibold))
            }
        }
    }

    private var timelineSection: some View {
        let window = viewModel.dayWindow
        let blocks = viewModel.timelineBlocks
        let eventsByID = viewModel.eventsByID

        return Group {
            if blocks.isEmpty {
                Text("这天没有安排")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 12)
            } else {
                DayTimeline(
                    blocks: blocks,
                    eventsByID: eventsByID,
                    windowStart: window.start,
                    windowEnd: window.end,
                    timeZone: viewModel.timeZone,
                    goalTitle: { viewModel.goalTitle(for: $0) },
                    onTap: { viewModel.beginEditing($0) }
                )
            }
        }
    }

    private var monthHeader: some View {
        HStack {
            Button { viewModel.moveMonth(by: -1) } label: {
                Image(systemName: "chevron.left").font(.subheadline.weight(.semibold))
            }
            Spacer()
            Text(viewModel.monthTitle).font(.headline)
            Spacer()
            Button { viewModel.moveMonth(by: 1) } label: {
                Image(systemName: "chevron.right").font(.subheadline.weight(.semibold))
            }
        }
    }

    private var weekdayHeader: some View {
        LazyVGrid(columns: columns, spacing: 4) {
            ForEach(viewModel.weekdaySymbols, id: \.self) { symbol in
                Text(symbol)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity)
            }
        }
    }

    private var monthGrid: some View {
        LazyVGrid(columns: columns, spacing: 4) {
            ForEach(viewModel.cells) { cell in
                DayCell(
                    cell: cell,
                    isSelected: viewModel.isSelected(cell)
                ) {
                    if let date = cell.date { viewModel.select(date) }
                }
            }
        }
    }

    private var agendaSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(viewModel.selectedDayTitle).font(.subheadline.weight(.semibold))
                Spacer()
                Text("\(viewModel.selectedDayEvents.count) 件事")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            if viewModel.selectedDayEvents.isEmpty {
                Text("这天没有安排")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 12)
            } else {
                ForEach(viewModel.selectedDayEvents) { event in
                    Button {
                        viewModel.beginEditing(event)
                    } label: {
                        EventRow(event: event, viewModel: viewModel)
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    @ViewBuilder
    private var notificationSection: some View {
        if notifications.isDenied {
            HStack(spacing: 10) {
                Image(systemName: "bell.slash.fill")
                    .foregroundStyle(.orange)
                VStack(alignment: .leading, spacing: 2) {
                    Text("通知已关闭")
                        .font(.caption.weight(.semibold))
                    Text("事件到点不会提醒你")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button("去设置") {
                    if let url = URL(string: UIApplication.openSettingsURLString) {
                        UIApplication.shared.open(url)
                    }
                }
                .font(.caption)
            }
            .padding(12)
            .background(Color.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
        } else if notifications.isEnabled {
            Label("已设置 \(notifications.scheduledCount) 个提醒", systemImage: "bell.badge")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }

    @ViewBuilder
    private var goalsSection: some View {
        if !viewModel.goals.isEmpty {
            VStack(alignment: .leading, spacing: 12) {
                Text("目标进度").font(.subheadline.weight(.semibold))
                ForEach(viewModel.goals) { goal in
                    GoalProgressRow(
                        goal: goal,
                        progress: viewModel.progressByGoal[goal.id]
                    )
                }
            }
        }
    }
}

private struct DayCell: View {
    let cell: MonthCell
    let isSelected: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(spacing: 3) {
                Text(cell.date == nil ? "" : "\(cell.dayNumber)")
                    .font(.callout.monospacedDigit())
                    .foregroundStyle(dayColor)

                HStack(spacing: 2) {
                    ForEach(Array(cell.colors.enumerated()), id: \.offset) { _, color in
                        Circle()
                            .fill(color.tint)
                            .frame(width: 4, height: 4)
                    }
                }
                .frame(height: 4)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 6)
            .background(
                isSelected ? Color.accentColor.opacity(0.14) : Color.clear,
                in: RoundedRectangle(cornerRadius: 8)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(isSelected ? Color.accentColor : Color.clear, lineWidth: 1)
            )
            .overlay(alignment: .top) {
                if cell.isToday {
                    Circle()
                        .fill(Color.accentColor)
                        .frame(width: 3, height: 3)
                        .offset(y: 2)
                }
            }
        }
        .buttonStyle(.plain)
        .disabled(cell.date == nil)
    }

    private var dayColor: Color {
        guard cell.date != nil else { return .clear }
        return cell.isToday ? .accentColor : .primary
    }
}

private struct EventRow: View {
    let event: CalendarEvent
    @ObservedObject var viewModel: CalendarViewModel

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(viewModel.timeLabel(event.startAt))
                    .font(.caption.monospacedDigit())
                    .foregroundStyle(.secondary)
                Text(viewModel.timeLabel(event.endAt))
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.tertiary)
            }
            .frame(width: 42, alignment: .leading)

            VStack(alignment: .leading, spacing: 3) {
                Text(event.title)
                    .font(.callout)
                if let goal = viewModel.goalTitle(for: event) {
                    Text(goal)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }

            Spacer(minLength: 4)

            if event.status == .proposed {
                Text("待确认")
                    .font(.caption2)
                    .foregroundStyle(.orange)
            } else if event.status == .done {
                Image(systemName: "checkmark.circle.fill")
                    .font(.caption)
                    .foregroundStyle(.green)
            }
        }
        .padding(10)
        .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 10))
    }
}

private struct GoalProgressRow: View {
    let goal: Goal
    let progress: GoalProgress?

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline) {
                Text(goal.title)
                    .font(.caption)
                    .lineLimit(1)
                Spacer(minLength: 8)
                Text(progress?.percentLabel ?? "0%")
                    .font(.caption2.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            ProgressView(value: progress?.percent ?? 0, total: 100)
                .tint(progress?.isComplete == true ? .green : .accentColor)
        }
    }
}

private struct EventDetailSheet: View {
    let event: CalendarEvent
    @ObservedObject var viewModel: CalendarViewModel

    private var startBinding: Binding<Date> {
        Binding(get: { viewModel.editingStart }, set: { viewModel.setEditingStart($0) })
    }

    private var endBinding: Binding<Date> {
        Binding(get: { viewModel.editingEnd }, set: { viewModel.setEditingEnd($0) })
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                VStack(alignment: .leading, spacing: 5) {
                    Text(event.title)
                        .font(.title3.weight(.semibold))
                    if let goal = viewModel.goalTitle(for: event) {
                        Label(goal, systemImage: "target")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }

                HStack(spacing: 12) {
                    Text("颜色")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    EventColorPicker(selected: event.color) {
                        viewModel.setEventColor($0)
                    }
                    Spacer()
                }

                VStack(spacing: 10) {
                    DatePicker("开始", selection: startBinding, displayedComponents: [.date, .hourAndMinute])
                    DatePicker("结束", selection: endBinding, displayedComponents: [.date, .hourAndMinute])
                }
                .font(.callout)

                if event.seriesID != nil, viewModel.seriesOccurrenceCount > 0 {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("这是一组重复事件，共 \(viewModel.seriesOccurrenceCount) 次")
                            .font(.caption)
                            .foregroundStyle(.secondary)

                        Picker("应用到", selection: Binding(
                            get: { viewModel.editScope },
                            set: { viewModel.editScope = $0 }
                        )) {
                            ForEach(SeriesScope.allCases) { scope in
                                Text(scope.label).tag(scope)
                            }
                        }
                        .pickerStyle(.segmented)

                        Text("「保存时间」和「删除」都按这个范围执行")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                    .padding(.vertical, 2)
                }

                if !viewModel.editingConflicts.isEmpty {
                    let names = viewModel.editingConflicts.map(\.title).joined(separator: "、")
                    Label("与「\(names)」时间重叠", systemImage: "exclamationmark.triangle.fill")
                        .font(.caption)
                        .foregroundStyle(.orange)
                }

                if let error = viewModel.errorMessage {
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(.red)
                }

                Button {
                    viewModel.saveEventTime()
                } label: {
                    Label("保存时间", systemImage: "checkmark")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)

                HStack(spacing: 10) {
                    if event.stepID != nil {
                        Button {
                            viewModel.complete(event)
                        } label: {
                            Label("标记完成", systemImage: "checkmark.circle")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.large)
                    }

                    Button(role: .destructive) {
                        viewModel.delete(event)
                    } label: {
                        Label("删除", systemImage: "trash")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.large)
                    .tint(.red)
                }
            }
            .padding(20)
        }
    }
}

private struct DayTimeline: View {
    let blocks: [TimelineBlock]
    let eventsByID: [String: CalendarEvent]
    let windowStart: Date
    let windowEnd: Date
    let timeZone: TimeZone
    let goalTitle: (CalendarEvent) -> String?
    let onTap: (CalendarEvent) -> Void

    private let rowHeight: CGFloat = 54
    private let gutterWidth: CGFloat = 46
    private let columnSpacing: CGFloat = 4

    private var hourCount: Int {
        max(1, Int(windowEnd.timeIntervalSince(windowStart) / 3600))
    }

    private var hourFormatter: DateFormatter {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.timeZone = timeZone
        formatter.dateFormat = "HH:mm"
        return formatter
    }

    var body: some View {
        GeometryReader { geometry in
            let contentWidth = max(80, geometry.size.width - gutterWidth)

            ZStack(alignment: .topLeading) {
                ForEach(0..<hourCount, id: \.self) { index in
                    hourRow(index)
                        .offset(y: CGFloat(index) * rowHeight)
                }

                ForEach(blocks, id: \.eventID) { block in
                    blockView(block, contentWidth: contentWidth)
                        .offset(
                            x: gutterWidth + CGFloat(block.column) * (blockWidth(block, contentWidth: contentWidth) + columnSpacing),
                            y: CGFloat(block.start.timeIntervalSince(windowStart) / 3600) * rowHeight + 2
                        )
                }
            }
        }
        .frame(height: CGFloat(hourCount) * rowHeight + 6)
    }

    private func blockWidth(_ block: TimelineBlock, contentWidth: CGFloat) -> CGFloat {
        let count = CGFloat(max(1, block.columnCount))
        return (contentWidth - columnSpacing * (count - 1)) / count
    }

    private func hourRow(_ index: Int) -> some View {
        let hourDate = windowStart.addingTimeInterval(TimeInterval(index) * 3600)
        return HStack(alignment: .top, spacing: 6) {
            Text(hourFormatter.string(from: hourDate))
                .font(.caption2.monospacedDigit())
                .foregroundStyle(.secondary)
                .frame(width: gutterWidth - 6, alignment: .trailing)
            Rectangle()
                .fill(Color.secondary.opacity(0.15))
                .frame(height: 1)
        }
    }

    @ViewBuilder
    private func blockView(_ block: TimelineBlock, contentWidth: CGFloat) -> some View {
        if let event = eventsByID[block.eventID] {
            let height = max(24, CGFloat(block.end.timeIntervalSince(block.start) / 3600) * rowHeight - 4)

            Button {
                onTap(event)
            } label: {
                VStack(alignment: .leading, spacing: 2) {
                    Text(event.title)
                        .font(.caption.weight(.semibold))
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                    if height > 40 {
                        Text("\(hourFormatter.string(from: block.start))–\(hourFormatter.string(from: block.end))")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                    if height > 64, let goal = goalTitle(event) {
                        Text(goal)
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                            .lineLimit(1)
                    }
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 7)
                .padding(.vertical, 5)
                .frame(
                    width: blockWidth(block, contentWidth: contentWidth),
                    height: height,
                    alignment: .topLeading
                )
                .background(
                    event.status == .proposed
                        ? Color.orange.opacity(0.15)
                        : EventColor.tint(for: event.color).opacity(0.16),
                    in: RoundedRectangle(cornerRadius: 7)
                )
                .overlay(alignment: .leading) {
                    Rectangle()
                        .fill(event.status == .proposed ? Color.orange : EventColor.tint(for: event.color))
                        .frame(width: 3)
                        .clipShape(Capsule())
                        .padding(.vertical, 4)
                }
            }
            .buttonStyle(.plain)
        }
    }
}


private struct EventComposerSheet: View {
    @ObservedObject var viewModel: CalendarViewModel

    private let weekdayLabels: [Int: String] = [
        1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"
    ]

    private var startBinding: Binding<Date> {
        Binding(get: { viewModel.draftStart }, set: { viewModel.setDraftStart($0) })
    }

    private var endBinding: Binding<Date> {
        Binding(get: { viewModel.draftEnd }, set: { viewModel.setDraftEnd($0) })
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    TextField("例如：小组站会", text: $viewModel.draftTitle)
                        .textFieldStyle(.roundedBorder)
                        .font(.body)

                    HStack(spacing: 12) {
                        Text("颜色")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        EventColorPicker(selected: viewModel.draftColor) {
                            viewModel.draftColor = $0
                        }
                    }

                    VStack(spacing: 10) {
                        DatePicker("开始", selection: startBinding, displayedComponents: [.date, .hourAndMinute])
                        DatePicker("结束", selection: endBinding, displayedComponents: [.date, .hourAndMinute])
                    }
                    .font(.callout)

                    Divider()

                    Toggle("每周重复", isOn: $viewModel.draftRepeatsWeekly)
                        .font(.callout)

                    if viewModel.draftRepeatsWeekly {
                        VStack(alignment: .leading, spacing: 10) {
                            Text("重复星期")
                                .font(.caption)
                                .foregroundStyle(.secondary)

                            HStack(spacing: 6) {
                                ForEach(Array(1...7), id: \.self) { weekday in
                                    weekdayChip(weekday)
                                }
                            }

                            Stepper("持续 \(viewModel.draftWeeks) 周", value: $viewModel.draftWeeks, in: 1...26)
                                .font(.callout)
                        }
                    }

                    Text(viewModel.draftRepeatsWeekly
                         ? "将创建 \(viewModel.draftOccurrenceCount) 个事件"
                         : "将创建 1 个事件")
                        .font(.caption)
                        .foregroundStyle(.secondary)

                    if !viewModel.draftConflicts.isEmpty {
                        Label(
                            "与 \(viewModel.draftConflicts.count) 个已有事件时间重叠",
                            systemImage: "exclamationmark.triangle.fill"
                        )
                        .font(.caption)
                        .foregroundStyle(.orange)
                    }

                    if let error = viewModel.composeError {
                        Text(error)
                            .font(.caption)
                            .foregroundStyle(.red)
                    }

                    Button {
                        viewModel.saveComposed()
                    } label: {
                        Label("添加", systemImage: "checkmark")
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)

                    Button {
                        viewModel.isComposing = false
                    } label: {
                        Text("取消").frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.large)
                }
                .padding(20)
            }
            .navigationTitle("新建事件")
            .navigationBarTitleDisplayMode(.inline)
        }
    }

    private func weekdayChip(_ weekday: Int) -> some View {
        let selected = viewModel.draftWeekdays.contains(weekday)
        return Button {
            viewModel.toggleDraftWeekday(weekday)
        } label: {
            Text(weekdayLabels[weekday] ?? "?")
                .font(.caption.weight(.semibold))
                .frame(width: 36, height: 36)
                .background(
                    selected ? Color.accentColor : Color(.tertiarySystemBackground),
                    in: Circle()
                )
                .foregroundStyle(selected ? Color.white : Color.primary)
        }
        .buttonStyle(.plain)
    }
}

private struct WeekTimeline: View {
    let days: [Date]
    let eventsForDay: (Date) -> [CalendarEvent]
    let windowForDay: (Date) -> (start: Date, end: Date)
    let startHour: Int
    let endHour: Int
    let timeZone: TimeZone
    let onSelectDay: (Date) -> Void
    let onSelectEvent: (CalendarEvent) -> Void

    private let rowHeight: CGFloat = 38
    private let gutterWidth: CGFloat = 30
    private let columnSpacing: CGFloat = 2

    private var hourCount: Int { max(1, endHour - startHour) }

    private var calendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        calendar.firstWeekday = 2
        return calendar
    }

    private var weekdayLabels: [String] { ["一", "二", "三", "四", "五", "六", "日"] }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            headerRow

            GeometryReader { geometry in
                let columnWidth = max(14, (geometry.size.width - gutterWidth) / CGFloat(max(days.count, 1)))

                ZStack(alignment: .topLeading) {
                    ForEach(0..<hourCount, id: \.self) { index in
                        HStack(spacing: 4) {
                            Text(String(format: "%02d", startHour + index))
                                .font(.system(size: 9).monospacedDigit())
                                .foregroundStyle(.secondary)
                                .frame(width: gutterWidth - 6, alignment: .trailing)
                            Rectangle()
                                .fill(Color.secondary.opacity(0.12))
                                .frame(height: 0.5)
                        }
                        .offset(y: CGFloat(index) * rowHeight)
                    }

                    ForEach(Array(days.enumerated()), id: \.offset) { index, day in
                        dayColumn(day: day, columnWidth: columnWidth)
                            .offset(x: gutterWidth + CGFloat(index) * columnWidth)
                    }
                }
            }
            .frame(height: CGFloat(hourCount) * rowHeight + 4)
        }
    }

    private var headerRow: some View {
        HStack(spacing: 0) {
            Spacer().frame(width: gutterWidth)
            ForEach(Array(days.enumerated()), id: \.offset) { index, day in
                Button {
                    onSelectDay(day)
                } label: {
                    VStack(spacing: 2) {
                        Text(weekdayLabels[index])
                            .font(.system(size: 9))
                            .foregroundStyle(.secondary)
                        Text("\(calendar.component(.day, from: day))")
                            .font(.system(size: 12, weight: isToday(day) ? .bold : .regular))
                            .foregroundStyle(isToday(day) ? Color.accentColor : Color.primary)
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.plain)
            }
        }
    }

    @ViewBuilder
    private func dayColumn(day: Date, columnWidth: CGFloat) -> some View {
        let window = windowForDay(day)
        let blocks = TimelineLayout.blocks(
            events: eventsForDay(day),
            windowStart: window.start,
            windowEnd: window.end
        )

        ZStack(alignment: .topLeading) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                let width = max(
                    8,
                    (columnWidth - columnSpacing * CGFloat(max(block.columnCount, 1) - 1))
                        / CGFloat(max(block.columnCount, 1))
                )
                let height = max(
                    14,
                    CGFloat(block.end.timeIntervalSince(block.start) / 3600) * rowHeight - 1
                )
                let x = CGFloat(block.column) * (width + columnSpacing)
                let y = CGFloat(block.start.timeIntervalSince(window.start) / 3600) * rowHeight

                Button {
                    onSelectEvent(blockEvent(block))
                } label: {
                    let tint = EventColor.tint(for: blockEvent(block).color)

                    Text(blockTitle(block))
                        .font(.system(size: 8, weight: .medium))
                        .foregroundStyle(tint)
                        .lineLimit(2)
                        .minimumScaleFactor(0.7)
                        .padding(.horizontal, 2)
                        .frame(width: width - 2, height: height, alignment: .topLeading)
                        .background(tint.opacity(0.5), in: RoundedRectangle(cornerRadius: 3))
                }
                .buttonStyle(.plain)
                .offset(x: x + 1, y: y + 1)
            }
        }
        .frame(width: columnWidth, height: CGFloat(hourCount) * rowHeight, alignment: .topLeading)
    }

    private func blockEvent(_ block: TimelineBlock) -> CalendarEvent {
        // The blocks are derived from this day's events; the id is enough to refetch.
        eventsForDay(blockStartDay(block)).first(where: { $0.id == block.eventID })
            ?? CalendarEvent(id: block.eventID, userID: "", title: "", startAt: block.start, endAt: block.end)
    }

    private func blockStartDay(_ block: TimelineBlock) -> Date {
        calendar.startOfDay(for: block.start)
    }

    private func blockTitle(_ block: TimelineBlock) -> String {
        eventsForDay(blockStartDay(block)).first(where: { $0.id == block.eventID })?.title ?? ""
    }

    private func isToday(_ day: Date) -> Bool {
        calendar.isDate(day, inSameDayAs: Date())
    }
}
