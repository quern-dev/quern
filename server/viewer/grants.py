"""Viewer tokens: view-only, one recording each, issued by this server (#449).

A token is 32 random bytes, URL-safe. Only its SHA-256 is kept, in memory, so
a token never appears in a log, a recording or a file, and tokens die with the
server -- sessions don't survive a restart either (#254).

Expiry is read from the recording, not kept here: a token works while its
recording is filming and for `grace_seconds` after the recording's own
`stopped_at`. So no stop path can forget to start the clock -- a recording
that fails, is stopped through the CLI or by a shutdown expires its tokens
the same way.

Built as a grant with a scope, `view:recording/<id>`, so the session grants
#254 plans are a wider scope from the same issuer, not a second mechanism. A
view token never widens into control.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()


@dataclass(frozen=True)
class Grant:
    scope: str           # "view:recording/<id>"
    recording_id: str
    issued_at: datetime


class ViewerGrants:
    """Issue, look up and revoke viewer tokens."""

    def __init__(self, recordings, *, grace_seconds: int) -> None:
        self._recordings = recordings
        self._grace = grace_seconds
        self._by_digest: dict[str, Grant] = {}

    def issue(self, recording_id: str) -> str:
        """A new token for `recording_id`. Returned once; only its hash is kept."""
        token = secrets.token_urlsafe(32)
        self._by_digest[_digest(token)] = Grant(
            scope=f"view:recording/{recording_id}", recording_id=recording_id,
            issued_at=datetime.now(UTC))
        return token

    def revoke(self, recording_id: str) -> int:
        """Revoke every token for `recording_id`. Returns how many there were."""
        doomed = [d for d, g in self._by_digest.items() if g.recording_id == recording_id]
        for d in doomed:
            del self._by_digest[d]
        return len(doomed)

    def recording_for(self, token: str, *, now: datetime | None = None) -> str | None:
        """The recording a live token may view, or None.

        None for an unknown or revoked token, a recording this server no longer
        knows, and a recording stopped more than the grace period ago -- which
        also prunes it. Compared in constant time against each stored digest's
        own value, so a lookup leaks nothing about near misses.
        """
        if not token or len(token) > 128:
            return None
        digest = _digest(token)
        grant = next((g for d, g in self._by_digest.items()
                      if hmac.compare_digest(d, digest)), None)
        if grant is None:
            return None
        try:
            rec = self._recordings.get(grant.recording_id)
        except Exception:  # noqa: BLE001 -- an unknown recording is simply not viewable
            self._by_digest.pop(digest, None)
            return None
        stopped = getattr(rec, "stopped_at", None)
        if getattr(rec, "state", None) != "recording" and stopped is not None:
            if ((now or datetime.now(UTC)) - stopped).total_seconds() > self._grace:
                self._by_digest.pop(digest, None)
                return None
        return grant.recording_id

    def count(self, recording_id: str) -> int:
        return sum(1 for g in self._by_digest.values() if g.recording_id == recording_id)
