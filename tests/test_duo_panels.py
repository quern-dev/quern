"""A simulator with several built-in panels -- iPhone Duo -- is captured on
the lit one (#536).

Unfolded, the Duo lights its inner panel (screen 3) and darkens the cover
(screen 1), and `simctl io screenshot` captures screen 1 unless told
otherwise: every screenshot was black. sim-bridge says which panel is lit;
these pin how the server asks and what it does with the answer. The rotations
themselves are checked in Swift, by scripts/sim-bridge-selftest.sh.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from server.device.controller import DeviceController
from server.device.ios.sim_bridge import SimBridgeBackend
from server.device.ios.simctl import SimctlBackend
from server.models import DeviceError

DUO = "0AA1FC84-86D2-4488-98F6-5CE07DD27A6B"
INNER = {"screen_id": 3, "device_name": "primary-1", "orientation": 3,
         "size": [951, 669], "native_size": [669, 951]}


# -- simctl ------------------------------------------------------------------


async def test_a_display_is_passed_to_simctl(tmp_path):
    simctl = SimctlBackend()
    seen = []

    async def run(*args, **_kwargs):
        seen.append(args)
        from pathlib import Path
        Path(args[-1]).write_bytes(b"png")
        return "", ""

    simctl._run_simctl = run
    assert await simctl.screenshot(DUO, display=3) == b"png"
    assert seen[0][:4] == ("io", DUO, "screenshot", "--display=3")


async def test_no_display_is_simctls_default(tmp_path):
    simctl = SimctlBackend()
    seen = []

    async def run(*args, **_kwargs):
        seen.append(args)
        from pathlib import Path
        Path(args[-1]).write_bytes(b"png")
        return "", ""

    simctl._run_simctl = run
    await simctl.screenshot(DUO)
    assert not any(a.startswith("--display") for a in seen[0])


# -- sim-bridge --------------------------------------------------------------


def _bridge(answer: dict) -> SimBridgeBackend:
    mgr = MagicMock()
    mgr.admit = MagicMock(return_value=_Admit())
    mgr.send = AsyncMock(return_value=answer)
    return SimBridgeBackend(mgr)


class _Admit:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


async def test_the_lit_panel_is_read_from_the_bridge():
    bridge = _bridge({"ok": True, "count": 2, "panels": [], "lit": INNER})
    assert await bridge.panels(DUO) == (INNER, 2)
    bridge._mgr.send.assert_awaited_once_with({"cmd": "panels", "udid": DUO})


async def test_a_single_panel_device_has_no_lit_panel():
    """Null from the bridge is every ordinary simulator: the old path."""
    bridge = _bridge({"ok": True, "count": 1, "panels": [], "lit": None})
    assert await bridge.panels(DUO) == (None, 1)


async def test_a_lit_panel_of_the_wrong_shape_is_none():
    assert await _bridge({"ok": True, "count": 2, "lit": ["3"]}).panels(DUO) == (None, 2)


async def test_a_missing_count_is_none_found():
    """Not one: only a definite answer may mark a device single-panel."""
    assert await _bridge({"ok": True, "lit": None}).panels(DUO) == (None, 0)


async def test_a_bridge_that_cannot_read_the_panels_says_so():
    """Not None: "no panel" and "could not ask" must not look alike."""
    with pytest.raises(DeviceError, match="could not read the panels"):
        await _bridge({"ok": False, "error": "device not found"}).panels(DUO)


# -- the controller ----------------------------------------------------------


def _controller(*, bridge_ok=True, lit=None, count=None, raises=None) -> DeviceController:
    ctrl = DeviceController()
    ctrl._sim_bridge_ok = bridge_ok
    ctrl.sim_bridge = MagicMock()
    count = count if count is not None else (2 if lit else 1)
    ctrl.sim_bridge.panels = AsyncMock(return_value=(lit, count), side_effect=raises)
    ctrl.simctl = MagicMock()
    ctrl.simctl.screenshot = AsyncMock(return_value=b"png")
    ctrl.resolve_udid = AsyncMock(return_value=DUO)
    ctrl._is_android = lambda _u: False
    ctrl._is_physical = lambda _u: False
    return ctrl


async def test_the_lit_panel_is_what_is_captured():
    ctrl = _controller(lit=INNER)
    await ctrl.raw_screenshot(DUO)
    ctrl.simctl.screenshot.assert_awaited_once_with(DUO, display=3)


async def test_an_ordinary_simulator_is_captured_as_before():
    ctrl = _controller(lit=None)
    await ctrl.raw_screenshot(DUO)
    ctrl.simctl.screenshot.assert_awaited_once_with(DUO, display=None)


async def test_a_failed_panel_read_still_captures():
    """The default screen is what was captured before this existed; failing
    the screenshot over which screen would be worse."""
    ctrl = _controller(raises=DeviceError("bridge gone", tool="sim-bridge"))
    assert await ctrl.raw_screenshot(DUO) == b"png"
    ctrl.simctl.screenshot.assert_awaited_once_with(DUO, display=None)


async def test_a_failure_of_any_kind_still_captures():
    """Not only DeviceError: a bridge timing out or gone raises others."""
    for exc in (TimeoutError(), OSError("broken pipe"), RuntimeError("bad answer")):
        ctrl = _controller(raises=exc)
        assert await ctrl.raw_screenshot(DUO) == b"png"
        ctrl.simctl.screenshot.assert_awaited_once_with(DUO, display=None)


async def test_a_one_panel_device_is_not_asked_again():
    """Every screenshot went through sim-bridge's lock to ask (review); a
    device type never gains a panel, so one answer is enough."""
    ctrl = _controller(lit=None, count=1)
    await ctrl.raw_screenshot(DUO)
    await ctrl.raw_screenshot(DUO)
    ctrl.sim_bridge.panels.assert_awaited_once()
    assert ctrl.simctl.screenshot.await_count == 2


async def test_a_device_with_no_screens_yet_is_asked_again():
    """Booting, it reports none: that is not "one panel"."""
    ctrl = _controller(lit=None, count=0)
    await ctrl.raw_screenshot(DUO)
    await ctrl.raw_screenshot(DUO)
    assert ctrl.sim_bridge.panels.await_count == 2


async def test_a_duo_is_asked_every_time():
    """Its lit panel changes with the fold."""
    ctrl = _controller(lit=INNER)
    await ctrl.raw_screenshot(DUO)
    await ctrl.raw_screenshot(DUO)
    assert ctrl.sim_bridge.panels.await_count == 2


async def test_the_annotated_screenshot_is_of_the_lit_panel_too():
    ctrl = _controller(lit=INNER)
    ctrl.get_ui_elements = AsyncMock(return_value=([], DUO))
    from unittest.mock import patch
    with patch("server.device.controller_ui.annotate_screenshot", return_value=(b"x", "image/png")):
        await ctrl.screenshot_annotated(udid=DUO)
    ctrl.simctl.screenshot.assert_awaited_once_with(DUO, display=3)


async def test_without_sim_bridge_the_bridge_is_not_asked():
    ctrl = _controller(bridge_ok=False, lit=INNER)
    await ctrl.raw_screenshot(DUO)
    ctrl.sim_bridge.panels.assert_not_called()
    ctrl.simctl.screenshot.assert_awaited_once_with(DUO, display=None)


async def test_a_screen_id_that_is_not_a_number_is_not_passed():
    ctrl = _controller(lit={**INNER, "screen_id": "3"})
    await ctrl.raw_screenshot(DUO)
    ctrl.simctl.screenshot.assert_awaited_once_with(DUO, display=None)
