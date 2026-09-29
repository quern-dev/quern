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

import json
from pathlib import Path

import pytest

from server.device import ax_recovery
from server.device.idb import IdbBackend
from server.device.sim_bridge import SimBridgeBackend, SimBridgeManager

FIXTURES = Path(__file__).parent / "fixtures" / "ax-wedge"
WEDGED = json.loads((FIXTURES / "wedged-idb-describe-all.json").read_text())
HEALTHY = json.loads((FIXTURES / "healthy-idb-describe-all.json").read_text())
UDID = "F5AF3736-C05F-493F-AA52-CA883B13B18C"


def test_the_captured_tree_is_the_one_the_detector_is_for():
    """Guards the fixture itself. If someone replaces it with a healthy
    capture the tests below would pass while testing nothing."""
    assert ax_recovery.looks_poisoned(WEDGED)
    assert not ax_recovery.looks_poisoned(HEALTHY)


class TestTheDecisionIsSharedAndGuarded:
    async def test_a_poisoned_tree_resets_the_bridge(self, monkeypatch):
        killed = []
        monkeypatch.setattr(ax_recovery, "reset_bridge",
                            lambda u: killed.append(u) or _true())
        assert await ax_recovery.should_retry(UDID, WEDGED, False) is True
        assert killed == [UDID]

    async def test_a_healthy_tree_kills_nothing(self, monkeypatch):
        killed = []
        monkeypatch.setattr(ax_recovery, "reset_bridge",
                            lambda u: killed.append(u) or _true())
        assert await ax_recovery.should_retry(UDID, HEALTHY, False) is False
        assert killed == []

    async def test_the_guard_stops_a_second_round(self, monkeypatch):
        """Without it the retry re-enters the same read and a bridge that
        stays wedged loops forever."""
        killed = []
        monkeypatch.setattr(ax_recovery, "reset_bridge",
                            lambda u: killed.append(u) or _true())
        assert await ax_recovery.should_retry(UDID, WEDGED, True) is False
        assert killed == []

    async def test_a_failed_reset_is_not_a_retry(self, monkeypatch):
        """Re-reading after a kill that did not happen returns the same tree."""
        monkeypatch.setattr(ax_recovery, "reset_bridge", lambda _u: _false())
        assert await ax_recovery.should_retry(UDID, WEDGED, False) is False


async def _true():
    return True


async def _false():
    return False


def _idb_returning(monkeypatch, trees):
    """An IdbBackend whose subprocess yields `trees` in order."""
    backend = IdbBackend()
    seq = list(trees)

    async def fake_run(self, *args):
        return json.dumps(seq.pop(0) if seq else HEALTHY), ""

    monkeypatch.setattr(IdbBackend, "_run", fake_run)
    return backend, seq


class TestIdbHealsOnEveryReader:
    @pytest.mark.parametrize("method", ["describe_all", "describe_all_flat",
                                        "describe_all_nested"])
    async def test_a_wedged_read_is_retried_after_a_reset(self, monkeypatch, method):
        backend, _ = _idb_returning(monkeypatch, [WEDGED, HEALTHY])
        resets = []
        monkeypatch.setattr(ax_recovery, "reset_bridge",
                            lambda u: resets.append(u) or _true())
        monkeypatch.setattr("server.device.probing.probe_container",
                            lambda *a, **k: _empty())

        out = await getattr(backend, method)(UDID)

        assert resets == [UDID], f"{method} never reset the bridge"
        assert not ax_recovery.looks_poisoned(out), f"{method} returned the wedge"
        assert len(out) > 1

    @pytest.mark.parametrize("method", ["describe_all", "describe_all_flat",
                                        "describe_all_nested"])
    async def test_a_healthy_read_resets_nothing(self, monkeypatch, method):
        backend, _ = _idb_returning(monkeypatch, [HEALTHY])
        resets = []
        monkeypatch.setattr(ax_recovery, "reset_bridge",
                            lambda u: resets.append(u) or _true())
        monkeypatch.setattr("server.device.probing.probe_container",
                            lambda *a, **k: _empty())

        await getattr(backend, method)(UDID)

        assert resets == []

    @pytest.mark.parametrize("method", ["describe_all", "describe_all_flat",
                                        "describe_all_nested"])
    async def test_a_bridge_that_stays_wedged_is_reset_once(self, monkeypatch, method):
        """The answer is still poisoned, and that is correct -- what matters is
        that it is returned rather than looped on."""
        backend, _ = _idb_returning(monkeypatch, [WEDGED, WEDGED, WEDGED])
        resets = []
        monkeypatch.setattr(ax_recovery, "reset_bridge",
                            lambda u: resets.append(u) or _true())
        monkeypatch.setattr("server.device.probing.probe_container",
                            lambda *a, **k: _empty())

        out = await getattr(backend, method)(UDID)

        assert len(resets) == 1
        assert ax_recovery.looks_poisoned(out)


async def _empty():
    return []


class TestSimBridgeNestedHealsToo:
    async def test_a_wedged_nested_read_is_retried(self, monkeypatch):
        backend = SimBridgeBackend(SimBridgeManager())
        seq = [WEDGED, HEALTHY]

        async def fetch(self, _udid):
            return seq.pop(0)

        monkeypatch.setattr(SimBridgeBackend, "_fetch_nested", fetch)
        resets = []
        monkeypatch.setattr(ax_recovery, "reset_bridge",
                            lambda u: resets.append(u) or _true())

        out = await backend.describe_all_nested(UDID)

        assert resets == [UDID]
        assert not ax_recovery.looks_poisoned(out)

    async def test_a_healthy_nested_read_resets_nothing(self, monkeypatch):
        backend = SimBridgeBackend(SimBridgeManager())

        async def fetch(self, _udid):
            return HEALTHY

        monkeypatch.setattr(SimBridgeBackend, "_fetch_nested", fetch)
        resets = []
        monkeypatch.setattr(ax_recovery, "reset_bridge",
                            lambda u: resets.append(u) or _true())

        await backend.describe_all_nested(UDID)

        assert resets == []
