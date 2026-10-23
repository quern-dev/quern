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
from server.models import (
    DeviceError,
    DeviceOperationUnsupportedError,
    DeviceType,
    InvalidDeviceRequestError,
)

DUO = "0AA1FC84-86D2-4488-98F6-5CE07DD27A6B"
COVER = {"screen_id": 1, "device_name": "primary", "orientation": 1,
         "size": [466, 678], "native_size": [466, 678]}
INNER = {"screen_id": 3, "device_name": "primary-1", "orientation": 3,
         "size": [951, 669], "native_size": [669, 951]}


# -- the helper wrapper ---------------------------------------------------------


class FakeProc:
    """A subprocess with real pipes: what was written is readable until the
    process is killed, and a hung one keeps its pipes open -- so, as with
    asyncio's own, nothing written survives a cancelled `communicate()`."""

    def __init__(self, code=0, out=b"", err=b"", hang=False):
        self.returncode = code
        self._hang = hang
        self.killed = False
        self._done = asyncio.Event()
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdout.feed_data(out)
        self.stderr.feed_data(err)
        if not hang:
            self._finish()

    def _finish(self):
        for s in (self.stdout, self.stderr):
            if not s.at_eof():
                s.feed_eof()
        self._done.set()

    async def communicate(self):
        out, err = await asyncio.gather(self.stdout.read(), self.stderr.read())
        await self.wait()
        return out, err

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._finish()

    async def wait(self):
        await self._done.wait()
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


def _building(monkeypatch):
    """Fake a build: the SDK lookup answers, and clang writes its `-o`."""
    calls = []

    async def exec_(*argv, **_kw):
        calls.append(argv)
        if "clang" in argv:
            Path(argv[argv.index("-o") + 1]).write_bytes(b"new")
            return FakeProc()
        return FakeProc(out=b"/SDK\n")

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
    calls = _building(monkeypatch)
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


