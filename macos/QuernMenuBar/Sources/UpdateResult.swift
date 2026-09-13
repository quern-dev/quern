// What `quern update` recorded about its own run.
//
// The exit code cannot carry this. "Already up to date" has to exit 0, the
// same as a real update, or every script that treats nonzero as failure
// breaks. So a caller seeing 0 cannot tell whether anything happened -- and
// the menu bar, assuming it had, polled for thirty seconds waiting for a
// version to change that was never going to, then announced that an update had
// finished when none was attempted.
//
// Read rather than parsed out of the CLI's output: the wording is for people
// and will change, and matching on it would make every message edit a
// behaviour change somewhere else.

import Foundation

struct UpdateResult {
    enum Outcome: String {
        case updated
        case noOp = "no_op"
        case failed
    }

    let outcome: Outcome
    let detail: String
    let version: String?

    /// When the update that wrote this finished.
    ///
    /// Load-bearing, not metadata. The CLI that performs the upgrade *to* the
    /// first version writing this file is the old one, which does not write it
    /// -- so a real update can leave the previous run's record sitting there,
    /// and a stale "no_op" read as this run's answer would skip the relaunch
    /// after an update that genuinely happened. The caller compares this
    /// against when it started.
    let finishedAt: Date?

    var isNoOp: Bool { outcome == .noOp }

    /// Whether this record describes a run that started at or after `start`.
    func describes(runStartedAt start: Date) -> Bool {
        guard let finishedAt else { return false }
        return finishedAt >= start
    }

    static let file = StateReader.quernDir
        .appendingPathComponent("last-update.json")

    /// The record, or nil if there isn't a readable one.
    ///
    /// nil on anything unexpected -- missing, unreadable, unparseable, an
    /// outcome this build does not know. The caller then falls back to the
    /// version poll, which is what it did before this file existed, so an old
    /// menu bar against a newer CLI is merely no better off rather than wrong.
    static func read(from url: URL = UpdateResult.file) -> UpdateResult? {
        guard let data = try? Data(contentsOf: url),
              let object = try? JSONSerialization.jsonObject(with: data),
              let d = object as? [String: Any],
              let raw = d["outcome"] as? String,
              let outcome = Outcome(rawValue: raw)
        else { return nil }
        return UpdateResult(
            outcome: outcome,
            detail: d["detail"] as? String ?? "",
            version: d["version"] as? String,
            finishedAt: (d["finished_at"] as? String).flatMap(Self.parse)
        )
    }
}

extension UpdateResult {
    /// The CLI writes `datetime.now(UTC).isoformat()`, which carries fractional
    /// seconds. ISO8601DateFormatter rejects those unless told to expect them,
    /// and it is the difference between a usable timestamp and none.
    static func parse(_ text: String) -> Date? {
        let withFraction = ISO8601DateFormatter()
        withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = withFraction.date(from: text) { return d }
        return ISO8601DateFormatter().date(from: text)
    }
}
