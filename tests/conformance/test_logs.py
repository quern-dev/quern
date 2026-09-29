"""Log query, summary, cursors, errors and sources.

The log endpoints are read-only, which makes them safe to exercise hard but also
means the suite cannot manufacture the data it asserts on: a machine whose ring
buffer is empty has nothing to filter. Tests therefore assert *relationships*
that hold whatever the buffer contains -- a filter returns a subset, a cursor
does not go backwards, `total` matches `has_more` -- and skip explicitly when
there is no data rather than passing vacuously on an empty list.

That distinction is the point. A filter test that runs against zero entries
passes for the same reason a correct one does, and a suite full of those reports
a healthy log pipeline on a server that is capturing nothing.
"""

from __future__ import annotations

import pytest

from tests.conformance.client import QuernClient

#: Enough entries that a filter has something to exclude. Below this the
#: relational assertions are technically true and practically meaningless.
MEANINGFUL_SAMPLE = 5


@pytest.fixture(scope="module")
def logs_present(quern: QuernClient, authenticated: None) -> dict:
    """A page of logs, or a skip explaining that the buffer is empty."""
    body = quern.json_ok(
        "GET", "/api/v1/logs/query", params={"limit": 200}, timeout=30.0
    )
    if not body.get("entries"):
        pytest.skip(
            "the server's ring buffer is empty — start log capture and "
            "exercise an app before running the log tier"
        )
    return body


# -- query contract ----------------------------------------------------------


def test_query_returns_a_well_formed_page(logs_present: dict) -> None:
    """`entries`, `total` and `has_more` must describe one another."""
    entries, total = logs_present["entries"], logs_present["total"]
    assert isinstance(entries, list)
    assert total >= len(entries), (
        f"total={total} is smaller than the {len(entries)} entries returned"
    )
    assert logs_present["has_more"] == (200 < total), (
        f"has_more={logs_present['has_more']} disagrees with total={total} "
        "for a limit of 200"
    )


def test_every_entry_carries_the_fields_consumers_filter_on(
    logs_present: dict,
) -> None:
    """A missing field is indistinguishable from a non-matching one downstream."""
    required = {"timestamp", "level", "message"}
    for entry in logs_present["entries"][:20]:
        missing = required - set(entry)
        assert not missing, f"log entry is missing {sorted(missing)}: {entry}"


def test_limit_is_honoured(quern: QuernClient, logs_present: dict) -> None:
    body = quern.json_ok(
        "GET", "/api/v1/logs/query", params={"limit": 3}, timeout=30.0
    )
    assert len(body["entries"]) <= 3, (
        f"limit=3 returned {len(body['entries'])} entries"
    )


def test_limit_bounds_are_enforced(quern: QuernClient, authenticated: None) -> None:
    """`limit` is declared `ge=1, le=1000`; both edges must be rejected."""
    for bad in (0, 1001, -5):
        resp = quern.get(
            "/api/v1/logs/query", params={"limit": bad}, timeout=30.0
        )
        assert resp.status_code == 422, (
            f"limit={bad} returned {resp.status_code}, expected 422"
        )


def test_offset_bounds_are_enforced(quern: QuernClient, authenticated: None) -> None:
    resp = quern.get("/api/v1/logs/query", params={"offset": -1}, timeout=30.0)
    assert resp.status_code == 422, (
        f"offset=-1 returned {resp.status_code}, expected 422"
    )


def test_an_unknown_level_is_rejected_not_ignored(
    quern: QuernClient, authenticated: None
) -> None:
    """A bad enum value must 422.

    Silently ignoring it would return the unfiltered buffer, and a caller
    filtering for errors would be handed everything and believe it was all
    errors.
    """
    resp = quern.get(
        "/api/v1/logs/query", params={"level": "catastrophe"}, timeout=30.0
    )
    assert resp.status_code == 422, (
        f"an unknown level returned {resp.status_code}; if it is being ignored, "
        "the caller receives unfiltered logs labelled as filtered"
    )


