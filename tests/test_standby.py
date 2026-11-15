"""StandBy on an iOS simulator (#680).

devicectl shows and hides it; these pin how quern drives devicectl and what it
reports, with devicectl faked. The device side was live-tested on an iPhone 18
Pro (iOS 27.2, Xcode 27.2): StandBy read `unavailable` whenever the simulator
was unlocked and `hidden` once locked, in portrait or landscape; a show from a
portrait lock screen turned it to landscape itself; and devicectl refused a
show on an unlocked simulator, saying StandBy presents only on the lock screen.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.device.controller import DeviceController
from server.device.ios.devicectl import DevicectlBackend
from server.models import (
    DeviceError,
    DeviceOperationUnsupportedError,
    DeviceType,
    InvalidDeviceRequestError,
)

SIM = "B7C00E83-B66A-49DB-AD90-D58A2359748B"


def _controller(*states: str, can: bool = True) -> DeviceController:
    """A controller whose devicectl reads `states` in turn (the last repeats)."""
    ctrl = DeviceController()
    ctrl._device_type_cache[SIM] = DeviceType.SIMULATOR
    ctrl._STANDBY_SETTLE_S = 0.6
    ctrl.resolve_udid = AsyncMock(return_value=SIM)
    answers = list(states)

    async def state(_udid):
        value = answers.pop(0) if len(answers) > 1 else answers[0]
        if isinstance(value, Exception):
            raise value
        return value

    ctrl.devicectl = MagicMock()
    ctrl.devicectl.can_standby = AsyncMock(return_value=can)
    ctrl.devicectl.standby_state = AsyncMock(side_effect=state)
    ctrl.devicectl.set_standby = AsyncMock(return_value="showing")
    ctrl.press_button = AsyncMock(return_value=SIM)
    return ctrl


async def test_a_show_on_a_locked_simulator_just_shows():
    ctrl = _controller("hidden", "showing")

    result = await ctrl.set_standby(True)

    ctrl.devicectl.set_standby.assert_awaited_once_with(SIM, show=True)
    ctrl.press_button.assert_not_awaited()
    assert result["standby"] == "showing"
    assert result["standby_before"] == "hidden"
    assert result["arranged"] == []
    assert "note" not in result


async def test_a_show_on_an_unlocked_simulator_locks_it_first_and_says_so():
    """StandBy presents only on the lock screen. Locking sends the app in front
    to the background, which the caller must hear about."""
    ctrl = _controller("unavailable", "hidden", "showing")

    result = await ctrl.set_standby(True)

    ctrl.press_button.assert_awaited_once_with("LOCK", udid=SIM)
    ctrl.devicectl.set_standby.assert_awaited_once_with(SIM, show=True)
    assert result["arranged"] == ["locked"]
    assert result["standby"] == "showing"
    assert "background" in result["note"]


async def test_it_waits_for_the_lock_before_asking_to_show():
    """devicectl refuses a show off the lock screen, so the show waits until
    StandBy reads available: the last state read before the show is
    `hidden`, not one of the `unavailable` reads while the lock lands."""
    ctrl = _controller()
    states = iter(["unavailable", "unavailable", "unavailable", "hidden", "showing"])
    seen: list[str] = []

    async def read(_udid):
        seen.append(next(states, "showing"))
        return seen[-1]

    shown_after: list[str] = []

    async def show(_udid, show):
        shown_after.append(seen[-1])
        return "showing"

    ctrl.devicectl.standby_state = AsyncMock(side_effect=read)
    ctrl.devicectl.set_standby = AsyncMock(side_effect=show)

    await ctrl.set_standby(True)

    assert shown_after == ["hidden"]
    assert seen.count("unavailable") == 3


async def test_a_lock_that_does_not_make_standby_available_is_an_error_not_a_show():
    ctrl = _controller("unavailable")

    with pytest.raises(DeviceError, match="still unavailable"):
        await ctrl.set_standby(True)
    ctrl.devicectl.set_standby.assert_not_awaited()


async def test_the_answer_is_what_the_device_reports_not_what_was_asked():
    """A show that did not take is said, not reported as showing."""
    ctrl = _controller("hidden", "hidden")

    result = await ctrl.set_standby(True)

    assert result["standby"] == "hidden"
    assert "reports hidden" in result["note"]


async def test_a_show_when_showing_does_nothing():
    ctrl = _controller("showing")

    result = await ctrl.set_standby(True)

    ctrl.devicectl.set_standby.assert_not_awaited()
    ctrl.press_button.assert_not_awaited()
    assert result["standby"] == "showing"


async def test_a_hide_hides_and_never_locks():
    ctrl = _controller("showing", "hidden")

    result = await ctrl.set_standby(False)

    ctrl.devicectl.set_standby.assert_awaited_once_with(SIM, show=False)
    ctrl.press_button.assert_not_awaited()
    assert result["standby"] == "hidden"
    assert "note" not in result


async def test_a_hide_when_not_showing_does_nothing():
    ctrl = _controller("unavailable")

    result = await ctrl.set_standby(False)

    ctrl.devicectl.set_standby.assert_not_awaited()
    assert result["standby"] == "unavailable"
    assert "note" not in result


async def test_a_hide_that_did_not_take_is_said():
    ctrl = _controller("showing", "showing")

    result = await ctrl.set_standby(False)

    assert "reports showing" in result["note"]


@pytest.mark.parametrize("kind", [DeviceType.DEVICE, DeviceType.ANDROID_EMULATOR,
                                  DeviceType.ANDROID_DEVICE])
async def test_anything_but_an_ios_simulator_is_refused_before_anything_is_asked(kind):
    ctrl = _controller("hidden")
    ctrl._device_type_cache[SIM] = kind

    with pytest.raises(InvalidDeviceRequestError):
        await ctrl.set_standby(True)
    with pytest.raises(InvalidDeviceRequestError):
        await ctrl.get_standby()
    ctrl.devicectl.standby_state.assert_not_awaited()


async def test_without_devicectls_standby_it_says_so():
    ctrl = _controller("hidden", can=False)

    with pytest.raises(DeviceOperationUnsupportedError, match="Xcode 27.2"):
        await ctrl.set_standby(True)


async def test_get_standby_reads_the_state():
    ctrl = _controller("unavailable")

    assert await ctrl.get_standby() == {"udid": SIM, "standby": "unavailable"}


# -- devicectl's side ---------------------------------------------------------------


def _backend(answer: str | Exception) -> tuple[DevicectlBackend, AsyncMock]:
    backend = DevicectlBackend()
    run = AsyncMock(side_effect=answer) if isinstance(answer, Exception) else AsyncMock(
        return_value=(answer, ""))
    backend._run_devicectl = run
    return backend, run


def _json(state) -> str:
    return json.dumps({"info": {}, "result": {"deviceIdentifier": SIM, "operation": "query",
                                              "state": state}})


async def test_the_state_is_read_from_devicectls_json():
    backend, run = _backend(_json("hidden"))

    assert await backend.standby_state(SIM) == "hidden"
    run.assert_awaited_once_with("device", "info", "standby", "--device", SIM, json_output=True)


@pytest.mark.parametrize("show,flag", [(True, "--show"), (False, "--hide")])
async def test_show_and_hide_send_their_flag(show, flag):
    backend, run = _backend(_json("showing" if show else "hidden"))

    await backend.set_standby(SIM, show)

    run.assert_awaited_once_with("device", "settings", "standby", "--device", SIM, flag,
                                 json_output=True)


@pytest.mark.parametrize("out", ["", "not json", json.dumps({"result": {}}),
                                 _json(None), _json(3)])
async def test_an_answer_with_no_state_is_an_error_not_a_state(out):
    """A failed read must not read as a state: `unavailable` would send a
    caller off to lock a simulator that may be showing StandBy already."""
    backend, _ = _backend(out)

    with pytest.raises(DeviceError, match="StandBy"):
        await backend.standby_state(SIM)


async def test_devicectls_refusal_keeps_its_reason():
    backend, _ = _backend(DeviceError(
        "devicectl device failed: ERROR: StandBy can only be presented while the device is "
        "showing its lock screen.", tool="devicectl"))

    with pytest.raises(DeviceError, match="lock screen"):
        await backend.set_standby(SIM, True)


async def test_whether_devicectl_has_standby_is_asked_once(monkeypatch):
    from server.device.ios import devicectl as mod

    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(mod, "probe_command", probe)
    monkeypatch.setattr(mod, "xcode_available", lambda: True)
    backend = mod.DevicectlBackend()

    assert await backend.can_standby()
    assert await backend.can_standby()
    assert probe.await_count == 1


# -- once quern has acted, nothing hides it (review) ------------------------------


async def test_a_read_that_fails_after_the_show_is_reported_not_raised():
    """StandBy was shown: raising now would send a caller to retry a show that
    happened."""
    ctrl = _controller("hidden", DeviceError("devicectl device failed: transient"))

    result = await ctrl.set_standby(True)

    ctrl.devicectl.set_standby.assert_awaited_once()
    assert result["standby"] is None
    assert "could not be read back" in result["note"]


async def test_a_refused_show_after_the_lock_says_quern_locked_it():
    """devicectl's own refusal reads as "lock it first" -- straight after quern
    did. The error says the simulator is locked."""
    ctrl = _controller("unavailable", "hidden")
    ctrl.devicectl.set_standby = AsyncMock(side_effect=DeviceError(
        "devicectl device failed: ERROR: StandBy can only be presented while the device "
        "is showing its lock screen."))

    with pytest.raises(DeviceError, match="pressed LOCK.*locked.*lock screen"):
        await ctrl.set_standby(True)


async def test_a_refused_show_with_nothing_arranged_is_devicectls_own_error():
    ctrl = _controller("hidden")
    ctrl.devicectl.set_standby = AsyncMock(side_effect=DeviceError("devicectl device failed: x"))

    with pytest.raises(DeviceError, match="^devicectl device failed: x$"):
        await ctrl.set_standby(True)


async def test_polls_that_fail_after_the_lock_still_try_the_show():
    """Not knowing whether the lock landed is no reason to stop: the show
    itself answers it."""
    ctrl = _controller("unavailable", DeviceError("transient"), DeviceError("transient"),
                       "showing")
    ctrl._STANDBY_SETTLE_S = 0.3

    result = await ctrl.set_standby(True)

    ctrl.devicectl.set_standby.assert_awaited_once_with(SIM, show=True)
    assert result["arranged"] == ["locked"]
    assert result["standby"] == "showing"


async def test_a_hide_waits_for_it_to_go():
    ctrl = _controller("showing", "showing", "showing", "hidden")

    result = await ctrl.set_standby(False)

    assert result["standby"] == "hidden"
    assert "note" not in result


async def test_the_change_is_marked_after_the_last_read():
    """StandBy covers the screen: the next tap must find it recent and settle."""
    import time

    ctrl = _controller("hidden", "showing")
    read_at: list[float] = []
    inner = ctrl.devicectl.standby_state.side_effect

    async def timed(udid):
        read_at.append(time.monotonic())
        return await inner(udid)

    ctrl.devicectl.standby_state = AsyncMock(side_effect=timed)

    await ctrl.set_standby(True)

    assert ctrl._last_ui_change[SIM] >= read_at[-1]


async def test_a_devicectl_that_hangs_is_an_error_not_a_state():
    import asyncio

    backend = DevicectlBackend()
    backend._ORIENTATION_TIMEOUT_S = 0.05

    async def hang(*_a, **_k):
        await asyncio.sleep(5)

    backend._run_devicectl = hang

    with pytest.raises(DeviceError, match="did not answer about StandBy"):
        await backend.standby_state(SIM)


async def test_a_simulator_that_cannot_show_it_is_a_400_with_what_to_do():
    """Measured on a shut-down iOS 27.0 simulator: devicectl's capability error."""
    backend, _ = _backend(DeviceError(
        'devicectl device failed: ERROR: The capability "StandBy" is not supported by this '
        "device. (com.apple.dt.CoreDeviceError error 1001 (0x3E9))"))

    with pytest.raises(DeviceOperationUnsupportedError, match="iPhone feature.*booted"):
        await backend.standby_state(SIM)


async def test_a_cancelled_json_read_leaves_no_temp_file(monkeypatch, tmp_path):
    import asyncio
    import tempfile

    from server.device.ios import devicectl as mod

    made: list[str] = []
    real = tempfile.NamedTemporaryFile

    def named(*a, **k):
        f = real(*a, dir=tmp_path, **k)
        made.append(f.name)
        return f

    monkeypatch.setattr(mod.tempfile, "NamedTemporaryFile", named)

    async def never(*_a, **_k):
        await asyncio.sleep(5)

    monkeypatch.setattr(mod.asyncio, "create_subprocess_exec", never)
    backend = DevicectlBackend()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(backend._run_devicectl("device", "info", "standby",
                                                      json_output=True), 0.05)
    assert made and not any(Path(m).exists() for m in made)
