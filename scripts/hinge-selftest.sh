#!/usr/bin/env bash
# Check tools/hinge-control (#537) without a simulator:
#
# 1. The payload encoder still produces the bytes that moved an iPhone Duo on
#    Xcode 27.1 / iOS 27.1 -- the hinge at 130 degrees and the orientation
#    picker at "portrait". Apple changes these private records with Xcode
#    releases, and upstream (tddworks/baguette) may lag, so a changed encoder
#    must be a decision, never an accident. Built for the host from
#    Sources/HingePayload.h, since the helper itself only runs in a simulator.
# 2. The helper still compiles for the simulator, the way quern builds it
#    (server/device/ios/hinge.py): iphonesimulator SDK, signed at link time.
#
# Run it on the newest Xcode before a release (docs/release-channels.md).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HC="$ROOT/tools/hinge-control"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

HINGE_130="d3000000040000810900000870726f76696465720000000028000009636f6d2e6170706c652e5669727475616c697a6174696f6e2e5669727475616c4d616368696e657307000008736f7572636500001400000968696e67652d736c696465722d636f6e74726f6c0500000874797065000000000500000972616e67650000000600000876616c75650000003f0000840000000000406040"
PORTRAIT="d3000000040000810900000870726f76696465720000000028000009636f6d2e6170706c652e5669727475616c697a6174696f6e2e5669727475616c4d616368696e657307000008736f7572636500001a0000096f7269656e746174696f6e2d7069636b65722d636f6e74726f6c000005000008747970650000000004000009656e756d0600000876616c756500000008000089706f727472616974"

clang -fobjc-arc -framework Foundation -o "$OUT/payload" "$HC/selftest/payload.m"
# macOS's bash 3.2 has no mapfile.
"$OUT/payload" > "$OUT/payload.txt"
got_hinge="$(sed -n 1p "$OUT/payload.txt")"
got_portrait="$(sed -n 2p "$OUT/payload.txt")"
fail=0
[[ "$got_hinge" == "$HINGE_130" ]] || { echo "hinge payload changed:"; echo "  got  $got_hinge"; echo "  want $HINGE_130"; fail=1; }
[[ "$got_portrait" == "$PORTRAIT" ]] || { echo "orientation payload changed:"; echo "  got  $got_portrait"; echo "  want $PORTRAIT"; fail=1; }

SDK="$(xcrun --sdk iphonesimulator --show-sdk-path)"
ARCH="$(uname -m)"
xcrun clang -arch "$ARCH" -isysroot "$SDK" -target "${ARCH}-apple-ios17.0-simulator" \
  -framework Foundation -fobjc-arc -Wall -Werror -Wl,-adhoc_codesign \
  -o "$OUT/hinge-control" "$HC/Sources/HingeControl.m" "$HC/Sources/HingeAngle.m" \
  || { echo "the helper does not build for the simulator"; fail=1; }

if [[ $fail -ne 0 ]]; then echo "hinge self-test FAILED" >&2; exit 1; fi
echo "hinge self-test passed"
