# Quern Probe (Android)

A deterministic, offline Kotlin fixture for exercising Quern's Android tooling.
The counterpart to `tools/probe-app/` on iOS, and combined by design: UI
surfaces, logging and diagnostics live in one artifact rather than three apps.

## Why

Quern's test suite mocks every subprocess, correctly — `adb` does not belong in
a unit test. The consequence is that nothing in CI has ever run a Quern tool
against a real Android device, and that is where #78 was hiding: `am start`
exits 0 when it cannot resolve an intent, so `open_url` reported success for a
URL nothing could open.

Verifying against Chrome or Settings is not a substitute. Their layouts shift
between versions, they need the network, and you cannot add a surface to them.

## Build and install

```sh
./build.sh                          # build only
./build.sh --install                # build + install on the attached device
./build.sh --install emulator-5554  # build + install on a specific one
```

`build.sh` finds a JDK itself (Android Studio ships one that is not on `PATH`)
and falls back to `~/.gradle` when `GRADLE_USER_HOME` points somewhere
unmounted, which fails with an unhelpful `.lck` `FileNotFoundException`.

Package: `com.quern.probe`.

## Tabs

| Tab | Exercises |
|---|---|
| Text | typing fidelity per input type (`field_default`, `field_url`, `field_email`, `field_password`); `text_event_log` echoes the last change the app was actually told about |
| Controls | `control_switch`, `control_checkbox`, `control_slider` (mirrored into `control_slider_value`, since SeekBar exposes its value inconsistently across API levels), alert dismissal |
| Scroll | `scroll_list`, 200 rows labelled `row_0` … `row_199`; `row_199` is only reachable by actually scrolling |
| Logs | every logging path — `Log.v/d/i/w/e`, `println`, `System.err`, a logged stack trace — idle until started |
| Links | deep link landing surface; `link_count` and `link_last_uri` |
| Web | a `WebView` with fixed local content and named DOM ids, for webview-automation work that currently depends on a third-party app |
| Diag | crash (main and background thread) and ANR triggers |

The tab bar is itself a fixture for tab-selection probing.

## Deep links, and what the Links tab is for

The manifest registers two filters deliberately:

- `quernprobe://open/...` — a custom scheme, always resolves. The control case.
- `https://probe.quern.dev/...` — **without** `autoVerify`, which is the state a
  debug build is normally in: no `assetlinks.json` entry for the signing key, so
  Android will not treat it as a verified App Link and a package-less VIEW
  intent falls through to a browser. Passing the package explicitly is what
  drives it into the app.

`link_count` exists because `open_url` cannot currently be trusted to say
whether a link landed (#78). The app is the only honest witness. This is the
habit the deep link guide recommends generally: assert on the destination, not
on the tool's status.

`MainActivity` is `singleTop` so a VIEW intent resumes the running instance
through `onNewIntent` instead of creating a second one. Without it, every deep
link resets the app to the first tab — which is realistic for some apps, but
makes the fixture measure activity lifecycle rather than link delivery.

## Logging is idle by default

Emitters start only when `log_start` is tapped. A fixture that logs
continuously pollutes the log stream during every other test in the same app,
and log volume is itself under test. Making emission a mode the test controls
puts that switch on the right side of the boundary.

## Self-test

```sh
python3 selftest.py [--serial emulator-5554]
```

Drives the app through Quern's REST API — identifiers only, no coordinates, so
a regression in the tools fails here rather than in someone's session.

One check is inverted on purpose:

```
PASS  KNOWN BUG #78: unresolvable URL still reports ok
```

It asserts the *current, wrong* behaviour. When #78 is fixed this check will
fail, which is the signal to invert it rather than delete it.

## Emulator

```sh
./start-emulator.sh [Pixel_7]
```

The default graphics path (`gpu auto`, which selects Vulkan/lavapipe) has hung
twice on this hardware, both times with the same signature: repeated VkInstance
create/destroy, then

```text
ERROR | detected a hanging thread 'QEMU2 main loop'. No response for 15017 ms
```

and the process dies, taking the run with it. Software rendering has not
reproduced it in any run. That is a good trade here — the probe draws almost
nothing, so the GPU is not what makes a run slow.

These are launch flags rather than edits to the AVD's `config.ini`, so the same
AVD stays fast for interactive use.

Two related things, both worth knowing because they cost an hour between them:

* **A crashed emulator leaves a consent dialog queued.** The next launch prints
  `Showing crashdialog to get consent` and never boots — it is waiting on a
  window nobody is looking at. `-no-metrics` avoids it. To recover, delete the crash
  database — the directory is named after the current user, so find it rather
  than copying a path: `rm -f "${TMPDIR:-/tmp}"/android-$(id -un)/emu-crash-*.db`,
  or `find /tmp -name 'emu-crash-*.db' 2>/dev/null` if that misses.
* **AVD configs store absolute paths.** After this machine's home directory moved
  from `/Users/jham` to `/Volumes/Home/jham`, ten files under `~/.android` still
  pointed at the old location — including `skin.path`, which produced
  `unknown skin name 'pixel_7'` even though the skin was installed. The `.ini`
  files survived only because the emulator falls back to `path.rel`. If an
  emulator misbehaves after a home move, grep `~/.android` for the old path
  before believing anything else.

## Timing

The self-test polls with `wait_for()` rather than sleeping a fixed interval.
The WebView checks originally used `time.sleep(4)`, which passed on a warm
emulator and failed on a cold one under software rendering — the DOM simply was
not up yet, and the test reported it as missing. A fixed sleep encodes one
machine's speed; polling states the intent.
