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
            writer.onFailure = { [unowned self] in self.fellBackTo.append(true) }
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
            // Asserts on the queue rather than on the writes. The version that
            // checked only `requested` passed against its own mutation:
            // dropping `queued` from `heading` still produced [false, true],
            // because the second queue attempt overwrites the first with the
            // same value. Only a third distinct request makes the difference
            // visible.
            let rig = Rig(answersImmediately: nil)
            rig.writer.set(false, persisted: true)
            rig.writer.set(true, persisted: true)
            rig.writer.set(true, persisted: true)
            rig.writer.set(false, persisted: true)
            rig.pending[0](0)
            // Not [false, true, false]: the repeat was recognised as already
            // queued, so the last distinct answer replaced it rather than
            // stacking behind it.
            Harness.expect(rig.requested, [false, false], "writes")
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

// The real SettingsModel, not a rig that mirrors it.
//
// The first version of these tests built a stand-in that copied the production
// wiring. It reproduced both regressions faithfully and then passed against
// their fixes' mutations, because mutating SettingsWindow.swift could not
// affect a copy living in the test file. That is the recorded shape of a test
// proving nothing, and it is why the decision moved out of the view's
// `.onChange` and into the model where it can be called.
enum SettingsModelWritingTests {
    /// A model whose writes are captured instead of shelling out.
    private final class Rig {
        let model = SettingsModel()
        private(set) var requested: [Bool] = []
        private var pending: [(Int32) -> Void] = []
        var persisted: Bool

        init(persisted: Bool) {
            self.persisted = persisted
            // Only the subprocess is replaced. The writer, its failure handler
            // and the model's guards are all the shipped ones.
            model.writeAutoCheck = { [unowned self] value, done in
                self.requested.append(value)
                self.pending.append(done)
            }
            refresh()
        }

        /// A snapshot landing, the way StateReader's poll delivers one.
        func refresh() {
            var snap = QuernSnapshot()
            var info = UpdateInfo()
            info.autoCheck = persisted
            snap.update = info
            model.apply(snap)
        }

        func completeFirst(_ code: Int32, persisting: Bool? = nil) {
            let done = pending.removeFirst()
            if code == 0, let persisting { persisted = persisting }
            done(code)
        }
    }

    static func all() {
        Harness.test("a refresh during a write does not undo the click") {
            // `apply()` assigns the property on every refresh, and the file
            // still holds the old value until the write lands. Assigning from
            // it mid-write pushed the checkbox back to where the user had just
            // moved it from -- and, once the guard compared against the
            // in-flight value instead of the snapshot, queued that echo as a
            // fresh request and wrote the change straight back off.
            let rig = Rig(persisted: false)
            rig.model.userSetAutoCheck(true)
            Harness.expect(rig.requested, [true], "the write started")

            rig.refresh()
            // The checkbox must still show the click. A refresh mid-write reads
            // a file that has not caught up yet, so assigning from it pushes the
            // control back to where the user moved it from.
            Harness.expect(rig.model.autoCheckUpdates, true,
                           "a refresh reverted the checkbox mid-write")
            rig.completeFirst(0, persisting: true)

            Harness.expect(rig.requested, [true], "a refresh queued a reversal")
            Harness.expect(rig.persisted, true, "the user's change was lost")
        }

        Harness.test("a failed write reverts to the disk, not to the opposite") {
            // Disk is off. Click on, then off inside the window. The `on` write
            // is superseded; the `off` write runs and fails, so the file is
            // still off and the checkbox must end off. Handing back the
            // negation of the failed write said `on`, which then differed from
            // the file and went out as a third write nobody asked for.
            let rig = Rig(persisted: false)
            rig.model.userSetAutoCheck(true)
            rig.model.userSetAutoCheck(false)
            rig.completeFirst(0)
            Harness.expect(rig.requested, [true, false], "the `off` write followed")

            rig.completeFirst(1)

            Harness.expect(rig.model.autoCheckUpdates, false, "should show the disk")
            Harness.expect(rig.requested.count, 2, "the revert issued another write")
        }

        Harness.test("two clicks still survive a write in flight") {
            let rig = Rig(persisted: true)
            rig.model.userSetAutoCheck(false)
            rig.model.userSetAutoCheck(true)
            Harness.expect(rig.requested, [false], "only the first has run")
            rig.completeFirst(0, persisting: false)
            Harness.expect(rig.requested, [false, true], "the second followed")
        }

        Harness.test("reconciling with the file never writes") {
            // The reason the whole property has a guard: apply() runs three
            // times a second, and every one of those assignments would
            // otherwise shell out.
            let rig = Rig(persisted: true)
            rig.persisted = false
            rig.refresh()
            Harness.expect(rig.model.autoCheckUpdates, false, "the UI followed")
            Harness.expect(rig.requested.isEmpty, "a refresh wrote to disk")
        }
    }
}

// The invariant `reconcile` leans on, asserted rather than left implicit.
enum ReconcileInvariantTests {
    static func all() {
        Harness.test("apply assigns the snapshot before reconciling from it") {
            // If it did not, `didSet` would compare the new value against a
            // stale `persisted`, see a difference, and write the file back to
            // the value it already holds -- three times a second.
            let model = SettingsModel()
            var wrote: [Bool] = []
            model.writeAutoCheck = { value, _ in wrote.append(value) }

            for value in [false, true, false, false, true] {
                var snap = QuernSnapshot()
                var info = UpdateInfo()
                info.autoCheck = value
                snap.update = info
                model.apply(snap)
                Harness.expect(model.autoCheckUpdates, value, "UI followed \(value)")
            }
            Harness.expect(wrote.isEmpty, "reconciling with the file wrote to it")
        }
    }
}
