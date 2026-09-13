// Where the menu bar's log lines go, and how to find them again.
//
// Everything here used to be bare `NSLog`, which lands in the unified log under
// the process name and nothing else. That is enough to find the app's output
// and not enough to find anything within it: a settings write, a daemon
// restart and an update all read the same. Filtering meant grepping prose.
//
// `os.Logger` carries a subsystem and a category, so the same lines become
// selectable:
//
//     log show --last 30m --predicate 'subsystem == "dev.quern.menubar"'
//     log show --last 30m --predicate 'subsystem == "dev.quern.menubar" \
//                                      AND category == "settings"'
//     log stream --predicate 'subsystem == "dev.quern.menubar"'
//
// The subsystem is the bundle identifier, which is what Console.app groups by.
// Categories are named for what a reader would be looking for rather than for
// the type that emits them -- "settings", not "SettingWriter" -- because the
// person filtering is chasing a behaviour, not a class.

import Foundation
import os

enum Log {
    static let subsystem = "dev.quern.menubar"

    /// Writes to config.json: the three controls that shell out to the CLI.
    static let settings = Logger(subsystem: subsystem, category: "settings")

    /// Starting, stopping and restarting the daemon.
    static let lifecycle = Logger(subsystem: subsystem, category: "lifecycle")

    /// Checking for and applying updates, including the relaunch.
    static let updater = Logger(subsystem: subsystem, category: "updater")

    /// The status item and the windows it opens.
    static let ui = Logger(subsystem: subsystem, category: "ui")
}