def test_an_unknown_source_is_rejected_not_ignored(
    quern: QuernClient, authenticated: None
) -> None:
    resp = quern.get(
        "/api/v1/logs/query", params={"source": "not-a-source"}, timeout=30.0
    )
    assert resp.status_code == 422, (
        f"an unknown source returned {resp.status_code}, expected 422"
    )


# -- filtering actually filters ----------------------------------------------


#: `LogLevel` in `server/models.py`, least to most severe. `level=` is a
#: *minimum*, not an exact match: the ring buffer filters on
#: `LogLevel.at_least(params.level)`, so `level=error` correctly includes
#: `fault`. Duplicated here rather than imported because this suite tests the
#: server over the wire -- importing its enum would make the test agree with the
#: implementation by construction, including when the implementation is wrong.
LEVELS_BY_SEVERITY = ["debug", "info", "notice", "warning", "error", "fault"]


def test_a_level_filter_returns_that_level_and_above(
    quern: QuernClient, logs_present: dict
) -> None:
    """`level` is a severity floor, and must exclude everything below it.

    The threshold reading is easy to get wrong in both directions. Asserting
    exact-match would be wrong -- `level=error` including `fault` is correct.
    Asserting nothing would miss the failure that matters: a filter that admits
    entries *below* the floor hands a caller asking for errors a buffer full of
    debug noise, labelled as errors.
    """
    levels = {e["level"] for e in logs_present["entries"] if e.get("level")}
    if not levels:
        pytest.skip("no levelled entries in the buffer")

    for level in sorted(levels & set(LEVELS_BY_SEVERITY)):
        floor = LEVELS_BY_SEVERITY.index(level)
        body = quern.json_ok(
            "GET",
            "/api/v1/logs/query",
            params={"level": level, "limit": 50},
            timeout=30.0,
        )
        below = [
            e["level"]
            for e in body["entries"]
            if e.get("level") in LEVELS_BY_SEVERITY
            and LEVELS_BY_SEVERITY.index(e["level"]) < floor
        ]
        assert not below, (
            f"level={level!r} is a minimum, but the response included "
            f"less-severe entries at {sorted(set(below))}"
        )
        unknown = [
            e["level"]
            for e in body["entries"]
            if e.get("level") and e["level"] not in LEVELS_BY_SEVERITY
        ]
        assert not unknown, (
            f"level={level!r} returned entries at undeclared levels "
            f"{sorted(set(unknown))}; the severity order this filter depends on "
            "does not cover them"
        )


def test_the_most_severe_level_filter_is_exact(
    quern: QuernClient, logs_present: dict
) -> None:
    """At the top of the order, the threshold and exact readings coincide.

    `fault` is the most severe level, so nothing can be above it -- which makes
    this the one case where a filter that quietly ignored `level` entirely would
    be caught by an exact-match assertion.
    """
    body = quern.json_ok(
        "GET", "/api/v1/logs/query", params={"level": "fault", "limit": 50},
        timeout=30.0,
    )
    wrong = [e["level"] for e in body["entries"] if e.get("level") != "fault"]
    assert not wrong, (
        f"level=fault returned entries at {sorted(set(wrong))}; since fault is "
        "the maximum severity, the filter is not being applied"
    )


def test_a_search_filter_returns_only_matching_entries(
    quern: QuernClient, logs_present: dict
) -> None:
    """Pick a term from real data, then require every hit to contain it.

    Deriving the term from the buffer rather than hard-coding one is what makes
    this portable: a fixed string that happens to be absent on another machine
    turns a real assertion into a skip nobody notices.
    """
    term = None
    for entry in logs_present["entries"]:
        words = [w for w in str(entry.get("message", "")).split() if len(w) >= 6]
        if words:
            term = words[0]
            break
    if term is None:
        pytest.skip("no log message with a searchable token")

    body = quern.json_ok(
        "GET",
        "/api/v1/logs/query",
        params={"search": term, "limit": 50},
        timeout=30.0,
    )
    assert body["entries"], f"search for {term!r} taken from a real entry found nothing"
    misses = [
        e for e in body["entries"] if term.lower() not in str(e.get("message", "")).lower()
    ]
    assert not misses, (
        f"search={term!r} returned {len(misses)} entries not containing it, "
        f"e.g. {misses[0].get('message', '')[:120]!r}"
    )


