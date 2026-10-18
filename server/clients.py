"""Client keys: credentials the owner issues to other callers (#254, phase 2).

The owner's API key is the owner. Anyone else -- another person's agent, a CI
machine, a second agent the owner wants kept in its lane -- gets a client key
with `quern clients add <label>`. A client key is a principal of its own: it
may watch devices, reserve them, and change the ones it holds, and nothing
Mac-wide (server/scopes.py). Keys are revoked one at a time.

Stored hashed in `~/.quern/clients.json`; the key itself is printed once, at
`add`, and never again. Only the CLI writes that file. The server reads it, and
reloads it when it changes, so a revoke takes effect on a running server
without a restart. The server never writes it: a server writing `last_used`
into the same file could land between the CLI's read and write and undo a
revoke. Last use goes in a file of its own, `clients-seen.json`, which only the
server writes.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from server import principal

logger = logging.getLogger(__name__)

#: Short, lowercase and plain: a label becomes the prefix of every session id
#: its holder opens (`<label>:<id>`), and session ids are capped at 64.
LABEL = re.compile(r"[a-z0-9][a-z0-9-]{0,15}")
#: Marks a client key, so the owner's key is never looked up as one and a
#: pasted key says what it is.
KEY_PREFIX = "qc_"
#: Seconds between writes of the last-used times.
_SEEN_FLUSH_SECONDS = 60.0


class ClientKeyError(ValueError):
    """A label that cannot be added or revoked; the message says why."""


@dataclass(frozen=True)
class ClientRecord:
    label: str
    digest: str
    created_at: str
    revoked_at: str | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


def _digest(key: str | bytes) -> str:
    raw = key.encode("utf-8") if isinstance(key, str) else key
    return hashlib.sha256(raw).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def default_path() -> Path:
    from server.config import CONFIG_DIR

    return CONFIG_DIR / "clients.json"


class ClientKeys:
    """The issued client keys, read from disk and reloaded when it changes."""

    def __init__(self, path: Path | None = None, *, clock=time.monotonic) -> None:
        self.path = path or default_path()
        self.seen_path = self.path.with_name("clients-seen.json")
        self._clock = clock
        self._stamp: tuple[int, int] | None = None
        self._records: dict[str, ClientRecord] = {}
        self._by_digest: dict[str, str] = {}
        self._seen: dict[str, str] = {}
        self._seen_dirty = False
        self._seen_flushed = float("-inf")

    # -- reading --------------------------------------------------------

    def _reload(self) -> None:
        """Re-read the file if it changed. A file that cannot be read admits
        no client at all: refusing everyone is recoverable, and admitting on
        the strength of a file nobody could read is not."""
        try:
            st = self.path.stat()
        except FileNotFoundError:
            self._stamp, self._records, self._by_digest = None, {}, {}
            return
        except OSError as e:
            logger.error("Cannot read %s, so no client key is accepted: %s", self.path, e)
            self._stamp, self._records, self._by_digest = None, {}, {}
            return
        stamp = (st.st_mtime_ns, st.st_size)
        if stamp == self._stamp:
            return
        try:
            records = _parse(self.path.read_text())
        except (OSError, ValueError) as e:
            logger.error("Cannot read %s, so no client key is accepted: %s", self.path, e)
            records = {}
        self._stamp = stamp
        self._records = records
        self._by_digest = {r.digest: r.label for r in records.values() if r.active}

    def records(self) -> list[ClientRecord]:
        self._reload()
        return sorted(self._records.values(), key=lambda r: r.label)

    def is_active(self, label: str) -> bool:
        self._reload()
        record = self._records.get(label)
        return record is not None and record.active

    def principal_for(self, key: bytes) -> str | None:
        """The principal a presented key acts as, or None.

        A dictionary lookup by digest: what is compared is a SHA-256 of a
        secret, and timing it reveals nothing about any key."""
        if not key.startswith(KEY_PREFIX.encode()):
            return None
        self._reload()
        label = self._by_digest.get(_digest(key))
        if label is None:
            return None
        self._note_used(label)
        return principal.client(label)

    def last_used(self) -> dict[str, str]:
        try:
            data = json.loads(self.seen_path.read_text())
        except (OSError, ValueError):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)} \
            if isinstance(data, dict) else {}

    def _note_used(self, label: str) -> None:
        self._seen[label] = _now()
        self._seen_dirty = True
        now = self._clock()
        if now - self._seen_flushed < _SEEN_FLUSH_SECONDS:
            return
        self._seen_flushed = now
        self._seen_dirty = False
        merged = {**self.last_used(), **self._seen}
        try:
            _write_private(self.seen_path, merged)
        except OSError as e:     # bookkeeping: never fails a request
            logger.warning("Could not record client key use in %s: %s", self.seen_path, e)

    # -- writing (the CLI) ----------------------------------------------

    def add(self, label: str) -> str:
        """Issue a key for `label` and return it. The only time it is shown."""
        if not LABEL.fullmatch(label or ""):
            raise ClientKeyError(
                f"{label!r} is not a usable label: 1-16 of a-z, 0-9 and '-', "
                "starting with a letter or digit")
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        with self._locked() as records:
            existing = records.get(label)
            if existing is not None and existing.active:
                raise ClientKeyError(
                    f"{label} already has a key; revoke it first "
                    f"(quern clients revoke {label}) to issue a new one")
            records[label] = ClientRecord(label=label, digest=_digest(key), created_at=_now())
        return key

    def revoke(self, label: str) -> ClientRecord:
        with self._locked() as records:
            existing = records.get(label)
            if existing is None:
                raise ClientKeyError(f"no client key labelled {label}")
            if not existing.active:
                raise ClientKeyError(f"{label}'s key was already revoked at {existing.revoked_at}")
            records[label] = ClientRecord(label=label, digest=existing.digest,
                                          created_at=existing.created_at, revoked_at=_now())
            return records[label]

    @contextlib.contextmanager
    def _locked(self) -> Iterator[dict[str, ClientRecord]]:
        """Read, change and write under a lock, so two `quern clients` at once
        cannot each start from the same file and drop the other's change.

        A file that exists and cannot be parsed is refused, not replaced:
        writing would discard every key in it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(f".{self.path.name}.lock")
        with open(lock, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                text = self.path.read_text() if self.path.exists() else ""
                try:
                    records = _parse(text) if text.strip() else {}
                except ValueError as e:
                    raise ClientKeyError(
                        f"{self.path} is not readable ({e}); fix or move it aside first"
                    ) from None
                yield records
                _write_private(self.path, {"clients": [
                    {"label": r.label, "digest": r.digest, "created_at": r.created_at,
                     "revoked_at": r.revoked_at}
                    for r in sorted(records.values(), key=lambda r: r.label)]})
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


def _parse(text: str) -> dict[str, ClientRecord]:
    data = json.loads(text)
    entries = data.get("clients") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise ValueError("expected an object with a 'clients' list")
    records: dict[str, ClientRecord] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("a client entry is not an object")
        label, digest = entry.get("label"), entry.get("digest")
        if not (isinstance(label, str) and LABEL.fullmatch(label)
                and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise ValueError(f"a client entry has a bad label or digest: {label!r}")
        revoked = entry.get("revoked_at")
        records[label] = ClientRecord(
            label=label, digest=digest, created_at=str(entry.get("created_at") or ""),
            revoked_at=revoked if isinstance(revoked, str) and revoked else None)
    return records


def _write_private(path: Path, data: dict) -> None:
    """Swap in a new file readable only by its owner: digests are not keys,
    but nobody else on the Mac has a reason to read them."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data, indent=2) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
