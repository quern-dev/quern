"""Arrival order, for cursors that do not depend on anyone's clock (#317).

A summary cursor used to be a timestamp: "entries stamped after the newest
one you have seen". Entries do not arrive in timestamp order -- a device
clock runs ahead, a crash report is stamped when the crash happened and
arrives later, and a flow is stamped when its request *started* and stored
when it finishes -- so anything that arrived late with an earlier stamp fell
behind the cursor and was never returned by any delta.

Arrival order has none of that. Each store numbers what it takes in, and a
cursor is a number: "everything that arrived after this". The numbers carry
the process's boot id, because they restart at zero with the server and a
cursor from before a restart must not be read as "nothing new since".
"""

from __future__ import annotations

import base64
import secrets
import struct
from dataclasses import dataclass
from datetime import UTC, datetime


class ArrivalClock:
    """Numbers arrivals, 1, 2, 3, ... for the life of the process.

    Shared by every store a single cursor has to span -- the three log
    buffers share one, so one number orders an entry against all of them.
    """

    def __init__(self) -> None:
        self.boot = secrets.token_bytes(4)
        self._last = 0

    def tick(self) -> int:
        self._last += 1
        return self._last

    @property
    def now(self) -> int:
        """The number of the latest arrival, or 0 before any."""
        return self._last


@dataclass(frozen=True)
class ArrivalCursor:
    boot: bytes
    seq: int


@dataclass(frozen=True)
class TimestampCursor:
    """The old kind. Honoured as it always was, which is to say imperfectly."""

    at: datetime


def make_arrival_cursor(clock: ArrivalClock, seq: int) -> str:
    raw = clock.boot + struct.pack(">Q", seq)
    return "a_" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_any_cursor(cursor: str) -> ArrivalCursor | TimestampCursor | None:
    """Either kind of cursor, or None if it is neither."""
    try:
        b64 = cursor[2:] + "=" * (-len(cursor[2:]) % 4)
        raw = base64.urlsafe_b64decode(b64)
    except (ValueError, TypeError):
        return None
    if cursor.startswith("a_") and len(raw) == 12:
        return ArrivalCursor(boot=raw[:4], seq=struct.unpack(">Q", raw[4:])[0])
    if cursor.startswith("c_") and len(raw) == 8:
        epoch_us = struct.unpack(">Q", raw)[0]
        try:
            return TimestampCursor(at=datetime.fromtimestamp(epoch_us / 1_000_000, tz=UTC))
        except (OverflowError, OSError, ValueError):
            return None
    return None
