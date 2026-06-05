#!/bin/bash
# Build QuernProbe.app for the iOS simulator — no Xcode project required.
#
# Usage:
#   ./build.sh                      build only
#   ./build.sh --install [udid]    build and install (default: booted)
set -euo pipefail
cd "$(dirname "$0")"

SDK=$(xcrun --sdk iphonesimulator --show-sdk-path)
ARCH=$(uname -m)
TARGET="${ARCH}-apple-ios16.0-simulator"
OUT="build/QuernProbe.app"

mkdir -p "$OUT"
swiftc -parse-as-library \
       -sdk "$SDK" \
       -target "$TARGET" \
       Sources/*.swift \
       -o "$OUT/QuernProbe"
cp Info.plist "$OUT/Info.plist"
echo "Built $OUT ($TARGET)"

if [ "${1:-}" = "--install" ]; then
  UDID="${2:-booted}"
  xcrun simctl install "$UDID" "$OUT"
  echo "Installed com.quern.probe on $UDID"
fi
