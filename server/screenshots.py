"""Screenshots the server saves to disk, and who may fetch them (#254, 3b).

Some answers carry a path to a screenshot the server took -- the before and
after of `capture_screenshots`, `screen_context.screenshot` when an element
was not found, the screenshot timeline. On another machine that path names
nothing, so each file is recorded against the session that caused it, and
`GET /api/v1/screenshots?path=` serves it: to that session, or the owner.
Only recorded paths are served -- the route never reads a path it was not
told about -- and the record is bounded and in memory, so a restart, or a
long run, means an old one answers 404.
"""

from __future__ import annotations

import secrets
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path

DIR = Path("/tmp/quern/screenshots")

#: Paths remembered; the oldest is forgotten past this.
MAX_RECORDED = 2000

_recorded: OrderedDict[str, tuple[str, str]] = OrderedDict()


def record(path: str) -> None:
    """Remember `path` as the current caller's, principal and session."""
    from server import visibility

    caller = visibility.current_caller()
    _recorded[path] = (caller.principal, caller.session)
    _recorded.move_to_end(path)
    while len(_recorded) > MAX_RECORDED:
        _recorded.popitem(last=False)


def owner_of(path: str) -> tuple[str, str] | None:
    """(principal, session) that caused `path`, or None if it is not one."""
    return _recorded.get(path)


async def capture(controller, udid: str, label: str) -> str | None:
    """A screenshot of `udid` saved for an answer to point at; its path, or
    None when it could not be taken (best effort: the action it goes with
    has happened either way)."""
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
        # The random part: two captures in a second with one label wrote one
        # file, and one caller's path showed another's screen.
        path = DIR / f"{ts}_{label}_{secrets.token_hex(3)}.png"
        image_bytes, _ = await controller.screenshot(udid=udid, scale=0.5)
        path.write_bytes(image_bytes)
    except Exception:  # noqa: BLE001 -- best effort, as the callers always were
        return None
    record(str(path))
    return str(path)
