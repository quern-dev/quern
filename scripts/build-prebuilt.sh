#!/bin/bash
# Build, sign and notarize the screen-mirror app and quern-media that a release
# ships pre-built (#662, #663), so a Mac without the developer tools still gets
# USB iPhone mirroring, simulator preview and recorded video.
#
#   scripts/build-prebuilt.sh <out-dir> [version]
#
# <version>, bare (0.27.0-beta.1), is what the screen mirror's About panel says;
# the release cut builds this before its version bump. Default: the sources'.
#
# Leaves in <out-dir>:
#   Quern Preview.app      universal, macOS 13+, stamped with its source digest,
#                          signed (hardened runtime + camera), notarized, stapled
#   quern-media            universal, macOS 14+, signed the same way, notarized
#   quern-media.sha256     its stamp: the source digest, then the signed binary's
#
# Each is stamped from this checkout's sources by quern's own code, so a Mac
# whose sources match installs it instead of compiling (preview.shipped_bundle,
# media_engine.shipped_binary). Called by `release-menubar.sh --app-only`;
# --publish puts <out-dir> into the tarball as prebuilt/.
#
# Environment:
#   DEVELOPER_ID_APP   "Developer ID Application: Name (TEAMID)"
#   NOTARY_PROFILE     notarytool keychain profile
#   PYTHON             an interpreter with quern's dependencies
#                      (default: <repo>/.venv/bin/python)
set -euo pipefail

OUT="${1:?usage: build-prebuilt.sh <out-dir> [version]}"
VERSION="${2:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PY="${PYTHON:-$REPO_ROOT/.venv/bin/python}"
ENTITLEMENTS="$SCRIPT_DIR/prebuilt.entitlements"
: "${DEVELOPER_ID_APP:?set DEVELOPER_ID_APP to your Developer ID Application identity}"
: "${NOTARY_PROFILE:?set NOTARY_PROFILE to your notarytool keychain profile}"
# shellcheck source=lib/notarize.sh
source "$SCRIPT_DIR/lib/notarize.sh"

# The screen mirror's deployment target, set on purpose: built locally it
# inherits the host's (measured: minos 26.0 on a macOS 27 host), and too new a
# value is "You can't use this version of the application with this version
# of macOS". 14.0, not the menu bar's 13.0: it discovers iPhones with
# AVCaptureDevice's `.external` device type, macOS 14 and later. quern-media's
# is its Package.swift's, also macOS 14.
PREVIEW_TARGET="14.0"

quern_py() { ( cd "$REPO_ROOT" && "$PY" -c "$1" ); }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
rm -rf "$OUT/Quern Preview.app" "$OUT/quern-media" "$OUT/quern-media.sha256"

echo "==> Compiling ios-preview (universal, macOS $PREVIEW_TARGET)"
SOURCES=()
while IFS= read -r line; do SOURCES+=("$line"); done < <(quern_py '
from server.device.media import preview
for p in preview._all_sources():
    print(p)')
FLAGS=()
while IFS= read -r line; do FLAGS+=("$line"); done < <(quern_py '
from server.device.media import preview
print("\n".join(preview._COMPILE_FLAGS))')
for arch in arm64 x86_64; do
  xcrun swiftc -O -target "${arch}-apple-macos${PREVIEW_TARGET}" \
    -o "$WORK/ios-preview-$arch" "${SOURCES[@]}" "${FLAGS[@]}"
done
lipo -create -output "$WORK/ios-preview" "$WORK/ios-preview-arm64" "$WORK/ios-preview-x86_64"
quern_py "
from pathlib import Path
from server.device.media import preview
preview.stage_release_bundle(Path('''$OUT'''), Path('''$WORK/ios-preview'''),
                             '''$VERSION''' or None)"
APP="$OUT/Quern Preview.app"

echo "==> Building quern-media (universal)"
MEDIA_PKG="$REPO_ROOT/macos/QuernMedia"
xcrun swift build --package-path "$MEDIA_PKG" --scratch-path "$WORK/media" \
  -c release --product quern-media --arch arm64 --arch x86_64
BIN_DIR="$(xcrun swift build --package-path "$MEDIA_PKG" --scratch-path "$WORK/media" \
  -c release --arch arm64 --arch x86_64 --show-bin-path)"
cp "$BIN_DIR/quern-media" "$OUT/quern-media"
MEDIA="$OUT/quern-media"

# Both carry the camera entitlement, which #662 expected the hardened runtime
# to require. Measured on macOS 27 with a USB iPhone 11, it does not for an
# iPhone's screen: opened as an app, the signed screen mirror asked for camera
# access in its own name (Quern Preview's usage description) and streamed once
# allowed, and a copy re-signed without the entitlement streamed too.
# quern-media, a command-line tool the server starts, is not asked at all:
# whatever launched the chain is (Terminal, or Quern.app), and from a shell it
# streamed signed or not. The entitlement stays as the documented way to ask.
# What the shipped copy does buy is one stable signature: the camera grant is
# keyed to it, so it survives updates that leave the preview alone.
echo "==> Signing (Developer ID, hardened runtime, camera)"
codesign --force --options runtime --timestamp --entitlements "$ENTITLEMENTS" \
  --sign "$DEVELOPER_ID_APP" "$APP"
codesign --force --options runtime --timestamp --entitlements "$ENTITLEMENTS" \
  --identifier dev.quern.media --sign "$DEVELOPER_ID_APP" "$MEDIA"
for target in "$APP" "$MEDIA"; do
  codesign --verify --strict --verbose=2 "$target"
  # A gate: the entitlement is what this build says it signs with.
  if ! codesign -d --entitlements - --xml "$target" 2>/dev/null \
      | grep -q "com.apple.security.device.camera"; then
    echo "error: ${target##*/} was signed without the camera entitlement" >&2
    exit 1
  fi
done

echo "==> Notarizing"
ZIP="$WORK/prebuilt.zip"
mkdir -p "$WORK/zip"
ditto "$APP" "$WORK/zip/Quern Preview.app"
cp "$MEDIA" "$WORK/zip/quern-media"
ditto -c -k "$WORK/zip" "$ZIP"
notarize_wait "$ZIP"
# A bare binary cannot carry a stapled ticket; Gatekeeper finds it online.
xcrun stapler staple "$APP"
spctl -a -vvv "$APP"

# After signing: the stamp names the signed binary's own hash.
quern_py "
from pathlib import Path
from server.device.media import media_engine as m
Path('''$OUT/quern-media.sha256''').write_text(
    m.stamp_text(m.expected_digest(), m.binary_digest(Path('''$MEDIA'''))))"

for target in "$APP/Contents/MacOS/ios-preview" "$MEDIA"; do
  echo "  ${target##*/}: $(lipo -archs "$target"), minos $(vtool -show-build "$target" 2>/dev/null | awk '/minos/{print $2; exit}')"
done
echo "==> Pre-built copies in $OUT"
