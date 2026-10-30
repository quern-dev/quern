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
KINDS = {".app.tar.gz": "app", ".ipa": "ipa", ".apk": "apk",
         # A project's `.quern/knowledge`, loaded with load_landmarks (#560).
         ".knowledge.tar.gz": "knowledge"}

#: Seconds an upload is kept when nothing installs it.
TTL_SECONDS = 3600

#: Bytes received between checks of the volume's free space.
DISK_CHECK_BYTES = 16 * 1024 * 1024

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
                      "bundle as .app.tar.gz (or a knowledge base as .knowledge.tar.gz)")


class UploadStore:
    def __init__(self, root: Path, max_bytes: int,
                 clock: Callable[[], float] = time.time, *, min_free_bytes: int = 0,
                 disk_free: Callable[[], int] | None = None,
                 knowledge_max_bytes: int = 5 * 1024 * 1024,
                 knowledge_max_files: int = 2000) -> None:
        #: What one knowledge-base upload may unpack to (#560).
        self.knowledge_max_bytes = knowledge_max_bytes
        self.knowledge_max_files = knowledge_max_files
        #: Free space the uploads' volume keeps, whatever the keys hold: the
        #: cap is per key, and the owner decides how many keys there are
        #: (#481). 0 for no floor.
        self.min_free_bytes = min_free_bytes
        self._disk_free = disk_free or self._free_on_volume
        #: Bytes promised to the disk but not on it yet -- the rest of a
        #: declared upload, a bundle about to unpack -- across every key, so
        #: uploads side by side cannot each find the same room above the
        #: floor (review).
        self._disk_pending = 0
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
        #: Swept but not yet gone from disk: still counted against their key,
        #: and tried again by the next sweep if deleting failed (CodeRabbit).
        self._deleting: dict[str, Upload] = {}

    def reset(self) -> None:
        """Start empty: what a previous run received has no ids any more."""
        shutil.rmtree(self.root, ignore_errors=True)
        self._uploads.clear()

    def _free_on_volume(self) -> int:
        existing = self.root
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        return shutil.disk_usage(existing).free

    def _check_disk(self, more: int = 0) -> None:
        """Refuse when `more` bytes would leave the volume under its floor."""
        if not self.min_free_bytes:
            return
        try:
            free = self._disk_free()
        except OSError as e:
            # Not known is not room: refused, rather than read as plenty.
            raise UploadError(f"the server could not read its free disk space ({e}); "
                              "try again", 507) from None
        free -= self._disk_pending
        if free - more < self.min_free_bytes:
            mb = 1024 * 1024
            raise UploadError(
                f"the server's disk is too full for this upload: {free // mb} MB free, and "
                f"it keeps {self.min_free_bytes // mb} MB free (config.json's "
                '{"uploads": {"min_free_mb": ...}}) -- try again once space is freed', 507)

    def held(self, principal: str) -> int:
        """What `principal` has on disk here, and on the way."""
        return (sum(u.size for u in self._uploads.values() if u.principal == principal)
                + sum(u.size for u in self._deleting.values() if u.principal == principal)
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
        self._check_disk(declared or 0)
        upload_id = "up_" + secrets.token_hex(8)
        directory = self.root / upload_id
        directory.mkdir(parents=True, mode=0o700)
        target = directory / name
        size = 0
        reserved = 0
        checked_at = 0
        promised = 0
        try:
            promised = declared or 0
            self._disk_pending += promised
            with open(target, "wb") as out:
                async for chunk in chunks:
                    # Checked against everything held now, in-flight bytes
                    # included -- each chunk between awaits, so two uploads
                    # cannot both take the last of the room.
                    if self.held(principal) + len(chunk) > self.max_bytes:
                        raise UploadError(self._too_big(size + len(chunk),
                                                        self.max_bytes - self.held(principal)
                                                        + size), 413)
                    if declared is None:
                        # No length promised: each chunk is checked before it
                        # is written, or an upload ending between periodic
                        # checks finished under the floor (CodeRabbit).
                        self._check_disk(len(chunk))
                    size += len(chunk)
                    self._inflight[principal] = self._inflight.get(principal, 0) + len(chunk)
                    out.write(chunk)
                    # Written now, so no longer only promised.
                    paid = min(promised, len(chunk))
                    promised -= paid
                    self._disk_pending -= paid
                    if size - checked_at >= DISK_CHECK_BYTES:
                        checked_at = size
                        self._check_disk()
            if size == 0:
                raise UploadError("the upload was empty")
            path, held = target, size
            if kind == "knowledge":
                # Small and bounded by its own limits, so unpacked at once:
                # what is held is what it unpacks to (review, as for .app).
                path, held = await _to_thread_to_the_end(
                    _unpack_knowledge, target, directory / "knowledge",
                    min(self.knowledge_max_bytes, self.max_bytes - self.held(principal)),
                    self.knowledge_max_files)
                target.unlink()
            if kind == "app":
                # Held is what it unpacks to: a small archive of zeros
                # unpacked to many times the cap while counting as small
                # (review). Measured first and reserved before unpacking, so
                # bundles unpacking side by side cannot each take the room
                # (CodeRabbit).
                unpacked = await _to_thread_to_the_end(
                    _measure_app, target, self.max_bytes - self.held(principal))
                if self.held(principal) + unpacked > self.max_bytes:
                    raise UploadError(self._too_big(unpacked, self.max_bytes
                                                    - self.held(principal)), 413)
                self._check_disk(unpacked)
                self._inflight[principal] = self._inflight.get(principal, 0) + unpacked
                reserved = unpacked
                self._disk_pending += unpacked
                promised += unpacked
                path, held = await _to_thread_to_the_end(
                    _unpack_app, target, directory / "app", unpacked)
                target.unlink()
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        finally:
            self._disk_pending -= promised
            left = self._inflight.get(principal, 0) - size - reserved
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

    def expire(self, still_valid: Callable[[str], bool]) -> list[Path]:
        """The directories to delete: the expired uploads, those of principals
        no longer valid (a revoked key's), and any a previous sweep failed to
        delete. Chosen on the event loop -- chosen in a worker thread, the
        dict changed under the loop's own reads of it -- and still counted
        against their key until `deleted` says they are gone (CodeRabbit)."""
        now = self._clock()
        gone = [u for u in self._uploads.values()
                if u.id not in self._busy
                and (now - u.created >= TTL_SECONDS or not still_valid(u.principal))]
        for upload in gone:
            del self._uploads[upload.id]
            self._deleting[upload.id] = upload
        return [self.root / upload_id for upload_id in self._deleting]

    def deleted(self, directory: Path) -> bool:
        """After deleting `directory`: True, and its bytes freed, if it is gone."""
        if directory.exists():
            return False
        self._deleting.pop(directory.name, None)
        return True

    def sweep(self, still_valid: Callable[[str], bool]) -> list[str]:
        """`expire`, and the directories deleted here. The ids deleted."""
        gone = []
        for d in self.expire(still_valid):
            shutil.rmtree(d, ignore_errors=True)
            if self.deleted(d):
                gone.append(d.name)
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


async def _to_thread_to_the_end(fn, *args):
    """`fn(*args)` in a worker thread, and, if the caller is cancelled -- the
    client went away -- not returning until the thread has stopped too: the
    caller's cleanup would otherwise delete the directory and free its room
    while the thread still wrote into it (CodeRabbit)."""
    work = asyncio.ensure_future(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        # Shielded each time: a second cancel during this wait would cancel
        # the wrapper and return while the OS thread ran on (CodeRabbit).
        while not work.done():
            with contextlib.suppress(BaseException):
                await asyncio.shield(work)
        raise


def _unpack_knowledge(archive: Path, dest: Path, max_bytes: int,
                      max_files: int) -> tuple[Path, int]:
    """A knowledge base's files, unpacked under `dest`, and their bytes (#560).

    Files and directories only: a knowledge base has no use for a link, and
    one is refused rather than followed. Everything else in it is unpacked and
    left for the loader, which reads what it understands and ignores the rest
    -- so a later addition (screenshots) needs no change here."""
    if not hasattr(tarfile, "data_filter"):
        raise UploadError("this Python cannot unpack an archive safely (it needs 3.11.4 "
                          "or later)", 501)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members, total, files = [], 0, 0
            for member in tar:
                members.append(member)
                if member.issym() or member.islnk():
                    raise UploadError(f"the knowledge base is refused: {member.name!r} is a "
                                      "link; send files")
                if member.isfile():
                    files += 1
                    total += max(member.size, 0)
                if total > max_bytes:
                    raise UploadError(f"the knowledge base unpacks to more than "
                                      f"{max_bytes // (1024 * 1024)} MB "
                                      "(uploads.knowledge_max_mb)", 413)
                if files > max_files:
                    raise UploadError(f"the knowledge base holds more than {max_files} files "
                                      "(uploads.knowledge_max_files)", 413)
            if (why := unsafe_member(members)) is not None:
                raise UploadError(f"the knowledge base is refused: {why}")
            dest.mkdir(mode=0o700)
            tar.extractall(dest, members=members, filter="data")
    except (tarfile.TarError, EOFError, OSError) as e:
        raise UploadError(f"the knowledge base could not be unpacked: {e}") from None
    return dest, total


def _measure_app(archive: Path, room: int) -> int:
    """What the .app in `archive` unpacks to, once checked: see `_unpack_app`."""
    return _open_app(archive, None, room)[1]


def _unpack_app(archive: Path, dest: Path, room: int) -> tuple[Path, int]:
    """The .app inside `archive`, unpacked under `dest`, and its bytes."""
    path, total = _open_app(archive, dest, room)
    assert path is not None
    return path, total


def _open_app(archive: Path, dest: Path | None, room: int) -> tuple[Path | None, int]:
    """Check the .app inside `archive`, and with a `dest` unpack it there:
    (its path, or None unpacked nowhere, and its bytes). tarfile's `data`
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
            if dest is None:
                return None, total
            dest.mkdir(mode=0o700)
            tar.extractall(dest, members=members, filter="data")
    except (tarfile.TarError, EOFError, OSError) as e:
        raise UploadError(f"the .app archive could not be unpacked: {e}") from None
    app = dest / next(iter(tops))
    if not app.is_dir() or app.is_symlink():
        raise UploadError("the .app in the archive is not a directory")
    return app, total
