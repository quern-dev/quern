// Writes a setting without losing the user's last answer.
//
// The obvious guard -- "only write when the new value differs from the
// snapshot" -- exists because `apply()` assigns these properties on every
// refresh, and writing back on that path would shell out three times a second.
// But the snapshot only catches up when the next poll lands, so during the
// window after a click it still holds the *old* value. Click twice inside that
// window and the second click compares against a stale snapshot, matches, and
// is dropped: the file keeps the first answer while the checkbox shows the
// second. The two disagree until something else refreshes.
//
// So the comparison is against where this thing is actually heading -- the
// value queued, or in flight, or last persisted, in that order -- rather than
// against what was last read from disk.
//
// Writes are also serialized. Two `quern set-...` processes racing each other
// can finish in either order, and the loser's value is the one that sticks. One
// at a time, with a superseded request simply replaced, means the last thing
// the user asked for is the last thing written.

import Foundation

final class SettingWriter<Value: Equatable> {
    /// Performs the write, calling back with the exit status and whatever the
    /// CLI printed. The output is carried because it is the only account of
    /// *why* a write failed; without it a failed setting reverts the control
    /// and leaves nothing to look at.
    typealias Write = (Value, @escaping (Int32, String) -> Void) -> Void

    private let write: Write
    /// What this writes, for the log. "the update check", not "updateCheck".
    private let name: String
    /// Where the account goes. Injected the way LifecycleController does it, so
    /// a test can read what was logged instead of writing to the system log.
    var log: (String) -> Void = { Log.settings.notice("\($0, privacy: .public)") }
    private var inFlight: Value?
    private var queued: Value?

    /// Called when a write fails and nothing newer is waiting.
    ///
    /// Takes no value deliberately. It used to hand back the negation of the
    /// write that failed, which is only the same as "what is on disk" when
    /// nothing superseded anything -- click on then off, let the `off` write
    /// fail, and the negation says `on` while the file says `off`. The caller
    /// knows the persisted value and this does not, so the caller supplies it.
    ///
    /// That also stops the revert becoming a write. Assigning the persisted
    /// value fires the UI's change handler, which re-enters `set()`, where it
    /// matches `persisted` and stops. Assigning anything else does not.
    var onFailure: (() -> Void)?

    init(name: String, write: @escaping Write) {
        self.name = name
        self.write = write
    }

    /// True while a write is outstanding. For tests and for leak checks.
    var isBusy: Bool { inFlight != nil }

    /// Ask for the setting to become `value`.
    ///
    /// `persisted` is what is currently on disk, used only when nothing is in
    /// flight -- once something is, where we are heading is known exactly and
    /// the snapshot is the less accurate answer.
    func set(_ value: Value, persisted: Value) {
        dispatchPrecondition(condition: .onQueue(.main))
        let heading = queued ?? inFlight ?? persisted
        guard value != heading else { return }
        if inFlight != nil {
            queued = value
            return
        }
        start(value)
    }

    private func start(_ value: Value) {
        inFlight = value
        write(value) { [weak self] code, output in
            guard let self else { return }
            self.inFlight = nil
            // Every write, not only the failures. Forty-odd settings changes
            // left no trace anywhere, so a write that failed reverted the
            // control with nothing to look at -- and a write that succeeded
            // could not be told from one that never ran.
            if code == 0 {
                self.log("\(self.name): set to \(value)")
            } else {
                let detail = output.trimmingCharacters(in: .whitespacesAndNewlines)
                self.log("\(self.name): could not set to \(value) "
                         + "(exit \(code))" + (detail.isEmpty ? "" : ": \(detail)"))
            }
            if let next = self.queued {
                // Superseded. Whether this one failed no longer matters -- the
                // user has since asked for something else, and reverting the UI
                // to a value they have already moved away from would be a
                // worse answer than letting the next write settle it.
                self.queued = nil
                if code == 0, next == value {
                    // Except when the queue holds what this write just put on
                    // disk. Three clicks -- beta, stable, beta -- leave beta
                    // queued behind a beta write, and running it sends a second
                    // identical command. For the channel that is not merely
                    // wasteful: `set-channel` discards the cached update check,
                    // so the redundant write throws the update hint away again.
                    //
                    // Only on success. A failed write did not reach the disk,
                    // so the queued copy of the same value is a retry rather
                    // than a repeat, and dropping it would leave the user's
                    // choice unwritten with nothing left to correct it.
                    return
                }
                self.start(next)
                return
            }
            if code != 0 {
                self.onFailure?()
            }
        }
    }
}
