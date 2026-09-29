"""Every read path heals a wedged accessibility bridge, not just one (#337).

The recovery added for #66 was wired into `SimBridgeBackend.describe_all` and
nowhere else, so `describe_all_nested` and the whole of `IdbBackend` returned a
poisoned tree and said nothing. idb is not a corner: it is the fallback
whenever sim-bridge is unavailable (Xcode < 26, Intel), and #66 recorded the
wedge against raw `idb ui describe-all` directly.

The trees here are **real captured output**, not hand-written: a simulator
wedged by the XCUITest fixture, read with `idb ui describe-all`. A dict written
from the docstring would prove the detector matches the docstring.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from server.device import ax_recovery
from server.device.idb import IdbBackend
from server.device.sim_bridge import SimBridgeBackend, SimBridgeManager

FIXTURES = Path(__file__).parent / "fixtures" / "ax-wedge"
WEDGED = json.loads((FIXTURES / "wedged-idb-describe-all.json").read_text())
HEALTHY = json.loads((FIXTURES / "healthy-idb-describe-all.json").read_text())
#: A real `--nested` capture: one root Application carrying four children.
#: The flat healthy tree is the wrong control for a nested reader -- its 17
#: elements trip `len != 1` and the frame is never examined, so a nested test
#: fed it never exercises the discrimination those paths actually use
#: (review of #337).
HEALTHY_NESTED = json.loads(
    (Path(__file__).parent / "fixtures" / "idb_describe_all_nested_output.json").read_text()
)
UDID = "F5AF3736-C05F-493F-AA52-CA883B13B18C"


def test_the_captured_tree_is_the_one_the_detector_is_for():
    """Guards the fixture itself. If someone replaces it with a healthy
    capture the tests below would pass while testing nothing."""
    assert ax_recovery.looks_poisoned(WEDGED)
    assert not ax_recovery.looks_poisoned(HEALTHY)


def test_a_nested_root_with_children_is_never_a_wedge():
    """On a nested read `len != 1` can never fire -- the shape is always one
    root -- so without this the decision rests on two attributes of a single
    element, and a 0x0 unlabelled root over real content would get the bridge
    killed on a healthy screen."""
    assert len(HEALTHY_NESTED) == 1, "the fixture is not a nested capture"
    root = dict(HEALTHY_NESTED[0])
    root["frame"] = {"x": 0, "y": 0, "width": 0, "height": 0}
    root["AXLabel"] = None
    assert root["children"], "the fixture root has no children to protect"
    assert not ax_recovery.looks_poisoned([root])


def test_a_childless_zero_frame_root_is_still_a_wedge():
    """The hardening must not swallow the real signature."""
    root = dict(HEALTHY_NESTED[0])
    root["frame"] = {"x": 0, "y": 0, "width": 0, "height": 0}
    root["AXLabel"] = None
    root["children"] = []
    assert ax_recovery.looks_poisoned([root])


class TestTheRecoveryWatchesRatherThanWaits:
    async def test_a_healthy_tree_kills_nothing(self, monkeypatch):
        killed = []
        monkeypatch.setattr(ax_recovery, "reset_bridge",
                            lambda u: killed.append(u) or _true())
        out = await ax_recovery.reread_after_recovery(
            UDID, HEALTHY, lambda: _value(WEDGED))
        assert out is HEALTHY and killed == []

    async def test_it_keeps_reading_until_the_bridge_answers(self, monkeypatch):
        """The live defect. `reset_bridge` returns at ~0.07s while the bridge
        needs ~0.8-1.3s, so a single immediate re-read got the same poisoned
        tree and handed it back as the answer."""
        state = _respawning_pids(monkeypatch)

        async def kill(_udid):
            state["killed"] = True
            return True

        monkeypatch.setattr(ax_recovery, "reset_bridge", kill)
        seq = [WEDGED, WEDGED, HEALTHY]
        reads = []

        async def reread():
            reads.append(1)
            return seq.pop(0)

        out = await ax_recovery.reread_after_recovery(UDID, WEDGED, reread, budget=5.0)
        assert not ax_recovery.looks_poisoned(out)
        assert len(reads) == 3, "it stopped re-reading before the bridge answered"

    async def test_a_bridge_that_never_answers_gives_up_inside_the_budget(
        self, monkeypatch
    ):
        """Bounded, so a permanently wedged bridge cannot hang the caller --
        and the poisoned tree is the honest answer, not an exception."""
        state = _respawning_pids(monkeypatch)

        async def kill(_udid):
            state["killed"] = True
            return True

        monkeypatch.setattr(ax_recovery, "reset_bridge", kill)
        out = await ax_recovery.reread_after_recovery(
            UDID, WEDGED, lambda: _value(WEDGED), budget=0.3)
        assert ax_recovery.looks_poisoned(out)

    async def test_a_failed_reset_is_not_re_read(self, monkeypatch):
        monkeypatch.setattr(ax_recovery, "reset_bridge", lambda _u: _false())
        reads = []

        async def reread():
            reads.append(1)
            return HEALTHY

        out = await ax_recovery.reread_after_recovery(UDID, WEDGED, reread)
        assert out is WEDGED and reads == []


async def _value(v):
    return v


async def _bounded(coro, seconds=10.0):
    """Run `coro` under an independent deadline.

    The recursion-guard tests below assert that a *missing* guard is caught.
    Without a bound of their own they inherit the one under test: losing
    `_recovered=True` gives every recursion a fresh recovery budget, so the
    assertion is never reached and the test hangs instead of failing. As per
    the path instruction -- a test that blocks forever when the guard it
    checks is removed is not checking it (review of #337).
    """
    return await asyncio.wait_for(coro, timeout=seconds)


def _respawning_pids(monkeypatch):
    """`bridge_pids_for` as it really behaves: the old pid until the kill, a
    different one after. A constant would make the pid wait spin for the whole
    budget, which is not what the code under test does."""
    state = {"killed": False}

    async def pids(_udid):
        return [2] if state["killed"] else [1]

    monkeypatch.setattr(ax_recovery, "bridge_pids_for", pids)
    return state


async def _true():
    return True


async def _false():
    return False


def _idb_returning(monkeypatch, trees):
    """An IdbBackend whose subprocess yields `trees` in order."""
    backend = IdbBackend()
    seq = list(trees)

    async def fake_run(self, *args):
        # The last tree repeats. A fallback to HEALTHY would quietly end a
        # "stays wedged" scenario the moment the script ran out.
        return json.dumps(seq.pop(0) if len(seq) > 1 else seq[0]), ""

    monkeypatch.setattr(IdbBackend, "_run", fake_run)
    return backend, seq


class TestIdbHealsOnEveryReader:
    @pytest.mark.parametrize("method", ["describe_all", "describe_all_flat",
                                        "describe_all_nested"])
    async def test_a_wedged_read_is_retried_after_a_reset(self, monkeypatch, method):
        backend, _ = _idb_returning(monkeypatch, [WEDGED, HEALTHY])
        state = _respawning_pids(monkeypatch)
        resets = []

        async def kill(udid):
            resets.append(udid)
            state["killed"] = True
            return True

        monkeypatch.setattr(ax_recovery, "reset_bridge", kill)
        monkeypatch.setattr("server.device.probing.probe_container",
                            lambda *a, **k: _empty())

        out = await getattr(backend, method)(UDID)

        assert resets == [UDID], f"{method} never reset the bridge"
        assert not ax_recovery.looks_poisoned(out), f"{method} returned the wedge"
        assert len(out) > 1

    @pytest.mark.parametrize("method,healthy", [
        ("describe_all", HEALTHY), ("describe_all_flat", HEALTHY),
        ("describe_all_nested", HEALTHY_NESTED),
    ])
    async def test_a_healthy_read_resets_nothing(self, monkeypatch, method, healthy):
        backend, _ = _idb_returning(monkeypatch, [healthy])
        state = _respawning_pids(monkeypatch)
        resets = []

        async def kill(udid):
            resets.append(udid)
            state["killed"] = True
            return True

        monkeypatch.setattr(ax_recovery, "reset_bridge", kill)
        monkeypatch.setattr("server.device.probing.probe_container",
                            lambda *a, **k: _empty())

        await getattr(backend, method)(UDID)

        assert resets == []

    @pytest.mark.parametrize("method", ["describe_all", "describe_all_flat",
                                        "describe_all_nested"])
    async def test_a_bridge_that_stays_wedged_is_reset_once(self, monkeypatch, method):
        """The answer is still poisoned, and that is correct -- what matters is
        that it is returned rather than looped on."""
        monkeypatch.setattr(ax_recovery, "_RESPAWN_BUDGET", 0.3)
        backend, _ = _idb_returning(monkeypatch, [WEDGED])  # then WEDGED forever
        state = _respawning_pids(monkeypatch)
        resets = []

        async def kill(udid):
            resets.append(udid)
            state["killed"] = True
            return True

        monkeypatch.setattr(ax_recovery, "reset_bridge", kill)
        monkeypatch.setattr("server.device.probing.probe_container",
                            lambda *a, **k: _empty())

        out = await _bounded(getattr(backend, method)(UDID))

        assert len(resets) == 1
        assert ax_recovery.looks_poisoned(out)


async def _empty():
    return []


class TestSimBridgeNestedHealsToo:
    async def test_a_wedged_nested_read_is_retried(self, monkeypatch):
        backend = SimBridgeBackend(SimBridgeManager())
        seq = [WEDGED, HEALTHY_NESTED]

        async def fetch(self, _udid):
            return seq.pop(0)

        monkeypatch.setattr(SimBridgeBackend, "_fetch_nested", fetch)
        _respawning_pids(monkeypatch)
        resets = []
        monkeypatch.setattr(ax_recovery, "reset_bridge",
                            lambda u: resets.append(u) or _true())

        out = await backend.describe_all_nested(UDID)

        assert resets == [UDID]
        assert not ax_recovery.looks_poisoned(out)

    async def test_a_bridge_that_stays_wedged_is_reset_once(self, monkeypatch):
        """Finding 1 of the review: dropping `_recovered=True` from this
        retry left every test green while the call ran 401 fetches and 400
        SIGKILLs of CoreSimulatorBridge before a tripwire stopped it."""
        backend = SimBridgeBackend(SimBridgeManager())

        async def fetch(self, _udid):
            return WEDGED

        monkeypatch.setattr(SimBridgeBackend, "_fetch_nested", fetch)
        monkeypatch.setattr(ax_recovery, "_RESPAWN_BUDGET", 0.3)
        state = _respawning_pids(monkeypatch)
        resets = []

        async def kill(udid):
            resets.append(udid)
            state["killed"] = True   # or phase 1 waits out the budget
            return True

        monkeypatch.setattr(ax_recovery, "reset_bridge", kill)

        out = await _bounded(backend.describe_all_nested(UDID, _recovered=False))

        assert len(resets) == 1, f"reset {len(resets)} times, not once"
        assert ax_recovery.looks_poisoned(out)

    async def test_a_healthy_nested_read_resets_nothing(self, monkeypatch):
        backend = SimBridgeBackend(SimBridgeManager())

        async def fetch(self, _udid):
            return HEALTHY_NESTED

        monkeypatch.setattr(SimBridgeBackend, "_fetch_nested", fetch)
        resets = []
        monkeypatch.setattr(ax_recovery, "reset_bridge",
                            lambda u: resets.append(u) or _true())

        await backend.describe_all_nested(UDID)

        assert resets == []


class TestACancelledReadDoesNotLeakItsChild:
    """The recovery deadline cancels whatever it is awaiting, which these two
    subprocess helpers never had to survive before. A cancel that does not kill
    the child leaves a `pgrep`, `lsof` or `idb` running past the request that
    asked for it -- on the path that fires when a simulator is already unwell,
    and once per poll.

    Synchronised on the mock entering `communicate()` rather than on a sleep.
    The first version waited a fixed 0.3s and then cancelled, which races: a
    slow spawn means the cancel arrives before the process is recorded, and the
    test then fails without ever reaching the handler it exists to check
    (review of #343). It also spawned a real process, which the path
    instruction forbids -- and the contract being checked is "the handler kills
    and re-raises", which a mock establishes exactly.
    """

    @staticmethod
    def _fake_proc(entered, release):
        class FakeProc:
            returncode = None

            def __init__(self):
                self.killed = False
                self.waited = False

            async def communicate(self):
                entered.set()
                await release.wait()          # never, in these tests
                return b"", b""

            def kill(self):
                self.killed = True

            async def wait(self):
                self.waited = True
                return -9

        return FakeProc()

    async def _cancel_while_communicating(self, monkeypatch, module, call):
        """Start `call`, wait until the fake is inside `communicate()`, cancel."""
        entered, release = asyncio.Event(), asyncio.Event()
        proc = self._fake_proc(entered, release)

        async def fake_exec(*_a, **_k):
            return proc

        monkeypatch.setattr(module.asyncio, "create_subprocess_exec", fake_exec)
        task = asyncio.ensure_future(call())
        # Bounded: if the helper never reaches `communicate()` this fails here
        # rather than hanging, and the cancel below would have been racing.
        await asyncio.wait_for(entered.wait(), timeout=5)
        task.cancel()
        # Bounded. If a handler swallows the cancel instead of re-raising, the
        # fake's `communicate()` never returns -- `release` is never set -- and
        # an unbounded `await task` would hang here rather than failing. The
        # rule this test exists under applies to the test itself.
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        return proc

    async def test_ax_recovery_run_kills_and_reaps_on_cancel(self, monkeypatch):
        from server.device import ax_recovery as axr

        proc = await self._cancel_while_communicating(
            monkeypatch, axr, lambda: axr._run("/bin/sleep", "30", timeout=30),
        )
        assert proc.killed, "the cancelled child was never killed"
        assert proc.waited, "the killed child was never reaped"

    async def test_idb_run_kills_and_reaps_on_cancel(self, monkeypatch):
        from server.device import idb as idb_module

        backend = IdbBackend()
        monkeypatch.setattr(IdbBackend, "_resolve_binary", lambda self: "/bin/sleep")
        monkeypatch.setattr(IdbBackend, "_companion_path", lambda self: None)

        proc = await self._cancel_while_communicating(
            monkeypatch, idb_module, lambda: backend._run("ui", "describe-all"),
        )
        assert proc.killed, "the cancelled idb child was never killed"
        assert proc.waited, "the killed idb child was never reaped"
