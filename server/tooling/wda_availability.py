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
    from server.device.ios._xcode import xcode_available
    from server.device.ios.wda import _find_xctestrun, _xcode_build_id
    from server.models import WdaNotSetUpError

    reason: str | None = None
    try:
        # xcode_available first, which never shows the install dialog; it is
        # true for the Command Line Tools too, and only a full Xcode has an
        # `xcodebuild` that answers -version.
        if not xcode_available() or await _xcode_build_id() is None:
            reason = NO_XCODE
        else:
            try:
                _find_xctestrun()
            except WdaNotSetUpError:
                reason = NOT_BUILT
    except Exception:  # noqa: BLE001 -- could not tell is not unavailable
        reason = None
    return reason


def known_reason() -> str | None:
    """The last answer `wda_unavailable_reason` gave, however old, for the
    routing that asks synchronously; None before the first check. The server
    checks at startup, and every WDA start checks again."""
    return _cached[1] if _cached is not None else None


def forget() -> None:
    """Drop the cached answer: after setup_wda builds WDA, say."""
    global _cached
    _cached = None
