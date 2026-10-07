"""Simulator settings written to disk (server/device/ios/sim_settings.py).

Everything runs against a temporary devices directory and a fake simctl: no
simulator is booted, shut down or written to.
"""

from __future__ import annotations

import asyncio
import json
import plistlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.device.ios import sim_settings
from server.models import DeviceError, RebootRequiredError

UDID = "6401A02A-FCAC-42E6-BE10-EB3AAD523A39"
CATALOG = sim_settings.catalog()
RESTRICTIONS = CATALOG["files"]["restrictions"]
KEYBOARD = CATALOG["files"]["keyboard"]


@pytest.fixture
def device(tmp_path, monkeypatch):
    """A simulator directory as a booted-once iOS 26.5 simulator leaves it."""
    monkeypatch.setattr(sim_settings, "DEVICES_DIR", tmp_path)
    root = tmp_path / UDID
    (root / Path(RESTRICTIONS).parent).mkdir(parents=True)
    _write(root / RESTRICTIONS, {"restrictedBool": {"allowCamera": {"value": True}}})
    _write(root / "device.plist", {"runtime": "com.apple.CoreSimulator.SimRuntime.iOS-26-5"})
    return root


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(data, fmt=plistlib.FMT_BINARY))


def _read(path: Path) -> dict:
    return plistlib.loads(path.read_bytes())


class FakeSimctl:
    def __init__(self, booted: bool):
        self.booted = booted
        self.calls: list[str] = []
        self.on_boot = None

    async def list_devices(self):
        state = "booted" if self.booted else "shutdown"
        return [SimpleNamespace(udid=UDID, state=SimpleNamespace(value=state))]

    async def shutdown(self, udid):
        self.calls.append("shutdown")
        self.booted = False

    async def boot(self, udid):
        self.calls.append("boot")
        self.booted = True
        if self.on_boot:
            self.on_boot()

    async def wait_until_booted(self, udid):
        self.calls.append("wait")


# -- reading ------------------------------------------------------------------


def test_a_fresh_simulator_reads_as_the_default(device):
    assert sim_settings.read_state(UDID, "auto_correction") == "on"
    assert sim_settings.read_state(UDID, "auto_capitalization") == "on"


def test_a_written_restriction_reads_back(device):
    _write(device / RESTRICTIONS, {"restrictedBool": {
        "allowAutoCorrection": {"ask": False, "value": False}}})
    assert sim_settings.read_state(UDID, "auto_correction") == "off"


def test_an_unreadable_file_is_not_read_as_the_default(device):
    """"Could not ask" must not read as "on": a status built on that would
    report a setting the caller had turned off as still on."""
    (device / RESTRICTIONS).write_bytes(b"not a plist")
    assert sim_settings.read_state(UDID, "auto_correction") is None


def test_a_value_the_catalog_does_not_know_is_unknown(device):
    _write(device / RESTRICTIONS, {"restrictedBool": {"allowAutoCorrection": "maybe"}})
    assert sim_settings.read_state(UDID, "auto_correction") == "unknown"


def test_the_runtime_is_read_from_the_device(device):
    assert sim_settings.runtime_of(UDID) == "iOS 26.5"


# -- writing ------------------------------------------------------------------


def test_every_key_the_setting_covers_is_written(device):
    """Settings writes the restriction and keyboard keys together; writing one
    left the other saying the opposite."""
    sim_settings.write_state(UDID, "auto_correction", "off")
    restrictions = _read(device / RESTRICTIONS)["restrictedBool"]
    assert restrictions["allowAutoCorrection"] == {"ask": False, "value": False}
    keyboard = _read(device / KEYBOARD)
    assert keyboard["KeyboardAutocorrection"] is False
    assert keyboard["HWKeyboardAutocorrection"] is False, \
        "the hardware-keyboard switch is the one quern's own typing goes through"


def test_other_contents_of_the_file_are_kept(device):
    sim_settings.write_state(UDID, "password_autofill", "off")
    restrictions = _read(device / RESTRICTIONS)["restrictedBool"]
    assert restrictions["allowCamera"] == {"value": True}
    assert restrictions["allowPasswordAutoFill"] == {"ask": False, "value": False}


def test_files_are_written_as_binary_plists(device):
    sim_settings.write_state(UDID, "auto_capitalization", "off")
    assert (device / KEYBOARD).read_bytes().startswith(b"bplist00")


def test_a_simulator_never_booted_is_refused_rather_than_given_a_new_file(device):
    """UserSettings.plist is created at first boot, with contents the system
    expects; a fresh file written in its place would replace them."""
    (device / RESTRICTIONS).unlink()
    with pytest.raises(DeviceError, match="boot this simulator once"):
        sim_settings.write_state(UDID, "password_autofill", "off")
    assert not (device / RESTRICTIONS).exists()


def test_an_unreadable_file_is_not_overwritten(device):
    (device / RESTRICTIONS).write_bytes(b"garbage")
    with pytest.raises(DeviceError, match="cannot read"):
        sim_settings.write_state(UDID, "password_autofill", "off")
    assert (device / RESTRICTIONS).read_bytes() == b"garbage"


# -- applying -----------------------------------------------------------------


