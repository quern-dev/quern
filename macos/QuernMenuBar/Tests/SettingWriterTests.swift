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
