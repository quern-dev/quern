"""Apps sent to the server to install, when the caller's disk is not the
server's (#254, phase 3b).

`install_app` names a path, and the server reads it. From another machine
there is no such path -- and for a client key on this one, a path is the
server reading whatever on the Mac it is told to, which a client key may not
do. So a client sends the app instead: an .ipa or .apk as it is, a .app
bundle as a gzipped tar (a bundle is a directory, and its frameworks hold
symlinks a zip would lose). It is installed by the id this answers.

Kept per principal -- one key never sees, installs or deletes another's --
under a cap on what a principal holds at once, and deleted once installed,
after an hour, or when the key that sent it is revoked.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import shutil
import tarfile
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

#: What an upload's name may end with, and so what it is.
KINDS = {".app.tar.gz": "app", ".ipa": "ipa", ".apk": "apk"}

#: Seconds an upload is kept when nothing installs it.
TTL_SECONDS = 3600

#: Members a .app's archive may hold: a bundle has thousands, not millions.
MAX_MEMBERS = 200_000


class UploadError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Upload:
    id: str
    principal: str
    name: str
    kind: str
    size: int
    created: float
    #: What to install: the .ipa or .apk, or the unpacked .app directory.
    path: Path = field(repr=False)

    def summary(self, now: float) -> dict:
        return {"upload_id": self.id, "name": self.name, "kind": self.kind,
                "bytes": self.size,
                "expires_in": max(0, int(self.created + TTL_SECONDS - now))}


def kind_of(name: str) -> str:
    """The kind an upload's name says it is; UploadError for anything else, or
    a name that is not a plain file name."""
    if (not name or "\0" in name or len(name.encode()) > 200 or name != Path(name).name
            or name.startswith(".")):
        raise UploadError(f"{name!r} is not a file name: send the app's own name, "
                          "like MyApp.ipa or MyApp.app.tar.gz")
    for suffix, kind in KINDS.items():
        if name.endswith(suffix) and len(name) > len(suffix):
            return kind
    raise UploadError(f"{name!r} is not an app: send an .ipa, an .apk, or a .app "
                      "bundle as .app.tar.gz")


class UploadStore:
    def __init__(self, root: Path, max_bytes: int,
                 clock: Callable[[], float] = time.time) -> None:
        self.root = root
        self.max_bytes = max_bytes
        self._clock = clock
        self._uploads: dict[str, Upload] = {}
        #: Bytes each principal has on the way in: counted against the cap as
        #: they arrive, or uploads side by side each saw the whole room free
        #: and together held several times the cap (review).
        self._inflight: dict[str, int] = {}
        #: Uploads being installed: not swept or deleted from under it.
        self._busy: set[str] = set()

    def reset(self) -> None:
        """Start empty: what a previous run received has no ids any more."""
        shutil.rmtree(self.root, ignore_errors=True)
        self._uploads.clear()

    def held(self, principal: str) -> int:
        """What `principal` has on disk here, and on the way."""
        return (sum(u.size for u in self._uploads.values() if u.principal == principal)
                + self._inflight.get(principal, 0))

    def list(self, principal: str) -> list[Upload]:
        return [u for u in self._uploads.values() if u.principal == principal]

    async def receive(self, principal: str, name: str, chunks: AsyncIterator[bytes],
                      declared: int | None = None) -> Upload:
        """Write what `chunks` carries, under the cap, and keep it for
        `principal`. A .app's archive is unpacked here, so a bad one is
        refused now, not at install."""
        kind = kind_of(name)
        room = self.max_bytes - self.held(principal)
        if declared is not None and declared > room:
            raise UploadError(self._too_big(declared, room), 413)
        upload_id = "up_" + secrets.token_hex(8)
        directory = self.root / upload_id
        directory.mkdir(parents=True, mode=0o700)
        target = directory / name
        size = 0
        try:
            with open(target, "wb") as out:
                async for chunk in chunks:
                    # Checked against everything held now, in-flight bytes
                    # included -- each chunk between awaits, so two uploads
                    # cannot both take the last of the room.
                    if self.held(principal) + len(chunk) > self.max_bytes:
                        raise UploadError(self._too_big(size + len(chunk),
                                                        self.max_bytes - self.held(principal)
                                                        + size), 413)
                    size += len(chunk)
                    self._inflight[principal] = self._inflight.get(principal, 0) + len(chunk)
                    out.write(chunk)
            if size == 0:
                raise UploadError("the upload was empty")
            path, held = target, size
            if kind == "app":
                # Held is what it unpacks to: a small archive of zeros
                # unpacked to many times the cap while counting as small (review).
                path, held = await asyncio.to_thread(
                    _unpack_app, target, directory / "app",
                    self.max_bytes - self.held(principal))
                target.unlink()
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        finally:
            left = self._inflight.get(principal, 0) - size
            if left > 0:
                self._inflight[principal] = left
            else:
                self._inflight.pop(principal, None)
        upload = Upload(upload_id, principal, name, kind, held, self._clock(), path)
        self._uploads[upload_id] = upload
        return upload

    def get(self, upload_id: str, principal: str) -> Upload:
        """The caller's own upload. Another principal's reads as missing."""
        upload = self._uploads.get(upload_id)
        if upload is None or upload.principal != principal:
            raise UploadError(f"no upload {upload_id!r}: it was installed, expired after "
                              "an hour, or was never yours -- send it again", 404)
        return upload

    @contextlib.contextmanager
    def installing(self, upload_id: str):
        """While installing: neither the sweep nor a DELETE removes it."""
        self._busy.add(upload_id)
        try:
            yield
        finally:
            self._busy.discard(upload_id)

    def busy(self, upload_id: str) -> bool:
        return upload_id in self._busy

    def discard(self, upload_id: str) -> None:
        upload = self._uploads.pop(upload_id, None)
        if upload is not None:
            shutil.rmtree(self.root / upload.id, ignore_errors=True)

    def sweep(self, still_valid: Callable[[str], bool]) -> list[str]:
        """Delete the expired, and those of principals no longer valid (a
        revoked key's). The ids deleted."""
        now = self._clock()
        gone = [u.id for u in self._uploads.values()
                if u.id not in self._busy
                and (now - u.created >= TTL_SECONDS or not still_valid(u.principal))]
        for upload_id in gone:
            self.discard(upload_id)
        return gone

    def _too_big(self, size: int, room: int) -> str:
        cap = self.max_bytes // (1024 * 1024)
        return (f"the upload is over the cap: {size // (1024 * 1024)} MB with "
                f"{max(room, 0) // (1024 * 1024)} MB left of this key's {cap} MB "
                '(config.json\'s {"uploads": {"max_mb": ...}}; installed or deleted '
                "uploads free their space)")


def unsafe_member(members: list[tarfile.TarInfo]) -> str | None:
    """Why these members could write outside the directory they unpack into,
    or None. Checked by name alone, before tarfile is asked: its `data`
    filter had escapes through symlinks, fixed only in 3.11.13, 3.12.11 and
    3.13.4 (CVE-2025-4517 and others), and this runs on 3.11 up (review)."""
    import posixpath

    links: set[str] = set()
    seen: set[str] = set()
    for m in members:
        name = posixpath.normpath(m.name)
        parts = name.split("/")
        if m.name.startswith("/") or ".." in parts or name in ("", "."):
            return f"{m.name!r} is not a name inside the bundle"
        if name in seen:
            return f"{m.name!r} appears twice"
        seen.add(name)
        if m.islnk():
            return f"{m.name!r} is a hard link"
        if not (m.isfile() or m.isdir() or m.issym()):
            return f"{m.name!r} is not a file, directory or symlink"
        # Written through a link -- a member under a name that is a link.
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in links:
                return f"{m.name!r} is inside a symlink"
        if m.issym():
            target = m.linkname
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(name), target))
            if target.startswith("/") or resolved == ".." or resolved.startswith("../"):
                return f"{m.name!r} links outside the bundle ({target!r})"
            links.add(name)
    return None


