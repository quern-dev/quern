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
        leader.restart = AsyncMock(side_effect=lambda: gate.wait())
        for _ in range(3):
            with pytest.raises(BrokenPipeError):
                await leader.broadcast({"action": "clear_mock", "rule_id": "m1"})
        await _settle()
        assert leader.restart.await_count == 1
        gate.set()
        await _settle()

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


class TestASessionProxy:
    async def test_one_that_cannot_take_a_command_is_stopped(self):
        leader, follower = _proxies(follower_fails=True)
        await leader.broadcast({"action": "clear_mock", "rule_id": "m1"})
        follower.stop.assert_awaited_once()

    @pytest.mark.parametrize("stop_raises", [False, True])
    async def test_one_whose_process_outlives_the_stop_is_killed(self, stop_raises):
        """The stop mocked out above says nothing about the process: here the
        process is still running after it, and is killed (review)."""
        leader, follower = _proxies(follower_fails=True)
        if stop_raises:
            follower.stop = AsyncMock(side_effect=RuntimeError("stop failed"))

        class Process:
            pid, returncode = 4242, None
            signals: list[str] = []

            def kill(self):
                self.signals.append("kill")
                self.returncode = -9

            terminate = kill

            async def wait(self):
                return self.returncode

        follower._process = Process()
        await leader.broadcast({"action": "clear_mock", "rule_id": "m1"})
        assert follower._process.returncode == -9
