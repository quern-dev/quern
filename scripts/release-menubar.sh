#!/usr/bin/env bash
# Build, sign, notarize, and publish the Quern menu-bar app as a release asset.
#
# Run on a Mac with the Developer ID Application identity in the keychain.
# Slots into docs/release-channels.md at the `gh release create` step: after
# the GitHub Release for the tag exists, run this to attach a tarball asset
# that bundles a signed+notarized Quern.app alongside the full source tree.
#
# The updater (server/lifecycle/updater.py) prefers this asset over GitHub's
# auto-generated source tarball, so end users get the menu-bar app on update.
#
# Usage:
#   scripts/release-menubar.sh <tag>      # e.g. v0.14.0
#
# Required environment:
#   DEVELOPER_ID_APP   Codesigning identity, e.g.
#                      "Developer ID Application: Your Name (TEAMID)"
#   NOTARY_PROFILE     `xcrun notarytool` keychain profile name created via
#                      `xcrun notarytool store-credentials`.
set -euo pipefail

TAG="${1:-}"
if [[ -z "$TAG" ]]; then
  echo "usage: $0 <tag>   (e.g. v0.14.0)" >&2
  exit 2
fi
: "${DEVELOPER_ID_APP:?set DEVELOPER_ID_APP to your Developer ID Application identity}"
: "${NOTARY_PROFILE:?set NOTARY_PROFILE to your notarytool keychain profile}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VERSION="${TAG#v}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
BUILD_DIR="$WORK/app"
APP="$BUILD_DIR/Quern.app"
PREFIX="quern-$VERSION"
STAGE="$WORK/$PREFIX"
TARBALL="$REPO_ROOT/dist/$PREFIX.tar.gz"

echo "==> Building Quern.app v$VERSION"
VERSION="$VERSION" UNIVERSAL=1 "$REPO_ROOT/macos/QuernMenuBar/build.sh" "$BUILD_DIR"

echo "==> Signing (Developer ID, hardened runtime)"
codesign --force --deep --options runtime --timestamp \
  --sign "$DEVELOPER_ID_APP" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

echo "==> Notarizing"
ZIP="$WORK/Quern.zip"
ditto -c -k --keepParent "$APP" "$ZIP"
xcrun notarytool submit "$ZIP" --keychain-profile "$NOTARY_PROFILE" --wait
xcrun stapler staple "$APP"
spctl -a -vvv "$APP" || echo "warning: spctl assessment reported an issue — review above"

echo "==> Assembling release tarball: $TARBALL"
# Source tree at the tag (respects .gitignore/.gitattributes), then drop the
# signed app in at the top level. Single $PREFIX/ dir so the updater's
# extracted[0] detection (updater.py) finds one directory.
mkdir -p "$STAGE" "$(dirname "$TARBALL")"
git -C "$REPO_ROOT" archive --format=tar "$TAG" | tar -x -C "$STAGE"
cp -R "$APP" "$STAGE/Quern.app"
tar -czf "$TARBALL" -C "$WORK" "$PREFIX"

echo "==> Uploading asset to release $TAG"
gh release upload "$TAG" "$TARBALL" --clobber

echo "Done: $TARBALL uploaded to $TAG"