def _unpack_app(archive: Path, dest: Path, room: int) -> tuple[Path, int]:
    """The .app inside `archive`, unpacked under `dest`. tarfile's `data`
    filter refuses absolute names, `..`, links that leave `dest`, and device
    files; the sizes are summed first, so a small archive cannot unpack into
    more than the cap."""
    if not hasattr(tarfile, "data_filter"):
        raise UploadError("this Python cannot unpack an archive safely (it needs 3.11.4 "
                          "or later): send an .ipa instead", 501)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members = []
            total = 0
            for member in tar:
                members.append(member)
                total += max(member.size, 0)
                if len(members) > MAX_MEMBERS or total > room:
                    raise UploadError("the .app archive unpacks to more than the upload "
                                      "cap allows", 413)
            if (why := unsafe_member(members)) is not None:
                raise UploadError(f"the .app archive is refused: {why}")
            tops = {Path(m.name).parts[0] for m in members if Path(m.name).parts}
            if len(tops) != 1 or not next(iter(tops)).endswith(".app"):
                raise UploadError("a .app.tar.gz holds one .app bundle at its top: "
                                  f"found {sorted(tops)[:5] or 'nothing'}")
            dest.mkdir(mode=0o700)
            tar.extractall(dest, members=members, filter="data")
    except (tarfile.TarError, EOFError, OSError) as e:
        raise UploadError(f"the .app archive could not be unpacked: {e}") from None
    app = dest / next(iter(tops))
    if not app.is_dir() or app.is_symlink():
        raise UploadError("the .app in the archive is not a directory")
    return app, total