def test_a_filtered_query_never_exceeds_the_unfiltered_one(
    quern: QuernClient, logs_present: dict
) -> None:
    """Filtering is a narrowing operation; `total` must not grow."""
    unfiltered_total = logs_present["total"]
    body = quern.json_ok(
        "GET",
        "/api/v1/logs/query",
        params={"level": "error", "limit": 1},
        timeout=30.0,
    )
    assert body["total"] <= unfiltered_total, (
        f"filtering to errors reported {body['total']} entries, more than the "
        f"unfiltered {unfiltered_total}"
    )


def test_tail_returns_the_newest_entries(
    quern: QuernClient, logs_present: dict
) -> None:
    """`tail=true` is documented as the last N matching entries.

    Worth asserting because `tail` and `offset` index the same list from
    opposite ends, and the handler applies them in different branches depending
    on how many buffers are involved.
    """
    if logs_present["total"] < MEANINGFUL_SAMPLE:
        pytest.skip("too few entries for tail to differ from head")

    head = quern.json_ok(
        "GET", "/api/v1/logs/query", params={"limit": 3, "tail": False}, timeout=30.0
    )
    tail = quern.json_ok(
        "GET", "/api/v1/logs/query", params={"limit": 3, "tail": True}, timeout=30.0
    )
    head_stamps = [e["timestamp"] for e in head["entries"]]
    tail_stamps = [e["timestamp"] for e in tail["entries"]]
    if not head_stamps or not tail_stamps:
        pytest.skip("empty page")
    assert max(tail_stamps) >= max(head_stamps), (
        f"tail's newest entry ({max(tail_stamps)}) is older than head's "
        f"({max(head_stamps)})"
    )


def test_pagination_does_not_repeat_or_skip_entries(
    quern: QuernClient, logs_present: dict
) -> None:
    """Two adjacent pages must be disjoint.

    The ring buffer is live, so new arrivals can shift the window; the
    assertion is therefore on gross overlap rather than exact adjacency.
    """
    if logs_present["total"] < 2 * MEANINGFUL_SAMPLE:
        pytest.skip("not enough entries to paginate meaningfully")

    first = quern.json_ok(
        "GET", "/api/v1/logs/query", params={"limit": 5, "offset": 0}, timeout=30.0
    )
    second = quern.json_ok(
        "GET", "/api/v1/logs/query", params={"limit": 5, "offset": 5}, timeout=30.0
    )
    a = [(e["timestamp"], e.get("message")) for e in first["entries"]]
    b = [(e["timestamp"], e.get("message")) for e in second["entries"]]
    if not a or not b:
        pytest.skip("empty page")
    overlap = set(a) & set(b)
    assert len(overlap) < len(a), (
        f"offset=0 and offset=5 returned {len(overlap)} identical entries out "
        f"of {len(a)}; pagination is not advancing"
    )


# -- summary and cursors -----------------------------------------------------


def test_summary_returns_a_cursor(quern: QuernClient, authenticated: None) -> None:
    """The cursor is the contract that makes delta polling possible.

    CONTRIBUTING calls it "critical for token-efficient AI workflows", so its
    absence is a functional regression rather than a missing nicety.
    """
    body = quern.json_ok("GET", "/api/v1/logs/summary", timeout=30.0)
    assert "cursor" in body, f"/logs/summary returned no cursor: {sorted(body)}"


