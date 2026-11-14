"""Turning a simulator from quern (#243).

sim-bridge sends Simulator.app's own rotation event and reads the screen's
orientation back; these pin how quern drives it and what it reports, with
sim-bridge faked. The bridge side was live-tested on an iPhone 18 Pro (iOS
27.2) and an iPhone 17 Pro (iOS 27.0), Xcode 27.2: each orientation read back
as set, and taps off the centre landed in both landscapes, where before this
one missed.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.device.controller import DeviceController
from server.device.ios.sim_bridge import SimBridgeBackend, SimBridgeManager
from server.models import (
    DeviceError,
    DeviceOperationUnsupportedError,
    DeviceType,
    InvalidDeviceRequestError,
)

SIM = "B7C00E83-B66A-49DB-AD90-D58A2359748B"


def _controller(*, reads=(1, 4), panels=1, devicectl=False) -> DeviceController:
    """A controller whose sim-bridge reads `reads` in turn (the last repeats)
    and reports `panels` built-in panels; `devicectl` says whether devicectl
    can turn it, else sim-bridge's event does."""
    ctrl = DeviceController()
    ctrl._device_type_cache[SIM] = DeviceType.SIMULATOR
    ctrl._sim_bridge_ok = True
    ctrl._ORIENTATION_SETTLE_S = 0.6
    ctrl.resolve_udid = AsyncMock(return_value=SIM)
    answers = list(reads)
    ctrl.sets: list[int] = []

    async def orientation(_udid, set_to=None):
        if set_to is not None:
            ctrl.sets.append(set_to)
        value = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(value, Exception):
            raise value
        return value, panels

    ctrl.sim_bridge = MagicMock()
    ctrl.sim_bridge.orientation = AsyncMock(side_effect=orientation)
    ctrl.hinge = MagicMock()
    ctrl.hinge.orient = AsyncMock()
    ctrl.devicectl = MagicMock()
    ctrl.devicectl.can_set_orientation = AsyncMock(return_value=devicectl)
    ctrl.devicectl.set_orientation = AsyncMock()
    return ctrl


@pytest.mark.parametrize("name,value", [
    ("portrait", 1), ("pud", 2), ("landscape-left", 4), ("landscape-right", 3),
])
async def test_each_orientation_is_sent_as_the_value_it_reads_back(name, value):
    """Without devicectl, sim-bridge's event turns it. The bridge sets and
    reads one number, so it means one thing both ways; the names are
    devicectl's, measured: its landscapeLeft reads back 4."""
    ctrl = _controller(reads=(1, value))

    result = await ctrl.set_orientation(name)

    assert ctrl.sets == [value]
    assert result["status"] == "ok"
    assert result["orientation"] == name
    assert result["interface_orientation"] == name
    assert "note" not in result


async def test_the_interface_is_read_back_not_assumed():
    """A portrait-only app -- or the iPhone home screen -- stays portrait when
    the device turns. That is the answer, said; not an error, because the
    device did turn and a retry would change nothing."""
    ctrl = _controller(reads=(1, 1))

    result = await ctrl.set_orientation("landscape-left")

    assert ctrl.sets == [4]
    assert result["status"] == "ok"
    assert result["interface_orientation"] == "portrait"
    assert result["interface_orientation_before"] == "portrait"
    assert "stayed portrait" in result["note"]


async def test_it_waits_for_the_turn_to_play_out():
    """The guest rotates after the bridge answers: a read taken straight away
    is the old orientation, and reporting it would call a turn that worked a
    failure."""
    ctrl = _controller(reads=(1, 1, 1, 3))

    result = await ctrl.set_orientation("landscape-right")

    assert result["interface_orientation"] == "landscape-right"
    assert "note" not in result


async def test_a_failed_read_back_does_not_undo_the_turn():
    """Once the event is sent the device has turned; a read that fails after
    it is reported, never raised, or a caller retries a turn that happened."""
    ctrl = _controller(reads=(1, 1, DeviceError("bridge died")))

    result = await ctrl.set_orientation("landscape-left")

    assert ctrl.sets == [4]
    assert result["status"] == "ok"
    assert result["interface_orientation"] is None
    assert "could not be read back" in result["note"]


async def test_a_failure_after_a_stale_read_is_no_reading():
    """A read from before the turn, then the bridge failing: that earlier
    reading is not the answer -- reported, it would call an app that did
    turn one that does not (review)."""
    ctrl = _controller(reads=(1, 1, 1, DeviceError("bridge died")))

    result = await ctrl.set_orientation("landscape-left")

    assert result["interface_orientation"] is None
    assert "could not be read back" in result["note"]


