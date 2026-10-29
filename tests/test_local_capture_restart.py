"""Changing local capture keeps the rules callers set (#562).

The process list is a launch argument, so `set_local_capture` restarts
mitmdump. `stop()` clears mocks, the intercept and bypass patterns -- right for
a stop someone asked for -- so every mock on the server used to stop applying
while the call returned 200 (measured on a live server: the next request went
upstream). `ProxyAdapter.restart()` re-sends them to the new process.

These drive the real `start()`, `stop()` and `restart()`, with no process
behind them; only the spawn (`_spawn()`) and the stdin pipe (`send_command()`)
are faked.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.api import proxy as proxy_api
from server.sources import proxy as proxy_mod
from server.sources.proxy import ProxyAdapter

ALPHA = "32593887-01F3-47B8-AF34-B2D444DB37FA"
#: What the addon sends back for each command a restart re-sends.
ECHOES = {"set_mock": "mock_set", "set_bypass": "bypass_updated",
          "set_intercept": "intercept_set"}


async def _echo(a: ProxyAdapter, command: dict, *, skip: str | None = None) -> None:
    """What the addon writes back for `command`, carrying its fields as the
    addon's echoes do (the adapter takes the intercept pattern from it)."""
    event = ECHOES.get(command["action"])
    if event and event != skip:
        fields = {k: v for k, v in command.items() if k != "action"}
        await a._handle_status_event({"event": event, **fields})


@pytest.fixture(autouse=True)
def _no_state_file(monkeypatch):
    # stop() records the proxy as stopped in ~/.quern/state.json.
    monkeypatch.setattr(proxy_mod, "update_state", lambda **kw: None)


def _adapter(*, starts: bool = True) -> ProxyAdapter:
    a = ProxyAdapter(local_capture_processes=["Safari"])
    a._running = True
    a.sent = []
    a.spawns = []
    a.fail_send_at = None  # index in `sent` at which a send breaks
    a.addon_runs = True    # sends `running`, as an addon older than #562 does not
    a.echoes = True        # echoes each rule, as the addon does
    a.restart_confirm_timeout = 0.3

    async def send(command):
        # Like the real one: nothing to write to without a running process.
        if not a._running:
            return
        if a.fail_send_at is not None and len(a.sent) == a.fail_send_at:
            raise BrokenPipeError("mitmdump went away")
        a.sent.append(command)
        if a.echoes:
            await _echo(a, command)

    async def spawn():
        a.spawns.append(list(a.local_capture_processes))
        a._running = starts
        if starts and a.addon_runs:
            await a._handle_status_event({"event": "running"})
        return starts

    a.send_command = send
    a._spawn = spawn
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

    assert a.spawns == [["Safari", "Metatext"]]
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
        "held_flows_released": 1,
        "restored": {"mock_rules": 2, "bypass_patterns": 1,
                     "intercept": "~d auth.example.com"},
        "confirmed": True,
    }


@pytest.mark.asyncio
async def test_restart_with_no_rules_sends_nothing():
    a = _adapter()
    result = await a.restart(local_capture_processes=["Safari"])
    assert a.sent == []
    assert result == {
        "held_flows_released": 0,
        "restored": {"mock_rules": 0, "bypass_patterns": 0, "intercept": None},
        "confirmed": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["running", "mock_set", "bypass_updated",
                                     "intercept_set"])
async def test_a_restart_is_unconfirmed_until_the_addon_says_so(missing):
    """Returning on the stdin write let the caller's next request beat the new
    process: it is not listening for about 0.4 s, and a request then goes
    upstream, unmocked. Each thing waited for is needed on its own."""
    a = await _with_rules(_adapter())
    if missing == "running":
        a.addon_runs = False
    else:
        a.echoes = False
        real_send = a.send_command

        async def send(command):
            await real_send(command)
            await _echo(a, command, skip=missing)

        a.send_command = send

    result = await asyncio.wait_for(
        a.restart(local_capture_processes=["Metatext"]), timeout=2)

    assert result["confirmed"] is False
    assert result["restored"]["mock_rules"] == 2  # sent, just not confirmed


@pytest.mark.asyncio
async def test_a_proxy_that_exits_after_echoing_is_unconfirmed():
    """Every event arrived, then mitmdump exited and the read loop cleared
    `_running`: confirming that would report a stopped proxy as serving."""
    a = await _with_rules(_adapter())
    real_send = a.send_command

    async def send(command):
        await real_send(command)
        if command["action"] == "set_intercept":  # the last one sent
            a._running = False

    a.send_command = send
    result = await asyncio.wait_for(
        a.restart(local_capture_processes=["Metatext"]), timeout=2)
    assert result["confirmed"] is False


