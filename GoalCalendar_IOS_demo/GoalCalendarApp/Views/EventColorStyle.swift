import SwiftUI

extension EventColor {
    /// Pure yellow is unreadable on a white background, so the "yellow" option
    /// is a darker gold. The label still says 黄.
    var tint: Color {
        switch self {
        case .blue: return .blue
        case .red: return .red
        case .yellow: return Color(red: 0.86, green: 0.62, blue: 0.05)
        }
    }

    static func tint(for color: EventColor?) -> Color {
        (color ?? .blue).tint
    }
}

/// Three swatches, used in both the composer and the detail sheet.
struct EventColorPicker: View {
    let selected: EventColor?
    let onPick: (EventColor) -> Void

    var body: some View {
        HStack(spacing: 12) {
            ForEach(EventColor.allCases, id: \.rawValue) { color in
                Button {
                    onPick(color)
                } label: {
                    Circle()
                        .fill(color.tint)
                        .frame(width: 26, height: 26)
                        .overlay(
                            Circle().strokeBorder(
                                (selected ?? .blue) == color ? Color.primary : Color.clear,
                                lineWidth: 2
                            )
                        )
                }
                .buttonStyle(.plain)
            }
        }
    }
}
