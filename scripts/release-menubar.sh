#!/usr/bin/env bash
# Build, sign, notarize, and publish the Quern menu-bar app as a release asset.
#
# Run on a Mac with the Developer ID Application identity in the keychain.
# Slots into docs/release-channels.md at the `gh release create` step: the
# release asset is a tarball bundling a signed+notarized Quern.app alongside
# the full source tree. The updater (server/lifecycle/updater.py) prefers it
# over GitHub's auto-generated source tarball, so users get the app on update.
#
# Usage:
#   scripts/release-menubar.sh <tag>                  # everything, one shot
#   scripts/release-menubar.sh --app-only <version>   # phase 1: app -> dist/
#   scripts/release-menubar.sh --publish <tag>        # phase 2: archive+upload
#
# The split exists because the two phases have different prerequisites and
# very different failure modes. Building, signing and notarizing need only a
# version string; archiving and uploading need the tag and the GitHub Release
# to exist. Notarization is also the slow step, the one that depends on
# Apple's service being reachable, and the one that would otherwise abort a
# release *after* the tag was cut. Running it first means a bad day at the
# notary service costs you nothing to clean up.
#
#   scripts/release-menubar.sh --app-only 0.15.0-beta.2
#   ... cut the tag, create the GitHub Release ...
#   scripts/release-menubar.sh --publish v0.15.0-beta.2
#
# --publish re-verifies the staged app rather than trusting it: signature,
# stapled ticket, Gatekeeper acceptance, and that its stamped version matches
# the tag. A prebuilt app can come from anywhere, including a previous
# release, and uploading a bundle that disagrees with its tag is the kind of
# mistake nobody catches until a user reports the wrong version.
#
# Required environment (phase 1 / one-shot only):
#   DEVELOPER_ID_APP   Codesigning identity, e.g.
#                      "Developer ID Application: Your Name (TEAMID)"
#   NOTARY_PROFILE     `xcrun notarytool` keychain profile name created via
#                      `xcrun notarytool store-credentials`.
#
# Optional:
#   APP                Path to a prebuilt Quern.app for --publish.
#                      Defaults to dist/Quern.app, where --app-only leaves it.
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
usage:
  release-menubar.sh <tag>                 build, sign, notarize, publish
  release-menubar.sh --app-only <version>  build, sign, notarize -> dist/
  release-menubar.sh --publish <tag>       archive a staged app and upload
USAGE
  exit 2
}

MODE="all"
case "${1:-}" in
  --app-only) MODE="app"; shift ;;
  --publish)  MODE="publish"; shift ;;
  -h|--help)  usage ;;
  "")         usage ;;
  -*)         echo "unknown option: $1" >&2; usage ;;
esac

ARG="${1:-}"
[[ -z "$ARG" ]] && usage

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# --app-only takes a bare version; the other modes take a tag. Accept either
# spelling in both and normalise, so `v0.15.0` and `0.15.0` cannot disagree
# about what got built.
VERSION="${ARG#v}"
TAG="$ARG"
[[ "$TAG" == v* ]] || TAG="v$TAG"

# DEVELOPER_ID_APP is required in every mode. Phase 1 signs with it; phase 2
# checks the staged bundle against it. "Validly signed and notarized" says
# nothing about *whose* signature it carries, and --publish will accept a
# bundle handed to it by path, so without this any Developer ID app would pass.
: "${DEVELOPER_ID_APP:?set DEVELOPER_ID_APP to your Developer ID Application identity}"
if [[ "$MODE" != "publish" ]]; then
  : "${NOTARY_PROFILE:?set NOTARY_PROFILE to your notarytool keychain profile}"
fi

# The team identifier out of "Developer ID Application: Name (TEAMID)".
EXPECTED_TEAM="${DEVELOPER_ID_APP##*(}"
EXPECTED_TEAM="${EXPECTED_TEAM%)}"
if [[ -z "$EXPECTED_TEAM" || "$EXPECTED_TEAM" == "$DEVELOPER_ID_APP" ]]; then
  echo "error: could not read a team id from DEVELOPER_ID_APP" >&2
  echo "       expected the form: Developer ID Application: Name (TEAMID)" >&2
  exit 2
fi

# Capture the caller's APP before the internal one shadows it.
APP_OVERRIDE="${APP:-}"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
BUILD_DIR="$WORK/app"
APP="$BUILD_DIR/Quern.app"
PREFIX="quern-$VERSION"
STAGE="$WORK/$PREFIX"
DIST="$REPO_ROOT/dist"
STAGED_APP="$DIST/Quern.app"
TARBALL="$DIST/$PREFIX.tar.gz"

# Read the version a bundle was actually stamped with, so the two phases can
# be checked against each other instead of assumed to match.
app_version() {
  /usr/libexec/PlistBuddy -c "Print :CFBundleShortVersionString" \
    "$1/Contents/Info.plist" 2>/dev/null || true
}

if [[ "$MODE" != "publish" ]]; then

echo "==> Building Quern.app v$VERSION"
VERSION="$VERSION" UNIVERSAL=1 "$REPO_ROOT/macos/QuernMenuBar/build.sh" "$BUILD_DIR"

echo "==> Signing (Developer ID, hardened runtime)"
codesign --force --deep --options runtime --timestamp \
  --sign "$DEVELOPER_ID_APP" "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

