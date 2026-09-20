#!/bin/bash
# Build QuernProbe.app — no Xcode project required.
#
# Usage:
#   ./build.sh                            simulator build
#   ./build.sh --install [udid]           build and install (default: booted)
#   ./build.sh --scene                    the scene-lifecycle bundle
#   ./build.sh --device <device-udid>     build for a real iPhone, signed
#   ./build.sh --device <udid> --install  and install it over USB or the network
#
# The device build needs a development provisioning profile that lists the
# device; find-profile.py locates one and says what to do when there is none.
# It is the same sources and the same Info.plist as the simulator build, so a
# test written against the fixture means the same thing on both.
set -euo pipefail
cd "$(dirname "$0")"

# --scene builds the scene-lifecycle bundle from the same sources. An app has a
# scene manifest or it does not, and that choice decides which callbacks iOS
# calls, so the two lifecycles cannot both be exercised by one running binary.
# Two Info.plists over one source tree keeps the view controllers and the
# self-test shared.
SCENE=""
DEVICE_UDID=""
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --scene) SCENE=1 ;;
    --device)
      shift
      DEVICE_UDID="${1:-}"
      if [ -z "$DEVICE_UDID" ]; then
        echo "--device needs a device udid (xcrun devicectl list devices)" >&2
        exit 2
      fi
      ;;
    *) ARGS+=("$1") ;;
  esac
  shift
done
set -- "${ARGS[@]:-}"

ARCH=$(uname -m)
if [ -n "$DEVICE_UDID" ]; then
  SDK=$(xcrun --sdk iphoneos --show-sdk-path)
  # No `-simulator` suffix: that suffix is the whole difference between a
  # binary the simulator loads and one the phone does, and it fails at install
  # rather than at build.
  TARGET="arm64-apple-ios16.0"
else
  SDK=$(xcrun --sdk iphonesimulator --show-sdk-path)
  TARGET="${ARCH}-apple-ios16.0-simulator"
fi

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

# Device bundles are kept apart from simulator ones. They share a name and
# cannot share a directory: installing a simulator bundle on a phone fails with
# a signature error rather than an architecture one, which sends you looking in
# the wrong place.
if [ -n "$DEVICE_UDID" ]; then
  OUT="build/device/${NAME}.app"
else
  OUT="build/${NAME}.app"
fi

rm -rf "$OUT"
mkdir -p "$OUT"
swiftc -parse-as-library \
       -sdk "$SDK" \
       -target "$TARGET" \
       ${FLAGS[@]+"${FLAGS[@]}"} \
       Sources/*.swift \
       -o "$OUT/$NAME"
cp "$PLIST" "$OUT/Info.plist"

if [ -n "$DEVICE_UDID" ]; then
  # iOS refuses a bundle with no MinimumOSVersion, and reports it as a generic
  # install failure. The simulator does not care, which is why the shared
  # Info.plist does not carry it -- it is a property of this build, not of the
  # fixture.
  /usr/libexec/PlistBuddy -c "Add :MinimumOSVersion string 16.0" "$OUT/Info.plist" >/dev/null 2>&1 \
    || /usr/libexec/PlistBuddy -c "Set :MinimumOSVersion 16.0" "$OUT/Info.plist"
  /usr/libexec/PlistBuddy -c "Add :CFBundleSupportedPlatforms array" "$OUT/Info.plist" >/dev/null 2>&1 || true
  /usr/libexec/PlistBuddy -c "Add :CFBundleSupportedPlatforms:0 string iPhoneOS" "$OUT/Info.plist" >/dev/null 2>&1 || true

  PROFILE_INFO=$(./find-profile.py "$DEVICE_UDID")
  PROFILE=$(echo "$PROFILE_INFO" | sed -n 1p)
  TEAM=$(echo "$PROFILE_INFO" | sed -n 2p)
  IDENTITY=$(echo "$PROFILE_INFO" | sed -n 3p)

  cp "$PROFILE" "$OUT/embedded.mobileprovision"

  # Entitlements are not copied from the profile wholesale: a wildcard profile
  # grants `TEAM.*`, and an app signed with that literal identifier gets a
  # keychain and container shared with every other app signed the same way.
  # Naming the bundle id keeps this fixture's state its own.
  ENTITLEMENTS=$(mktemp -t quernprobe-entitlements).plist
  cat > "$ENTITLEMENTS" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>application-identifier</key>
    <string>${TEAM}.${BUNDLE}</string>
    <key>com.apple.developer.team-identifier</key>
    <string>${TEAM}</string>
    <key>get-task-allow</key>
    <true/>
</dict>
</plist>
PLIST

  codesign --force --sign "$IDENTITY" \
           --entitlements "$ENTITLEMENTS" \
           --generate-entitlement-der \
           --timestamp=none \
           "$OUT" >/dev/null
  rm -f "$ENTITLEMENTS"
  echo "Built $OUT ($TARGET, signed by $IDENTITY)"
else
  echo "Built $OUT ($TARGET)"
fi

if [ "${1:-}" = "--install" ]; then
  if [ -n "$DEVICE_UDID" ]; then
    xcrun devicectl device install app --device "$DEVICE_UDID" "$OUT" >/dev/null
    echo "Installed $BUNDLE on $DEVICE_UDID"
  else
    UDID="${2:-booted}"
    xcrun simctl install "$UDID" "$OUT"
    echo "Installed $BUNDLE on $UDID"
  fi
fi