async def test_a_read_held_up_is_bounded_and_is_no_reading():
    """A read stuck behind another command could wait 20s for the bridge's
    lock and 30s more for its answer (review). It is cut at what is left of
    the settle time -- never less than the floor -- and counts as no reading."""
    import asyncio

    ctrl = _controller(reads=(1, 1))
    ctrl._ORIENTATION_SETTLE_S = 0.2
    ctrl._ORIENTATION_READ_FLOOR_S = 0.1
    calls = 0
    inner = ctrl.sim_bridge.orientation.side_effect

    async def stuck_after_the_turn(udid, set_to=None):
        nonlocal calls
        calls += 1
        if calls <= 2:  # the read before, and the turn
            return await inner(udid, set_to)
        await asyncio.sleep(30)

    ctrl.sim_bridge.orientation = AsyncMock(side_effect=stuck_after_the_turn)
    started = time.monotonic()

    result = await ctrl.set_orientation("landscape-left")

    assert time.monotonic() - started < 2
    assert result["interface_orientation"] is None
    assert "could not be read back" in result["note"]


async def test_the_change_is_marked_after_the_turn_has_played_out():
    """Marked again once the wait is over, so a tap straight after the call
    still finds the rotation recent and waits for it to settle."""
    ctrl = _controller(reads=(1, 1, 1, 4))
    read_at: list[float] = []
    inner = ctrl.sim_bridge.orientation.side_effect

    async def timed(udid, set_to=None):
        read_at.append(time.monotonic())
        return await inner(udid, set_to)

    ctrl.sim_bridge.orientation = AsyncMock(side_effect=timed)

    await ctrl.set_orientation("landscape-left")

    assert ctrl._last_ui_change[SIM] >= read_at[-1]


async def test_a_turn_the_bridge_could_not_send_raises():
    """Nothing turned, so this one is an error: the caller should know."""
    ctrl = _controller(reads=(1, DeviceError("no PurpleWorkspacePort")))

    with pytest.raises(DeviceError, match="PurpleWorkspacePort"):
        await ctrl.set_orientation("landscape-left")


async def test_a_foldable_is_set_hinge_with_that_orientation():
    """iPhone Duo ignored the rotation event (measured), and turns through
    Device Hub's picker. set_hinge already reports that as it should, and
    without comparing to the request: unfolded, `portrait` is the inner
    panel's landscape, so a comparison would call every unfolded turn a
    refusal (review)."""
    ctrl = _controller(reads=(1, 4), panels=2)
    ctrl.set_hinge = AsyncMock(return_value={"udid": SIM, "status": "ok", "lit_panel": {}})

    result = await ctrl.set_orientation("portrait")

    ctrl.set_hinge.assert_awaited_once_with(SIM, orientation="portrait")
    assert ctrl.sets == []
    assert result == {"udid": SIM, "status": "ok", "lit_panel": {}}


async def test_devicectl_turns_it_where_it_can():
    """Apple's own command, where this Xcode's devicectl has it; sim-bridge's
    event is then not sent, and the answer says which turned it."""
    ctrl = _controller(reads=(1, 4), devicectl=True)

    result = await ctrl.set_orientation("landscape-left")

    ctrl.devicectl.set_orientation.assert_awaited_once_with(SIM, "landscape-left")
    assert ctrl.sets == []
    assert result["turned_with"] == "devicectl"
    assert result["interface_orientation"] == "landscape-left"


async def test_without_devicectl_the_event_turns_it_and_says_so():
    ctrl = _controller(reads=(1, 4), devicectl=False)

    result = await ctrl.set_orientation("landscape-left")

    ctrl.devicectl.set_orientation.assert_not_awaited()
    assert result["turned_with"] == "sim-bridge"


async def test_a_turn_devicectl_refused_raises():
    """Nothing turned, so the caller should know."""
    ctrl = _controller(devicectl=True)
    ctrl.devicectl.set_orientation = AsyncMock(side_effect=DeviceError("devicectl device failed"))

    with pytest.raises(DeviceError, match="devicectl"):
        await ctrl.set_orientation("landscape-left")


async def test_lying_flat_keeps_the_interface_and_is_not_called_a_refusal():
    """Face up or down, the interface stays as it was: that is no app
    declining to turn, so no such note."""
    ctrl = _controller(reads=(4, 4), devicectl=True)

    result = await ctrl.set_orientation("face-up")

    ctrl.devicectl.set_orientation.assert_awaited_once_with(SIM, "face-up")
    assert result["interface_orientation"] == "landscape-left"
    assert "does not support" not in result["note"]
    assert "keeps the interface" in result["note"]


async def test_lying_flat_needs_devicectl():
    ctrl = _controller(devicectl=False)

    with pytest.raises(DeviceOperationUnsupportedError, match="devicectl"):
        await ctrl.set_orientation("face-down")
    assert ctrl.sets == []


