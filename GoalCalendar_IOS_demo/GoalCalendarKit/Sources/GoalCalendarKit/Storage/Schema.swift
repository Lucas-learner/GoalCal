import Foundation

/// Schema DDL for the five tables (users, goals, steps, events, state_logs)
/// plus a derived view for goal progress.
///
/// Conventions enforced at the database level because the app must not be able
/// to violate them:
/// - all timestamps are fixed-format UTC ISO-8601 text, so lexical ordering and
///   `BETWEEN` range queries are correct without any datetime functions;
/// - `end_at > start_at` is a CHECK constraint, not an app-level convention;
/// - deleting a user cascades to their goals/events/state logs;
/// - deleting a goal cascades to its steps;
/// - deleting a step nulls out `events.step_id` rather than destroying the
///   user's calendar history.
enum Schema {

    static let version: Int32 = 4

    /// `PRAGMA user_version` driven, so future migrations can be appended
    /// without guessing whether a table already exists.
    static func migrate(_ connection: SQLiteConnection) throws {
        let current = try readUserVersion(connection)
        guard current < version else { return }

        try connection.execute("PRAGMA journal_mode = WAL")

        if current < 1 {
            try connection.execute(v1)
            try connection.execute("PRAGMA user_version = 1")
        }
        if current < 2 {
            try connection.execute(v2)
            try connection.execute("PRAGMA user_version = 2")
        }
        if current < 3 {
            try connection.execute(v3)
            try connection.execute("PRAGMA user_version = 3")
        }
        if current < 4 {
            try connection.execute(v4)
            try connection.execute("PRAGMA user_version = 4")
        }
    }

    private static func readUserVersion(_ connection: SQLiteConnection) throws -> Int32 {
        let statement = try connection.prepare("PRAGMA user_version")
        guard try statement.step() else { return 0 }
        return Int32(statement.int(0))
    }

    /// Adds habit support: a step can repeat daily or N times a week.
    /// Additive columns only, so existing rows keep working.
    /// Groups the occurrences of one repeating event so the series can be
    /// rescheduled or deleted as a unit.
    /// Three-colour highlight for events. NULL means the default.
    private static let v4 = """
    ALTER TABLE events ADD COLUMN color TEXT;
    """

    private static let v3 = """
    ALTER TABLE events ADD COLUMN series_id TEXT;
    CREATE INDEX IF NOT EXISTS idx_events_series ON events(series_id);
    """

    private static let v2 = """
    ALTER TABLE steps ADD COLUMN recurrence_kind TEXT;
    ALTER TABLE steps ADD COLUMN recurrence_times_per_week INTEGER;
    """

    private static let v1 = """
    CREATE TABLE IF NOT EXISTS users (
        id                  TEXT    PRIMARY KEY NOT NULL,
        name                TEXT    NOT NULL,
        timezone_identifier TEXT    NOT NULL,
        work_start_minute   INTEGER NOT NULL,
        work_end_minute     INTEGER NOT NULL,
        created_at          TEXT    NOT NULL
    );

    CREATE TABLE IF NOT EXISTS goals (
        id           TEXT PRIMARY KEY NOT NULL,
        user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title        TEXT NOT NULL,
        detail       TEXT,
        status       TEXT NOT NULL DEFAULT 'active',
        target_date  TEXT,
        created_at   TEXT NOT NULL,
        completed_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_goals_user_status ON goals(user_id, status);

    CREATE TABLE IF NOT EXISTS steps (
        id                TEXT    PRIMARY KEY NOT NULL,
        goal_id           TEXT    NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
        seq               INTEGER NOT NULL,
        title             TEXT    NOT NULL,
        detail            TEXT,
        percent           REAL    NOT NULL,
        estimated_minutes INTEGER NOT NULL DEFAULT 30,
        status            TEXT    NOT NULL DEFAULT 'pending',
        created_at        TEXT    NOT NULL,
        completed_at      TEXT,
        UNIQUE(goal_id, seq)
    );
    CREATE INDEX IF NOT EXISTS idx_steps_goal_seq ON steps(goal_id, seq);
    CREATE INDEX IF NOT EXISTS idx_steps_goal_status ON steps(goal_id, status);

    CREATE TABLE IF NOT EXISTS events (
        id         TEXT PRIMARY KEY NOT NULL,
        user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        step_id    TEXT REFERENCES steps(id) ON DELETE SET NULL,
        goal_id    TEXT REFERENCES goals(id) ON DELETE SET NULL,
        title      TEXT NOT NULL,
        notes      TEXT,
        start_at   TEXT NOT NULL,
        end_at     TEXT NOT NULL,
        status     TEXT NOT NULL DEFAULT 'confirmed',
        source     TEXT NOT NULL DEFAULT 'manual',
        created_at TEXT NOT NULL,
        CHECK (end_at > start_at)
    );
    CREATE INDEX IF NOT EXISTS idx_events_user_window ON events(user_id, start_at, end_at);
    CREATE INDEX IF NOT EXISTS idx_events_step ON events(step_id);

    CREATE TABLE IF NOT EXISTS state_logs (
        id         TEXT PRIMARY KEY NOT NULL,
        user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        state      TEXT NOT NULL,
        note       TEXT,
        created_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_state_user_time ON state_logs(user_id, created_at);

    CREATE VIEW IF NOT EXISTS goal_progress AS
    SELECT
        g.id AS goal_id,
        COUNT(s.id) AS total_steps,
        COALESCE(SUM(CASE WHEN s.status = 'done' THEN 1 ELSE 0 END), 0) AS done_steps,
        CASE
            WHEN COUNT(s.id) = 0 THEN 0.0
            ELSE (CAST(COALESCE(SUM(CASE WHEN s.status = 'done' THEN 1 ELSE 0 END), 0) AS REAL) / COUNT(s.id)) * 100.0
        END AS percent
    FROM goals g
    LEFT JOIN steps s ON s.goal_id = g.id
    GROUP BY g.id;
    """
}
