import Foundation

/// Minimal logging shim.
///
/// A package rather than a script, so it should not hard-code writes to
/// stderr — tests would spray output and a future embedder may want the lines
/// somewhere else. Deliberately not a protocol with an injected instance:
/// every call site here is diagnostic, and threading a logger through them
/// would cost more than it buys.
public enum MediaLog {
    public nonisolated(unsafe) static var handler: (@Sendable (String) -> Void)? = { message in
        FileHandle.standardError.write(Data((message + "\n").utf8))
    }

    public static func log(_ message: String) {
        handler?(message)
    }

    /// Silences output for the duration of a block. Used by tests.
    public static func silenced<T>(_ body: () throws -> T) rethrows -> T {
        let previous = handler
        handler = nil
        defer { handler = previous }
        return try body()
    }
}