@pytest.mark.asyncio
async def test_a_proxy_whose_process_exited_is_unconfirmed_before_the_loop_notices():
    """The read loop is still working through queued events, so `_running` is
    still True; the subprocess has a return code already."""
    a = await _with_rules(_adapter())
    real_send = a.send_command

    async def send(command):
        await real_send(command)
        if command["action"] == "set_intercept":  # the last one sent
            a._process = SimpleNamespace(returncode=1)

    a.send_command = send
    result = await asyncio.wait_for(
        a.restart(local_capture_processes=["Metatext"]), timeout=2)
    assert a.is_running
    assert result["confirmed"] is False


@pytest.mark.asyncio
async def test_a_mock_echo_short_of_the_count_is_unconfirmed():
    a = await _with_rules(_adapter())
    a.echoes = False
    real_send = a.send_command
    echoed = []

    async def send(command):
        await real_send(command)
        if command["action"] != "set_mock" or not echoed:
            echoed.append(command)  # every rule but the second mock
            await _echo(a, command)

    a.send_command = send
    result = await asyncio.wait_for(
        a.restart(local_capture_processes=["Metatext"]), timeout=2)
    assert result["confirmed"] is False


@pytest.mark.asyncio
async def test_a_failed_start_keeps_the_rules_for_the_next_one():
    a = await _with_rules(_adapter(starts=False))

    result = await a.restart(local_capture_processes=["Metatext"])

    assert result == {"held_flows_released": 1, "restored": None, "confirmed": False}
    assert [r["rule_id"] for r in a._mock_rules] == ["m1", "m2"]
    assert a.get_bypass_patterns() == ["~d apple.com"]
    assert a._intercept_pattern == "~d auth.example.com"


@pytest.mark.asyncio
async def test_a_mock_set_during_the_restart_reaches_the_new_process():
    """Without the lock it lands between stop and start: either cleared by the
    stop or recorded with no process to send it to."""
    a = _adapter()
    real_spawn = a._spawn
    gate, spawning = asyncio.Event(), asyncio.Event()

    async def slow_spawn():
        spawning.set()
        await asyncio.wait_for(gate.wait(), timeout=1)
        return await real_spawn()

    a._spawn = slow_spawn
    restart = asyncio.create_task(a.restart(local_capture_processes=["Metatext"]))
    await asyncio.wait_for(spawning.wait(), timeout=1)  # stopped, not yet started
    mock = asyncio.create_task(
        a.set_mock("~d late.example.com", {"status_code": 418}, rule_id="late"))
    await asyncio.sleep(0)
    assert not mock.done()  # waiting its turn, not racing the restart
    gate.set()
    await asyncio.wait_for(asyncio.gather(restart, mock), timeout=2)

    assert [r["rule_id"] for r in a._mock_rules] == ["late"]
    assert [c["rule_id"] for c in a.sent if c["action"] == "set_mock"] == ["late"]


@pytest.mark.asyncio
async def test_a_start_after_a_failed_restart_sends_the_kept_rules():
    """The rules a failed restart kept reach the next process -- listed but
    never sent would be the #562 symptom one call later."""
    a = await _with_rules(_adapter(starts=False))
    await a.restart(local_capture_processes=["Metatext"])
    a._spawn = AsyncMock(side_effect=lambda: setattr(a, "_running", True) or True)

    await a.start()

    assert [c["rule_id"] for c in a.sent if c["action"] == "set_mock"] == ["m1", "m2"]
    assert {"action": "set_bypass", "patterns": ["~d apple.com"]} in a.sent
    assert {"action": "set_intercept", "pattern": "~d auth.example.com"} in a.sent


@pytest.mark.asyncio
async def test_a_send_that_fails_keeps_every_rule():
    a = await _with_rules(_adapter())
    a.fail_send_at = 1

    result = await a.restart(local_capture_processes=["Metatext"])

    assert result["restored"] is None
    # Stopped, so the next start is not turned away and sends them all.
    assert not a.is_running
    assert [r["rule_id"] for r in a._mock_rules] == ["m1", "m2"]
    assert a.get_bypass_patterns() == ["~d apple.com"]
    assert a._intercept_pattern == "~d auth.example.com"


@pytest.mark.asyncio
async def test_a_stop_during_the_restart_waits_for_it():
    """Without the lock a stop landing while the rules are re-sent clears them,
    and the restart goes on sending to a process that is gone."""
    a = await _with_rules(_adapter())
    gate, sending = asyncio.Event(), asyncio.Event()
    real_send = a.send_command

    async def slow_send(command):
        sending.set()
        await asyncio.wait_for(gate.wait(), timeout=1)
        await real_send(command)

    a.send_command = slow_send
    restart = asyncio.create_task(a.restart(local_capture_processes=["Metatext"]))
    await asyncio.wait_for(sending.wait(), timeout=1)  # blocked re-sending
    stop = asyncio.create_task(a.stop())
    await asyncio.sleep(0)
    assert not stop.done()
    gate.set()
    result = await asyncio.wait_for(restart, timeout=2)
    await asyncio.wait_for(stop, timeout=2)

    assert result["restored"]["mock_rules"] == 2
    assert len([c for c in a.sent if c["action"] == "set_mock"]) == 2
    assert not a.is_running
    assert a._mock_rules == []