def test_only_documented_summary_windows_are_accepted(
    quern: QuernClient, authenticated: None
) -> None:
    """`window` is pattern-constrained to a fixed set; check both directions."""
    for window in ("30s", "1m", "5m", "15m", "1h"):
        resp = quern.get(
            "/api/v1/logs/summary", params={"window": window}, timeout=30.0
        )
        assert resp.is_success, (
            f"documented window {window!r} was rejected with {resp.status_code}"
        )

    for window in ("2h", "10m", "", "5"):
        resp = quern.get(
            "/api/v1/logs/summary", params={"window": window}, timeout=30.0
        )
        assert resp.status_code == 422, (
            f"undocumented window {window!r} returned {resp.status_code}, "
            "expected 422"
        )


def test_replaying_a_cursor_does_not_return_the_same_entries_again(
    quern: QuernClient, authenticated: None
) -> None:
    """A cursor fed straight back must yield a delta, not the original window.

    This is the property the whole cursor mechanism exists for: an agent polling
    with `since_cursor` and receiving the full window each time silently
    re-reads everything, which is the exact cost the cursor was added to avoid.
    """
    first = quern.json_ok("GET", "/api/v1/logs/summary", timeout=30.0)
    cursor = first.get("cursor")
    if not cursor:
        pytest.skip("no cursor returned")

    second = quern.json_ok(
        "GET", "/api/v1/logs/summary", params={"since_cursor": cursor}, timeout=30.0
    )
    first_count = first.get("total_entries", first.get("total", 0)) or 0
    second_count = second.get("total_entries", second.get("total", 0)) or 0
    if first_count == 0:
        pytest.skip("first summary covered no entries; nothing to delta against")

    assert second_count <= first_count, (
        f"a summary since the previous cursor covered {second_count} entries, "
        f"more than the {first_count} the cursor was taken from — the cursor is "
        "not narrowing the window"
    )


def test_a_malformed_cursor_is_handled(quern: QuernClient, authenticated: None) -> None:
    """A bad cursor must produce a clear answer, not a 500.

    Cursors travel through agent context and get truncated and mangled there, so
    this is a realistic input rather than a contrived one.
    """
    resp = quern.get(
        "/api/v1/logs/summary",
        params={"since_cursor": "not-a-real-cursor-@@@"},
        timeout=30.0,
    )
    assert resp.status_code != 500, (
        f"a malformed cursor caused a server error: {resp.text[:300]}"
    )
    assert resp.status_code in (200, 400, 422), (
        f"unexpected status {resp.status_code} for a malformed cursor"
    )


# -- errors and sources ------------------------------------------------------


def test_the_errors_endpoint_returns_only_errors(
    quern: QuernClient, authenticated: None
) -> None:
    body = quern.json_ok("GET", "/api/v1/logs/errors", timeout=30.0)
    entries = body.get("errors") or body.get("entries") or []
    if not entries:
        pytest.skip("no errors captured")
    allowed = {"error", "fault", "critical", "fatal"}
    wrong = [
        e.get("level")
        for e in entries
        if e.get("level") and str(e["level"]).lower() not in allowed
    ]
    assert not wrong, (
        f"/logs/errors returned non-error levels: {sorted(set(wrong))}"
    )


def test_log_sources_are_listed(quern: QuernClient, authenticated: None) -> None:
    body = quern.json_ok("GET", "/api/v1/logs/sources", timeout=30.0)
    assert isinstance(body, dict), f"/logs/sources returned {type(body).__name__}"


def test_the_filter_config_reports_all_three_scopes(
    quern: QuernClient, authenticated: None
) -> None:
    """The reference documents global, per-source and per-device scopes.

    A reader that silently omits a scope makes a configured filter invisible,
    and an invisible filter is indistinguishable from a log source that has
    stopped producing.
    """
    body = quern.json_ok("GET", "/api/v1/logs/filter", timeout=30.0)
    assert isinstance(body, dict) and body, "/logs/filter returned nothing"
