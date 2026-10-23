"""Folding a foldable simulator -- iPhone Duo -- from quern (#537).

The helper runs inside the simulator; these pin how quern drives it and what
it reports, with `simctl spawn` and sim-bridge faked. The helper's own bytes
are checked by scripts/hinge-selftest.sh, and the whole path was live-tested
on a Duo (Xcode 27.1, iOS 27.1).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.device.controller import DeviceController
from server.device.ios import hinge as hinge_mod
from server.device.ios.hinge import HingeControl
from server.models import DeviceError, DeviceType, InvalidDeviceRequestError

DUO = "0AA1FC84-86D2-4488-98F6-5CE07DD27A6B"
COVER = {"screen_id": 1, "device_name": "primary", "orientation": 1,
         "size": [466, 678], "native_size": [466, 678]}
INNER = {"screen_id": 3, "device_name": "primary-1", "orientation": 3,
         "size": [951, 669], "native_size": [669, 951]}


# -- the helper wrapper ---------------------------------------------------------


class FakeProc:
    def __init__(self, code=0, out=b"", err=b"", hang=False):
        self.returncode = code
        self._out, self._err, self._hang = out, err, hang
        self.killed = False
        self.stdout = None

    async def communicate(self):
        if self._hang:
            await asyncio.sleep(60)
        return self._out, self._err

    def kill(self):
        self.killed = True

    async def wait(self):
        return self.returncode


def _spawning(monkeypatch, *procs):
    """Fake `create_subprocess_exec`: each call takes the next FakeProc and
    records its argv."""
    calls = []
    queue = list(procs)

    async def exec_(*argv, **_kw):
        calls.append(argv)
        return queue.pop(0) if queue else FakeProc()

    monkeypatch.setattr(hinge_mod.asyncio, "create_subprocess_exec", exec_)
    return calls


@pytest.fixture
def helper(tmp_path, monkeypatch):
    """A HingeControl whose binary is already built and current."""
    monkeypatch.setattr(hinge_mod, "_bin_dir", lambda: tmp_path)
    h = HingeControl()
    (tmp_path / "hinge-control").write_bytes(b"bin")
    (tmp_path / "hinge-control.sha256").write_text(hinge_mod.sources_digest())
    return h


async def test_a_current_build_is_not_rebuilt(helper, monkeypatch):
    calls = _spawning(monkeypatch)
    assert await helper.ensure_binary() == helper.binary_path
    assert calls == []


async def test_a_changed_source_rebuilds_signed_at_link_time(helper, monkeypatch, tmp_path):
    (tmp_path / "hinge-control.sha256").write_text("stale")
    calls = _spawning(monkeypatch, FakeProc(out=b"/SDK\n"), FakeProc())
    await helper.ensure_binary()
    clang = calls[1]
    assert "-Wl,-adhoc_codesign" in clang
    assert any(str(a).endswith("-apple-ios17.0-simulator") for a in clang)
    assert "codesign" not in [str(a) for a in clang], "never re-signed after linking"
    assert (tmp_path / "hinge-control.sha256").read_text() == hinge_mod.sources_digest()


async def test_a_failed_build_leaves_no_stamp(helper, monkeypatch, tmp_path):
    """A stamp surviving a failed build would name a binary it does not describe."""
    (tmp_path / "hinge-control.sha256").write_text("stale")
    _spawning(monkeypatch, FakeProc(out=b"/SDK\n"), FakeProc(code=1, err=b"clang: error"))
    with pytest.raises(DeviceError, match="could not build"):
        await helper.ensure_binary()
    assert not (tmp_path / "hinge-control.sha256").exists()


async def test_the_angle_is_read(helper, monkeypatch):
    _spawning(monkeypatch, FakeProc(out=b"angle 130\n"))
    assert await helper.read_angle(DUO) == 130.0


async def test_no_reading_is_none_not_an_error(helper, monkeypatch):
    """The guest goes quiet after SpringBoard restarts: an answer, not a fault."""
    _spawning(monkeypatch, FakeProc(code=1, err=b"no hinge sample in 1 second"))
    assert await helper.read_angle(DUO) is None


async def test_a_helper_that_could_not_run_raises(helper, monkeypatch):
    _spawning(monkeypatch, FakeProc(code=127, err=b"spawn failed"))
    with pytest.raises(DeviceError, match="could not read the hinge"):
        await helper.read_angle(DUO)


async def test_a_move_sweeps_with_a_deadline(helper, monkeypatch):
    calls = _spawning(monkeypatch, FakeProc(out=b"pid 9\n"))
    await helper.move(DUO, 0, 130, 800)
    argv = list(calls[0])
    assert argv[:4] == ["xcrun", "simctl", "spawn", DUO]
    assert "--deadline" in argv and argv[-4:] == ["sweep", "0", "130", "800"]


async def test_a_zero_duration_sets_the_angle_at_once(helper, monkeypatch):
    calls = _spawning(monkeypatch, FakeProc())
    await helper.move(DUO, 0, 45, 0)
    assert list(calls[0])[-2:] == ["angle", "45"]


@pytest.mark.parametrize("code,match", [
    (1, "refused the pose event"), (2, "rejected the command"), (3, "before its deadline"),
])
async def test_a_failed_move_says_why(helper, monkeypatch, code, match):
    _spawning(monkeypatch, FakeProc(code=code))
    with pytest.raises(DeviceError, match=match):
        await helper.move(DUO, 0, 130, 800)


async def test_a_move_that_times_out_is_killed_in_the_guest_too(helper, monkeypatch):
    """The host's simctl spawn does not own the guest process, which would go
    on and move the hinge after quern gave up."""
    monkeypatch.setattr(hinge_mod, "_SLACK_S", 0.05)
    hung = FakeProc(hang=True)
    hung.stdout = MagicMock(read=AsyncMock(return_value=b"pid 4242\n"))
    calls = _spawning(monkeypatch, hung, FakeProc())
    with pytest.raises(DeviceError, match="did not finish"):
        await helper.move(DUO, 0, 130, 0)
    assert hung.killed
    assert list(calls[1])[-3:] == ["kill", "-9", "4242"]


# -- the controller ------------------------------------------------------------


def _controller(*, panels=None, angles=(0.0, 130.0)) -> DeviceController:
    """A controller whose sim-bridge answers `panels` in turn (each a (lit,
    count) pair; the last repeats) and whose helper reads `angles` in turn."""
    ctrl = DeviceController()
    ctrl._device_type_cache[DUO] = DeviceType.SIMULATOR
    ctrl._sim_bridge_ok = True
    ctrl.resolve_udid = AsyncMock(return_value=DUO)
    answers = list(panels or [(COVER, 2), (INNER, 2)])

    async def panels_(_udid):
        return answers.pop(0) if len(answers) > 1 else answers[0]

    ctrl.sim_bridge = MagicMock()
    ctrl.sim_bridge.panels = AsyncMock(side_effect=panels_)
    ctrl.hinge = MagicMock()
    reads = list(angles)
    ctrl.hinge.read_angle = AsyncMock(
        side_effect=lambda _u: reads.pop(0) if len(reads) > 1 else reads[0])
    ctrl.hinge.move = AsyncMock()
    ctrl.hinge.orient = AsyncMock()
    return ctrl


async def test_open_sweeps_from_where_the_hinge_is_and_reports_what_lit():
    ctrl = _controller()
    result = await ctrl.set_hinge(DUO, pose="open")
    ctrl.hinge.move.assert_awaited_once_with(DUO, 0.0, 130.0, 800)
    assert result["lit_panel"]["screen_id"] == 3 and result["lit_panel_before"]["screen_id"] == 1
    assert result["angle"] == 130.0 and "warning" not in result


async def test_closed_expects_the_cover():
    ctrl = _controller(panels=[(INNER, 2), (COVER, 2)], angles=(130.0, 0.0))
    result = await ctrl.set_hinge(DUO, pose="closed")
    ctrl.hinge.move.assert_awaited_once_with(DUO, 130.0, 0.0, 800)
    assert result["lit_panel"]["device_name"] == "primary" and "warning" not in result


async def test_a_pose_whose_panel_did_not_light_says_so(monkeypatch):
    """Report the outcome, not the request: SpringBoard decides the panel."""
    ctrl = _controller(panels=[(COVER, 2)])
    monkeypatch.setattr(ctrl, "_HINGE_SETTLE_S", 0.05)
    result = await ctrl.set_hinge(DUO, pose="open")
    assert result["lit_panel"]["device_name"] == "primary"
    assert "did not light" in result["warning"]


async def test_no_reading_sets_the_angle_at_once():
    """A sweep from a guessed angle would jump there first."""
    ctrl = _controller(angles=(None, 130.0))
    await ctrl.set_hinge(DUO, pose="open")
    ctrl.hinge.move.assert_awaited_once_with(DUO, 130.0, 130.0, 0)


async def test_an_angle_has_no_expected_panel_and_never_waits(monkeypatch):
    ctrl = _controller(panels=[(COVER, 2)])
    monkeypatch.setattr(ctrl, "_HINGE_SETTLE_S", 30)
    started = time.monotonic()
    result = await ctrl.set_hinge(DUO, angle=100, duration=0.3)
    assert time.monotonic() - started < 5
    ctrl.hinge.move.assert_awaited_once_with(DUO, 0.0, 100, 300)
    assert result["requested_angle"] == 100 and "warning" not in result


async def test_orientation_alone_turns_without_moving_the_hinge():
    ctrl = _controller(panels=[(INNER, 2)])
    await ctrl.set_hinge(DUO, orientation="pud")
    ctrl.hinge.move.assert_not_called()
    ctrl.hinge.orient.assert_awaited_once_with(DUO, "pud")


async def test_a_fold_is_a_screen_change_and_is_remembered():
    ctrl = _controller()
    before = time.monotonic()
    await ctrl.set_hinge(DUO, pose="open")
    assert ctrl._last_ui_change[DUO] >= before
    assert ctrl._lit_seen[DUO] == (3, 3), "so the next tap does not count it twice"


@pytest.mark.parametrize("kwargs,match", [
    ({}, "one of pose or angle"),
    ({"pose": "open", "angle": 10}, "not both"),
    ({"pose": "ajar"}, "pose must be"),
    ({"angle": 200}, "0 to 180"),
    ({"angle": 10, "duration": 9}, "0 to 5"),
    ({"orientation": "sideways"}, "orientation must be"),
])
async def test_a_bad_request_is_refused_before_anything_moves(kwargs, match):
    ctrl = _controller()
    with pytest.raises(InvalidDeviceRequestError, match=match):
        await ctrl.set_hinge(DUO, **kwargs)
    ctrl.hinge.move.assert_not_called()


async def test_a_phone_with_one_screen_is_refused():
    ctrl = _controller(panels=[(None, 1)])
    with pytest.raises(InvalidDeviceRequestError, match="one screen"):
        await ctrl.set_hinge(DUO, pose="open")
    ctrl.hinge.move.assert_not_called()
    assert DUO in ctrl._single_panel


async def test_android_is_refused():
    ctrl = _controller()
    ctrl._device_type_cache[DUO] = DeviceType.ANDROID_EMULATOR
    with pytest.raises(InvalidDeviceRequestError, match="foldable simulator"):
        await ctrl.get_hinge(DUO)


async def test_get_hinge_reports_angle_and_panel():
    ctrl = _controller(panels=[(INNER, 2)], angles=(130.0,))
    result = await ctrl.get_hinge(DUO)
    assert result["angle"] == 130.0 and result["lit_panel"]["screen_id"] == 3


async def test_get_hinge_with_no_reading_says_why():
    ctrl = _controller(panels=[(INNER, 2)], angles=(None,))
    result = await ctrl.get_hinge(DUO)
    assert result["angle"] is None and "SpringBoard" in result["note"]


# -- a fold made elsewhere ----------------------------------------------------------


async def test_a_fold_in_device_hub_counts_as_a_change():
    """A tap sent during the panel swap reached the screen that was there."""
    ctrl = _controller(panels=[(COVER, 2), (INNER, 2)])
    await ctrl._note_fold(DUO)
    assert DUO not in ctrl._last_ui_change, "the first look records, counts nothing"
    await ctrl._note_fold(DUO)
    assert DUO in ctrl._last_ui_change


async def test_a_turn_counts_too():
    turned = {**INNER, "orientation": 4}
    ctrl = _controller(panels=[(INNER, 2), (turned, 2)])
    await ctrl._note_fold(DUO)
    await ctrl._note_fold(DUO)
    assert DUO in ctrl._last_ui_change


async def test_no_change_is_no_change():
    ctrl = _controller(panels=[(INNER, 2)])
    await ctrl._note_fold(DUO)
    await ctrl._note_fold(DUO)
    assert DUO not in ctrl._last_ui_change


async def test_a_phone_is_asked_once():
    ctrl = _controller(panels=[(None, 1)])
    await ctrl._note_fold(DUO)
    await ctrl._note_fold(DUO)
    ctrl.sim_bridge.panels.assert_awaited_once()


async def test_a_failed_read_does_not_stop_the_tap():
    ctrl = _controller()
    ctrl.sim_bridge.panels = AsyncMock(side_effect=TimeoutError())
    await ctrl._note_fold(DUO)
    assert DUO not in ctrl._last_ui_change


async def test_the_settle_check_looks_for_a_fold():
    ctrl = _controller(panels=[(COVER, 2), (INNER, 2)])
    ctrl.wait_for_settle = AsyncMock(return_value={"settled": True})
    assert await ctrl._settle_if_just_changed(DUO) is None
    waited = await ctrl._settle_if_just_changed(DUO)
    assert waited is not None
    ctrl.wait_for_settle.assert_awaited_once()


# -- the routes ---------------------------------------------------------------------


async def test_the_routes(monkeypatch):
    from fastapi import HTTPException

    from server.api import device_ui
    from server.models import HingeRequest

    controller = MagicMock()
    controller.set_hinge = AsyncMock(return_value={"status": "ok", "lit_panel": INNER})
    controller.get_hinge = AsyncMock(return_value={"angle": 130.0, "lit_panel": INNER})
    monkeypatch.setattr(device_ui, "_get_controller", lambda _r: controller)
    request = MagicMock()
    out = await device_ui.set_hinge(request=request, body=HingeRequest(udid=DUO, pose="open"))
    assert out["lit_panel"]["screen_id"] == 3
    controller.set_hinge.assert_awaited_once_with(udid=DUO, pose="open", angle=None,
                                                  duration=None, orientation=None)
    assert (await device_ui.get_hinge(request=request, udid=DUO))["angle"] == 130.0

    controller.set_hinge = AsyncMock(side_effect=InvalidDeviceRequestError("one screen",
                                                                           tool="hinge-control"))
    with pytest.raises(HTTPException) as caught:
        await device_ui.set_hinge(request=request, body=HingeRequest(udid=DUO, pose="open"))
    assert caught.value.status_code == 400


def test_the_helper_sources_are_where_quern_builds_from():
    for rel in (*hinge_mod.SOURCES, *hinge_mod.HEADERS):
        assert (hinge_mod.HINGE_DIR / rel).is_file(), rel
    assert (hinge_mod.HINGE_DIR / "LICENSE.baguette").is_file(), "the upstream licence travels"
    assert Path(hinge_mod.HINGE_DIR / "hinge-pin.json").is_file()
