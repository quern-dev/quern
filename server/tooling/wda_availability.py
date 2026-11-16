"""Whether WebDriverAgent can run on a physical iPhone from this Mac (#664).

WDA is built with Xcode and launched with `xcodebuild test-without-building`,
so with no Xcode there is no WDA to start, built or not, and every place that
would fall back to WDA says why it cannot, rather than starting a build that
cannot run. Phones on iOS 27 and later are driven without WDA, but not without
Xcode: `devicectl`, which launches and stops their apps and matches them to
their tunnels, comes with it too (measured on a Mac with only the Command Line
Tools: "devicectl is installed by Xcode").
"""

from __future__ import annotations

import asyncio
import os
import time

NO_XCODE = ("Xcode is not installed. WebDriverAgent, which iPhones before iOS 27 "
            "need, is built and run with it, and devicectl, which every iPhone needs, "
            "comes with it")
NOT_BUILT = "WebDriverAgent has not been built on this Mac (the setup_wda tool builds it)"

#: How long an answer stands: Xcode can be installed while quern runs, and
#: `xcodebuild -version` costs a process.
_TTL_S = 60.0
_cached: tuple[float, str | None] | None = None


async def wda_unavailable_reason() -> str | None:
    """Why WDA cannot run on a phone from this Mac, or None when it can,
    cached for a minute (`_check`)."""
    global _cached
    now = time.monotonic()
    if _cached is not None and now - _cached[0] < _TTL_S:
        return _cached[1]
    reason = await _check()
    _cached = (now, reason)
    return reason


async def _check() -> str | None:
    """Why WDA cannot run on a phone from this Mac, or None when it can.

    None also when it cannot be told: a check that could not run is no
    reason to refuse.
    """
    from server.device.ios.wda import _find_xctestrun
    from server.models import WdaNotSetUpError

    reason: str | None = None
    try:
        if not await asyncio.to_thread(_full_xcode):
            reason = NO_XCODE
        else:
            try:
                _find_xctestrun()
            except WdaNotSetUpError:
                reason = NOT_BUILT
    except Exception:  # noqa: BLE001 -- could not tell is not unavailable
        reason = None
    return reason


_refreshing: asyncio.Task | None = None


def known_reason() -> str | None:
    """The last answer `wda_unavailable_reason` gave, for the routing that
    asks synchronously; None before the first check. The server checks at
    startup. An answer past its minute is returned as it stands and asked
    again in the background, so Xcode installed or WDA built while the server
    runs is followed from a later read (CodeRabbit on #707)."""
    global _refreshing
    if _cached is None:
        return None
    if time.monotonic() - _cached[0] >= _TTL_S and (_refreshing is None or _refreshing.done()):
        try:
            _refreshing = asyncio.get_running_loop().create_task(wda_unavailable_reason())
        except RuntimeError:  # no loop: a sync caller outside the server
            pass
    return _cached[1]


def _full_xcode() -> bool:
    """Whether the developer directory in effect holds an `xcodebuild`.

    `DEVELOPER_DIR` first, as xcrun reads it, then `xcode-select -p`, which
    never shows the install dialog. The Command Line Tools answer it too, with
    a directory that has no `xcodebuild`. Read afresh each time, unlike
    `xcode_available`'s cached answer, so an Xcode installed while the server
    runs is seen; and a file test, not running `xcodebuild -version`, so a
    probe that fails is not taken for Xcode missing (CodeRabbit on #707).
    """
    import subprocess

    developer_dir = os.environ.get("DEVELOPER_DIR", "")
    if not developer_dir:
        result = subprocess.run(["xcode-select", "-p"], capture_output=True, text=True,
                                timeout=5)
        if result.returncode != 0:
            return False
        developer_dir = result.stdout.strip()
    return bool(developer_dir) and os.access(
        os.path.join(developer_dir, "usr", "bin", "xcodebuild"), os.X_OK)


def forget() -> None:
    """Mark the cached answer stale, after setup_wda has run.

    Stale, not dropped: the next asker checks afresh, and the routing that
    asks synchronously keeps the old answer until that check replaces it.
    Dropped, a setup that failed read as WDA being there, and a crash latched
    in that window sent the next read to it (CodeRabbit on #707)."""
    global _cached
    if _cached is not None:
        _cached = (float("-inf"), _cached[1])