async def test_a_foldable_cannot_lie_flat():
    """Its picker has the four orientations only."""
    ctrl = _controller(panels=2, devicectl=True)
    ctrl.set_hinge = AsyncMock()

    with pytest.raises(InvalidDeviceRequestError, match="foldable"):
        await ctrl.set_orientation("face-up")
    ctrl.set_hinge.assert_not_awaited()
    ctrl.devicectl.set_orientation.assert_not_awaited()


async def test_a_turn_counts_as_a_screen_change():
    """A tap straight after must wait for the rotation, as after a fold."""
    ctrl = _controller()

    await ctrl.set_orientation("landscape-left")

    assert SIM in ctrl._last_ui_change


@pytest.mark.parametrize("kind", [DeviceType.DEVICE, DeviceType.ANDROID_EMULATOR,
                                  DeviceType.ANDROID_DEVICE])
async def test_anything_but_an_ios_simulator_is_refused_before_anything_is_sent(kind):
    ctrl = _controller()
    ctrl._device_type_cache[SIM] = kind

    with pytest.raises(InvalidDeviceRequestError):
        await ctrl.set_orientation("landscape-left")
    ctrl.sim_bridge.orientation.assert_not_awaited()


async def test_an_unknown_orientation_is_refused():
    ctrl = _controller()

    with pytest.raises(InvalidDeviceRequestError, match="portrait, pud"):
        await ctrl.set_orientation("sideways")
    ctrl.sim_bridge.orientation.assert_not_awaited()


async def test_without_sim_bridge_it_says_so():
    ctrl = _controller()
    ctrl._sim_bridge_ok = False

    with pytest.raises(DeviceOperationUnsupportedError, match="sim-bridge"):
        await ctrl.set_orientation("landscape-left")


# -- the bridge's side of the wire ------------------------------------------------


def _bridge(answer: dict) -> tuple[SimBridgeBackend, AsyncMock]:
    mgr = SimBridgeManager()
    mgr.send = AsyncMock(return_value=answer)  # type: ignore[method-assign]
    return SimBridgeBackend(mgr), mgr.send


async def test_the_bridge_sends_the_turn_and_parses_the_answer():
    bridge, send = _bridge({"ok": True, "orientation": 3, "panels": 1})

    assert await bridge.orientation(SIM, set_to=3) == (3, 1)
    assert send.await_args.args[0] == {"cmd": "orientation", "udid": SIM, "set": 3}


async def test_a_read_sends_no_turn():
    bridge, send = _bridge({"ok": True, "orientation": 1, "panels": 2})

    assert await bridge.orientation(SIM) == (1, 2)
    assert "set" not in send.await_args.args[0]


async def test_an_unreadable_orientation_is_none_not_portrait():
    bridge, _ = _bridge({"ok": True, "orientation": None, "panels": 1})

    assert await bridge.orientation(SIM) == (None, 1)


async def test_a_refused_turn_raises_with_the_bridges_reason():
    bridge, _ = _bridge({"ok": False,
                         "error": "could not turn the simulator: no PurpleWorkspacePort"})

    with pytest.raises(DeviceError, match="PurpleWorkspacePort"):
        await bridge.orientation(SIM, set_to=3)


# -- devicectl ---------------------------------------------------------------------


@pytest.mark.parametrize("name,apple", [
    ("portrait", "portrait"), ("pud", "portraitUpsideDown"),
    ("landscape-left", "landscapeLeft"), ("landscape-right", "landscapeRight"),
    ("face-up", "faceUp"), ("face-down", "faceDown"),
])
async def test_devicectl_is_given_apples_name(name, apple):
    from server.device.ios.devicectl import DevicectlBackend

    backend = DevicectlBackend()
    backend._run_devicectl = AsyncMock(return_value=("", ""))

    await backend.set_orientation(SIM, name)

    backend._run_devicectl.assert_awaited_once_with(
        "device", "orientation", "set", "--device", SIM, apple)


async def test_a_devicectl_that_hangs_is_an_error_not_a_turn(monkeypatch):
    import asyncio

    from server.device.ios.devicectl import DevicectlBackend

    backend = DevicectlBackend()
    backend._ORIENTATION_TIMEOUT_S = 0.05

    async def hang(*_a, **_k):
        await asyncio.sleep(5)

    backend._run_devicectl = hang

    with pytest.raises(DeviceError, match="did not turn"):
        await backend.set_orientation(SIM, "portrait")


async def test_whether_devicectl_can_turn_is_asked_once(monkeypatch):
    from server.device.ios import devicectl as mod

    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(mod, "probe_command", probe)
    monkeypatch.setattr(mod, "xcode_available", lambda: True)
    backend = mod.DevicectlBackend()

    assert await backend.can_set_orientation()
    assert await backend.can_set_orientation()
    assert probe.await_count == 1
    assert probe.await_args.args[:6] == ("xcrun", "devicectl", "device", "orientation", "set",
                                         "--help")
