"""Deleting a mock rule that never existed is a 404, not a cheerful 200.

`DELETE /api/v1/proxy/mocks/{rule_id}` answered
`200 {"status": "deleted", "rule_id": ...}` for an id that had never been a
rule, while `PATCH` on the same id answered 404. One unknown id, two verbs,
two opposite answers — and the DELETE body actively asserted something that
had not happened.

That matters because a leaked mock rule is not inert. It goes on matching real
traffic and serving synthetic responses, and the next person wondering why
their app received a 418 has no reason to suspect a rule something already
reported as deleted. Teardown code that deletes by id and checks the response
could not detect that it had failed (#182).

This is CONTRIBUTING's *"a failed check must never read as a passing one"*,
applied to a write rather than a read.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from server.config import ServerConfig
from server.main import create_app

KEY = "test-key-12345"
AUTH = {"Authorization": f"Bearer {KEY}"}
PATTERN = "~d example.com"
RESPONSE = {"status_code": 418, "body": "teapot"}


@pytest.fixture
def adapter():
    """A proxy adapter holding one real rule, with the addon stubbed out."""
    from server.sources.proxy import ProxyAdapter

    a = ProxyAdapter.__new__(ProxyAdapter)
    a._mock_rules = []
    a.send_command = AsyncMock()
    a._running = True  # `is_running` is a read-only property over this
    return a


@pytest.fixture
def client(adapter):
    """A TestClient whose proxy adapter is the fixture above.

    `enable_proxy=False` stops `create_app` building a second, real adapter;
    the one under test is attached afterwards.
    """
    app = create_app(
        config=ServerConfig(api_key=KEY), enable_oslog=False,
        enable_crash=False, enable_proxy=False,
    )
    app.state.proxy_adapter = adapter
    app.state.flow_store = None
    return TestClient(app)


class TestDeletingARuleThatNeverExisted:
    def test_it_is_a_404_not_a_deletion(self, client):
        """The bug itself: this answered 200 for an id that was never a rule."""
        resp = client.delete(
            "/api/v1/proxy/mocks/e16d6dcc-be41-4597-8891-37f941641871",
            headers=AUTH,
        )

        assert resp.status_code == 404, (
            f"reported {resp.status_code} for an id that never existed: "
            f"{resp.text[:200]}"
        )

    def test_the_body_does_not_claim_a_deletion(self, client):
        """The status code is not the only thing that lied. The body asserted
        `"status": "deleted"`, which a caller logging the response repeats as
        fact."""
        resp = client.delete("/api/v1/proxy/mocks/never-a-rule", headers=AUTH)

        assert "deleted" not in resp.text, (
            "the response still asserts a deletion that did not happen"
        )

    def test_it_agrees_with_patch_on_the_same_id(self, client):
        """The disagreement is the tell. One unknown id must not be a 404 to
        one verb and a success to another."""
        rid = "e16d6dcc-be41-4597-8891-37f941641871"

        deleted = client.delete(f"/api/v1/proxy/mocks/{rid}", headers=AUTH)
        patched = client.patch(
            f"/api/v1/proxy/mocks/{rid}",
            json={"pattern": "~d other.com"},
            headers=AUTH,
        )

        assert deleted.status_code == patched.status_code == 404, (
            f"DELETE said {deleted.status_code}, PATCH said "
            f"{patched.status_code} — for the same unknown id"
        )
        # The status code is half the answer. The detail is what the agent
        # driving quern over MCP actually reads, and a bare 404 reads as a
        # routing miss rather than "no such rule" -- so the message is the
        # deliverable, not decoration. Asserting only the code passes against
        # a handler that drops the detail entirely.
        assert deleted.json()["detail"] == patched.json()["detail"] == (
            f"Mock rule not found: {rid}"
        ), (
            f"DELETE said {deleted.json().get('detail')!r}, PATCH said "
            f"{patched.json().get('detail')!r}"
        )


class TestDeletingARuleThatDoesExist:
    """The control. Without it, returning 404 unconditionally would pass every
    test above and break deletion entirely."""

    def test_a_real_rule_is_deleted(self, client, adapter):
        """Deletion still works. Returning 404 unconditionally would satisfy
        every assertion in the class above."""
        adapter._mock_rules.append(
            {"rule_id": "real-1", "pattern": PATTERN, "response": RESPONSE},
        )

        resp = client.delete("/api/v1/proxy/mocks/real-1", headers=AUTH)

        assert resp.status_code == 200, resp.text[:200]
        assert resp.json()["status"] == "deleted"

    def test_the_rule_is_actually_gone(self, client, adapter):
        """Asserting the status code alone would pass against a handler that
        answers 200 and removes nothing — which is the bug's own shape."""
        adapter._mock_rules.append(
            {"rule_id": "real-2", "pattern": PATTERN, "response": RESPONSE},
        )

        client.delete("/api/v1/proxy/mocks/real-2", headers=AUTH)

        assert not [r for r in adapter._mock_rules if r["rule_id"] == "real-2"]

    def test_deleting_it_twice_reports_the_second_as_not_found(
        self, client, adapter,
    ):
        """The sequence a teardown actually performs."""
        adapter._mock_rules.append(
            {"rule_id": "real-3", "pattern": PATTERN, "response": RESPONSE},
        )

        first = client.delete("/api/v1/proxy/mocks/real-3", headers=AUTH)
        second = client.delete("/api/v1/proxy/mocks/real-3", headers=AUTH)

        assert first.status_code == 200
        assert second.status_code == 404, (
            "a second delete of the same id still reported success"
        )


