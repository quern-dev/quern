"""Mock rules and the bypass list: full CRUD against a live proxy.

Chosen as an early target because it is the densest bug surface in Quern that
needs no device at all -- five verbs over one resource, two accepted request
shapes, and server-side pattern validation. Every test here restores what it
changed; see the `mock_sandbox` and `bypass_sandbox` fixtures.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conformance.client import QuernClient


#: A pattern that matches a host nothing on this machine will ever contact, so a
#: rule left behind by a crashed run cannot silently mock real traffic.
def _unique_pattern() -> str:
    return f"~d conformance-{uuid.uuid4().hex[:12]}.invalid"


# -- create and read ---------------------------------------------------------


def test_a_created_mock_appears_in_the_listing(
    quern: QuernClient, mock_sandbox
) -> None:
    pattern = _unique_pattern()
    rule_id = mock_sandbox(pattern, status_code=418, body='{"conformance":true}')

    listing = quern.json_ok("GET", "/api/v1/proxy/mocks", timeout=20.0)
    found = [r for r in listing["rules"] if r["rule_id"] == rule_id]
    assert found, (
        f"rule {rule_id} was accepted but is not in the listing "
        f"({listing['total']} rule(s) present)"
    )
    assert found[0]["pattern"] == pattern
    assert found[0]["response"]["status_code"] == 418
    assert found[0]["response"]["body"] == '{"conformance":true}'


def test_the_listing_total_matches_the_rules_it_returns(
    quern: QuernClient, mock_sandbox
) -> None:
    """`total` is a separate field from `rules`, so it can disagree with it."""
    mock_sandbox(_unique_pattern())
    listing = quern.json_ok("GET", "/api/v1/proxy/mocks", timeout=20.0)
    assert listing["total"] == len(listing["rules"]), (
        f"total={listing['total']} but {len(listing['rules'])} rules returned"
    )


def test_status_reports_the_same_mock_count_as_the_listing(
    quern: QuernClient, mock_sandbox
) -> None:
    """Two endpoints report mock count; they must not drift apart.

    `proxy_status.mock_rules_count` is what an agent reads when orienting, and
    `/proxy/mocks` is what it reads when acting. A disagreement means one of
    those two decisions is made on a stale number.
    """
    mock_sandbox(_unique_pattern())
    listing = quern.json_ok("GET", "/api/v1/proxy/mocks", timeout=20.0)
    status = quern.json_ok("GET", "/api/v1/proxy/status", timeout=20.0)
    assert status["mock_rules_count"] == listing["total"], (
        f"/proxy/status says {status['mock_rules_count']} mock rule(s), "
        f"/proxy/mocks says {listing['total']}"
    )


def test_rule_ids_are_unique_across_creations(
    quern: QuernClient, mock_sandbox
) -> None:
    """Two rules with the *same* pattern must still be separately addressable.

    If creation returned a pattern-derived id, the second create would collide
    with the first and deleting one would delete both.
    """
    pattern = _unique_pattern()
    first = mock_sandbox(pattern, status_code=200)
    second = mock_sandbox(pattern, status_code=201)
    assert first != second, (
        f"two rules for the same pattern share the id {first!r}"
    )


# -- the two accepted request shapes -----------------------------------------


def test_flat_and_nested_request_shapes_are_equivalent(
    quern: QuernClient, mock_sandbox
) -> None:
    """`SetMockRequest` documents both shapes; they must produce one result.

    The flat form is what an agent writes by hand and the nested form is what
    the model declares, so a divergence shows up only for one kind of caller.
    """
    flat_id = mock_sandbox(
        _unique_pattern(),
        status_code=503,
        headers={"x-conformance": "flat"},
        body="flat-body",
    )
    nested_id = mock_sandbox(
        _unique_pattern(),
        response={
            "status_code": 503,
            "headers": {"x-conformance": "nested"},
            "body": "nested-body",
        },
    )

    rules = {
        r["rule_id"]: r
        for r in quern.json_ok("GET", "/api/v1/proxy/mocks", timeout=20.0)["rules"]
    }
    flat, nested = rules[flat_id]["response"], rules[nested_id]["response"]

    assert flat["status_code"] == nested["status_code"] == 503
    assert flat["headers"]["x-conformance"] == "flat"
    assert nested["headers"]["x-conformance"] == "nested"
    assert flat["body"] == "flat-body"
    assert nested["body"] == "nested-body"


def test_response_defaults_are_applied_when_omitted(
    quern: QuernClient, mock_sandbox
) -> None:
    """A pattern-only rule must still be a usable rule.

    `MockResponseSpec` defaults to 200 with a JSON content-type. If those
    defaults were dropped somewhere in the flat/nested normalisation, the rule
    would be created and then serve nothing coherent.
    """
    rule_id = mock_sandbox(_unique_pattern())
    rules = {
        r["rule_id"]: r
        for r in quern.json_ok("GET", "/api/v1/proxy/mocks", timeout=20.0)["rules"]
    }
    response = rules[rule_id]["response"]
    assert response["status_code"] == 200, (
        f"a pattern-only rule defaulted to status {response['status_code']}"
    )
    assert response["headers"], "a pattern-only rule was created with no headers"


# -- validation --------------------------------------------------------------


def test_an_invalid_filter_pattern_is_rejected(
    quern: QuernClient, proxy_running: dict
) -> None:
    """CONTRIBUTING is explicit that patterns are validated server-side.

    `~p` is the documented trap: it looks like "path" and does not exist, and
    the guidance is to use `~u`. A rule accepted with `~p` would match nothing
    and report success, which is the worst outcome available -- the caller
    believes traffic is mocked and it is not.
    """
    resp = quern.post(
        "/api/v1/proxy/mocks",
        json={"pattern": "~p /some/path", "status_code": 200},
        timeout=20.0,
    )
    if resp.is_success:
        rule_id = resp.json().get("rule_id")
        if rule_id:
            quern.delete(f"/api/v1/proxy/mocks/{rule_id}", timeout=20.0)
        pytest.fail(
            "`~p` was accepted as a filter pattern. It is not a mitmproxy "
            "operator, so the rule can never match; the caller is told the "
            "mock is active."
        )
    assert resp.status_code == 400, (
        f"expected 400 for an invalid pattern, got {resp.status_code}: "
        f"{resp.text[:300]}"
    )


def test_a_syntactically_broken_pattern_is_rejected(
    quern: QuernClient, proxy_running: dict
) -> None:
    resp = quern.post(
        "/api/v1/proxy/mocks",
        json={"pattern": "~d (unclosed", "status_code": 200},
        timeout=20.0,
    )
    if resp.is_success:
        rule_id = resp.json().get("rule_id")
        if rule_id:
            quern.delete(f"/api/v1/proxy/mocks/{rule_id}", timeout=20.0)
        pytest.fail("an unparseable filter pattern was accepted")
    assert resp.status_code == 400, (
        f"expected 400, got {resp.status_code}: {resp.text[:300]}"
    )


def test_creating_a_mock_without_a_pattern_is_rejected(
    quern: QuernClient, proxy_running: dict
) -> None:
    resp = quern.post("/api/v1/proxy/mocks", json={"status_code": 200}, timeout=20.0)
    assert resp.status_code == 422, (
        f"expected 422 for a missing required field, got {resp.status_code}"
    )


# -- update ------------------------------------------------------------------


def test_updating_a_rule_changes_only_what_was_named(
    quern: QuernClient, mock_sandbox
) -> None:
    """PATCH is partial: naming a pattern must not reset the response.

    This is the shape `update_cert_state` gets right and that CONTRIBUTING
    calls out as easy to get wrong elsewhere -- a handler that dumps the whole
    model erases the fields the caller had no opinion about.
    """
    rule_id = mock_sandbox(_unique_pattern(), status_code=404, body="original")
    new_pattern = _unique_pattern()

    quern.json_ok(
        "PATCH",
        f"/api/v1/proxy/mocks/{rule_id}",
        json={"pattern": new_pattern},
        timeout=20.0,
    )

    rules = {
        r["rule_id"]: r
        for r in quern.json_ok("GET", "/api/v1/proxy/mocks", timeout=20.0)["rules"]
    }
    assert rules[rule_id]["pattern"] == new_pattern
    assert rules[rule_id]["response"]["status_code"] == 404, (
        "updating the pattern reset the response status code"
    )
    assert rules[rule_id]["response"]["body"] == "original", (
        "updating the pattern erased the response body"
    )


def test_updating_the_response_preserves_the_pattern(
    quern: QuernClient, mock_sandbox
) -> None:
    pattern = _unique_pattern()
    rule_id = mock_sandbox(pattern, status_code=200, body="before")

    quern.json_ok(
        "PATCH",
        f"/api/v1/proxy/mocks/{rule_id}",
        json={"status_code": 500, "body": "after"},
        timeout=20.0,
    )
    rules = {
        r["rule_id"]: r
        for r in quern.json_ok("GET", "/api/v1/proxy/mocks", timeout=20.0)["rules"]
    }
    assert rules[rule_id]["pattern"] == pattern, (
        "updating the response changed the pattern"
    )
    assert rules[rule_id]["response"]["status_code"] == 500
    assert rules[rule_id]["response"]["body"] == "after"


def test_an_empty_update_is_rejected(quern: QuernClient, mock_sandbox) -> None:
    """Naming neither field is a caller error, not a no-op success."""
    rule_id = mock_sandbox(_unique_pattern())
    resp = quern.patch(f"/api/v1/proxy/mocks/{rule_id}", json={}, timeout=20.0)
    assert resp.status_code == 400, (
        f"expected 400 for an update naming nothing, got {resp.status_code}"
    )


def test_updating_an_unknown_rule_reports_not_found(
    quern: QuernClient, proxy_running: dict
) -> None:
    resp = quern.patch(
        f"/api/v1/proxy/mocks/{uuid.uuid4()}",
        json={"status_code": 200},
        timeout=20.0,
    )
    assert resp.status_code == 404, (
        f"expected 404 for an unknown rule id, got {resp.status_code}"
    )


def test_updating_a_rule_to_an_invalid_pattern_is_rejected(
    quern: QuernClient, mock_sandbox
) -> None:
    """Validation must apply on update, not only on create.

    A rule can otherwise be walked into an unmatchable state one PATCH after
    passing the check at creation.
    """
    pattern = _unique_pattern()
    rule_id = mock_sandbox(pattern, status_code=200)
    resp = quern.patch(
        f"/api/v1/proxy/mocks/{rule_id}",
        json={"pattern": "~p /nope"},
        timeout=20.0,
    )
    assert resp.status_code == 400, (
        f"expected 400 when updating to an invalid pattern, got "
        f"{resp.status_code}; the rule can no longer match anything"
    )

    rules = {
        r["rule_id"]: r
        for r in quern.json_ok("GET", "/api/v1/proxy/mocks", timeout=20.0)["rules"]
    }
    assert rules[rule_id]["pattern"] == pattern, (
        "a rejected update still changed the stored pattern"
    )


# -- delete ------------------------------------------------------------------


def test_a_deleted_rule_leaves_the_listing(quern: QuernClient, mock_sandbox) -> None:
    rule_id = mock_sandbox(_unique_pattern())
    quern.json_ok("DELETE", f"/api/v1/proxy/mocks/{rule_id}", timeout=20.0)

    listing = quern.json_ok("GET", "/api/v1/proxy/mocks", timeout=20.0)
    assert not [r for r in listing["rules"] if r["rule_id"] == rule_id], (
        f"rule {rule_id} was deleted but is still listed"
    )


def test_deleting_one_rule_leaves_the_others(
    quern: QuernClient, mock_sandbox
) -> None:
    """The obvious mistake in a delete-by-id is to clear everything."""
    keep_a = mock_sandbox(_unique_pattern())
    doomed = mock_sandbox(_unique_pattern())
    keep_b = mock_sandbox(_unique_pattern())

    quern.json_ok("DELETE", f"/api/v1/proxy/mocks/{doomed}", timeout=20.0)

    remaining = {
        r["rule_id"]
        for r in quern.json_ok("GET", "/api/v1/proxy/mocks", timeout=20.0)["rules"]
    }
    assert keep_a in remaining and keep_b in remaining, (
        "deleting one rule removed its neighbours"
    )
    assert doomed not in remaining


def test_deleting_an_unknown_rule_reports_not_found(
    quern: QuernClient, proxy_running: dict
) -> None:
    """Deleting something that was never there must not report success.

    PATCH on an unknown id returns 404 (`update_mock` maps the ValueError).
    DELETE takes the id straight to `clear_mock` and returns
    `{"status": "deleted", "rule_id": ...}` without ever checking, so a caller
    that deletes a typo'd id is told the deletion happened.

    That matters beyond tidiness: cleanup code that deletes by id and trusts the
    response cannot detect that it has leaked a rule, and a stale mock rule
    silently answers real traffic.
    """
    unknown = str(uuid.uuid4())
    resp = quern.delete(f"/api/v1/proxy/mocks/{unknown}", timeout=20.0)
    assert resp.status_code == 404, (
        f"deleting a nonexistent rule returned {resp.status_code} "
        f"({resp.text[:200]}) — a caller cannot tell a real deletion from a "
        "no-op"
    )


# -- bypass list -------------------------------------------------------------


def test_added_bypass_patterns_are_listed(
    quern: QuernClient, bypass_sandbox
) -> None:
    host = f"conformance-{uuid.uuid4().hex[:8]}.invalid"
    returned = bypass_sandbox(host)
    assert host in returned, f"POST /proxy/bypass returned {returned} without {host}"

    listed = quern.json_ok("GET", "/api/v1/proxy/bypass", timeout=20.0)
    assert host in listed["patterns"]
    assert listed["total"] == len(listed["patterns"])


def test_bypass_patterns_accumulate_rather_than_replace(
    quern: QuernClient, bypass_sandbox
) -> None:
    """The endpoint documents itself as *adding* patterns.

    Contrast `set_local_capture`, which the reference explicitly documents as
    replacing its list. Two similar-looking proxy endpoints with opposite
    semantics is exactly where an implementation slips.
    """
    first = f"conformance-a-{uuid.uuid4().hex[:8]}.invalid"
    second = f"conformance-b-{uuid.uuid4().hex[:8]}.invalid"

    bypass_sandbox(first)
    after = bypass_sandbox(second)

    assert first in after, (
        f"adding {second} dropped the previously added {first}; "
        "POST /proxy/bypass replaced the list instead of extending it"
    )
    assert second in after


def test_bypass_status_agrees_with_the_bypass_listing(
    quern: QuernClient, bypass_sandbox
) -> None:
    host = f"conformance-{uuid.uuid4().hex[:8]}.invalid"
    bypass_sandbox(host)

    listed = set(quern.json_ok("GET", "/api/v1/proxy/bypass", timeout=20.0)["patterns"])
    status = set(
        quern.json_ok("GET", "/api/v1/proxy/status", timeout=20.0)["bypass_patterns"]
    )
    assert listed == status, (
        f"/proxy/bypass and /proxy/status disagree: "
        f"only in bypass {sorted(listed - status)}, "
        f"only in status {sorted(status - listed)}"
    )


def test_an_empty_bypass_request_is_rejected(
    quern: QuernClient, proxy_running: dict
) -> None:
    resp = quern.post("/api/v1/proxy/bypass", json={"patterns": []}, timeout=20.0)
    assert resp.status_code == 400, (
        f"expected 400 for an empty pattern list, got {resp.status_code}"
    )


def test_a_bare_string_bypass_pattern_is_accepted(
    quern: QuernClient, bypass_sandbox, proxy_running: dict
) -> None:
    """The handler coerces a lone string into a list; that path needs covering.

    It is the shape a hand-written call most often takes, and it is handled by
    two lines that no other test exercises.
    """
    host = f"conformance-{uuid.uuid4().hex[:8]}.invalid"
    body = quern.json_ok(
        "POST", "/api/v1/proxy/bypass", json={"patterns": host}, timeout=20.0
    )
    assert host in body["patterns"], (
        f"a bare-string pattern was accepted but {host} is not in the list"
    )


def test_removing_a_bypass_pattern_leaves_the_others(
    quern: QuernClient, bypass_sandbox
) -> None:
    """Targeted removal must not empty the list.

    `clear_bypass` with no argument clears everything, so the two behaviours sit
    in one handler distinguished only by a query parameter being present.
    """
    keep = f"conformance-keep-{uuid.uuid4().hex[:8]}.invalid"
    drop = f"conformance-drop-{uuid.uuid4().hex[:8]}.invalid"
    bypass_sandbox(keep, drop)

    quern.json_ok(
        "DELETE", "/api/v1/proxy/bypass", params={"patterns": drop}, timeout=20.0
    )

    remaining = quern.json_ok("GET", "/api/v1/proxy/bypass", timeout=20.0)["patterns"]
    assert keep in remaining, f"removing {drop} also removed {keep}"
    assert drop not in remaining