# Submit and poll rather than using `notarytool --wait`. The built-in wait
# long-polls a single HTTPS request that intermittently dies with
# NSURLErrorDomain -1001 ("request timed out") on this machine, which under
# `set -e` aborts the release *after* signing and *before* upload, even though
# the submission succeeded server-side. Polling `notarytool info` instead makes
# a flaky poll a retry rather than a failed release. Same approach, and the
# same reason, as Apps/burrows/scripts/release-macos.sh in the burrows repo.
notarize_wait() {
  local target="$1" submit_out sub_id st i
  submit_out=$(xcrun notarytool submit "$target" \
    --keychain-profile "$NOTARY_PROFILE" 2>&1) || true
  printf '%s\n' "$submit_out" | grep -iE 'id:|error' | head -3
  sub_id=$(printf '%s\n' "$submit_out" | awk '/id:/{print $2; exit}')
  if [[ -z "$sub_id" ]]; then
    echo "error: notary submit failed for ${target##*/}" >&2
    printf '%s\n' "$submit_out" >&2
    return 1
  fi
  echo "  submission id: $sub_id — polling (tolerant of -1001 timeouts)…"
  i=0
  while (( i < 60 )); do
    i=$((i + 1))
    # `|| true` is load-bearing, and the reason this differs from the burrows
    # original it was taken from: that script runs under `set -eu`, where awk
    # succeeding masks a failed `notarytool info`. This one adds `pipefail`, so
    # the pipeline reports the failure, `set -e` acts on it, and the shell
    # exits mid-poll -- defeating the entire point of polling instead of using
    # `--wait`. An empty `st` is already the loop's "retry" case.
    st=$(xcrun notarytool info "$sub_id" \
      --keychain-profile "$NOTARY_PROFILE" 2>/dev/null \
      | awk -F': ' '/status:/{print $2; exit}') || true
    echo "  [$i] status: ${st:-<timeout, retrying>}"
    case "$st" in
      Accepted) return 0 ;;
      Invalid|Rejected)
        echo "error: notarization $st for ${target##*/}" >&2
        xcrun notarytool log "$sub_id" \
          --keychain-profile "$NOTARY_PROFILE" 2>&1 | head -40 >&2
        return 1 ;;
    esac
    sleep 20
  done
  echo "error: notarization timed out (server-side) for ${target##*/}" >&2
  return 1
}

echo "==> Notarizing"
ZIP="$WORK/Quern.zip"
ditto -c -k --keepParent "$APP" "$ZIP"
notarize_wait "$ZIP"
xcrun stapler staple "$APP"
# A gate, not a warning. This runs immediately before the tarball is built
# and uploaded, so a printed warning here published an app that Gatekeeper
# refuses to open -- and the release looked successful.
if ! spctl -a -vvv "$APP"; then
  echo "error: Gatekeeper rejected the stapled app — refusing to publish" >&2
  exit 1
fi

# Stage into dist/ (gitignored) so the app outlives this script's temp dir.
# One-shot mode stages too: if the upload fails, the expensive part is still
# on disk and --publish can finish the job without re-notarizing.
mkdir -p "$DIST"
rm -rf "$STAGED_APP"
ditto "$APP" "$STAGED_APP"
echo "==> Staged: $STAGED_APP (v$(app_version "$STAGED_APP"))"

if [[ "$MODE" == "app" ]]; then
  echo
  echo "Done. Signed, notarized and stapled:"
  echo "  $STAGED_APP"
  echo
  echo "Next: cut the tag and create the GitHub Release, then run"
  echo "  $0 --publish $TAG"
  exit 0
fi

fi  # end phase 1

# ---------------------------------------------------------------------------
# Phase 2 — archive and upload
# ---------------------------------------------------------------------------

if [[ "$MODE" == "publish" ]]; then
  APP="${APP_OVERRIDE:-$STAGED_APP}"
  echo "==> Verifying staged app: $APP"
  [[ -d "$APP" ]] || {
    echo "error: no app at $APP — run '$0 --app-only $VERSION' first," >&2
    echo "       or set APP=/path/to/Quern.app" >&2
    exit 1
  }
  # A staged app can come from anywhere, including an earlier release. Re-check
  # everything phase 1 guaranteed rather than trusting the path.
  #
  # Version first: it is the cheapest check, and staging one version then
  # tagging another is the mistake this two-phase flow actually invites.
  # Reporting it before a signature dump keeps the message findable.
  staged_version="$(app_version "$APP")"
  if [[ "$staged_version" != "$VERSION" ]]; then
    echo "error: staged app is v${staged_version:-<unknown>} but the tag is $TAG" >&2
    echo "       Rebuild with: $0 --app-only $VERSION" >&2
    exit 1
  fi
  codesign --verify --deep --strict --verbose=2 "$APP" || {
    echo "error: staged app is not validly signed" >&2; exit 1; }
  xcrun stapler validate "$APP" || {
    echo "error: staged app has no stapled notarization ticket" >&2; exit 1; }
  spctl -a -vvv "$APP" || {
    echo "error: Gatekeeper rejects the staged app" >&2; exit 1; }
  staged_team=$(codesign -dv "$APP" 2>&1 | awk -F= '/^TeamIdentifier=/{print $2; exit}')
  if [[ "$staged_team" != "$EXPECTED_TEAM" ]]; then
    echo "error: staged app is signed by team ${staged_team:-<none>}, expected $EXPECTED_TEAM" >&2
    echo "       Refusing to publish a bundle this release did not sign." >&2
    exit 1
  fi
  echo "  signed by $staged_team, stapled, Gatekeeper-accepted, v$staged_version"
fi

# The tag has to exist by now: the archive is taken from it, not from HEAD.
if ! git -C "$REPO_ROOT" rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
  echo "error: tag $TAG does not exist — cut it before publishing" >&2
  exit 1
fi

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
