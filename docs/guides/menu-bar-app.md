# The Menu Bar App

Quern runs as a background daemon, which means the honest answer to "is it
running?" usually involves a terminal. The menu-bar app puts that answer in the
corner of your screen instead, and gives you the handful of controls you would
otherwise type.

It is macOS only. Everything it does, the CLI already did.

## Getting it

If you installed Quern the normal way, it arrives with it. `quern setup`
installs the app and `quern update` keeps it current, so there is nothing
separate to download.

If you installed before v0.15.0 and have never updated, you will not have it —
the app ships inside the release asset rather than GitHub's generated source
tarball. One `quern update` brings it in.

### From a git clone

A clone does not get the app, and this is deliberate: `quern setup` skips it
on the assumption that anyone working from source would rather control their
own build than have one installed over it.

That left no instructions at all, which is worse. There are now:

```sh
scripts/install-menubar-app.sh            # the signed app from the matching release
scripts/install-menubar-app.sh --build    # build from macos/QuernMenuBar instead
```

Both install to `~/Applications/Quern.app` and quit a running copy first, then
launch it. The app starts the server itself, so that one command is the whole
setup — from there you can drive Quern from the menu bar and leave the CLI
alone.

One prerequisite, and the script checks it for you: `quern setup` must have run
at least once, because that is what writes `~/.local/bin/quern`. The app drives
the daemon through that wrapper. A GUI app does not inherit your shell's PATH,
so a `quern` that works in your terminal is not enough — it looks for that
exact file. If it is missing the script says so and tells you what to run.

Take the default unless you are working on the app itself. A local build is
unsigned, so its code-signing identity changes every time you rebuild, macOS
treats each build as a different app, and launch-at-login and any permissions
you grant will not persist. The released build is signed and notarized, so
they do.

Either way the app only reads `~/.quern/*.json`, so it reports on whichever
server is running regardless of which install started it, and it will not start
a second one on top of a server that is already up.

It is installed to `~/Applications/Quern.app`, so Spotlight and Launchpad both
find it. If you quit it and want it back:

```sh
open ~/Applications/Quern.app
```

`quern setup` also starts it, and will quit a running copy first so an update
actually takes effect.

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

**Settings.** Three things you would otherwise reach for the CLI to change.

*Network capture* carries "Install the capture certificate automatically". Off
by default: capturing HTTPS needs each device to trust Quern's certificate
authority, and installing one is a bigger commitment than turning capture on,
so Quern asks the first time. Turn this on to answer once instead. The same
setting is `quern set-auto-install-cert`, and it is shown here so a standing
policy is visible and reversible rather than buried in a config file.

*Updates* carries the channel picker, stable or beta, the same setting as
`quern set-channel`.

There are also two launch toggles, and a read-only view of the server's
address, version and uptime, plus the full UDID of the active device — which
the menu deliberately leaves out to keep itself narrow.

*Launch at login* starts the menu bar app when you log in. *Start the server
when Quern launches* is on by default: opening the Quern app and being told the
server is stopped, with a button to press, is a step that did not need to
exist. If the server is already running the app leaves it alone.

Those two together mean the daemon runs from login onward, which is why the
second one can be turned off. Starting the server opens its HTTP listener and
watches for crash reports. It does not begin capturing device or system logs,
and it does not touch the proxy or install any certificate — those stay behind
their own prompts.

Quitting the menu bar app does not stop the server. Hold ⌥ over Quit when you
want both.

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
