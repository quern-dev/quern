#!/usr/bin/env bash
# Build Quern.app from the Swift sources with swiftc (no Xcode/SwiftPM needed).
#
# Mirrors the compile-on-demand pattern used for tools/ios-preview.swift, but
# assembles a full .app bundle suitable for signing + notarization.
#
# Usage:
#   ./build.sh [OUTPUT_DIR]
#
# Environment:
#   VERSION    Version string stamped into Info.plist. Defaults to the version
#              parsed from the repo's pyproject.toml (single source of truth).
#   UNIVERSAL  If "1" (default), builds a universal arm64+x86_64 binary via lipo.
#              Set to "0" for a faster native-arch-only dev build.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
OUT_DIR="${1:-$SCRIPT_DIR/build}"
APP="$OUT_DIR/Quern.app"
UNIVERSAL="${UNIVERSAL:-1}"

# --- Resolve version (arg/env wins, else parse pyproject.toml) ---------------
if [[ -z "${VERSION:-}" ]]; then
  VERSION="$(grep -m1 '^version' "$REPO_ROOT/pyproject.toml" | cut -d'"' -f2 || true)"
fi
VERSION="${VERSION:-0.0.0}"

SWIFTC="$(command -v swiftc || true)"
if [[ -z "$SWIFTC" ]]; then
  echo "error: swiftc not found. Install Xcode Command Line Tools (xcode-select --install)." >&2
  exit 1
fi

echo "Building Quern.app v$VERSION (universal=$UNIVERSAL) → $APP"

rm -rf "$APP"
MACOS_DIR="$APP/Contents/MacOS"
RES_DIR="$APP/Contents/Resources"
mkdir -p "$MACOS_DIR" "$RES_DIR"

SOURCES=("$SCRIPT_DIR"/Sources/*.swift)
BIN="$MACOS_DIR/QuernMenuBar"
DEPLOY_TARGET="13.0"

compile_arch() {
  local arch="$1" out="$2"
  "$SWIFTC" -O -whole-module-optimization \
    -target "${arch}-apple-macos${DEPLOY_TARGET}" \
    -o "$out" "${SOURCES[@]}"
}

if [[ "$UNIVERSAL" == "1" ]]; then
  TMP="$(mktemp -d)"
  trap 'rm -rf "$TMP"' EXIT
  compile_arch "arm64" "$TMP/quern-arm64"
  compile_arch "x86_64" "$TMP/quern-x86_64"
  lipo -create -output "$BIN" "$TMP/quern-arm64" "$TMP/quern-x86_64"
else
  NATIVE="$(uname -m)"
  [[ "$NATIVE" == "arm64" ]] || NATIVE="x86_64"
  compile_arch "$NATIVE" "$BIN"
fi
chmod +x "$BIN"

# --- Info.plist (stamp version) ----------------------------------------------
sed "s/__VERSION__/$VERSION/g" "$SCRIPT_DIR/Info.plist.in" > "$APP/Contents/Info.plist"

# --- Resources ---------------------------------------------------------------
# App + status-bar icons (optional; the app falls back to an SF Symbol).
if [[ -f "$SCRIPT_DIR/Assets/AppIcon.png" ]]; then
  cp "$SCRIPT_DIR/Assets/AppIcon.png" "$RES_DIR/AppIcon.png"
fi
if [[ -f "$SCRIPT_DIR/Assets/StatusIcon.png" ]]; then
  cp "$SCRIPT_DIR/Assets/StatusIcon.png" "$RES_DIR/StatusIcon.png"
fi

echo "Built $APP"
