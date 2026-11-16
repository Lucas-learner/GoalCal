import Foundation
import SQLite3

/// Tells SQLite to copy bound strings instead of holding our Swift buffer.
/// Without this, a freed Swift String can leave a dangling pointer.
internal let SQLITE_TRANSIENT = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

public enum SQLiteError: Error, CustomStringConvertible {
    case openFailed(code: Int32, message: String)
    case prepareFailed(code: Int32, message: String, sql: String)
    case stepFailed(code: Int32, message: String, sql: String)
    case bindFailed(code: Int32, message: String, index: Int32)
    case notText(column: Int32)
    case transactionFailed(String)

    public var description: String {
        switch self {
        case .openFailed(let code, let message):
            return "SQLite open failed (\(code)): \(message)"
        case .prepareFailed(let code, let message, let sql):
            return "SQLite prepare failed (\(code)): \(message)\nSQL: \(sql)"
        case .stepFailed(let code, let message, let sql):
            return "SQLite step failed (\(code)): \(message)\nSQL: \(sql)"
        case .bindFailed(let code, let message, let index):
            return "SQLite bind failed at index \(index) (\(code)): \(message)"
        case .notText(let column):
            return "SQLite column \(column) is not text"
        case .transactionFailed(let message):
            return "SQLite transaction failed: \(message)"
        }
    }
}

/// A single value crossing the Swift/SQLite boundary.
public enum SQLValue: Equatable {
    case null
    case int(Int64)
    case double(Double)
    case text(String)
}

public final class SQLiteStatement {
    private let sql: String
    private var handle: OpaquePointer?

    init(connection: OpaquePointer, sql: String) throws {
        self.sql = sql
        let rc = sqlite3_prepare_v2(connection, sql, -1, &handle, nil)
        guard rc == SQLITE_OK else {
            throw SQLiteError.prepareFailed(
                code: rc,
                message: String(cString: sqlite3_errmsg(connection)),
                sql: sql
            )
        }
    }

    deinit {
        sqlite3_finalize(handle)
    }

    @discardableResult
    func bind(_ values: [SQLValue]) throws -> SQLiteStatement {
        sqlite3_reset(handle)
        sqlite3_clear_bindings(handle)
        for (offset, value) in values.enumerated() {
            let index = Int32(offset + 1)
            let rc: Int32
            switch value {
            case .null:
                rc = sqlite3_bind_null(handle, index)
            case .int(let number):
                rc = sqlite3_bind_int64(handle, index, number)
            case .double(let number):
                rc = sqlite3_bind_double(handle, index, number)
            case .text(let string):
                rc = sqlite3_bind_text(handle, index, string, -1, SQLITE_TRANSIENT)
            }
            guard rc == SQLITE_OK else {
                throw SQLiteError.bindFailed(
                    code: rc,
                    message: "unable to bind value at index \(index)",
                    index: index
                )
            }
        }
        return self
    }

    /// Returns true when a row is available, false when the statement is done.
    @discardableResult
    func step() throws -> Bool {
        let rc = sqlite3_step(handle)
        switch rc {
        case SQLITE_ROW:
            return true
        case SQLITE_DONE:
            return false
        default:
            throw SQLiteError.stepFailed(
                code: rc,
                message: handle.map { String(cString: sqlite3_errmsg(sqlite3_db_handle($0))) } ?? "unknown",
                sql: sql
            )
        }
    }

    func text(_ column: Int32) -> String? {
        guard let cString = sqlite3_column_text(handle, column) else { return nil }
        return String(cString: cString)
    }

    func int(_ column: Int32) -> Int64 {
        sqlite3_column_int64(handle, column)
    }

    func double(_ column: Int32) -> Double {
        sqlite3_column_double(handle, column)
    }

    func isNull(_ column: Int32) -> Bool {
        sqlite3_column_type(handle, column) == SQLITE_NULL
    }
}

public final class SQLiteConnection {
    private var handle: OpaquePointer?

    public init(path: String) throws {
        let flags = SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX
        let rc = sqlite3_open_v2(path, &handle, flags, nil)
        guard rc == SQLITE_OK, handle != nil else {
            let message = handle.map { String(cString: sqlite3_errmsg($0)) } ?? "unable to open database"
            throw SQLiteError.openFailed(code: rc, message: message)
        }
        // Must be enabled per connection, not once in the schema.
        try execute("PRAGMA foreign_keys = ON")
    }

    deinit {
        sqlite3_close_v2(handle)
    }

    public func execute(_ sql: String) throws {
        var errorPointer: UnsafeMutablePointer<CChar>?
        let rc = sqlite3_exec(handle, sql, nil, nil, &errorPointer)
        guard rc == SQLITE_OK else {
            let message = errorPointer.map { String(cString: $0) }
                ?? handle.map { String(cString: sqlite3_errmsg($0)) }
                ?? "unknown"
            if let errorPointer { sqlite3_free(errorPointer) }
            throw SQLiteError.stepFailed(code: rc, message: message, sql: sql)
        }
    }

    public func prepare(_ sql: String) throws -> SQLiteStatement {
        guard let handle else {
            throw SQLiteError.openFailed(code: SQLITE_MISUSE, message: "connection is closed")
        }
        return try SQLiteStatement(connection: handle, sql: sql)
    }

    public var lastInsertRowID: Int64 {
        handle.map { sqlite3_last_insert_rowid($0) } ?? 0
    }

    public var changes: Int32 {
        handle.map { sqlite3_changes($0) } ?? 0
    }

    public func transaction<T>(_ body: () throws -> T) throws -> T {
        try execute("BEGIN IMMEDIATE")
        do {
            let result = try body()
            try execute("COMMIT")
            return result
        } catch {
            // Rollback failures are secondary; surface the original cause.
            try? execute("ROLLBACK")
            throw error
        }
    }
}
