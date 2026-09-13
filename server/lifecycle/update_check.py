"""Non-blocking update check via quern.dev endpoint.

On daemon start, hits https://quern.dev/api/check-update with the local
version (and HEAD SHA for git installs). Cloudflare analytics count the
requests — no data is stored or logged server-side.

Rate-limited to once per 24 hours. Repeats periodically while the server
is running so long-lived servers still check in. Never blocks or crashes
the server.

Persists the structured result to ``~/.quern/update-info.json`` so MCP
clients and other consumers can surface "update available" without
re-hitting the endpoint. Opt out by setting ``"update_check": false`` in
``~/.quern/config.json``.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import logging
import ssl
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from server.config import CONFIG_DIR, read_user_config

logger = logging.getLogger("quern-debug-server.update-check")

LAST_CHECK_FILE = CONFIG_DIR / "last-update-check"
# Serialises "commit a check result" against "switch channel and invalidate".
# A separate file rather than the cache itself, because invalidation deletes
# the cache and a lock held on a deleted inode guards nothing.
CHANNEL_LOCK_FILE = CONFIG_DIR / "channel.lock"
UPDATE_INFO_FILE = CONFIG_DIR / "update-info.json"
CHECK_INTERVAL = 86400  # 24 hours
ENDPOINT = "https://quern.dev/api/check-update"
TIMEOUT = 5  # seconds


def _find_project_root() -> Path | None:
    """Find the project root by looking for pyproject.toml."""
    path = Path(__file__).resolve().parent
    for _ in range(5):
        if (path / "pyproject.toml").exists():
            return path
        parent = path.parent
        if parent == path:
            break
        path = parent
    return None


def _get_local_version() -> str | None:
    """Read version from pyproject.toml."""
    project_root = _find_project_root()
    if project_root is None:
        return None
    pyproject = project_root / "pyproject.toml"
    if not pyproject.exists():
        return None
    for line in pyproject.read_text().splitlines():
        if line.startswith("version"):
            return line.split('"')[1]
    return None


def _get_head_sha() -> str | None:
    """Get the local HEAD commit SHA (git installs only)."""
    project_root = _find_project_root()
    if project_root is None or not (project_root / ".git").exists():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None



def _is_ahead_of(latest_sha: str | None) -> bool:
    """Whether the local HEAD already contains ``latest_sha``.

    Answers the question quern.dev structurally cannot: it holds branch refs
    with no commit graph, so `clientSha !== latestSha` is the best it can do and
    "ahead" is indistinguishable from "behind" (#123).

    Deliberately does **not** fetch. An earlier version did, and it was wrong
    twice over: a failed fetch left a stale ref, `rev-list` then reported zero
    commits behind, and a genuinely outdated install was told nothing — the exact
    inversion of this function's contract. It also added up to 25s of blocking
    git to `check_for_updates()`, which `server/main.py` calls synchronously on
    the foreground startup path.

    Using the sha the endpoint just asserted avoids both. If the object is
    present locally, ancestry is decidable offline in milliseconds. If it is not
    present — a shallow clone, a genuinely newer upstream commit, a tarball
    install — the answer is unknowable here, and False means "believe the
    endpoint".

    Returns False on anything unexpected, so this can only ever suppress a claim
    it positively disproves. It can never invent an update or hide a real one.
    """
    if not latest_sha:
        return False
    project_root = _find_project_root()
    if project_root is None or not (project_root / ".git").exists():
        return False
    try:
        # Confirm we actually have the object before asking about ancestry:
        # `merge-base` on an unknown sha errors, and we must not read that as
        # a negative answer to a question we never got to ask.
        have = subprocess.run(
            ["git", "cat-file", "-e", f"{latest_sha}^{{commit}}"],
            cwd=str(project_root), capture_output=True, timeout=10,
        )
        if have.returncode != 0:
            return False
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", latest_sha, "HEAD"],
            cwd=str(project_root), capture_output=True, timeout=10,
        )
        # 0 = latest_sha is an ancestor of HEAD, i.e. we already contain it.
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


#: What the reader is told, and what the log keeps.
#:
#: Two audiences, so two levels. On screen there are three outcomes, the same
#: three every other updater has: an update is available, you are up to date,
#: or the check could not be made. The third gets one short reason and one
#: instruction, and that is all -- eight different wordings for "the network
#: did not work" is a taxonomy of our exception handling, not information.
#:
#: The full exception text still goes to ~/.quern/server.log on every failure,
#: because when the short answer is not enough the raw error is the only thing
#: that is, and it costs a line to keep.
#:
#: Three remedies, because there are three things a person can actually do:
#: fix their connection, wait for someone else to fix theirs, or report it.
_CHECK_CONNECTION = "Check your internet connection, then try again."
_WAIT = "Nothing to fix here — the update service is having trouble. Try again later."
_REPORT = "Try again. If it keeps happening, run `quern capture-env` and open an issue."


class CheckFailure(NamedTuple):
    summary: str
    """One short line naming the kind of failure. Shown."""

    remedy: str
    """What to do about it. Shown."""

    detail: str
    """The raw error, in the words of the thing that failed. Logged, not shown."""

    def lines(self) -> list[str]:
        """The surfaced form: a reason and an instruction, never the raw error."""
        return [f"Could not check for updates: {self.summary}", self.remedy]


def describe_failure(exc: BaseException) -> CheckFailure:
    """Sort an exception into one of three things the reader can do."""
    endpoint = ENDPOINT.split("//", 1)[-1].split("/", 1)[0]
    detail = f"{type(exc).__name__}: {exc}"
    # urllib wraps the real cause in a URLError whose str() is
    # "<urlopen error ...>" -- the wrapper's own name, with the type of the
    # thing that actually failed nowhere in it. The log is the only place the
    # exception survives now, so it keeps the cause.
    reason = getattr(exc, "reason", None)
    if reason is not None and not isinstance(reason, str):
        detail += f" (cause: {type(reason).__name__}: {reason})"

    # Ordered most-specific first. HTTPError is a URLError is an OSError, so
    # the reverse order would swallow the two specific cases whole.
    if isinstance(exc, urllib.error.HTTPError):
        return CheckFailure(f"{endpoint} answered {exc.code}.", _WAIT, detail)

    # Certificate verification specifically, not the whole ssl.SSLError family.
    # A handshake reset is a network failure and wants the network remedy; only
    # a rejected certificate means something is reading the traffic.
    #
    # quern used to be the usual cause of this, because configuring the system
    # proxy made it intercept its own update check. It no longer can -- see
    # ALWAYS_BYPASS in server/proxy/addon.py -- so reaching here now means
    # something else on the network is doing it.
    cert_error = exc if isinstance(exc, ssl.SSLCertVerificationError) else None
    if cert_error is None and isinstance(
        getattr(exc, "reason", None), ssl.SSLCertVerificationError
    ):
        cert_error = exc.reason  # type: ignore[attr-defined]
    if cert_error is not None:
        return CheckFailure(
            "something on this network is intercepting HTTPS.",
            _REPORT,
            f"{type(cert_error).__name__}: {cert_error}",
        )

    # Every network failure, in one bucket, keyed on the OS error family rather
    # than on a list of the ones we have seen. urlopen wraps connection-phase
    # errors in URLError, but a connection dropped while reading the response
    # arrives raw from http.client -- and both mean the same thing to a reader.
    if isinstance(exc, OSError):
        return CheckFailure(f"could not reach {endpoint}.", _CHECK_CONNECTION, detail)

    if isinstance(exc, ValueError):
        # json.JSONDecodeError is a ValueError.
        return CheckFailure(f"{endpoint} sent something unreadable.", _WAIT, detail)

    return CheckFailure("the check did not complete.", _REPORT, detail)


def check_for_updates(
    force: bool = False,
    on_error: Callable[[CheckFailure], None] | None = None,
) -> str | None:
    """Return a message if updates are available, None otherwise.

    Rate-limited to once per CHECK_INTERVAL seconds. Never blocks server
    startup — returns None on any error. Respects "update_check": false
    in ~/.quern/config.json.

    `force` means a person asked for this, right now, and it skips both gates.

    The rate limit exists so a running server does not hit the network every
    few minutes. It is the wrong answer for someone who has just asked: without
    a way past it, a release landing this afternoon would not be offered until
    tomorrow, and the menu bar -- which only knows what the cache last recorded
    -- had no way to ask.

    The opt-out is skipped for the same reason, though it took a second look to
    see it. "update_check": false turns off the *automatic* check, which is the
    only kind that happens without anyone asking; it is the checkbox every
    other updater has, and every one of them leaves Check Now working. Refusing
    an explicit request on the strength of it answers a question the setting
    was never asked. If the motivation for turning it off was to stop quern
    talking to the network unattended, that still holds -- clicking Check for
    Updates is not unattended, and is consent for the call it makes.
    """
    try:
        # Respect the opt-out -- but only for the automatic check, which is the
        # only one it governs. See the docstring.
        if not force and read_user_config().get("update_check") is False:
            return None

        # Check rate limit
        if not force and LAST_CHECK_FILE.exists():
            last_check = LAST_CHECK_FILE.stat().st_mtime
            if time.time() - last_check < CHECK_INTERVAL:
                return None

        # Touch before checking (so failures don't retry rapidly)
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        LAST_CHECK_FILE.touch()

        version = _get_local_version()
        head_sha = _get_head_sha()

        if not version and not head_sha:
            return None

        # Build query params — send version, channel, and optionally SHA.
        # The channel matters: quern.dev compares the SHA against that
        # channel's pointer branch (release/stable or release/beta). Omitting
        # it makes the endpoint assume stable, which reports a spurious update
        # to anyone on beta.
        from server.config import get_update_channel

        channel = get_update_channel()
        params = []
        if head_sha:
            params.append(f"sha={head_sha}")
        if version:
            params.append(f"version={version}")
        params.append(f"channel={channel}")

        # Hit the endpoint
        url = f"{ENDPOINT}?{'&'.join(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": "quern-update-check/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode())

        update_available = bool(data.get("update_available"))
        latest_version = data.get("latest_version")

        # quern.dev answers the sha question with string equality
        # (`clientSha !== latestSha`), because a Cloudflare Worker holding only
        # branch refs has no commit graph and cannot tell "ahead" from "behind".
        # A git install working on main is ahead of its channel pointer, so it
        # was told an update was available to an ancestor of its own HEAD (#123).
        #
        # `_update_via_git` never believed this -- it counts `HEAD..origin/<ref>`
        # and declines when that is zero -- so the update itself was always
        # refused and the defect was confined to the notification. This makes the
        # notification consult the same reality the action already does, rather
        # than adding a second mechanism.
        if update_available and head_sha and _is_ahead_of(data.get("latest_sha")):
            update_available = False
            latest_version = None
        if not update_available:
            message = None
        elif latest_version:
            message = (
                f'Update available (v{latest_version}) \u2014 run "quern update" '
                f"to install it"
            )
        else:
            message = 'Update available \u2014 run "quern update" to get the latest version'
        # The network call above can block for up to TIMEOUT seconds, which is
        # long enough for the user to switch channels underneath it. That
        # switch deletes both cache files precisely because this answer no
        # longer applies -- so writing it now would undo the invalidation and
        # leave read_update_info() reporting the old channel's verdict as
        # current. Re-read and discard rather than resurrect.
        #
        # This narrows the window to the gap between the comparison and the
        # write, instead of the whole request. An interprocess lock would close
        # it completely, but the update check is a best-effort background hint
        # whose worst case is a stale notification until the next run, and the
        # switch already cleared the rate-limit stamp so the next run is
        # immediate.
        # Held across the comparison *and* the write. Checking without the
        # lock only narrowed the window; a switch landing between the two
        # still recreated the invalidated result.
        with _channel_lock():
            if get_update_channel() != channel:
                return None

            # Persist structured result for the system API + MCP. Older
            # deployments of quern.dev returned only update_available, so
            # latest_version may still be absent.
            _write_update_info({
                "checked_at": datetime.now(UTC).isoformat(),
                "current_version": version,
                "latest_version": latest_version,
                "update_available": update_available,
                "message": message,
                # The answer above is only meaningful for the channel it was
                # asked about -- quern.dev compares against that channel's
                # pointer branch -- so record which one, and let readers detect
                # a result cached before a channel switch instead of trusting
                # it for 24 hours.
                "channel": channel,
            })
        return message

    except Exception as exc:
        failure = describe_failure(exc)
        # The raw error goes to the log every time, including the background
        # check nobody asked for. It is one line, and it is the only place the
        # exception survives now that the screen shows a summary -- so "show me
        # what actually happened" has an answer.
        logger.warning("Update check failed: %s", failure.detail)
        # Quiet on screen by default: this runs on every server start, where a
        # failed update check is not worth startup noise. `on_error` is how a
        # caller who *asked* gets told -- see `_cmd_check_updates`.
        if on_error is not None:
            try:
                on_error(failure)
            except Exception:
                # The docstring promises this never raises into a server start
                # path. A caller's reporting bug must not become quern's.
                logger.exception("Update check error handler raised")
        return None


def _write_update_info(info: dict) -> None:
    """Persist the latest check result to ``~/.quern/update-info.json``.

    Best-effort: a failed write must not break the periodic check.
    """
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        UPDATE_INFO_FILE.write_text(json.dumps(info, indent=2))
    except OSError as e:
        logger.debug("Failed to persist update info: %s", e)


@contextmanager
def _channel_lock():
    """Hold the channel lock, or proceed without it if it cannot be taken.

    Best-effort by design. This coordinates a background hint; failing to
    create a lock file must not stop a user from switching channels.
    """
    fd = None
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        fd = CHANNEL_LOCK_FILE.open("a+")
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError as e:
        logger.debug("Channel lock unavailable, proceeding without it: %s", e)
        if fd is not None:
            fd.close()
            fd = None
    try:
        yield
    finally:
        if fd is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
            fd.close()


def switch_channel(channel: str) -> None:
    """Persist the channel and discard the cached check as one operation.

    The two halves must not be separable. A check reads the channel, spends up
    to TIMEOUT seconds in the network call, then commits its result; if a
    switch lands between that commit's channel comparison and its write, the
    old channel's verdict is recreated after the invalidation meant to remove
    it. Sharing a lock with the commit makes the interleaving impossible: the
    switch either completes first, so the check sees the new channel and
    discards, or lands second and deletes what the check just wrote.

    Raises ValueError for an unknown channel, before anything is invalidated.
    """
    from server.config import set_update_channel

    with _channel_lock():
        set_update_channel(channel)
        _invalidate_locked()


def _invalidate_locked() -> None:
    """Delete the cache files. Caller holds the channel lock."""
    for path in (LAST_CHECK_FILE, UPDATE_INFO_FILE):
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.debug("Failed to clear %s: %s", path.name, e)


def invalidate_update_check() -> None:
    """Discard the cached update check so the next one runs immediately.

    Called when the update channel changes. The cached answer was computed
    against the old channel's pointer branch, and the rate-limit stamp would
    otherwise suppress a fresh check for up to 24 hours -- so a user who
    switched to beta could keep being told they were up to date against
    stable, or be offered a "newer" version they had just switched away from.

    Best-effort: this only makes a check happen sooner, so a failed unlink is
    not worth propagating.

    Prefer switch_channel() when the reason is a channel change, so the write
    and the invalidation cannot be separated by a concurrent check.
    """
    with _channel_lock():
        _invalidate_locked()


def read_update_info() -> dict | None:
    """Return the most recent persisted update check, or None if never run.

    Consumed by the system API so MCP clients can surface "update
    available" inline in tool responses without re-hitting quern.dev.
    """
    if not UPDATE_INFO_FILE.exists():
        return None
    try:
        return json.loads(UPDATE_INFO_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.debug("Failed to read update info: %s", e)
        return None


async def periodic_update_check() -> None:
    """Run check_for_updates() every CHECK_INTERVAL seconds.

    Designed to be launched as an asyncio task in the server lifespan.
    The file-based rate limit in check_for_updates() is the real gate —
    this loop just ensures it gets called regularly for long-lived servers.
    """
    while True:
        await asyncio.sleep(CHECK_INTERVAL)
        try:
            msg = await asyncio.to_thread(check_for_updates)
            if msg:
                logger.info(msg)
        except Exception:
            pass
