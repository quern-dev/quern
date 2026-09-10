# The Menu Bar App

Quern runs as a background daemon, which means the honest answer to "is it
running?" usually involves a terminal. The menu-bar app puts that answer in the
corner of your screen instead, and gives you the handful of controls you would
otherwise type.

It is macOS only. Everything it does, the CLI already did.

## Getting it

It arrives with Quern. `quern setup` installs it and `quern update` keeps it
current, so there is nothing separate to download.

If you installed before v0.15.0 and have never updated, you will not have it —
the app ships inside the release asset rather than GitHub's generated source
tarball. One `quern update` brings it in.

## What the menu shows

Clicking the icon gives you the daemon's state at a glance:

```
Quern is running · up 3h
Active device: iPhone 16 Pro (Simulator)
Proxy: running :9101
```

The active device row names the device rather than showing its 36-character
identifier, and says what kind it is — `(Simulator)`, `(Device)` or
`(Emulator)` — because a simulator and the phone on your desk otherwise look
identical. When Quern has not enumerated the device it says nothing rather than
guessing, so an unqualified name means "type unknown", not "simulator".

The icon itself dims when the server is stopped, and its tooltip tells you when
an update is waiting.

## What you can do from it

**Start, stop and restart the server.** The same operations as `quern start`,
`quern stop` and `quern restart`. There is no separate lifecycle: the app does
not own the daemon, it asks.

**Restart to update.** Appears only once an update has been staged, and
restarts into the new version. This is the Ollama pattern — no separate
updater, no second update path.

**Screen mirror.** Opens a live view of connected devices. Windows fit the
device's own proportions, open when you plug a phone in, and close when you
unplug it. See [Live Video Preview](ios-preview.md) for the detail.

This item is hidden if the screen-mirror app could not be built, which happens
when Xcode Command Line Tools are missing. `quern setup` will tell you, and
offer to open the installer.

**Settings.** The update channel picker — stable or beta, the same setting as
`quern set-channel` — and a launch-at-login toggle. It also shows the server's
address, version and uptime, and the full UDID of the active device, which the
menu deliberately leaves out to keep itself narrow.

## Quitting

**Quit Quern** closes the app and leaves the server running. That is usually
what you want: the daemon is doing the work, and the menu bar is only a window
onto it.

Hold Option to reveal **Quit and Stop Server** for when you mean both. If the
stop fails, the app stays open and tells you, rather than quitting and leaving
a server running with nothing to say so.

## Launch at login

The app registers itself the first time it runs, so it comes back after a
reboot. Turn it off in Settings.

This depends on the app having a stable code-signing identity, which the
released build has. If you build the app yourself from `macos/QuernMenuBar`,
the signature changes on every rebuild and macOS will treat each one as a
different app, so launch-at-login and permissions will not stick. That is
expected for local development.

## What it deliberately does not do

It is not a control surface for Quern's features. There is no proxy toggle, no
device picker, no log viewer. Those live in the CLI and the MCP tools, where an
agent can reach them and where the output is something you can pipe.

The menu bar answers "is it up, what is it pointed at, and can I restart it" —
the questions you would otherwise open a terminal to answer.
