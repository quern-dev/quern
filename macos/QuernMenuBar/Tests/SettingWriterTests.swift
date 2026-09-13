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
        private(set) var pending: [(Int32, String) -> Void] = []
        private(set) var fellBackTo: [Bool] = []
        var writer: SettingWriter<Bool>!

        /// `answersImmediately: nil` parks each completion instead.
        init(answersImmediately: Int32? = 0) {
            writer = SettingWriter(name: "a test setting") { [unowned self] value, done in
                self.requested.append(value)
                if let code = answersImmediately {
                    done(code, "")
                } else {
                    self.pending.append(done)
                }
            }
            writer.log = { _ in }
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

            rig.pending[0](0, "")   // the `off` write returns
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
            rig.pending[0](0, "")
            // Not three writes: the middle answer was superseded before it ran.
            // And not two either -- the queued value is what the write that
            // just succeeded already put on disk, so repeating it sends a
            // command nobody asked for. This assertion used to read
            // [false, false], encoding that redundant write as correct.
            Harness.expect(rig.requested, [false], "writes")
        }

        Harness.test("a queued value equal to the one just written is dropped") {
            // beta, stable, beta before the first write returns. The queue ends
            // holding beta, which is exactly what the in-flight write is about
            // to put on disk -- so running it sends a second identical command.
            // For the channel that is not merely wasteful: `set-channel`
            // discards the cached update check, so the redundant write throws
            // away the update hint a second time.
            let rig = Rig(answersImmediately: nil)
            rig.writer.set(false, persisted: true)
            rig.writer.set(true, persisted: true)
            rig.writer.set(false, persisted: true)
            rig.pending[0](0, "")
            Harness.expect(rig.requested, [false], "a redundant write went out")
        }

        Harness.test("but a queued value is retried when the write failed") {
            // Same shape, failed write. The value is not on disk, so the queued
            // copy of it is not redundant -- dropping it would leave the user's
            // choice unwritten with nothing to correct it.
            let rig = Rig(answersImmediately: nil)
            rig.writer.set(false, persisted: true)
            rig.writer.set(true, persisted: true)
            rig.writer.set(false, persisted: true)
            rig.pending[0](1, "")
            Harness.expect(rig.requested, [false, false], "the retry was dropped")
        }

        // Removed: "a value already queued is not queued twice".
        //
        // It promised a behaviour the design makes structurally impossible --
        // `queued` is one slot, so a repeat overwrites itself -- and no
        // mutation distinguished it. A review flagged it as passing against
        // its own mutation once; the redundant-write fix then collapsed its
        // scenario entirely. "only the last answer is written when several
        // arrive" covers what it was reaching for.

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
            rig.pending[0](1, "")   // the `off` write failed
            Harness.expect(rig.fellBackTo.isEmpty, "reverted despite a newer request")
            Harness.expect(rig.requested, [false, true], "the newer write ran")
        }

        Harness.test("the writer is idle once everything has settled") {
            let rig = Rig(answersImmediately: nil)
            rig.writer.set(false, persisted: true)
            Harness.expect(rig.writer.isBusy, true, "busy during the write")
            rig.pending[0](0, "")
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
        private var pending: [(Int32, String) -> Void] = []
        var persisted: Bool

        init(persisted: Bool) {
            self.persisted = persisted
            // Only the subprocess is replaced. The writer, its failure handler
            // and the model's guards are all the shipped ones.
            model.logSettings = { _ in }
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
            done(code, "")
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
            model.logSettings = { _ in }
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

// The other two controls in the same window, driven through the real model.
//
// Same defects, same shape, and the same rule about where to inject: only the
// subprocess is replaced, so the writers, their failure handlers and the
// model's guards are the shipped ones. A rig that stood in for any of those
// would pass while production stayed broken -- measured, twice.
enum OtherSettingsWritingTests {
    private final class Rig {
        let model = SettingsModel()
        private(set) var channels: [String] = []
        private(set) var certs: [Bool] = []
        private var pendingChannel: [(Int32, String) -> Void] = []
        private var pendingCert: [(Int32, String) -> Void] = []
        var channel: String
        var cert: Bool

        init(channel: String = "stable", cert: Bool = false) {
            self.channel = channel
            self.cert = cert
            model.logSettings = { _ in }
            model.writeChannel = { [unowned self] value, done in
                self.channels.append(value)
                self.pendingChannel.append(done)
            }
            model.writeAutoInstallCert = { [unowned self] value, done in
                self.certs.append(value)
                self.pendingCert.append(done)
            }
            refresh()
        }

        func refresh() {
            var snap = QuernSnapshot()
            var info = UpdateInfo()
            info.channel = channel
            snap.update = info
            snap.proxy = ProxyPolicy(autoInstallCert: cert)
            model.apply(snap)
        }

        func completeChannel(_ code: Int32, persisting: String? = nil) {
            let done = pendingChannel.removeFirst()
            if code == 0, let persisting { channel = persisting }
            done(code, "")
        }

        func completeCert(_ code: Int32, persisting: Bool? = nil) {
            let done = pendingCert.removeFirst()
            if code == 0, let persisting { cert = persisting }
            done(code, "")
        }
    }

    static func all() {
        Harness.test("a refresh never rewrites the channel it just read") {
            // The reason this control had a guard at all, and why it matters
            // more here than anywhere else: `set-channel` discards the cached
            // update check, so merely opening Settings on a beta machine used
            // to wipe the update hint.
            let rig = Rig(channel: "beta")
            for _ in 0..<5 { rig.refresh() }
            Harness.expect(rig.channels.isEmpty, "a refresh wrote the channel back")
            Harness.expect(rig.model.channel, "beta", "the picker followed")
        }

        Harness.test("a second channel change while the first is in flight is kept") {
            let rig = Rig(channel: "stable")
            rig.model.channel = "beta"
            rig.model.channel = "stable"
            Harness.expect(rig.channels, ["beta"], "only the first has run")
            rig.completeChannel(0, persisting: "beta")
            Harness.expect(rig.channels, ["beta", "stable"], "the second followed")
        }

        Harness.test("a refresh during a channel write does not undo it") {
            let rig = Rig(channel: "stable")
            rig.model.channel = "beta"
            rig.refresh()
            Harness.expect(rig.model.channel, "beta", "a refresh reverted the picker")
            rig.completeChannel(0, persisting: "beta")
            Harness.expect(rig.channels, ["beta"], "a refresh queued a reversal")
        }

        Harness.test("a failed channel write shows what is on disk") {
            let rig = Rig(channel: "stable")
            rig.model.channel = "beta"
            rig.completeChannel(1)
            Harness.expect(rig.model.channel, "stable", "should show the disk")
            Harness.expect(rig.channels.count, 1, "the revert issued another write")
        }

        Harness.test("a refresh never rewrites the certificate policy") {
            let rig = Rig(cert: true)
            for _ in 0..<5 { rig.refresh() }
            Harness.expect(rig.certs.isEmpty, "a refresh wrote the policy back")
            Harness.expect(rig.model.autoInstallCert, true, "the toggle followed")
        }

        Harness.test("a second certificate change in flight is kept") {
            let rig = Rig(cert: false)
            rig.model.autoInstallCert = true
            rig.model.autoInstallCert = false
            Harness.expect(rig.certs, [true], "only the first has run")
            rig.completeCert(0, persisting: true)
            Harness.expect(rig.certs, [true, false], "the second followed")
        }

        Harness.test("a refresh during a certificate write does not undo it") {
            // Asserts the visible state, not just that no write went out. The
            // write is already suppressed by the writer's own guard, so the
            // isBusy check exists solely to stop the control flicking back to
            // the old value while the write is in flight -- and only an
            // assertion on the control can see that.
            let rig = Rig(cert: false)
            rig.model.autoInstallCert = true
            rig.refresh()
            Harness.expect(rig.model.autoInstallCert, true,
                           "a refresh reverted the toggle mid-write")
            rig.completeCert(0, persisting: true)
            Harness.expect(rig.certs, [true], "a refresh queued a reversal")
        }

        Harness.test("a failed certificate write shows what is on disk") {
            let rig = Rig(cert: false)
            rig.model.autoInstallCert = true
            rig.completeCert(1)
            Harness.expect(rig.model.autoInstallCert, false, "should show the disk")
            Harness.expect(rig.certs.count, 1, "the revert issued another write")
        }

        Harness.test("the two controls do not interfere") {
            // They share a snapshot and a window, and a user can move both
            // before either write lands.
            let rig = Rig(channel: "stable", cert: false)
            rig.model.channel = "beta"
            rig.model.autoInstallCert = true
            Harness.expect(rig.channels, ["beta"], "channel write")
            Harness.expect(rig.certs, [true], "cert write")
            rig.completeChannel(0, persisting: "beta")
            rig.completeCert(0, persisting: true)
            rig.refresh()
            Harness.expect(rig.model.channel, "beta", "channel settled")
            Harness.expect(rig.model.autoInstallCert, true, "cert settled")
        }
    }
}

// What the log says about a settings write.
//
// Asserted because it is the only account there is. Forty-odd settings changes
// during one session of hand-testing left no trace anywhere: a write that
// failed reverted the control with nothing to look at, and a write that
// succeeded could not be told from one that never ran.
enum SettingWriteLoggingTests {
    private final class Rig {
        private(set) var lines: [String] = []
        private var pending: [(Int32, String) -> Void] = []
        var writer: SettingWriter<Bool>!

        init() {
            writer = SettingWriter(name: "the thing") { [unowned self] _, done in
                self.pending.append(done)
            }
            writer.log = { [unowned self] in self.lines.append($0) }
        }

        func complete(_ code: Int32, _ output: String = "") {
            pending.removeFirst()(code, output)
        }
    }

    static func all() {
        Harness.test("a successful write is logged") {
            let rig = Rig()
            rig.writer.set(true, persisted: false)
            rig.complete(0)
            Harness.expect(rig.lines.count, 1, "lines")
            Harness.expect(rig.lines.first?.contains("the thing") == true,
                           "the line does not say what was written")
            Harness.expect(rig.lines.first?.contains("true") == true,
                           "the line does not say what it was set to")
        }

        Harness.test("a failed write logs the exit code and the CLI's reason") {
            // The reason is the whole point. "Could not set it" with no cause
            // is the dead end this project keeps removing.
            let rig = Rig()
            rig.writer.set(true, persisted: false)
            rig.complete(1, "Error: could not find project root")
            Harness.expect(rig.lines.count, 1, "lines")
            let line = rig.lines.first ?? ""
            Harness.expect(line.contains("exit 1"), "no exit code: \(line)")
            Harness.expect(line.contains("could not find project root"),
                           "the CLI's reason was dropped: \(line)")
        }

        Harness.test("a failure with no output still names the exit code") {
            // A process killed by the watchdog prints nothing. The line must
            // not trail off into an empty colon.
            let rig = Rig()
            rig.writer.set(true, persisted: false)
            rig.complete(-3, "   \n  ")
            let line = rig.lines.first ?? ""
            Harness.expect(line.contains("exit -3"), "no exit code: \(line)")
            Harness.expect(line.hasSuffix(":") == false, "trailing colon: \(line)")
        }

        Harness.test("every write in a queue is logged, not just the last") {
            // Two clicks, two writes, two lines. Logging only the settled value
            // would hide a first write that failed on its way to a second that
            // worked.
            let rig = Rig()
            rig.writer.set(true, persisted: false)
            rig.writer.set(false, persisted: false)
            rig.complete(0)
            rig.complete(0)
            Harness.expect(rig.lines.count, 2, "lines")
        }

        Harness.test("a write that was never made is not logged") {
            let rig = Rig()
            rig.writer.set(false, persisted: false)
            Harness.expect(rig.lines.isEmpty, "logged a write it did not make")
        }
    }
}

// The channel before any snapshot has landed.
//
// `readChannel()` never returns nil -- it falls back to the default -- so
// `snapshot.update.channel` is only nil before the first refresh. That window
// is real: StateReader polls every three seconds, and Settings can be opened
// inside it.
enum ChannelWithoutASnapshotTests {
    static func all() {
        Harness.test("the first change writes even with no snapshot yet") {
            // `?? channel` passed the new value as its own persisted value, so
            // the writer saw no difference and never wrote. The picker moved
            // to beta and `set-channel beta` never ran.
            let model = SettingsModel()
            var wrote: [String] = []
            model.logSettings = { _ in }
            model.writeChannel = { value, _ in wrote.append(value) }

            Harness.expect(model.snapshot.update.channel == nil,
                           "precondition: no snapshot has landed")
            model.channel = "beta"

            Harness.expect(wrote, ["beta"], "the first channel change was dropped")
        }

        Harness.test("and still does not write when nothing changed") {
            let model = SettingsModel()
            var wrote: [String] = []
            model.logSettings = { _ in }
            model.writeChannel = { value, _ in wrote.append(value) }

            model.channel = "stable"   // already the initial value

            Harness.expect(wrote.isEmpty, "wrote a change that was not one")
        }
    }
}
