"""Tests for sim-bridge's admission bound (#68).

sim-bridge serialises every command through one lock, and uvicorn does not
cancel a handler when the HTTP client disconnects — verified empirically: a
request abandoned at 0.4s still completed its full 1896ms of work server-side.
So an abandoned request keeps its slot and runs to completion, and without a
bound a retry loop becomes a multi-minute outage in which the device looks hung
while returning correct results minutes late.

The bound is on concurrent *operations*, not commands. A first attempt bounded
commands and was wrong: one describe_all fans out into a describe-ui plus one
probe-point per empty container via asyncio.gather, so a single legitimate
request filled an 8-command queue by itself and then rejected its own
continuation work. Live reproduction showed the queue pinned at the limit with
nothing completing.

The lock itself stays and must: the wire protocol carries no request IDs
(`sim-bridge.swift` reads with `while let line = readLine()` and replies via
`respond()` with no correlation; `_dispatch` resolves a single
`_pending_response` future with whatever arrives). Two in-flight commands would
resolve each other's futures.
"""

from __future__ import annotations

import asyncio

import pytest

from server.device.sim_bridge import (
    MAX_CONCURRENT_OPERATIONS,
    SimBridgeBackend,
    SimBridgeManager,
)
from server.models import SimBridgeSaturatedError


@pytest.fixture
def mgr() -> SimBridgeManager:
    return SimBridgeManager()


@pytest.fixture
def backend(mgr) -> SimBridgeBackend:
    return SimBridgeBackend(mgr)


def _block(monkeypatch, backend) -> asyncio.Event:
    """Make the underlying command block until the returned event is set."""
    release = asyncio.Event()

    async def blocked(_cmd):
        await release.wait()
        return {"ok": True}

    monkeypatch.setattr(backend, "_send_admitted", blocked)
    return release


async def test_rejects_once_the_operation_limit_is_reached(backend, mgr, monkeypatch):
    release = _block(monkeypatch, backend)
    inflight = [asyncio.create_task(backend._send({"cmd": "tap"}))
                for _ in range(MAX_CONCURRENT_OPERATIONS)]
    await asyncio.sleep(0.05)
    assert mgr._operations == MAX_CONCURRENT_OPERATIONS

    with pytest.raises(SimBridgeSaturatedError) as exc:
        # wait_for so a missing guard fails the test instead of hanging on the
        # blocked fixture — the first version of this suite hung for minutes.
        await asyncio.wait_for(backend._send({"cmd": "tap"}), timeout=2)
    assert exc.value.tool == "sim-bridge"

    release.set()
    await asyncio.gather(*inflight)


async def test_a_single_operation_may_fan_out_past_the_limit(backend, mgr):
    """The regression that broke the first design.

    One describe_all issues a describe-ui plus one probe-point per empty
    container, concurrently. Those are continuation work, not new requests, and
    must not be rejected however many there are.
    """
    fanout = MAX_CONCURRENT_OPERATIONS * 5
    entered = asyncio.Event()
    sent = []

    async def record(cmd):
        sent.append(cmd["cmd"])
        # Hold until every follow-on command has entered, so they genuinely
        # overlap. With an instant mock they never coexist and the test cannot
        # detect a broken re-entrancy guard.
        if len(sent) >= fanout:
            entered.set()
        await entered.wait()
        return {"ok": True, "elements": []}

    backend._send_admitted = record

    async with mgr.admit():
        await asyncio.wait_for(
            asyncio.gather(*[
                backend._send({"cmd": "probe-point"}) for _ in range(fanout)
            ]),
            timeout=5,
        )

    assert len(sent) == fanout
    assert mgr._operations == 0


async def test_admission_is_reentrant_and_restores_state(mgr):
    assert mgr._operations == 0
    async with mgr.admit():
        assert mgr._operations == 1
        async with mgr.admit():       # nested: not re-admitted
            assert mgr._operations == 1
        assert mgr._operations == 1
    assert mgr._operations == 0


