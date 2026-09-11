#!/usr/bin/env bash
# Compile and run the menu-bar app's tests.
#
# Plain swiftc, the same as build.sh. Not SwiftPM: it wants Sources/<Target>/,
# which means splitting these sources into a library and an executable, and
# build.sh and release-menubar.sh both depend on the current layout. Touching
# the two scripts that produce a signed artifact is a poor trade for XCTest's
# test discovery.
#
# Sources/main.swift is excluded and Tests/main.swift takes its place: top-level
# code is only allowed in a file called main.swift, and a binary gets one.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${1:-}"
if [[ -z "$DEST" ]]; then
  DEST="$(mktemp -d)"
  trap 'rm -rf "$DEST"' EXIT
fi
OUT="$DEST/quern-menubar-tests"

command -v swiftc >/dev/null || {
  echo "error: swiftc not found. Install Xcode Command Line Tools:" >&2
  echo "       xcode-select --install" >&2
  exit 1
}

SOURCES=()
for f in "$SCRIPT_DIR"/Sources/*.swift; do
  [[ "$(basename "$f")" == "main.swift" ]] && continue
  SOURCES+=("$f")
done
TESTS=("$SCRIPT_DIR"/Tests/*.swift)

# The same deployment target build.sh uses. Not cosmetic: several SwiftUI
# modifiers here are deprecated after macOS 13, so compiling against the host's
# default SDK version turns -warnings-as-errors into a wall of failures about
# code the shipped build compiles cleanly. Host arch only -- this runs the
# binary, so a universal one would buy nothing.
ARCH="$(uname -m)"
mkdir -p "$(dirname "$OUT")"
# Gated the way build.sh gates it, and for its reason: a warning mid-edit
# should not block an iteration. CI sets STRICT=1.
STRICT="${STRICT:-0}"
STRICT_FLAGS=()
[[ "$STRICT" == "1" ]] && STRICT_FLAGS+=(-warnings-as-errors)

swiftc "${STRICT_FLAGS[@]+"${STRICT_FLAGS[@]}"}" \
  -target "${ARCH}-apple-macos13.0" \
  -o "$OUT" "${SOURCES[@]}" "${TESTS[@]}"
"$OUT"
