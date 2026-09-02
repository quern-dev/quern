#!/bin/bash
# Build QuernProbe.app for the iOS simulator — no Xcode project required.
#
# Usage:
#   ./build.sh                      build only
#   ./build.sh --install [udid]    build and install (default: booted)
set -euo pipefail
cd "$(dirname "$0")"

# --scene builds the scene-lifecycle bundle from the same sources. An app has a
# scene manifest or it does not, and that choice decides which callbacks iOS
# calls, so the two lifecycles cannot both be exercised by one running binary.
# Two Info.plists over one source tree keeps the view controllers and the
# self-test shared.
SCENE=""
ARGS=()
for a in "$@"; do
  if [ "$a" = "--scene" ]; then SCENE=1; else ARGS+=("$a"); fi
done
set -- "${ARGS[@]:-}"

SDK=$(xcrun --sdk iphonesimulator --show-sdk-path)
ARCH=$(uname -m)
TARGET="${ARCH}-apple-ios16.0-simulator"

if [ -n "$SCENE" ]; then
  NAME="QuernProbeScene"; PLIST="Info-Scene.plist"; BUNDLE="com.quern.probe.scene"
  # A compile-time flag, because implementing configurationForConnecting is
  # itself enough to switch the lifecycle — the manifest is not the only
  # trigger, so the method has to be absent from the app-delegate build.
  FLAGS=(-D SCENE_LIFECYCLE)
else
  NAME="QuernProbe"; PLIST="Info.plist"; BUNDLE="com.quern.probe"
  FLAGS=()
fi
OUT="build/${NAME}.app"

mkdir -p "$OUT"
swiftc -parse-as-library \
       -sdk "$SDK" \
       -target "$TARGET" \
       ${FLAGS[@]+"${FLAGS[@]}"} \
       Sources/*.swift \
       -o "$OUT/$NAME"
cp "$PLIST" "$OUT/Info.plist"
echo "Built $OUT ($TARGET)"

if [ "${1:-}" = "--install" ]; then
  UDID="${2:-booted}"
  xcrun simctl install "$UDID" "$OUT"
  echo "Installed $BUNDLE on $UDID"
fi
