// Writes a boolean setting without losing the user's last answer.
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

final class SettingWriter {
    /// Performs the write, calling back with the process exit status.
    typealias Write = (Bool, @escaping (Int32) -> Void) -> Void

    private let write: Write
    private var inFlight: Bool?
    private var queued: Bool?

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

    init(write: @escaping Write) {
        self.write = write
    }

    /// True while a write is outstanding. For tests and for leak checks.
    var isBusy: Bool { inFlight != nil }

    /// Ask for the setting to become `value`.
    ///
    /// `persisted` is what is currently on disk, used only when nothing is in
    /// flight -- once something is, where we are heading is known exactly and
    /// the snapshot is the less accurate answer.
    func set(_ value: Bool, persisted: Bool) {
        dispatchPrecondition(condition: .onQueue(.main))
        let heading = queued ?? inFlight ?? persisted
        guard value != heading else { return }
        if inFlight != nil {
            queued = value
            return
        }
        start(value)
    }

    private func start(_ value: Bool) {
        inFlight = value
        write(value) { [weak self] code in
            guard let self else { return }
            self.inFlight = nil
            if let next = self.queued {
                // Superseded. Whether this one failed no longer matters -- the
                // user has since asked for something else, and reverting the UI
                // to a value they have already moved away from would be a
                // worse answer than letting the next write settle it.
                self.queued = nil
                self.start(next)
                return
            }
            if code != 0 {
                self.onFailure?()
            }
        }
    }
}
