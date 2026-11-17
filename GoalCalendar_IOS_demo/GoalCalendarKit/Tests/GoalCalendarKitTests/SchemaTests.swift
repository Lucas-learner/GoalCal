import XCTest
@testable import GoalCalendarKit

final class SchemaTests: XCTestCase {

    private var directory: URL!
    private var path: String!

    override func setUpWithError() throws {
        directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("GoalCalendarTests-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        path = directory.appendingPathComponent("test.sqlite").path
    }

    override func tearDownWithError() throws {
        try? FileManager.default.removeItem(at: directory)
    }

    /// Requirement: five tables exactly, plus the derived progress view.
    func testCreatesAllFiveTablesAndProgressView() throws {
        _ = try Database(path: path)

        let connection = try SQLiteConnection(path: path)
        let statement = try connection.prepare("""
        SELECT type, name FROM sqlite_master
        WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        """)
        var tables: [String] = []
        var views: [String] = []
        while try statement.step() {
            let type = statement.text(0)
            let name = statement.text(1) ?? ""
            if type == "table" { tables.append(name) }
            if type == "view" { views.append(name) }
        }

        XCTAssertEqual(tables, ["events", "goals", "state_logs", "steps", "users"])
        XCTAssertEqual(views, ["goal_progress"])
    }

    func testMigrationIsIdempotent() throws {
        _ = try Database(path: path)
        // Re-opening must not throw or duplicate tables.
        let second = try Database(path: path)
        let connection = try SQLiteConnection(path: path)
        let statement = try connection.prepare("PRAGMA user_version")
        XCTAssertTrue(try statement.step())
        XCTAssertEqual(statement.int(0), Int64(Schema.version))
        XCTAssertNotNil(second)
    }

    func testEventRejectsEndBeforeStart() throws {
        let database = try Database(path: path)
        let user = try database.upsertUser(User(name: "A"))
        let start = Date(timeIntervalSince1970: 1_800_000_000)

        XCTAssertThrowsError(
            try database.insertEvent(CalendarEvent(
                userID: user.id,
                title: "Backwards",
                startAt: start,
                endAt: start.addingTimeInterval(-1800)
            ))
        )
    }

    func testDeletingGoalCascadesToSteps() throws {
        let database = try Database(path: path)
        let user = try database.upsertUser(User(name: "A"))
        let goal = try database.insertGoal(Goal(userID: user.id, title: "G"))
        try database.insertSteps([
            Step(goalID: goal.id, seq: 1, title: "s1", percent: 50),
            Step(goalID: goal.id, seq: 2, title: "s2", percent: 50)
        ])

        XCTAssertEqual(try database.steps(goalID: goal.id).count, 2)
        XCTAssertTrue(try database.deleteGoal(id: goal.id))
        XCTAssertEqual(try database.steps(goalID: goal.id).count, 0)
    }

    func testDeletingStepKeepsEventHistory() throws {
        let database = try Database(path: path)
        let user = try database.upsertUser(User(name: "A"))
        let goal = try database.insertGoal(Goal(userID: user.id, title: "G"))
        let steps = try database.insertSteps([Step(goalID: goal.id, seq: 1, title: "s1", percent: 100)])
        let step = try XCTUnwrap(steps.first)

        let start = Date(timeIntervalSince1970: 1_800_000_000)
        let event = try database.insertEvent(CalendarEvent(
            userID: user.id, stepID: step.id, goalID: goal.id,
            title: "work", startAt: start, endAt: start.addingTimeInterval(1800)
        ))

        try database.updateStepStatus(id: step.id, status: .skipped)
        // A step deletion must null the FK, not delete the user's history.
        let connection = try SQLiteConnection(path: path)
        let delete = try connection.prepare("DELETE FROM steps WHERE id = ?")
        try delete.bind([.text(step.id)])
        try delete.step()

        let survivor = try XCTUnwrap(try database.event(id: event.id))
        XCTAssertNil(survivor.stepID)
    }
}
