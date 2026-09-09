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
import json
import logging
import subprocess
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from server.config import CONFIG_DIR, read_user_config

logger = logging.getLogger("quern-debug-server.update-check")

LAST_CHECK_FILE = CONFIG_DIR / "last-update-check"
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


def check_for_updates() -> str | None:
    """Return a message if updates are available, None otherwise.

    Rate-limited to once per CHECK_INTERVAL seconds. Never blocks server
    startup — returns None on any error. Respects "update_check": false
    in ~/.quern/config.json.
    """
    try:
        # Respect opt-out
        config = read_user_config()
        if config.get("update_check") is False:
            return None

        # Check rate limit
        if LAST_CHECK_FILE.exists():
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
        # Persist structured result for the system API + MCP. Older
        # deployments of quern.dev returned only update_available, so
        # latest_version may still be absent.
        _write_update_info({
            "checked_at": datetime.now(UTC).isoformat(),
            "current_version": version,
            "latest_version": latest_version,
            "update_available": update_available,
            "message": message,
            # The answer above is only meaningful for the channel it was asked
            # about -- quern.dev compares against that channel's pointer branch
            # -- so record which one, and let readers detect a result cached
            # before a channel switch instead of trusting it for 24 hours.
            "channel": channel,
        })
        return message

    except Exception:
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


def invalidate_update_check() -> None:
    """Discard the cached update check so the next one runs immediately.

    Called when the update channel changes. The cached answer was computed
    against the old channel's pointer branch, and the rate-limit stamp would
    otherwise suppress a fresh check for up to 24 hours -- so a user who
    switched to beta could keep being told they were up to date against
    stable, or be offered a "newer" version they had just switched away from.

    Best-effort: this only makes a check happen sooner, so a failed unlink is
    not worth propagating.
    """
    for path in (LAST_CHECK_FILE, UPDATE_INFO_FILE):
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.debug("Failed to clear %s: %s", path.name, e)


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
