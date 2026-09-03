"""Detection and recovery for the XCUITest-poisoned accessibility bridge (#66).

The wedge is real and deterministic: one XCUITest run against a simulator, and
every foregrounded app reports a single bare Application element until the
bridge is restarted. What makes it worth automating is that the empty tree is
indistinguishable from every landmark on every screen drifting at once, so
people go and edit knowledge bases that were never wrong.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from server.device import ax_recovery

# Canonical simctl UDIDs: the code refuses anything else, because a loose match
# against a short placeholder is exactly how it would kill the wrong bridge.
SIM_A = "F5AF3736-C05F-493F-AA52-CA883B13B18C"
SIM_B = "C5D36699-A47F-4A03-8D1C-01503E10DD2F"


def _app(width=0.0, height=0.0, label=None, type_="Application"):
    return {"type": type_, "AXLabel": label,
            "frame": {"x": 0.0, "y": 0.0, "width": width, "height": height}}


def test_the_poisoned_signature_is_recognised():
    assert ax_recovery.looks_poisoned([_app()])


def test_a_launching_app_is_not_mistaken_for_a_wedge():
    """The false positive that would cost a needless kill.

    An app mid-launch legitimately reports one Application element. The zero
    frame is what separates "nothing has rendered yet" from "the bridge cannot
    see anything", so a sized root must not trigger recovery.
    """
    assert not ax_recovery.looks_poisoned([_app(width=393.0, height=852.0)])


def test_a_labelled_root_is_not_a_wedge():
    assert not ax_recovery.looks_poisoned([_app(label="Metatext")])


def test_a_populated_tree_is_never_a_wedge():
    assert not ax_recovery.looks_poisoned([_app(), _app(type_="Button")])


def test_an_empty_tree_is_not_the_wedge_signature():
    """Nothing at all is a different failure — the signature is exactly one
    element, and treating a zero-length read as the wedge would kill the bridge
    every time a query came back empty for any other reason."""
    assert not ax_recovery.looks_poisoned([])


async def test_only_the_bridge_serving_this_simulator_is_killed():
    """One bridge exists per booted simulator. Killing every match would
    disturb every other simulator in the pool to fix one of them."""
    killed: list[str] = []

    async def fake_run(*args, timeout=5.0):
        if args[0] == "pgrep":
            return 0, "111\n222\n"
        if args[0] == "lsof":
            pid = args[2]
            return 0, (f"/path/{SIM_A}/data" if pid == "111" else f"/path/{SIM_B}/data")
        if args[0] == "kill":
            killed.append(args[2])
            return 0, ""
        return 1, ""

    with patch.object(ax_recovery, "_run", side_effect=fake_run):
        assert await ax_recovery.reset_bridge(SIM_A) is True
    assert killed == ["111"], f"killed {killed}, expected only the matching bridge"


async def test_nothing_is_killed_when_no_bridge_matches():
    """Better to report an unhealthy tree than to kill an unrelated process."""
    async def fake_run(*args, timeout=5.0):
        if args[0] == "pgrep":
            return 0, "111\n"
        if args[0] == "lsof":
            return 0, f"/path/{SIM_B}/data"
        raise AssertionError(f"should not have run {args[0]}")

    with patch.object(ax_recovery, "_run", side_effect=fake_run):
        assert await ax_recovery.reset_bridge(SIM_A) is False


async def test_recovery_is_attempted_once_and_not_looped():
    """A retry storm here is actively harmful: sim-bridge serialises commands
    and does not cancel abandoned ones, so repeated recovery attempts turn into
    a multi-minute drain that presents as a hang (#68)."""
    from server.device.sim_bridge import SimBridgeBackend, SimBridgeManager

    backend = SimBridgeBackend(SimBridgeManager())
    calls = {"fetch": 0}

    async def always_poisoned(_udid):
        calls["fetch"] += 1
        return [_app()]

    with patch.object(backend, "_fetch_nested", side_effect=always_poisoned), \
         patch.object(ax_recovery, "reset_bridge", AsyncMock(return_value=True)) as reset:
        result = await backend.describe_all(SIM_A)

    assert reset.await_count == 1, "recovery must not loop"
    assert calls["fetch"] == 2, "one original read plus exactly one retry"
    assert ax_recovery.looks_poisoned(result), "the unhealthy tree is still returned"


@pytest.mark.parametrize("healthy_second_read", [True])
async def test_a_healthy_tree_after_recovery_is_returned(healthy_second_read):
    from server.device.sim_bridge import SimBridgeBackend, SimBridgeManager

    backend = SimBridgeBackend(SimBridgeManager())
    reads = iter([[_app()], [_app(width=393.0, height=852.0, label="Probe")]])

    async def two_reads(_udid):
        return next(reads)

    with patch.object(backend, "_fetch_nested", side_effect=two_reads), \
         patch.object(ax_recovery, "reset_bridge", AsyncMock(return_value=True)):
        result = await backend.describe_all(SIM_A)

    assert not ax_recovery.looks_poisoned(result)
    assert result[0]["AXLabel"] == "Probe"


async def test_an_empty_udid_kills_nothing():
    """The dangerous input. `"" in files` is true for every lsof line, so a
    loose match would SIGKILL every simulator's bridge to recover none."""
    async def fake_run(*args, timeout=5.0):
        raise AssertionError(f"should not have run {args[0]} for an empty udid")

    with patch.object(ax_recovery, "_run", side_effect=fake_run):
        assert await ax_recovery.bridge_pids_for("") == []
        assert await ax_recovery.reset_bridge("") is False


@pytest.mark.parametrize("udid", [
    "not-a-uuid",
    "F5AF3736",                                  # a prefix, not the whole thing
    "f5af3736-c05f-493f-aa52-ca883b13b18c",      # simctl emits uppercase
    "../../etc",
])
async def test_non_canonical_identifiers_are_refused(udid):
    async def fake_run(*args, timeout=5.0):
        raise AssertionError(f"should not have run {args[0]} for {udid!r}")

    with patch.object(ax_recovery, "_run", side_effect=fake_run):
        assert await ax_recovery.bridge_pids_for(udid) == []


async def test_a_partial_udid_overlap_does_not_match_another_simulator():
    """Matched as a path component, so one UDID cannot match a longer one."""
    target = "F5AF3736-C05F-493F-AA52-CA883B13B18C"

    async def fake_run(*args, timeout=5.0):
        if args[0] == "pgrep":
            return 0, "111\n"
        if args[0] == "lsof":
            # A different simulator whose path merely contains the target as a
            # substring of a longer component.
            return 0, f"/data/Devices/{target}EXTRA/data/foo\n"
        raise AssertionError("should not have killed anything")

    with patch.object(ax_recovery, "_run", side_effect=fake_run):
        assert await ax_recovery.bridge_pids_for(target) == []