def test_a_setting_already_in_place_changes_nothing(device):
    simctl = FakeSimctl(booted=True)
    sim_settings.write_state(UDID, "auto_correction", "off")
    result = asyncio.run(sim_settings.set_setting(
        simctl, UDID, "auto_correction", "off", reboot=False))
    assert result["changed"] is False and result["rebooted"] is False
    assert simctl.calls == [], "rebooted for a setting that was already right"


def test_a_shut_down_simulator_is_written_without_a_reboot(device):
    simctl = FakeSimctl(booted=False)
    result = asyncio.run(sim_settings.set_setting(
        simctl, UDID, "auto_correction", "off", reboot=False))
    assert result["changed"] is True and result["rebooted"] is False
    assert simctl.calls == []
    assert sim_settings.read_state(UDID, "auto_correction") == "off"


def test_a_booted_simulator_is_not_rebooted_without_permission(device):
    """A reboot ends the running app. Called in the middle of a test, a silent
    one would take the test's app state with it."""
    simctl = FakeSimctl(booted=True)
    with pytest.raises(RebootRequiredError, match="reboot: true"):
        asyncio.run(sim_settings.set_setting(
            simctl, UDID, "auto_correction", "off", reboot=False))
    assert simctl.calls == []
    assert sim_settings.read_state(UDID, "auto_correction") == "on", "wrote anyway"


def test_with_permission_a_booted_simulator_is_shut_down_written_and_booted(device):
    simctl = FakeSimctl(booted=True)
    order: list[str] = []
    simctl.on_boot = lambda: order.append(sim_settings.read_state(UDID, "auto_correction"))
    result = asyncio.run(sim_settings.set_setting(
        simctl, UDID, "auto_correction", "off", reboot=True))
    assert simctl.calls == ["shutdown", "boot", "wait"]
    assert order == ["off"], "booted before the change was written"
    assert result["changed"] is True and result["rebooted"] is True


def test_a_change_that_does_not_read_back_is_an_error(device):
    """The write went somewhere nothing reads, or the boot put it back."""
    simctl = FakeSimctl(booted=True)
    simctl.on_boot = lambda: sim_settings.write_state(UDID, "auto_correction", "on")
    with pytest.raises(DeviceError, match="reads back as on"):
        asyncio.run(sim_settings.set_setting(
            simctl, UDID, "auto_correction", "off", reboot=True))


def test_an_unverified_runtime_is_said_so(device):
    _write(device / "device.plist", {"runtime": "com.apple.CoreSimulator.SimRuntime.iOS-17-5"})
    result = asyncio.run(sim_settings.set_setting(
        FakeSimctl(booted=False), UDID, "password_autofill", "off", reboot=False))
    assert result["verified_here"] is False
    assert "iOS 17.5" in result["warning"]


def test_a_verified_runtime_carries_no_warning(device):
    result = asyncio.run(sim_settings.set_setting(
        FakeSimctl(booted=False), UDID, "password_autofill", "off", reboot=False))
    assert result["verified_here"] is True
    assert "warning" not in result


def test_an_unknown_setting_names_the_known_ones(device):
    with pytest.raises(DeviceError, match="auto_correction"):
        asyncio.run(sim_settings.set_setting(
            FakeSimctl(booted=False), UDID, "dark_mode", "on", reboot=False))


# -- the catalog --------------------------------------------------------------


def test_every_entry_is_complete():
    for name, entry in CATALOG["settings"].items():
        assert entry["writes"], name
        assert entry["writes"][0].get("absent") in ("on", "off"), \
            f"{name}: the first write's absent state is what a fresh simulator reads as"
        for write in entry["writes"]:
            assert write["file"] in CATALOG["files"], name
            assert set(write["values"]) == {"on", "off"}, name
        assert entry.get("verified"), f"{name} records no verification at all"


def test_the_mcp_tool_offers_exactly_the_catalog():
    """A setting in the catalog that the tool's enum leaves out cannot be set
    over MCP; one in the enum that the catalog lacks fails at the server."""
    source = (Path(__file__).parent.parent / "mcp/src/tools/device.ts").read_text()
    block = source[source.index('registerTool("set_simulator_setting"'):]
    names = re.search(r"name: z\.enum\(\[([^\]]*)\]\)", block).group(1)
    assert set(re.findall(r'"([a-z_]+)"', names)) == set(CATALOG["settings"])


def test_the_catalog_is_valid_json():
    json.loads(sim_settings.CATALOG_PATH.read_text())


# -- the boot wait --------------------------------------------------------------


def test_a_boot_that_never_finishes_fails_in_bounded_time(monkeypatch):
    """`simctl boot` returns as soon as the boot starts; waiting on bootstatus
    with no bound would hang the request that asked for the reboot."""
    from server.device.ios import simctl as simctl_module

    class Hung:
        returncode = None
        killed = False

        async def communicate(self):
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            return self.returncode

    proc = Hung()

    async def spawn(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(simctl_module.asyncio, "create_subprocess_exec", spawn)
    backend = simctl_module.SimctlBackend()
    with pytest.raises(DeviceError, match="did not finish booting"):
        asyncio.run(backend.wait_until_booted(UDID, timeout=0.05))
    assert proc.killed, "the bootstatus process was left running"
