"""Turning a simulator from quern (#243).

sim-bridge sends Simulator.app's own rotation event and reads the screen's
orientation back; these pin how quern drives it and what it reports, with
sim-bridge faked. The bridge side was live-tested on an iPhone 18 Pro (iOS
27.2) and an iPhone 17 Pro (iOS 27.0), Xcode 27.2: each orientation read back
as set, and taps off the centre landed in both landscapes, where before this
one missed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from server.device.controller import DeviceController
from server.models import (
    DeviceError,
    DeviceOperationUnsupportedError,
    DeviceType,
    InvalidDeviceRequestError,
)

SIM = "B7C00E83-B66A-49DB-AD90-D58A2359748B"


def _controller(*, reads=(1, 3), panels=1) -> DeviceController:
    """A controller whose sim-bridge reads `reads` in turn (the last repeats)
    and reports `panels` built-in panels."""
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
    return ctrl


@pytest.mark.parametrize("name,value", [
    ("portrait", 1), ("pud", 2), ("landscape-left", 3), ("landscape-right", 4),
])
async def test_each_orientation_is_sent_as_the_value_it_reads_back(name, value):
    """The bridge sets and reads UIDeviceOrientation, so one number means one
    thing both ways; the event's own landscape numbering, which is the other
    way round, stays inside sim-bridge."""
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

    assert ctrl.sets == [3]
    assert result["status"] == "ok"
    assert result["interface_orientation"] == "portrait"
    assert result["interface_orientation_before"] == "portrait"
    assert "stayed portrait" in result["note"]


async def test_it_waits_for_the_turn_to_play_out():
    """The guest rotates after the bridge answers: a read taken straight away
    is the old orientation, and reporting it would call a turn that worked a
    failure."""
    ctrl = _controller(reads=(1, 1, 1, 4))

    result = await ctrl.set_orientation("landscape-right")

    assert result["interface_orientation"] == "landscape-right"
    assert "note" not in result


async def test_a_failed_read_back_does_not_undo_the_turn():
    """Once the event is sent the device has turned; a read that fails after
    it is reported, never raised, or a caller retries a turn that happened."""
    ctrl = _controller(reads=(1, 1, DeviceError("bridge died")))

    result = await ctrl.set_orientation("landscape-left")

    assert ctrl.sets == [3]
    assert result["status"] == "ok"
    assert result["interface_orientation"] is None
    assert "could not be read back" in result["note"]


async def test_a_turn_the_bridge_could_not_send_raises():
    """Nothing turned, so this one is an error: the caller should know."""
    ctrl = _controller(reads=(1, DeviceError("no PurpleWorkspacePort")))

    with pytest.raises(DeviceError, match="PurpleWorkspacePort"):
        await ctrl.set_orientation("landscape-left")


async def test_a_foldable_turns_through_its_orientation_picker():
    """iPhone Duo ignored the rotation event (measured), and turns through
    Device Hub's picker, the path set_hinge's orientation takes."""
    ctrl = _controller(reads=(1, 3), panels=2)

    result = await ctrl.set_orientation("landscape-left")

    ctrl.hinge.orient.assert_awaited_once_with(SIM, "landscape-left")
    assert ctrl.sets == []
    assert result["interface_orientation"] == "landscape-left"


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
