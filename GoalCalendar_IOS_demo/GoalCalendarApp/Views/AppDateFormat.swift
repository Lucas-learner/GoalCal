import Foundation

/// Date strings shown in the chat.
///
/// The UI copy is Chinese-only, so dates are formatted with an explicit zh_CN
/// locale. Relying on `Locale.current` produced English dates on a simulator
/// whose app bundle declares no localization, and a half-English UI reads as a
/// bug. When the app is actually localised, switch this back to the device
/// locale in one place.
enum AppDateFormat {
    private static let locale = Locale(identifier: "zh_CN")

    private static var style: Date.FormatStyle {
        Date.FormatStyle()
            .month(.abbreviated)
            .day()
            .weekday(.abbreviated)
            .hour()
            .minute()
            .locale(locale)
    }

    static func dateTime(_ date: Date) -> String {
        date.formatted(style)
    }
}
