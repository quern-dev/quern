#!/usr/bin/env bash
# Compile tools/sim-bridge.swift and run its in-process self-tests -- the
# panel rotations measured on an iPhone Duo (#536) -- without a simulator.
#
# The Python suite cannot do this: tests never run swiftc (tests/conftest.py),
# and sim-bridge is otherwise compiled only at runtime, on first use. So
# nothing would notice a broken rotation until a foldable stopped responding.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT
swiftc -o "$OUT/sim-bridge" "$ROOT/tools/sim-bridge.swift" \
  -framework Foundation -framework IOSurface -framework CoreGraphics -framework ImageIO
# The bridge prints a ready event first; the answer is the line after it.
answer="$(printf '{"cmd":"selftest-panels"}\n' | "$OUT/sim-bridge" 2>/dev/null | grep -v '"event"' | tail -1)"
echo "$answer"
case "$answer" in
  *'"ok":true'*) echo "sim-bridge self-test passed" ;;
  *) echo "sim-bridge self-test FAILED" >&2; exit 1 ;;
esac
