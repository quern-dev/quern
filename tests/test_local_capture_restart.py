"""Changing local capture keeps the rules callers set (#562).

The process list is a launch argument, so `set_local_capture` restarts
mitmdump. `stop()` clears mocks, the intercept and bypass patterns -- right for
a stop someone asked for -- so every mock on the server used to stop applying
while the call returned 200 (measured on a live server: the next request went
upstream). `ProxyAdapter.restart()` re-sends them to the new process.

These drive the real `stop()`, with no process behind it; only the spawn in
`start()` and the stdin pipe in `send_command()` are faked.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.api import proxy as proxy_api
from server.sources import proxy as proxy_mod
from server.sources.proxy import ProxyAdapter

ALPHA = "32593887-01F3-47B8-AF34-B2D444DB37FA"


@pytest.fixture(autouse=True)
def _no_state_file(monkeypatch):
    # stop() records the proxy as stopped in ~/.quern/state.json.
    monkeypatch.setattr(proxy_mod, "update_state", lambda **kw: None)


def _adapter(*, starts: bool = True) -> ProxyAdapter:
    a = ProxyAdapter(local_capture_processes=["Safari"])
    a._running = True
    a.sent = []

    async def send(command):
        # Like the real one: nothing to write to without a running process.
        if a._running:
            a.sent.append(command)

    async def start():
        a.started_with = list(a.local_capture_processes)
        a._running = starts

    a.send_command = send
    a.start = start
    return a


async def _with_rules(a: ProxyAdapter) -> ProxyAdapter:
    await a.set_mock("~d api.example.com", {"status_code": 503}, rule_id="m1",
                     simulator_udid=ALPHA)
    await a.set_mock("~d cdn.example.com", {"status_code": 200}, rule_id="m2")
    await a.set_bypass(["~d apple.com"])
    await a.set_intercept("~d auth.example.com")
    a._held_flows["f1"] = {"id": "f1"}
    a.sent.clear()
    return a


@pytest.mark.asyncio
async def test_restart_resends_every_rule_to_the_new_process():
    a = await _with_rules(_adapter())

    result = await a.restart(local_capture_processes=["Safari", "Metatext"])

    assert a.started_with == ["Safari", "Metatext"]
    mocks = [c for c in a.sent if c["action"] == "set_mock"]
    assert [(m["rule_id"], m["pattern"], m["simulator_udid"]) for m in mocks] == [
        ("m1", "~d api.example.com", ALPHA),
        ("m2", "~d cdn.example.com", None),
    ]
    assert mocks[0]["response"] == {"status_code": 503}
    assert {"action": "set_bypass", "patterns": ["~d apple.com"]} in a.sent
    assert {"action": "set_intercept", "pattern": "~d auth.example.com"} in a.sent
    # The server's own view agrees with what mitmdump was sent.
    assert [r["rule_id"] for r in a._mock_rules] == ["m1", "m2"]
    assert a.get_bypass_patterns() == ["~d apple.com"]
    assert a._intercept_pattern == "~d auth.example.com"
    assert result == {
        "held_flows_dropped": 1,
        "restored": {"mock_rules": 2, "bypass_patterns": 1,
                     "intercept": "~d auth.example.com"},
    }


@pytest.mark.asyncio
async def test_restart_with_no_rules_sends_nothing():
    a = _adapter()
    result = await a.restart(local_capture_processes=["Safari"])
    assert a.sent == []
    assert result == {
        "held_flows_dropped": 0,
        "restored": {"mock_rules": 0, "bypass_patterns": 0, "intercept": None},
    }


@pytest.mark.asyncio
async def test_a_failed_start_keeps_the_rules_for_the_next_one():
    a = await _with_rules(_adapter(starts=False))

    result = await a.restart(local_capture_processes=["Metatext"])

    assert result == {"held_flows_dropped": 1, "restored": None}
    assert [r["rule_id"] for r in a._mock_rules] == ["m1", "m2"]
    assert a.get_bypass_patterns() == ["~d apple.com"]
    assert a._intercept_pattern == "~d auth.example.com"


@pytest.mark.asyncio
async def test_a_mock_set_during_the_restart_reaches_the_new_process():
    """Without the lock it lands between stop and start: either cleared by the
    stop or recorded with no process to send it to."""
    a = _adapter()
    real_start = a.start
    gate = asyncio.Event()

    async def slow_start():
        await gate.wait()
        await real_start()

    a.start = slow_start
    restart = asyncio.create_task(a.restart(local_capture_processes=["Metatext"]))
    await asyncio.sleep(0)  # restart is now inside start()
    mock = asyncio.create_task(
        a.set_mock("~d late.example.com", {"status_code": 418}, rule_id="late"))
    await asyncio.sleep(0)
    assert not mock.done()  # waiting its turn, not racing the restart
    gate.set()
    await asyncio.gather(restart, mock)

    assert [r["rule_id"] for r in a._mock_rules] == ["late"]
    assert [c["rule_id"] for c in a.sent if c["action"] == "set_mock"] == ["late"]


@pytest.mark.asyncio
async def test_an_explicit_stop_still_clears_the_rules():
    a = await _with_rules(_adapter())
    await a.stop()
    assert a._mock_rules == []
    assert a.get_bypass_patterns() == []
    assert a._intercept_pattern is None


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


def _request(adapter):
    req = MagicMock()
    req.app.state.proxy_adapter = adapter
    req.app.state.local_capture_processes = ["Safari"]
    return req


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [True, False])
async def test_set_local_capture_restarts_through_the_adapter(monkeypatch, running):
    a = await _with_rules(_adapter())
    a._running = running
    monkeypatch.setattr(proxy_api, "_ensure_ca_is_trusted", AsyncMock())
    monkeypatch.setattr(proxy_api, "update_state", lambda **kw: None)
    monkeypatch.setattr(proxy_api, "_get_proxy_status",
                        AsyncMock(return_value=proxy_api.ProxyStatusResponse(
                            status="running" if running else "stopped")))
    body = proxy_api.LocalCaptureRequest(processes=["Metatext"], only=True)

    with patch("server.config.set_local_capture_processes"):
        status = await proxy_api.set_local_capture.__wrapped__(_request(a), body)

    if running:
        assert status.restart is not None
        assert status.restart.connections_reset is True
        assert status.restart.held_flows_dropped == 1
        assert status.restart.restored["mock_rules"] == 2
        assert [r["rule_id"] for r in a._mock_rules] == ["m1", "m2"]
    else:
        # Nothing to restart: the list waits for the next start.
        assert status.restart is None
        assert a.local_capture_processes == ["Metatext"]
