// Quern menu-bar daemon manager — entry point.
//
// An LSUIElement (accessory) AppKit app: no Dock icon, just a status-bar
// item that surfaces daemon state and drives the `quern` CLI. Top-level
// code is only permitted in main.swift; the rest lives in sibling files.

import AppKit

let app = NSApplication.shared
// Accessory: live in the menu bar, never the Dock. Mirrors LSUIElement in
// Info.plist (both are set so the policy holds whether launched as a bundle
// or as a bare binary during development).
app.setActivationPolicy(.accessory)

let delegate = AppDelegate()
app.delegate = delegate
app.run()
