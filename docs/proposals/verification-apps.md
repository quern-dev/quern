# Proposal: Fixture apps for feature verification and regression detection

**Status:** proposal · **Raised:** 2026-09-02 · **Prompted by:** #78

## The problem

Quern's test suite mocks every subprocess, deliberately and correctly — `simctl`,
`adb` and `idb` do not belong in unit tests. The consequence is that **nothing in
CI has ever executed a Quern tool against a real device.** 1467 tests prove the
Python is self-consistent. They cannot prove that `am start` behaves the way the
code assumes it does.

It does not. Issue #78: `am start` exits 0 when it cannot resolve an intent, so
`open_url` returns `{"status": "ok"}` for a URL nothing on the device can open —
a response byte-for-byte identical to success. That bug has been present since
Android support shipped. No amount of reading found it. Booting an emulator
found it in four minutes.

The same day, a single 135-line guide was found to contain four factual errors.
Two were caught by reading source; **two were only findable by execution** — the
iOS error domain, and #78 itself. That ratio is the argument for this proposal.

## What already exists

Two fixture apps, both iOS, in very different states.

### QuernProbe — `tools/probe-app/` (in-repo, #33)

A deterministic offline UIKit playground. No Xcode project: it compiles with
bare `swiftc` into a hand-assembled bundle, the same compile-on-demand approach
as `tools/sim-bridge.swift`. Four tabs, every interactive element carrying a
stable accessibility identifier:

| Tab | Exercises |
|---|---|
| Text | typing fidelity per keyboard type; a delegate-event echo log |
| Location | `set_location`, simulated movement, an update counter |
| Controls | switch/slider/segment/stepper state, alert and sheet dismissal |
| Scroll | swipe and scroll-to-element against 200 stable rows |

It ships with `selftest.py`, which drives the app **through Quern's REST API**
using identifiers rather than coordinates. That is the right architecture: it is
an end-to-end harness, not a unit test.

**Its limit is coverage.** The self-test makes five assertions — shift fidelity,
keyboard shown, keyboard hidden, switch toggled, `row_0` present. All five
descend from the single HID investigation the app was built for. It is a good
fixture with one bug's worth of regression tests attached.

### LogTester — `~/Dev/Quern ios test app/logtester/` (**not in version control**)

A SwiftUI app that emits every logging path iOS offers, on a 2-second tick:
`print()`, `NSLog()`, `os_log()` at default/info/debug/error, `os_log` with a
custom subsystem and category, and the Swift `Logger` API at four levels. Plus
`NSLog` from `didFinishLaunching`, and a **Force Crash** button for the crash
pipeline. Built for both a simulator and a physical device.

This app is load-bearing and nobody has noticed. `server/sources/device_log.py`
documents its parsing regex against a sample line reading
`2026-02-21 ... LogTester{Foundation}[2915] <NOTICE>: message text` — the same
date as the app's last edit. **The physical-device log parser in production code
is anchored to output from an app that exists only as a loose directory on one
machine.** If that directory is lost, so is the fixture behind the parser, and
the next person to touch that regex has nothing to check it against.

It has no self-test harness of any kind.

## What fixture apps will not catch

Worth stating plainly so this is not oversold. Of the four significant bugs found
this cycle:

| Bug | Would a fixture app catch it? |
|---|---|
| #78 `open_url` silent success on Android | **Yes** — directly |
| Webview DOM invisible to sim-bridge | **Yes** — with a web view fixture |
| #68 sim-bridge unbounded queue | No — needs a load harness |
| #69 response mismatch after timeout | No — concurrency and timing |

Two of four. Fixture apps address the "does this tool actually work against a
real OS" class, not the "what happens under concurrency" class. The latter needs
different tooling and should not be folded into this.

## Proposed work

Ordered by where the evidence says the holes are, not by what is easiest.

1. **Bring LogTester into the repo**, as a Logs tab and a Diagnostics tab inside
   QuernProbe rather than as a second app. It is currently unversioned and
   already underpins a production regex, so getting it into git is the one item
   here that is a risk fix rather than an improvement. Emitters idle until
   started; crash triggers behind an explicit control.

2. **An Android probe app**, built in the combined shape from the start — UI
   surfaces, logging surfaces and diagnostics in one artifact. Every bug in this
   cycle that a fixture would have caught was on Android, and there is no Android
   fixture at all. Same contract as QuernProbe — stable identifiers,
   deterministic, offline — built with Gradle rather than bare `swiftc`.

3. **A deep-link surface on both probes**, including a deliberately unregistered
   scheme. That single case is #78, and it costs almost nothing to add.

4. **A web view tab on both**, so the webview work has a fixture that is not
   Metatext. Currently that investigation depends on a third-party app whose
   behaviour we do not control and cannot change.

5. **Run the self-tests automatically**, on a self-hosted macOS runner. Until
   this happens, every item above only catches regressions when someone
   remembers to look.

## Decisions needed

- **Regression detection or feature demonstration?** These pull in opposite
  directions. Regression detection wants deliberately hostile surfaces — an
  unregistered scheme, a web view, an auth-gated deep link, a list that is empty
  until populated — chosen because they break tools. Feature demonstration wants
  something that looks like a real app. The first finds more bugs; the second is
  better for the docs and the site. **Recommendation: regression detection.** The
  fixtures already lean that way, and demo material can come from Metatext.

- **One app per platform, or one per concern?** **Recommendation: one app per
  platform.** Merge LogTester into QuernProbe as additional tabs, and build the
  Android probe in that combined shape from the start.

  An earlier draft of this proposal argued the opposite — that a logging fixture
  wants to emit continuously while a UI fixture wants to sit still, so the
  concerns should stay separate. That does not survive examination. Continuous
  emission is a *mode*, not an app boundary: a Logs tab whose emitters are idle
  until started gives the same isolation, and gives it under the test's control
  rather than the app's. Wanting known log volume during a log-capture test is an
  argument for a start/stop switch, not for a second app.

  The cost of the split is the real problem. Two apps means two builds, two
  installs, two launches, two bundle IDs and two harnesses — and operational cost
  is precisely why the self-test does not run automatically today. LogTester
  never got a harness at all. One app is one CI job.

  The one genuine complication is the crash button, since `fatalError()`
  terminates the process and takes any in-progress UI test with it. That is
  solved by ordering rather than separation: keep crash triggers behind an
  explicit control, run them in their own phase, and relaunch afterwards. The
  self-test already has to handle app lifecycle.

  UIKit versus SwiftUI is not a barrier — `UIHostingController` hosts the
  existing SwiftUI view as a tab unchanged.

- **Does the Android probe need a physical device?** The emulator covers intent
  resolution and UI automation. It does not cover everything a real handset does.
  Out of scope for a first pass, but worth knowing it is a known gap rather than
  an oversight.