@pytest.fixture
def killed(monkeypatch):
    """os.kill, recorded rather than sent."""
    sent = []
    monkeypatch.setattr(hinge_mod.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    return sent


async def test_a_move_that_times_out_is_killed_in_the_guest_too(helper, monkeypatch, killed):
    """The host's simctl spawn does not own the guest process, which would go
    on and move the hinge after quern gave up. A simulator's processes are the
    Mac's, so it is killed from here, by the pid the helper printed first."""
    monkeypatch.setattr(hinge_mod, "_SLACK_S", 0.05)
    hung = FakeProc(out=b"pid 4242\n", hang=True)
    calls = _spawning(monkeypatch, hung, FakeProc(out=b"/sim/data/hinge-control\n"))
    with pytest.raises(DeviceError, match="did not finish within .* and was stopped"):
        await helper.move(DUO, 0, 130, 0)
    assert hung.killed
    assert list(calls[1]) == ["ps", "-p", "4242", "-o", "comm="]
    assert killed == [(4242, hinge_mod.signal.SIGKILL)]


async def test_a_cancelled_move_is_killed_in_the_guest_too(helper, monkeypatch, killed):
    """A client that disconnects cancels the call: the helper must not go on."""
    hung = FakeProc(out=b"pid 77\n", hang=True)
    _spawning(monkeypatch, hung, FakeProc(out=b"hinge-control\n"))
    task = asyncio.create_task(helper.move(DUO, 0, 130, 800))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert killed == [(77, hinge_mod.signal.SIGKILL)]


async def test_a_reused_pid_is_not_killed(helper, monkeypatch, killed):
    """By the time of the kill the helper may have gone and its pid been reused."""
    monkeypatch.setattr(hinge_mod, "_SLACK_S", 0.05)
    _spawning(monkeypatch, FakeProc(out=b"pid 4242\n", hang=True),
              FakeProc(out=b"/Applications/Mail.app/Contents/MacOS/Mail\n"))
    with pytest.raises(DeviceError, match="was stopped"):
        await helper.move(DUO, 0, 130, 0)
    assert killed == []


async def test_a_kill_that_fails_says_so(helper, monkeypatch):
    """Report the outcome, not the request: "stopped" only when it was."""
    monkeypatch.setattr(hinge_mod, "_SLACK_S", 0.05)

    def refuse(_pid, _sig):
        raise PermissionError("not permitted")

    monkeypatch.setattr(hinge_mod.os, "kill", refuse)
    _spawning(monkeypatch, FakeProc(out=b"pid 4242\n", hang=True),
              FakeProc(out=b"hinge-control\n"))
    with pytest.raises(DeviceError, match="could not be stopped in the simulator"):
        await helper.move(DUO, 0, 130, 0)


async def test_the_deadline_is_in_the_near_future(helper, monkeypatch):
    """A deadline already past makes every command a no-op that exits 3."""
    calls = _spawning(monkeypatch, FakeProc())
    now = time.time()
    await helper.move(DUO, 0, 130, 0)
    argv = list(calls[0])
    deadline = int(argv[argv.index("--deadline") + 1])
    assert now < deadline + 1 <= now + hinge_mod._SLACK_S + 2


async def test_a_sweep_has_its_own_playing_time_on_top_of_the_slack(helper, monkeypatch):
    seen = []

    async def spawn(_udid, _argv, timeout):
        seen.append(timeout)
        return 0, "", ""

    monkeypatch.setattr(helper, "_spawn", spawn)
    await helper.move(DUO, 0, 130, 4000)
    assert seen == [4.0 + hinge_mod._SLACK_S]


async def test_an_orientation_is_sent_with_a_deadline(helper, monkeypatch):
    calls = _spawning(monkeypatch, FakeProc())
    await helper.orient(DUO, "landscape-left")
    argv = list(calls[0])
    assert "--deadline" in argv and argv[-2:] == ["orientation", "landscape-left"]


async def test_an_unreadable_angle_raises(helper, monkeypatch):
    _spawning(monkeypatch, FakeProc(out=b"something else\n"))
    with pytest.raises(DeviceError, match="could not read the hinge"):
        await helper.read_angle(DUO)


async def test_exit_1_is_no_reading_only_when_the_helper_says_so(helper, monkeypatch):
    """simctl spawn and dyld exit 1 too; those are failures, not a quiet guest."""
    _spawning(monkeypatch, FakeProc(code=1, err=b"dyld: Library not loaded"))
    with pytest.raises(DeviceError, match="could not read the hinge"):
        await helper.read_angle(DUO)


async def test_a_build_is_renamed_into_place(helper, monkeypatch, tmp_path):
    """Never written in place: another server may be spawning the old one."""
    (tmp_path / "hinge-control.sha256").write_text("stale")
    calls = _building(monkeypatch)
    await helper.ensure_binary()
    out = calls[1][calls[1].index("-o") + 1]
    assert out != str(helper.binary_path)
    assert helper.binary_path.read_bytes() == b"new"
    assert not Path(out).exists()


async def test_a_build_that_times_out_is_killed(helper, monkeypatch, tmp_path):
    """Left running, it would go on writing a binary the next build writes too."""
    (tmp_path / "hinge-control.sha256").write_text("stale")
    monkeypatch.setattr(hinge_mod, "_COMPILE_TIMEOUT_S", 0.05)
    clang = FakeProc(hang=True)
    _spawning(monkeypatch, FakeProc(out=b"/SDK\n"), clang)
    with pytest.raises(DeviceError, match="did not finish"):
        await helper.ensure_binary()
    assert clang.killed
    assert not (tmp_path / "hinge-control.sha256").exists()


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


async def test_flat_expects_the_inner_panel():
    ctrl = _controller(angles=(0.0, 180.0))
    result = await ctrl.set_hinge(DUO, pose="flat")
    ctrl.hinge.move.assert_awaited_once_with(DUO, 0.0, 180.0, 800)
    assert result["lit_panel"]["device_name"] == "primary-1" and "warning" not in result


async def test_an_angle_waits_for_the_panel_to_change_and_hold():
    """The first read after the helper exits is usually the panel from before:
    SpringBoard swaps and rotates about a second later."""
    ctrl = _controller(panels=[(COVER, 2), (COVER, 2), (COVER, 2), (INNER, 2), (INNER, 2)],
                       angles=(0.0, 100.0))
    result = await ctrl.set_hinge(DUO, angle=100, duration=0.3)
    ctrl.hinge.move.assert_awaited_once_with(DUO, 0.0, 100, 300)
    assert result["lit_panel"]["device_name"] == "primary-1"
    assert result["requested_angle"] == 100 and "warning" not in result and "note" not in result


async def test_an_angle_that_swaps_nothing_says_the_panel_is_unchanged(monkeypatch):
    ctrl = _controller(panels=[(COVER, 2)], angles=(0.0, 10.0))
    monkeypatch.setattr(ctrl, "_HINGE_SETTLE_S", 0.1)
    result = await ctrl.set_hinge(DUO, angle=10)
    assert result["lit_panel"]["device_name"] == "primary"
    assert "Neither the lit panel nor its orientation changed" in result["note"]


async def test_the_angle_is_read_back_not_assumed():
    ctrl = _controller(angles=(0.0, 127.5))
    result = await ctrl.set_hinge(DUO, pose="open")
    assert result["angle"] == 127.5


async def test_a_failed_read_back_does_not_undo_a_move(monkeypatch):
    """The hinge moved: an error now would have the caller fold it again."""
    ctrl = _controller()
    monkeypatch.setattr(ctrl, "_HINGE_SETTLE_S", 0.05)
    reads = [0.0]

    async def read(_u):
        if reads:
            return reads.pop()
        raise DeviceError("simctl went away")

    ctrl.hinge.read_angle = AsyncMock(side_effect=read)
    ctrl.sim_bridge.panels = AsyncMock(side_effect=[(COVER, 2), RuntimeError("bridge"),
                                                    RuntimeError("bridge"), RuntimeError("b")]
                                       + [RuntimeError("bridge")] * 50)
    result = await ctrl.set_hinge(DUO, pose="open")
    assert result["status"] == "ok" and result["angle"] is None
    assert "Could not read the lit panel" in result["note"]
    assert "Could not read the angle back" in result["note"]
    assert result["lit_panel"] == COVER


async def test_a_missing_panel_mid_wait_keeps_the_last_one(monkeypatch):
    ctrl = _controller(panels=[(COVER, 2), (None, 2), (INNER, 2)])
    result = await ctrl.set_hinge(DUO, pose="open")
    assert result["lit_panel"]["device_name"] == "primary-1"


async def test_the_change_is_marked_when_the_call_returns(monkeypatch):
    """Not only before the wait: an immediate tap must still find it recent."""
    ctrl = _controller(panels=[(COVER, 2)])
    monkeypatch.setattr(ctrl, "_HINGE_SETTLE_S", 0.3)
    started = time.monotonic()
    await ctrl.set_hinge(DUO, pose="open")
    assert ctrl._last_ui_change[DUO] >= started + 0.3


async def test_a_sweep_with_no_reading_says_it_was_set_at_once():
    ctrl = _controller(angles=(None, 130.0))
    result = await ctrl.set_hinge(DUO, pose="open", duration=2)
    ctrl.hinge.move.assert_awaited_once_with(DUO, 130.0, 130.0, 0)
    assert "set at once" in result["note"]


async def test_a_pose_and_an_orientation_both_happen():
    ctrl = _controller()
    result = await ctrl.set_hinge(DUO, pose="open", orientation="landscape-left")
    ctrl.hinge.move.assert_awaited_once()
    ctrl.hinge.orient.assert_awaited_once_with(DUO, "landscape-left")
    assert result["orientation"] == "landscape-left"


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


async def test_a_physical_iphone_is_refused():
    ctrl = _controller()
    ctrl._device_type_cache[DUO] = DeviceType.DEVICE
    with pytest.raises(InvalidDeviceRequestError, match="foldable simulator"):
        await ctrl.set_hinge(DUO, pose="open")
    ctrl.sim_bridge.panels.assert_not_called()


async def test_no_sim_bridge_is_unsupported_not_a_bad_request():
    ctrl = _controller()
    ctrl._sim_bridge_ok = False
    with pytest.raises(DeviceOperationUnsupportedError, match="sim-bridge"):
        await ctrl.set_hinge(DUO, pose="open")
    ctrl.hinge.move.assert_not_called()


async def test_a_duo_showing_no_screens_is_not_called_unfoldable():
    """A Duo still booting reports no screens: that is not "has one screen"."""
    ctrl = _controller(panels=[(None, 0)])
    with pytest.raises(InvalidDeviceRequestError, match="Is it booted"):
        await ctrl.set_hinge(DUO, pose="open")
    assert DUO not in ctrl._single_panel


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
