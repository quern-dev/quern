# shellcheck shell=bash
# notarize_wait <file>: submit to Apple's notary service and poll until it
# answers. Sourced by release-menubar.sh and build-prebuilt.sh; needs
# NOTARY_PROFILE set.

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