class TestClearingAllRules:
    """`DELETE /mocks` keeps its semantics deliberately: the caller asked for
    "none left", and none are left. Only the single-rule case can distinguish
    removed from never-there."""

    def test_clearing_an_empty_set_is_still_a_success(self, client):
        """Deliberately *not* symmetric with the single-rule case: the caller
        asked for "none left", and none are left."""
        resp = client.delete("/api/v1/proxy/mocks", headers=AUTH)

        assert resp.status_code == 200, resp.text[:200]
        assert resp.json()["count"] == 0

    def test_the_count_reflects_what_was_removed(self, client, adapter):
        """`count` is what keeps the empty case detectable without an error,
        so it has to be the real number rather than a placeholder."""
        for i in range(3):
            adapter._mock_rules.append(
                {"rule_id": f"r{i}", "pattern": PATTERN, "response": RESPONSE},
            )

        resp = client.delete("/api/v1/proxy/mocks", headers=AUTH)

        assert resp.json()["count"] == 3
        assert adapter._mock_rules == []


class TestTheAdapterReportsWhatItRemoved:
    """The mechanism. The endpoint can only be honest if `clear_mock` tells it
    what happened."""

    async def test_removing_a_known_rule_counts_one(self, adapter):
        """The count has to be a count, not a bool -- the endpoint's 404
        decision reads it directly."""
        adapter._mock_rules.append(
            {"rule_id": "x", "pattern": PATTERN, "response": RESPONSE},
        )

        assert await adapter.clear_mock(rule_id="x") == 1

    async def test_removing_an_unknown_rule_counts_zero(self, adapter):
        """Zero for a miss, and the rule that *does* exist is left alone --
        a filter on the wrong key would remove it and still return 0."""
        adapter._mock_rules.append(
            {"rule_id": "x", "pattern": PATTERN, "response": RESPONSE},
        )

        assert await adapter.clear_mock(rule_id="not-x") == 0
        assert len(adapter._mock_rules) == 1, "it removed the wrong rule"

    async def test_the_addon_is_still_told_even_when_nothing_matched(
        self, adapter,
    ):
        """The local list is the record, but the addon holds its own copy.
        Sending covers the drift case, and costs nothing when the addon has no
        such rule either."""
        await adapter.clear_mock(rule_id="not-here")

        adapter.send_command.assert_awaited_once_with(
            {"action": "clear_mock", "rule_id": "not-here"},
        )
