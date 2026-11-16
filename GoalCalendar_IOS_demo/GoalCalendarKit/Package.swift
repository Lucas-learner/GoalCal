// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "GoalCalendarKit",
    platforms: [
        .iOS(.v18),
        .macOS(.v14)
    ],
    products: [
        .library(name: "GoalCalendarKit", targets: ["GoalCalendarKit"]),
        .executable(name: "goaldemo", targets: ["GoalCalendarDemo"])
    ],
    targets: [
        // Pure logic layer: models, SQLite persistence, LLM parsing, scheduling.
        // Deliberately has NO SwiftUI / UIKit dependency so it can be unit-tested
        // on the macOS command line with `swift test`, without a simulator.
        .target(
            name: "GoalCalendarKit",
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
        // Terminal walkthrough: parse a sentence, resolve its time, schedule a step.
        // Handy for eyeballing behaviour without booting the simulator.
        .executableTarget(
            name: "GoalCalendarDemo",
            dependencies: ["GoalCalendarKit"],
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
        .testTarget(
            name: "GoalCalendarKitTests",
            dependencies: ["GoalCalendarKit"],
            swiftSettings: [.swiftLanguageMode(.v5)]
        )
    ]
)
