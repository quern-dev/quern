# hinge-control

The in-simulator helper quern uses to fold and unfold an iPhone Duo and read its
hinge (#537). It runs **inside** the simulator, through `xcrun simctl spawn`.
Nothing on the host can set the hinge: `simctl` has no verb for it, `devicectl`
doesn't either, and Device Hub's window is opaque to accessibility.

## Where it comes from

Adapted from [tddworks/baguette](https://github.com/tddworks/baguette)'s
`Injected/HingeControl`, under its Apache-2.0 license (`LICENSE.baguette`). The
upstream commit this copy was last reviewed against is in `hinge-pin.json`.
`scripts/hinge-pin.py --check` lists upstream changes since then, and
`.github/workflows/hinge-pin.yml` keeps a "Hinge helper is behind baguette"
issue open each week while there are any. `scripts/hinge-pin.py --bump` records
a review, including one that found nothing to take.

baguette worked out the mechanism; its `docs/features/hinge/design.md` is the
reference. Device Hub dispatches each pose as a vendor-defined `IOHIDEvent`
(page `0xFF61`, usage `0x5B`) on a virtual HID service, carrying a small keyed
record. The helper registers a service of the same shape with the private
`HID.framework` (`HIDVirtualEventService`) and dispatches the same events. It
reads the hinge through CoreMotion's private `CMAngleManager`.

## What changed from upstream

- **Left out:**
  - the hardware-key service and its `button` verb. On the Xcode 27.1 release,
    Home and typing reach the Duo through quern's normal path.
  - the `frontmost` query (`Frontmost.m`, derived from idb). So nothing from idb
    is included.
- **Moved:** the payload encoder now lives in `Sources/HingePayload.h`, unchanged,
  so `scripts/hinge-selftest.sh` can compile it on the host and check its bytes.
- **Renamed:** the virtual service is named `quern hinge-control`.

`HingeAngle.h` and `HingeAngle.m` are unchanged, with an attribution header added.

## Building

quern compiles it on first use (`server/device/ios/hinge.py`), for the host's
architecture, against the iphonesimulator SDK:

```sh
xcrun clang -arch arm64 -isysroot "$(xcrun --sdk iphonesimulator --show-sdk-path)" \
  -target arm64-apple-ios17.0-simulator -framework Foundation -fobjc-arc \
  -Wl,-adhoc_codesign -o hinge-control Sources/HingeControl.m Sources/HingeAngle.m
```

**Signed at link time and never again.** The iOS 26+ simulator's dyld rejects a
binary re-signed after the build with `codesign --force` (baguette's finding).

## Verbs

| | |
|---|---|
| `read-angle` | prints `angle <degrees>` and exits; reads only |
| `watch-angle` | prints every sample until stopped |
| `[--deadline <unix-seconds>] angle <deg>` | sets the hinge at once |
| `[--deadline <unix-seconds>] sweep <from> <to> <ms>` | moves it at 60 Hz with Device Hub's ease-out |
| `[--deadline <unix-seconds>] orientation <portrait\|pud\|landscape-left\|landscape-right>` | the orientation picker |
| `serve` | the same verbs one per line on stdin, each answered `done <status>` |

Exit status: 0 done, 1 dispatch failed, 2 bad arguments, 3 deadline passed with
nothing done. A helper that acts prints `pid <n>` first.
