import Foundation

/// Minimal logging shim.
///
/// A package rather than a script, so it should not hard-code writes to
/// stderr — tests would spray output and a future embedder may want the lines
/// somewhere else. Deliberately not a protocol with an injected instance:
/// every call site here is diagnostic, and threading a logger through them
/// would cost more than it buys.
public enum MediaLog {
    /// Guards the storage. Separate from `scopeLock` on purpose: `silenced`
    /// holds that one across a whole block, and reading the handler inside
    /// the block would deadlock on a single non-recursive lock.
    private static let lock = NSLock()
    private static let scopeLock = NSLock()

    private nonisolated(unsafe) static var storage: (@Sendable (String) -> Void)? = { message in
        FileHandle.standardError.write(Data((message + "\n").utf8))
    }

    public static var handler: (@Sendable (String) -> Void)? {
        get { lock.lock(); defer { lock.unlock() }; return storage }
        set { lock.lock(); defer { lock.unlock() }; storage = newValue }
    }

    public static func log(_ message: String) {
        handler?(message)
    }

    /// Silences output for the duration of a block. Used by tests.
    ///
    /// Serialized end to end. Two overlapping scopes each save the current
    /// handler and restore it on the way out, so interleaved they restore
    /// each other's saved value -- and the one that saved `nil` wins, leaving
    /// logging off for the rest of the process.
    public static func silenced<T>(_ body: () throws -> T) rethrows -> T {
        scopeLock.lock()
        defer { scopeLock.unlock() }
        let previous = handler
        handler = nil
        defer { handler = previous }
        return try body()
    }
}
