// The write path for a boolean setting, with the CLI's completion under test
// control. Every case here is a defect the snapshot-comparison version had and
// none needed a running app to reproduce -- only a completion that had not come
// back yet, which is the state the old guard could not see.

import Foundation

enum SettingWriterTests {
    private final class Rig {
        private(set) var requested: [Bool] = []
        /// Completions the CLI has not answered yet. A test decides when -- and
        /// whether -- each one comes back, which is the state the snapshot
        /// comparison could not see and every defect here lived in.
        private(set) var pending: [(Int32) -> Void] = []
        private(set) var fellBackTo: [Bool] = []
        var writer: SettingWriter!

        /// `answersImmediately: nil` parks each completion instead.
        init(answersImmediately: Int32? = 0) {
            writer = SettingWriter { [unowned self] value, done in
                self.requested.append(value)
                if let code = answersImmediately {
                    done(code)
                } else {
                    self.pending.append(done)
                }
            }
            writer.onFailure = { [unowned self] in self.fellBackTo.append($0) }
        }
    }

    static func all() {
        Harness.test("a value that matches what is persisted is not written") {
            // The reason the guard exists: apply() assigns these on every
            // refresh, and writing back would shell out three times a second.
            let rig = Rig()
            rig.writer.set(true, persisted: true)
            Harness.expect(rig.requested.count, 0, "writes")
        }

        Harness.test("a change from what is persisted is written") {
            let rig = Rig()
            rig.writer.set(false, persisted: true)
            Harness.expect(rig.requested, [false], "writes")
        }

        Harness.test("a second change while the first is in flight is not lost") {
            // The reported bug. The snapshot still says `true` for up to one
            // refresh, so off-then-on compared the second click against a stale
            // value, matched, and dropped it -- leaving the file `off` and the
            // checkbox `on`.
            let rig = Rig(answersImmediately: nil)
            rig.writer.set(false, persisted: true)
            rig.writer.set(true, persisted: true)
            Harness.expect(rig.requested, [false], "only the first has run")

            rig.pending[0](0)   // the `off` write returns
            Harness.expect(rig.requested, [false, true], "the `on` write followed")
        }

        Harness.test("writes are serialized, never overlapped") {
            // Two `quern set-...` processes racing can finish in either order,
            // and the loser's value is the one that sticks.
            let rig = Rig(answersImmediately: nil)
            rig.writer.set(false, persisted: true)
            rig.writer.set(true, persisted: true)
            Harness.expect(rig.pending.count, 1, "outstanding writes")
        }

        Harness.test("only the last answer is written when several arrive") {
            let rig = Rig(answersImmediately: nil)
            rig.writer.set(false, persisted: true)
            rig.writer.set(true, persisted: true)
            rig.writer.set(false, persisted: true)
            rig.pending[0](0)
            // Not three writes: the middle answer was superseded before it ran.
            Harness.expect(rig.requested, [false, false], "writes")
        }

        Harness.test("a value already queued is not queued twice") {
            let rig = Rig(answersImmediately: nil)
            rig.writer.set(false, persisted: true)
            rig.writer.set(true, persisted: true)
            rig.writer.set(true, persisted: true)
            rig.pending[0](0)
            Harness.expect(rig.requested, [false, true], "writes")
        }

        Harness.test("a failed write hands back the value to show instead") {
            let rig = Rig(answersImmediately: 1)
            rig.writer.set(false, persisted: true)
            Harness.expect(rig.fellBackTo, [true], "fallback")
        }

        Harness.test("a successful write asks for no fallback") {
            let rig = Rig(answersImmediately: 0)
            rig.writer.set(false, persisted: true)
            Harness.expect(rig.fellBackTo.isEmpty, "reverted a write that worked")
        }

        Harness.test("a superseded failure does not drag the UI backwards") {
            // The user has already asked for something else. Reverting to a
            // value they moved away from would be a worse answer than letting
            // the next write settle it.
            let rig = Rig(answersImmediately: nil)
            rig.writer.set(false, persisted: true)
            rig.writer.set(true, persisted: true)
            rig.pending[0](1)   // the `off` write failed
            Harness.expect(rig.fellBackTo.isEmpty, "reverted despite a newer request")
            Harness.expect(rig.requested, [false, true], "the newer write ran")
        }

        Harness.test("the writer is idle once everything has settled") {
            let rig = Rig(answersImmediately: nil)
            rig.writer.set(false, persisted: true)
            Harness.expect(rig.writer.isBusy, true, "busy during the write")
            rig.pending[0](0)
            Harness.expect(rig.writer.isBusy, false, "still busy afterwards")
        }
    }
}

// The deadline that bounds the queue. Its own suite because it is a constant,
// and a constant nothing asserts can be changed without any test noticing.
enum SettingsWriteTimeoutTests {
    static func all() {
        Harness.test("a settings write is abandoned well before the CLI default") {
            // SettingWriter serializes these, so this deadline is how long a
            // queued write can wait for its predecessor. At the 120s default a
            // hung write held the next click for two minutes, which for a
            // checkbox reads as the app having ignored it.
            Harness.expect(QuernCLI.settingsWriteTimeout < 120,
                           "must be shorter than the run() default")
            Harness.expect(QuernCLI.settingsWriteTimeout <= 30,
                           "a queued click should not wait half a minute")
        }

        Harness.test("but long enough for the work it does") {
            // One small file rewritten under a lock, plus interpreter start-up,
            // measured at about half a second. Too tight and a slow machine
            // gets its writes killed, which is worse than a slow one.
            Harness.expect(QuernCLI.settingsWriteTimeout >= 10,
                           "leaves no headroom over the measured cost")
        }
    }
}
