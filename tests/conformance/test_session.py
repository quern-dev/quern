"""Session attribution: `X-Quern-Session` reaches the action log and the trace (#254).

Every request this suite makes carries the client's session id. These tests
check that the id comes back, rather than that it was sent: an action made
under one session must be found by asking for that session, and must not be
found by asking for another -- which is what makes the filter a filter, and
not a list that happens to contain the right entry.

A request with no valid session still succeeds, and its response carries
`X-Quern-Session-Warning` naming why: `missing` or `malformed`. Public paths
never carry it.

`GET /api/v1/crashes/latest` is the action used because it is logged and
needs no device.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from .client import QuernClient

WARNING = "X-Quern-Session-Warning"


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


def _warning_reason(resp) -> str | None:
    """The reason token of `X-Quern-Session-Warning`, or None when absent.

    The token, not the prose after it: the sentence is for a person and may
    be reworded, the token is the contract.
    """
    value = resp.headers.get(WARNING)
    return None if value is None else value.split(";", 1)[0].strip()


def test_a_malformed_session_is_not_refused_but_is_warned_about(
    quern: QuernClient, authenticated: None,
) -> None:
    """The header is attribution, so a bad one still gets the work done --
    and is told it was recorded as unattributed."""
    resp = quern.get(
        "/api/v1/crashes/latest", params={"limit": 1},
        headers={"X-Quern-Session": "not a valid id!"},
    )
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:300]}"
    assert _warning_reason(resp) == "malformed", resp.headers.get(WARNING)


def test_an_untagged_request_is_warned_about(
    quern: QuernClient, authenticated: None,
) -> None:
    resp = quern.get("/api/v1/crashes/latest", params={"limit": 1}, tagged=False)
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:300]}"
    assert _warning_reason(resp) == "missing", resp.headers.get(WARNING)


def test_a_tagged_request_carries_no_warning(
    quern: QuernClient, authenticated: None,
) -> None:
    """The negative, without which the two above could pass on a server that
    warns everyone."""
    resp = quern.get("/api/v1/crashes/latest", params={"limit": 1})
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:300]}"
    assert WARNING not in resp.headers, resp.headers.get(WARNING)


def test_an_unauthenticated_request_is_still_warned_about(quern: QuernClient) -> None:
    """A 401 carries the warning too: a caller missing both is told both."""
    resp = quern.get(
        "/api/v1/crashes/latest", params={"limit": 1},
        authenticated=False, tagged=False,
    )
    assert resp.status_code == 401, f"{resp.status_code}: {resp.text[:300]}"
    assert _warning_reason(resp) == "missing", resp.headers.get(WARNING)


def test_a_public_path_carries_no_warning(quern: QuernClient) -> None:
    """`/health` answers anyone, so an unnamed caller there is not news."""
    resp = quern.get("/health", authenticated=False, tagged=False)
    assert resp.status_code == 200, f"{resp.status_code}: {resp.text[:300]}"
    assert WARNING not in resp.headers, resp.headers.get(WARNING)
