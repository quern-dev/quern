#!/usr/bin/env bash
# Install the Quern menu-bar app on a git-clone install.
#
# `quern setup` deliberately skips this for clones -- it assumes a developer
# builds their own -- so a clone never gets the app and nothing says why. This
# is that missing step.
#
# Two modes, because they trade off differently:
#
#   --release   (default) Fetch the signed, notarized app from the GitHub
#               release matching this checkout's version. Launch-at-login and
#               macOS permissions work, because the code-signing identity is
#               stable. It does not track your working tree.
#
#   --build     Build from macos/QuernMenuBar. Always matches your checkout,
#               and needs Xcode Command Line Tools. The build is unsigned, so
#               its identity changes on every rebuild and macOS treats each
#               one as a different app -- launch-at-login and permissions will
#               not stick. Right for working on the app itself.
#
# Either way it installs to ~/Applications/Quern.app, the same place a release
# install uses, and quits a running copy first so the new one actually starts.
set -euo pipefail

MODE="release"
case "${1:-}" in
  --build)   MODE="build" ;;
  --release) MODE="release" ;;
  "")        ;;
  -h|--help)
    sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'
    exit 0 ;;
  *)
    echo "unknown option: $1  (try --help)" >&2
    exit 2 ;;
esac

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "error: the menu-bar app is macOS only" >&2
  exit 1
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APPS_DIR="$HOME/Applications"
DEST="$APPS_DIR/Quern.app"
TEAM_ID="3QUH73KW5Q"

VERSION="$(grep -m1 '^version' "$REPO_ROOT/pyproject.toml" | cut -d'"' -f2)"
[[ -n "$VERSION" ]] || { echo "error: could not read the version from pyproject.toml" >&2; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [[ "$MODE" == "build" ]]; then
  echo "==> Building Quern.app v$VERSION from source"
  command -v swiftc >/dev/null || {
    echo "error: swiftc not found. Install Xcode Command Line Tools:" >&2
    echo "       xcode-select --install" >&2
    exit 1
  }
  UNIVERSAL=0 "$REPO_ROOT/macos/QuernMenuBar/build.sh" "$WORK/app" >/dev/null
  SRC="$WORK/app/Quern.app"
  echo "    Unsigned dev build. Launch-at-login and permissions will not persist"
  echo "    across rebuilds -- the signing identity changes each time."
else
  TAG="v$VERSION"
  ASSET="quern-$VERSION.tar.gz"
  URL="https://github.com/quern-dev/quern/releases/download/$TAG/$ASSET"
  echo "==> Fetching the signed app from $TAG"
  curl -fsSL --max-time 180 -o "$WORK/$ASSET" "$URL" || {
    echo "error: could not download $ASSET" >&2
    echo "       Is $TAG released with an asset? Check:" >&2
    echo "       https://github.com/quern-dev/quern/releases/tag/$TAG" >&2
    echo "       Or build from source instead: $0 --build" >&2
    exit 1
  }
  # macOS tar, not Python's tarfile or bsdtar defaults: the archive carries
  # AppleDouble metadata that must be applied as extended attributes rather
  # than written as literal files, or the code signature seal breaks.
  /usr/bin/tar -xzf "$WORK/$ASSET" -C "$WORK" "quern-$VERSION/Quern.app"
  SRC="$WORK/quern-$VERSION/Quern.app"

  echo "==> Verifying"
  REQ="anchor apple generic and certificate leaf[subject.OU] = \"$TEAM_ID\""
  codesign --verify --deep --strict -R="$REQ" "$SRC" || {
    echo "error: the downloaded app is not validly signed by $TEAM_ID" >&2
    exit 1
  }
  spctl -a -vvv "$SRC" >/dev/null 2>&1 || {
    echo "error: Gatekeeper rejects the downloaded app" >&2
    exit 1
  }
  STAMPED="$(/usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" \
    "$SRC/Contents/Info.plist" 2>/dev/null || true)"
  [[ "$STAMPED" == "$VERSION" ]] || {
    echo "error: downloaded app is v${STAMPED:-unknown}, expected v$VERSION" >&2
    exit 1
  }
  echo "    signed by $TEAM_ID, notarized, v$STAMPED"
fi

echo "==> Installing to $DEST"
mkdir -p "$APPS_DIR"
# Quit first. `open` activates a running instance rather than starting the new
# binary, so without this the old build keeps running and nothing says so.
osascript -e 'tell application "Quern" to quit' >/dev/null 2>&1 || true
for _ in $(seq 1 20); do
  pgrep -f "Quern.app/Contents/MacOS/QuernMenuBar" >/dev/null || break
  sleep 0.25
done

STAGING="$APPS_DIR/Quern.app.incoming"
rm -rf "$STAGING"
/usr/bin/ditto "$SRC" "$STAGING"
rm -rf "$DEST"
mv "$STAGING" "$DEST"

open "$DEST"
echo
echo "Done. Running from $DEST"
echo "Quit it from its menu bar icon; 'open $DEST' brings it back."
