"""Session attribution: `X-Quern-Session` reaches the action log and the trace (#254).

Every request this suite makes carries the client's session id. These tests
check that the id comes back, rather than that it was sent: an action made
under one session must be found by asking for that session, and must not be
found by asking for another -- which is what makes the filter a filter, and
not a list that happens to contain the right entry.

`GET /api/v1/crashes/latest` is the action used because it is logged and
needs no device.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from .client import QuernClient


def _act(quern: QuernClient, session: str | None = None) -> None:
    """One logged action; with no `session`, under the client's own header."""
    headers = {"X-Quern-Session": session} if session else {}
    quern.json_ok(
        "GET", "/api/v1/crashes/latest", params={"limit": 1}, headers=headers,
    )


def _trace(quern: QuernClient, session: str, since: datetime) -> list[dict]:
    body = quern.json_ok(
        "GET", "/api/v1/trace",
        params={"session": session, "since": since.isoformat(), "limit": 1000},
    )
    return body["actions"]


def test_an_action_comes_back_under_the_session_that_made_it(
    quern: QuernClient, authenticated: None,
) -> None:
    since = datetime.now(UTC) - timedelta(seconds=1)
    _act(quern)
    mine = [a for a in _trace(quern, quern.session, since)
            if a["action"] == "get_latest_crashes"]
    assert mine, f"no get_latest_crashes under {quern.session!r} since {since}"
    assert all(a["session"] == quern.session for a in mine), mine


def test_a_session_filter_leaves_out_other_sessions(
    quern: QuernClient, authenticated: None,
) -> None:
    since = datetime.now(UTC) - timedelta(seconds=1)
    other = f"conformance-other-{uuid.uuid4().hex[:8]}"
    _act(quern, other)
    _act(quern)
    theirs = _trace(quern, other, since)
    assert [a["session"] for a in theirs] and all(
        a["session"] == other for a in theirs
    ), theirs
    mine = _trace(quern, quern.session, since)
    assert not any(a["session"] == other for a in mine), mine


def test_a_malformed_session_is_not_refused(
    quern: QuernClient, authenticated: None,
) -> None:
    """The header is attribution, so a bad one still gets the work done
    (`server/session.py` records it as unattributed)."""
    resp = quern.get(
        "/api/v1/crashes/latest", params={"limit": 1},
        headers={"X-Quern-Session": "not a valid id!"},
    )
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:300]}"
