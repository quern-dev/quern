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

import os
import secrets
import stat
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path("/tmp/quern")
DIR = ROOT / "screenshots"

#: A screenshot larger than this is not one quern saved.
MAX_SERVED = 50 * 1024 * 1024

#: Paths remembered; the oldest is forgotten past this.
MAX_RECORDED = 2000

_recorded: OrderedDict[str, tuple[str, str]] = OrderedDict()


def private_dir(path: Path, root: Path | None = None) -> Path:
    """`path`, made if missing, and every directory from `root` (ROOT) down
    to it a real directory this user owns, kept 0700. /tmp is shared: a
    /tmp/quern another user made first would be theirs to swap a screenshot
    for a symlink to the API key, which the route would then serve (review).
    OSError when it is not ours."""
    root = ROOT if root is None else root
    chain = [root, *[root / Path(*path.relative_to(root).parts[:i + 1])
                     for i in range(len(path.relative_to(root).parts))]]
    for d in chain:
        try:
            os.mkdir(d, 0o700)
        except FileExistsError:
            pass
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise PermissionError(f"{d} is not a directory this user owns: not using it")
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.chmod(d, 0o700)
    return path


def read(path: str) -> bytes:
    """The screenshot at `path`, opened without following a symlink, and only
    a regular file this user owns. OSError otherwise."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > MAX_SERVED:
            raise PermissionError(f"{path} is not a screenshot quern saved")
        with os.fdopen(fd, "rb", closefd=False) as f:
            return f.read(MAX_SERVED)
    finally:
        os.close(fd)


def record(path: str, *, as_owner: bool = False) -> None:
    """Remember `path` as the current caller's, principal and session -- or
    the owner's alone, for a screenshot of a device the caller did not act on."""
    from server import visibility

    caller = visibility.current_caller()
    _recorded[path] = ("owner", "") if as_owner else (caller.principal, caller.session)
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
        private_dir(DIR)
        ts = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
        # The random part: two captures in a second with one label wrote one
        # file, and one caller's path showed another's screen.
        path = DIR / f"{ts}_{label}_{secrets.token_hex(3)}.png"
        image_bytes, _ = await controller.screenshot(udid=udid, scale=0.5)
        # Never through a symlink someone left at the name.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(image_bytes)
    except Exception:  # noqa: BLE001 -- best effort, as the callers always were
        return None
    record(str(path))
    return str(path)
