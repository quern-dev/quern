#!/usr/bin/env bash
# Build (and optionally install) Quern Probe for Android.
#
# Sets JAVA_HOME and GRADLE_USER_HOME itself rather than relying on the shell:
# Android Studio ships a JDK that is not on PATH, and a GRADLE_USER_HOME
# pointing at an unmounted volume fails with a confusing FileNotFoundException
# about a .lck file. Both are respected if already set to something usable.
set -euo pipefail
cd "$(dirname "$0")"

if [ -z "${JAVA_HOME:-}" ] || [ ! -x "${JAVA_HOME}/bin/java" ]; then
    for c in "/Applications/Android Studio.app/Contents/jbr/Contents/Home" \
             "$(/usr/libexec/java_home 2>/dev/null || true)"; do
        [ -n "$c" ] && [ -x "$c/bin/java" ] && { export JAVA_HOME="$c"; break; }
    done
fi
[ -x "${JAVA_HOME:-}/bin/java" ] || { echo "No JDK found. Install one, or set JAVA_HOME."; exit 1; }

if [ ! -d "${GRADLE_USER_HOME:-$HOME/.gradle}" ]; then
    export GRADLE_USER_HOME="$HOME/.gradle"
fi

SDK="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-$HOME/Library/Android/sdk}}"
[ -d "$SDK" ] || { echo "Android SDK not found at $SDK. Set ANDROID_HOME."; exit 1; }
echo "sdk.dir=$SDK" > local.properties

./gradlew :app:assembleDebug -q
APK="app/build/outputs/apk/debug/app-debug.apk"
echo "Built $APK"

if [ "${1:-}" = "--install" ]; then
    SERIAL="${2:-}"
    if [ -n "$SERIAL" ]; then ADB=(adb -s "$SERIAL"); else ADB=(adb); fi
    "${ADB[@]}" install -r "$APK"
    echo "Installed com.quern.probe"
    echo "  ${ADB[*]} shell am start -n com.quern.probe/.MainActivity"
fi