@pytest.mark.asyncio
async def test_a_start_during_the_restart_does_not_spawn_a_second_proxy():
    """The restart has stopped the proxy, so `is_running` is False and
    start_proxy's own check passes. Without the lock both spawn, and the
    second kills the first (`_kill_stale_mitmdump`)."""
    a = await _with_rules(_adapter())
    real_spawn = a._spawn
    gate, spawning = asyncio.Event(), asyncio.Event()

    async def slow_spawn():
        spawning.set()
        await asyncio.wait_for(gate.wait(), timeout=1)
        return await real_spawn()

    a._spawn = slow_spawn
    restart = asyncio.create_task(a.restart(local_capture_processes=["Metatext"]))
    await asyncio.wait_for(spawning.wait(), timeout=1)
    start = asyncio.create_task(a.start())
    await asyncio.sleep(0)
    gate.set()
    await asyncio.wait_for(asyncio.gather(restart, start), timeout=2)

    assert a.spawns == [["Metatext"]]
    assert len([c for c in a.sent if c["action"] == "set_mock"]) == 2


@pytest.mark.asyncio
async def test_a_restart_queued_behind_a_stop_does_not_undo_it():
    a = await _with_rules(_adapter())
    await a.stop()

    assert await a.restart(local_capture_processes=["Metatext"]) is None
    assert a.spawns == []
    assert not a.is_running
    assert a.local_capture_processes == ["Metatext"]


@pytest.mark.asyncio
async def test_start_does_not_respawn_a_running_proxy():
    """A second spawn kills the running one, and a start_proxy that slipped in
    during a restart would replace the run the restart just made."""
    a = await _with_rules(_adapter())
    await a.start()
    assert a.spawns == []
    assert a.sent == []


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
        assert status.restart.held_flows_released == 1
        assert status.restart.confirmed is True
        assert status.restart.restored["mock_rules"] == 2
        assert [r["rule_id"] for r in a._mock_rules] == ["m1", "m2"]
    else:
        # Nothing to restart: the list waits for the next start.
        assert status.restart is None
        assert a.local_capture_processes == ["Metatext"]


@pytest.mark.asyncio
async def test_set_local_capture_during_a_start_applies_the_new_list(monkeypatch):
    """A start still spawning has built its command from the old list. Deciding
    on `is_running` outside the lock saw "stopped", only reconfigured, and the
    proxy came up capturing the old list."""
    a = _adapter()
    a._running = False
    gate, spawning = asyncio.Event(), asyncio.Event()
    launched: list[list[str]] = []

    async def slow_spawn():
        launched.append(list(a.local_capture_processes))  # the command is built
        spawning.set()
        await asyncio.wait_for(gate.wait(), timeout=1)
        a._running = True
        return True

    a._spawn = slow_spawn
    monkeypatch.setattr(proxy_api, "_ensure_ca_is_trusted", AsyncMock())
    monkeypatch.setattr(proxy_api, "update_state", lambda **kw: None)
    monkeypatch.setattr(proxy_api, "_get_proxy_status",
                        AsyncMock(return_value=proxy_api.ProxyStatusResponse(status="running")))
    body = proxy_api.LocalCaptureRequest(processes=["Metatext"], only=True)

    start = asyncio.create_task(a.start())
    await asyncio.wait_for(spawning.wait(), timeout=1)
    with patch("server.config.set_local_capture_processes"):
        change = asyncio.create_task(
            proxy_api.set_local_capture.__wrapped__(_request(a), body))
        await asyncio.sleep(0)
        gate.set()
        await asyncio.wait_for(asyncio.gather(start, change), timeout=2)

    assert launched[-1] == ["Metatext"]


def test_the_addon_says_running_once_its_servers_are_up(monkeypatch):
    """`started` comes from `load()`, before mitmproxy listens; a restart waits
    for `running`, from mitmproxy's `running()` hook."""
    from server.proxy import addon as addon_mod

    written: list[dict] = []
    monkeypatch.setattr(addon_mod, "_write_json", written.append)
    addon = addon_mod.IOSDebugAddon.__new__(addon_mod.IOSDebugAddon)

    addon.running()

    assert [w["event"] for w in written] == ["running"]