async def test_the_message_says_to_stop_retrying(backend, mgr, monkeypatch):
    """The symptom misdirects, so the error has to carry the diagnosis."""
    release = _block(monkeypatch, backend)
    inflight = [asyncio.create_task(backend._send({"cmd": "tap"}))
                for _ in range(MAX_CONCURRENT_OPERATIONS)]
    await asyncio.sleep(0.05)

    with pytest.raises(SimBridgeSaturatedError) as exc:
        await asyncio.wait_for(backend._send({"cmd": "tap"}), timeout=2)

    msg = str(exc.value).lower()
    assert "saturated" in msg
    assert "retrying" in msg
    assert "responsive" in msg      # must not read as a device failure
    # The budget is server-wide, not per-device; the message must say so.
    assert "shared across all booted" in msg

    release.set()
    await asyncio.gather(*inflight)


async def test_saturation_is_transient(backend, mgr, monkeypatch):
    release = _block(monkeypatch, backend)
    inflight = [asyncio.create_task(backend._send({"cmd": "tap"}))
                for _ in range(MAX_CONCURRENT_OPERATIONS)]
    await asyncio.sleep(0.05)
    with pytest.raises(SimBridgeSaturatedError):
        await asyncio.wait_for(backend._send({"cmd": "tap"}), timeout=2)

    release.set()
    await asyncio.gather(*inflight)
    assert mgr._operations == 0

    async def ok(_cmd):
        return {"ok": True}
    monkeypatch.setattr(backend, "_send_admitted", ok)
    assert await backend._send({"cmd": "tap"}) == {"ok": True}


async def test_slot_is_released_when_the_operation_raises(backend, mgr, monkeypatch):
    """A failing operation must not leak a slot — enough failures would
    otherwise wedge the bridge permanently."""
    async def boom(_cmd):
        raise RuntimeError("sim-bridge: exploded")

    monkeypatch.setattr(backend, "_send_admitted", boom)
    for _ in range(MAX_CONCURRENT_OPERATIONS + 3):
        with pytest.raises(RuntimeError):
            await backend._send({"cmd": "tap"})

    assert mgr._operations == 0, "operation slots leaked on the error path"


async def test_the_budget_is_shared_across_devices_by_design(backend, mgr, monkeypatch):
    """Regression for the review finding on #69.

    There is one SimBridgeManager driving one subprocess behind one lock, with
    `udid` carried per command — so the budget is server-wide and operations on
    different simulators contend. That is deliberate: a per-UDID budget would be
    N times larger against the same single serialised subprocess, which is the
    pile-up the bound exists to prevent.

    This pins the behaviour so a future per-UDID refactor has to be a decision
    rather than an accident.
    """
    release = _block(monkeypatch, backend)
    inflight = [
        asyncio.create_task(backend._send({"cmd": "tap", "udid": f"SIM-{i}"}))
        for i in range(MAX_CONCURRENT_OPERATIONS)
    ]
    await asyncio.sleep(0.05)

    # A different simulator entirely — still refused, because the subprocess is shared.
    with pytest.raises(SimBridgeSaturatedError):
        await asyncio.wait_for(backend._send({"cmd": "tap", "udid": "SIM-OTHER"}), timeout=2)

    release.set()
    await asyncio.gather(*inflight)


async def test_normal_sequential_traffic_is_unaffected(backend, mgr, monkeypatch):
    calls = []

    async def record(cmd):
        calls.append(cmd["cmd"])
        return {"ok": True}

    monkeypatch.setattr(backend, "_send_admitted", record)
    for _ in range(MAX_CONCURRENT_OPERATIONS * 4):
        assert await backend._send({"cmd": "tap"}) == {"ok": True}

    assert len(calls) == MAX_CONCURRENT_OPERATIONS * 4
    assert mgr._operations == 0
