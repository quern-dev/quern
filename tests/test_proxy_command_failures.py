"""A proxy quern cannot command does not go on serving stale rules (#656).

The addon reads commands from stdin, and its reader can end while mitmdump
keeps running, applying the last rules it had. A failed send left that proxy
serving: a removal never reached it, and a create whose send failed was kept
in quern's record with no owner. Now the main proxy is restarted from the
record, a session proxy is stopped and checked to have exited, a failed
create is taken back out of the record, and the followers are sent a command
even when the main proxy could not take it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from server.proxy.flow_store import FlowStore
from server.sources.proxy import ProxyAdapter


def _proxies(leader_fails=False, follower_fails=False):
    leader = ProxyAdapter(flow_store=FlowStore(), listen_port=9101)
    leader._running = True
    leader.send_command = AsyncMock(
        side_effect=BrokenPipeError("stdin gone") if leader_fails else None)
    leader.restart = AsyncMock()
    follower = ProxyAdapter(flow_store=FlowStore(), listen_port=9102)
    follower.leader = leader
    follower.send_command = AsyncMock(
        side_effect=BrokenPipeError("stdin gone") if follower_fails else None)
    follower.stop = AsyncMock()
    leader.followers = [follower]
    return leader, follower


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


class TestTheMainProxy:
    async def test_a_failed_command_still_reaches_the_followers_and_raises(self):
        leader, follower = _proxies(leader_fails=True)
        with pytest.raises(BrokenPipeError):
            await leader.broadcast({"action": "clear_mock", "rule_id": "m1"})
        follower.send_command.assert_awaited_once_with({"action": "clear_mock", "rule_id": "m1"})

    async def test_a_failed_command_restarts_it_from_the_record(self):
        leader, _ = _proxies(leader_fails=True)
        with pytest.raises(BrokenPipeError):
            await leader.broadcast({"action": "clear_mock", "rule_id": "m1"})
        await _settle()
        leader.restart.assert_awaited_once()

    async def test_a_burst_of_failures_restarts_it_once(self):
        leader, _ = _proxies(leader_fails=True)
        gate = asyncio.Event()

        async def held_restart():
            await gate.wait()             # still restarting when the next ones fail

        leader.restart = AsyncMock(side_effect=held_restart)
        try:
            for _ in range(3):
                with pytest.raises(BrokenPipeError):
                    await leader.broadcast({"action": "clear_mock", "rule_id": "m1"})
            await _settle()
            assert leader.restart.await_count == 1
        finally:
            gate.set()                        # never left waiting, pass or fail
            await asyncio.wait_for(leader._restart_task, 2)

    async def test_a_stopped_proxy_is_not_restarted(self):
        """Not running: a stop someone asked for is not undone."""
        leader, _ = _proxies(leader_fails=True)
        leader._running = False
        with pytest.raises(BrokenPipeError):
            await leader.broadcast({"action": "clear_mock", "rule_id": "m1"})
        await _settle()
        leader.restart.assert_not_awaited()

    @pytest.mark.parametrize("kind", ["mock", "intercept"])
    async def test_a_create_it_refuses_is_taken_back_everywhere(self, kind):
        leader, follower = _proxies(leader_fails=True)
        with pytest.raises(BrokenPipeError):
            if kind == "mock":
                await leader.set_mock("~d x.test", {"status_code": 418})
            else:
                await leader.set_intercept("~d x.test")
        assert leader._mock_rules == [] and leader.intercept_rules() == []
        sent = [c.args[0]["action"] for c in follower.send_command.await_args_list]
        assert sent == [f"set_{kind}", f"clear_{kind}"]


class _Process:
    """A mitmdump stand-in: `ignores` lists the signals it shrugs off."""

    def __init__(self, ignores=()):
        self.pid, self.returncode, self.ignores, self.signals = 4242, None, set(ignores), []
        self.stdin = None

    def _signal(self, name):
        self.signals.append(name)
        if name not in self.ignores:
            self.returncode = -9 if name == "kill" else -15

    def terminate(self):
        self._signal("terminate")

    def kill(self):
        self._signal("kill")

    async def wait(self):
        return self.returncode


def _wired_as_session_capture_wires_it(follower_process):
    """A follower sharing the main proxy's rules lock, as SessionProxies._start
    makes one -- what the first version's own-lock test double hid (review)."""
    leader, follower = _proxies(follower_fails=True)
    follower.stop = ProxyAdapter.stop.__get__(follower)          # the real one
    follower._rules_lock = leader._rules_lock
    follower._running = True
    follower._process = follower_process
    return leader, follower


class TestASessionProxy:
    async def test_a_rule_change_it_fails_does_not_hang(self):
        leader, follower = _wired_as_session_capture_wires_it(_Process())
        await asyncio.wait_for(leader.set_mock("~d x.test", {"status_code": 418}), 3)
        assert follower._running is False

    async def test_one_that_cannot_take_a_command_is_stopped(self):
        process = _Process()
        leader, _ = _wired_as_session_capture_wires_it(process)
        await asyncio.wait_for(leader.broadcast({"action": "clear_mock", "rule_id": "m1"}), 3)
        assert process.returncode is not None

    async def test_one_whose_process_shrugs_off_the_stop_is_killed_again(self):
        """The stop clears _process; checked after it, this never ran (review)."""
        process = _Process(ignores={"terminate"})
        leader, _ = _wired_as_session_capture_wires_it(process)
        await asyncio.wait_for(leader.broadcast({"action": "clear_mock", "rule_id": "m1"}), 10)
        assert process.returncode == -9 and "kill" in process.signals


class TestAFailedUpdate:
    async def test_the_record_goes_back_to_the_rule_as_it_was(self):
        leader, follower = _proxies()
        await leader.set_mock("~d x.test", {"status_code": 418})
        rule_id = leader._mock_rules[0]["rule_id"]
        leader.send_command = AsyncMock(side_effect=BrokenPipeError("gone"))
        with pytest.raises(BrokenPipeError):
            await leader.update_mock(rule_id=rule_id, pattern="~d y.test")
        assert leader._mock_rules[0]["pattern"] == "~d x.test"
        last = follower.send_command.await_args_list[-1].args[0]
        assert last["action"] == "set_mock" and last["pattern"] == "~d x.test"
