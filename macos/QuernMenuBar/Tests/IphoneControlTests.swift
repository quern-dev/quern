// The iPhone control setting (#664), through the real model: only the
// subprocesses -- the CLI write and the Xcode check -- are replaced, as in
// OtherSettingsWritingTests.

import Foundation

enum IphoneControlTests {
    private final class Rig {
        let model = SettingsModel()
        private(set) var writes: [String] = []
        private var pending: [(Int32, String) -> Void] = []
        var reader: String

        init(reader: String = "coredevice") {
            self.reader = reader
            model.logSettings = { _ in }
            model.writeDeviceReader = { [unowned self] value, done in
                self.writes.append(value)
                self.pending.append(done)
            }
            refresh()
        }

        func refresh() {
            var snap = QuernSnapshot()
            snap.iphone = IphoneControl(reader: reader)
            model.apply(snap)
        }

        func complete(_ code: Int32, persisting: String? = nil) {
            let done = pending.removeFirst()
            if code == 0, let persisting { reader = persisting }
            done(code, "")
        }
    }

    static func all() {
        Harness.test("a refresh never rewrites iPhone control") {
            let rig = Rig(reader: "wda")
            for _ in 0..<5 { rig.refresh() }
            Harness.expect(rig.writes.isEmpty, "a refresh wrote the setting back")
            Harness.expect(rig.model.deviceReader, "wda", "the picker followed")
        }

        Harness.test("choosing WDA writes it once, and a refresh mid-write keeps it") {
            let rig = Rig()
            rig.model.deviceReader = "wda"
            rig.refresh()
            Harness.expect(rig.model.deviceReader, "wda", "a refresh reverted the picker")
            rig.complete(0, persisting: "wda")
            Harness.expect(rig.writes, ["wda"], "written once")
        }

        Harness.test("a failed iPhone control write shows what is on disk") {
            let rig = Rig()
            rig.model.deviceReader = "wda"
            rig.complete(1)
            Harness.expect(rig.model.deviceReader, "coredevice", "should show the disk")
            Harness.expect(rig.writes.count, 1, "the revert issued another write")
        }

        Harness.test("Through WDA is greyed out only without Xcode, and not when chosen") {
            let rig = Rig()
            for (xcode, expected) in [(false, true), (true, false)] as [(Bool?, Bool)] {
                rig.model.xcodeInstalled = xcode
                Harness.expect(rig.model.wdaChoiceDisabled, expected, "xcode \(String(describing: xcode))")
            }
            // Could not tell is no reason to refuse, as on the server.
            rig.model.xcodeInstalled = nil
            Harness.expect(!rig.model.wdaChoiceDisabled, "disabled when it could not tell")
            // Set to WDA with no Xcode: the picker stays, so it can be moved back.
            let wda = Rig(reader: "wda")
            wda.model.xcodeInstalled = false
            Harness.expect(!wda.model.wdaChoiceDisabled, "a wda setting could not be moved back")
        }

        Harness.test("the note says when WDA is set and cannot run") {
            let warned = SettingsModel.iphoneControlNote(reader: "wda", xcodeInstalled: false)
            Harness.expect(warned.contains("Xcode is not installed")
                           && warned.contains("either way"), "no warning: \(warned)")
            let greyed = SettingsModel.iphoneControlNote(reader: "coredevice", xcodeInstalled: false)
            Harness.expect(greyed.contains("Xcode is not installed"), "the greyed choice is not explained")
            for xcode in [true, nil] as [Bool?] {
                for reader in ["coredevice", "wda"] {
                    let note = SettingsModel.iphoneControlNote(reader: reader, xcodeInstalled: xcode)
                    Harness.expect(!note.contains("not installed"), "\(reader)/\(String(describing: xcode)): \(note)")
                }
            }
        }

        Harness.test("opening the window asks whether Xcode is installed") {
            let model = SettingsModel()
            model.readXcode = { done in done(false) }
            model.refreshXcode()
            Harness.expect(model.xcodeInstalled, false, "the answer was not kept")
        }

        Harness.test("only a developer directory with an xcodebuild is Xcode") {
            let dir = FileManager.default.temporaryDirectory
                .appendingPathComponent("quern-xcode-\(UUID().uuidString)")
            defer { try? FileManager.default.removeItem(at: dir) }
            let bin = dir.appendingPathComponent("usr/bin")
            try? FileManager.default.createDirectory(at: bin, withIntermediateDirectories: true)
            Harness.expect(!XcodeCheck.hasXcodebuild(developerDirectory: dir.path),
                           "the Command Line Tools read as Xcode")
            Harness.expect(!XcodeCheck.hasXcodebuild(developerDirectory: ""), "none named read as Xcode")
            let tool = bin.appendingPathComponent("xcodebuild").path
            FileManager.default.createFile(atPath: tool, contents: Data(),
                                           attributes: [.posixPermissions: 0o755])
            Harness.expect(XcodeCheck.hasXcodebuild(developerDirectory: dir.path), "Xcode not seen")
        }
    }
}
