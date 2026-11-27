"""pymobiledevice3's command line reaches a Wi-Fi-only phone through the
tunnel macOS keeps to it (#781): screenshots, the device log and crash
reports, each given `--rsd` into a tunnel this server holds while it runs."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from server.device.ios import native_tunnel
from server.device.ios.native_tunnel import NativeTunnelUnavailable
from server.device.ios.pmd3 import Pmd3Backend
from server.models import DeviceError

PHONE = "00008030-000C59623A69802E"
HOST, PORT = "fd22::1", 62589
PNG = b"\x89PNG\r\n\x1a\nfake"
TUNNELD = "server.device.ios.tunneld"


@pytest.fixture
def pool(_no_native_tunnels, monkeypatch):
    """Native opens succeed with a fake tunnel; `pool.held()` is its lease count."""
    from server.device.ios import devicectl

    monkeypatch.setattr(devicectl, "_identity_aliases", {})
    opened: list[native_tunnel._Held] = []

    async def _open(udid, serial):
        held = native_tunnel._Held(udid=udid, tunnel=MagicMock(aclose=AsyncMock()),
                                   host=HOST, port=PORT, identity="remoted")
        opened.append(held)
        return held

    monkeypatch.setattr(_no_native_tunnels, "_open", _open)
    _no_native_tunnels.opened = opened
    _no_native_tunnels.leases = lambda: sum(h.leases for h in opened)
    return _no_native_tunnels


def _exec_writing(png: bytes | None, seen: list, leases=lambda: 0, returncode: int = 0,
                  stderr: bytes = b""):
    """A fake subprocess: records its argv and the leases held while it runs,
    and writes `png` to its last argument, as `dvt screenshot` does."""
    async def fake_exec(*argv, **kwargs):
        seen.append((list(argv), leases()))
        if png is not None and returncode == 0:
            Path(argv[-1]).write_bytes(png)
        proc = MagicMock()
        proc.returncode = returncode
        proc.communicate = AsyncMock(return_value=(b"", stderr))
        return proc
    return fake_exec


def _no_tunneld():
    return (patch(f"{TUNNELD}.is_tunneld_running", AsyncMock(return_value=True)),
            patch(f"{TUNNELD}.resolve_tunnel_udid", AsyncMock(return_value=None)),
            patch(f"{TUNNELD}.find_pymobiledevice3_binary", return_value=Path("/bin/pmd3")))


class TestScreenshot:
    async def test_a_wifi_phone_is_shot_through_apples_tunnel(self, pool):
        seen: list = []
        a, b, c = _no_tunneld()
        with a, b, c, patch("asyncio.create_subprocess_exec",
                            _exec_writing(PNG, seen, pool.leases)):
            assert await Pmd3Backend().screenshot(PHONE, os_version="27.2.1") == PNG

        argv, leases_while_running = seen[0]
        assert argv[1:4] == ["developer", "dvt", "screenshot"]
        assert argv[4:7] == ["--rsd", HOST, str(PORT)]
        assert "--udid" not in argv
        assert leases_while_running == 1          # held while the child used it
        assert pool.leases() == 0                 # and given back after

    async def test_a_phone_before_ios_17_does_not_ask_for_it(self, pool):
        seen: list = []
        a, b, c = _no_tunneld()
        with a, b, c, patch("asyncio.create_subprocess_exec", _exec_writing(PNG, seen)):
            await Pmd3Backend().screenshot(PHONE, os_version="iOS 16.7.10")

        assert pool.opened == []
        assert seen[0][0][4:6] == ["--udid", PHONE]

    async def test_with_no_tunnel_at_all_it_falls_back_and_says_why(self, _no_native_tunnels):
        seen: list = []
        a, b, c = _no_tunneld()
        with a, b, c, patch("asyncio.create_subprocess_exec",
                            _exec_writing(None, seen, returncode=1,
                                          stderr=b"Device not found")):
            with pytest.raises(DeviceError) as e:
                await Pmd3Backend().screenshot(PHONE)

        assert seen[0][0][4:6] == ["--udid", PHONE]
        assert "not opened from a test" in str(e.value)     # the native reason

    async def test_a_tunneld_tunnel_still_comes_first(self, pool):
        seen: list = []
        with patch(f"{TUNNELD}.is_tunneld_running", AsyncMock(return_value=True)), \
                patch(f"{TUNNELD}.resolve_tunnel_udid", AsyncMock(return_value=PHONE)), \
                patch(f"{TUNNELD}.find_pymobiledevice3_binary", return_value=Path("/bin/pmd3")), \
                patch("asyncio.create_subprocess_exec", _exec_writing(PNG, seen)):
            await Pmd3Backend().screenshot(PHONE)

        assert seen[0][0][4:6] == ["--tunnel", PHONE]
        assert pool.opened == []

    async def test_a_cancelled_shot_gives_the_tunnel_back(self, pool):
        async def hangs(*argv, **kwargs):
            proc = MagicMock(returncode=None)

            async def communicate():
                await asyncio.sleep(10)
            proc.communicate = communicate
            return proc

        a, b, c = _no_tunneld()
        with a, b, c, patch("asyncio.create_subprocess_exec", hangs):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(Pmd3Backend().screenshot(PHONE), 0.1)
        assert pool.leases() == 0


class TestDeviceLog:
    def _adapter(self):
        from server.sources.device_log import PhysicalDeviceLogAdapter

        return PhysicalDeviceLogAdapter(udid=PHONE, on_entry=AsyncMock())

    async def test_a_wifi_phones_log_rides_apples_tunnel_while_it_runs(self, pool, monkeypatch):
        from server.sources import device_log

        monkeypatch.setattr(device_log, "resolve_tunnel_udid", AsyncMock(return_value=None))
        monkeypatch.setattr(device_log, "find_pymobiledevice3_binary", lambda: Path("/bin/pmd3"))
        monkeypatch.setattr(device_log, "_supports_json", AsyncMock(return_value=True))
        adapter = self._adapter()

        cmd = await adapter._build_command()
        assert cmd[3:6] == ["--rsd", HOST, str(PORT)]
        assert pool.leases() == 1

        # The run takes the hold over and gives it back when its output ends.
        proc = MagicMock(returncode=None)
        proc.stdout = _lines(b"")
        proc.stderr = _lines(b"")
        proc.wait = AsyncMock(return_value=0)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
            adapter._build_command = AsyncMock(return_value=cmd)
            await adapter.start()
            await asyncio.wait_for(adapter._read_task, 2)
        assert pool.leases() == 0

    async def test_a_run_that_fails_to_start_gives_it_back(self, pool, monkeypatch):
        from server.sources import device_log

        monkeypatch.setattr(device_log, "resolve_tunnel_udid", AsyncMock(return_value=None))
        monkeypatch.setattr(device_log, "find_pymobiledevice3_binary", lambda: Path("/bin/pmd3"))
        monkeypatch.setattr(device_log, "_supports_json", AsyncMock(return_value=True))
        adapter = self._adapter()
        with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=OSError("no"))):
            await adapter.start()
        assert pool.leases() == 0
        assert "Failed to start" in adapter._error

    def _no_tunneld_here(self, monkeypatch):
        from server.sources import device_log

        monkeypatch.setattr(device_log, "resolve_tunnel_udid", AsyncMock(return_value=None))
        monkeypatch.setattr(device_log, "find_pymobiledevice3_binary", lambda: Path("/bin/pmd3"))
        monkeypatch.setattr(device_log, "_supports_json", AsyncMock(return_value=True))

    async def test_a_phone_before_ios_17_goes_straight_to_udid(self, pool, monkeypatch):
        from server.sources.device_log import PhysicalDeviceLogAdapter

        self._no_tunneld_here(monkeypatch)
        adapter = PhysicalDeviceLogAdapter(udid=PHONE, on_entry=AsyncMock(),
                                           os_version="16.7.10")
        cmd = await adapter._build_command()
        assert cmd[3:5] == ["--udid", PHONE]
        assert pool.opened == []

    async def test_a_cancelled_start_gives_the_hold_back(self, pool, monkeypatch):
        """Cancelled after the command took the hold, before a run took it over."""
        from server.sources import device_log

        self._no_tunneld_here(monkeypatch)
        adapter = self._adapter()

        async def hangs(binary):
            await asyncio.sleep(10)
        monkeypatch.setattr(device_log, "_supports_json", hangs)
        task = asyncio.create_task(adapter.start())
        for _ in range(100):
            await asyncio.sleep(0.01)
            if pool.leases():
                break
        assert pool.leases() == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert pool.leases() == 0

    async def test_stop_gives_back_a_hold_no_run_took(self, pool, monkeypatch):
        self._no_tunneld_here(monkeypatch)
        adapter = self._adapter()
        await adapter._build_command()
        assert pool.leases() == 1
        await adapter.stop()
        assert pool.leases() == 0

    async def test_with_no_tunnel_it_falls_back_to_udid(self, _no_native_tunnels, monkeypatch):
        from server.sources import device_log

        monkeypatch.setattr(device_log, "resolve_tunnel_udid", AsyncMock(return_value=None))
        monkeypatch.setattr(device_log, "find_pymobiledevice3_binary", lambda: Path("/bin/pmd3"))
        monkeypatch.setattr(device_log, "_supports_json", AsyncMock(return_value=True))
        cmd = await self._adapter()._build_command()
        assert cmd[3:5] == ["--udid", PHONE]


def _lines(data: bytes):
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


class TestCrashReports:
    async def _run_capturing(self, monkeypatch, pool=None, out="/DiagnosticLogs\n"):
        from server.sources import ios_crash

        seen: list = []

        async def fake_run(argv, what, timeout):
            seen.append((list(argv), pool.leases() if pool is not None else 0))
            return out, ""

        monkeypatch.setattr(ios_crash, "_run", fake_run)
        return ios_crash, seen

    async def test_a_phone_usbmux_lists_keeps_udid(self, pool, monkeypatch):
        ios_crash, seen = await self._run_capturing(monkeypatch, pool)
        monkeypatch.setattr(ios_crash, "_on_usbmux", AsyncMock(return_value=True))
        await ios_crash.list_reports(["pmd3"], PHONE)
        assert seen[0][0][3:5] == ["--udid", PHONE]
        assert pool.opened == []

    async def test_could_not_ask_usbmux_keeps_udid(self, pool, monkeypatch):
        ios_crash, seen = await self._run_capturing(monkeypatch, pool)   # conftest: None
        await ios_crash.list_reports(["pmd3"], PHONE)
        assert seen[0][0][3:5] == ["--udid", PHONE]

    async def test_a_wifi_phone_is_listed_through_apples_tunnel(self, pool, monkeypatch):
        ios_crash, seen = await self._run_capturing(monkeypatch, pool)
        monkeypatch.setattr(ios_crash, "_on_usbmux", AsyncMock(return_value=False))
        await ios_crash.list_reports(["pmd3"], PHONE)
        argv, leases = seen[0]
        assert argv[3:6] == ["--rsd", HOST, str(PORT)]
        assert leases == 1 and pool.leases() == 0

    async def test_pull_and_delete_ride_it_too(self, pool, monkeypatch, tmp_path):
        ios_crash, seen = await self._run_capturing(
            monkeypatch, pool, out='{"removed": ["a.ips"], "failed": []}\n')
        monkeypatch.setattr(ios_crash, "_on_usbmux", AsyncMock(return_value=False))
        with pytest.raises(ios_crash.IosCrashError):     # the fake pull copies nothing
            await ios_crash.pull_reports(["pmd3"], PHONE, ["a.ips"], tmp_path)
        assert await ios_crash.remove_reports(PHONE, ["a.ips"]) == (["a.ips"], [])

        pull, delete = seen[0][0], seen[1][0]
        assert pull[3:6] == ["--rsd", HOST, str(PORT)]
        assert delete[3:7] == ["--rsd", HOST, str(PORT), "a.ips"]
        assert pool.leases() == 0

    async def test_a_wifi_phone_with_no_tunnel_says_both_reasons(self, _no_native_tunnels,
                                                                monkeypatch):
        ios_crash, seen = await self._run_capturing(monkeypatch)
        monkeypatch.setattr(ios_crash, "_on_usbmux", AsyncMock(return_value=False))
        with pytest.raises(ios_crash.IosCrashError, match="usbmuxd does not list.*not opened"):
            await ios_crash.list_reports(["pmd3"], PHONE)
        assert seen == []

    def test_the_delete_script_takes_either_target(self):
        """The delete runs as its own process; both argument forms parse."""
        from server.sources.ios_crash import _REMOVE_SCRIPT

        compile(_REMOVE_SCRIPT, "<remove>", "exec")
        assert 'args[0] == "--rsd"' in _REMOVE_SCRIPT
        assert "rsd_handshake_uuid(args[1])" in _REMOVE_SCRIPT


class TestTheCrashCheck:
    """The push reader's check for #669's crash (`_crash_reports_since`)."""

    def _ctrl(self, monkeypatch, error):
        from server.device import controller as controller_mod
        from server.sources import ios_crash

        ctrl = controller_mod.DeviceController()
        ctrl.crash_service_udid = AsyncMock(return_value=PHONE)
        monkeypatch.setattr(ios_crash, "command", lambda: ["pmd3"])
        monkeypatch.setattr(ios_crash, "list_reports", AsyncMock(side_effect=error))
        return ctrl

    async def test_a_phone_with_no_route_at_all_is_not_asked_again(self, monkeypatch):
        from server.sources import ios_crash

        error = ios_crash.IosCrashError("usbmuxd does not list it, and no tunnel")
        error.__cause__ = NativeTunnelUnavailable("not there")
        ctrl = self._ctrl(monkeypatch, error)
        assert await ctrl._crash_reports_since(PHONE, 0.0) is None

    async def test_any_other_failure_is_raised_for_the_retry(self, monkeypatch):
        from server.sources import ios_crash

        ctrl = self._ctrl(monkeypatch, ios_crash.IosCrashError("crash ls timed out"))
        with pytest.raises(ios_crash.IosCrashError, match="timed out"):
            await ctrl._crash_reports_since(PHONE, 0.0)


class TestTheCrashUdid:
    async def test_a_wifi_phone_is_read_by_its_hardware_udid(self, monkeypatch):
        from server.device import controller as controller_mod
        from server.device.ios import devicectl

        core = "B34C4EE9-AF48-53C6-BD13-2BFA66E7EE91"
        monkeypatch.setattr(devicectl, "_identity_aliases", {core: core, PHONE: core})
        ctrl = controller_mod.DeviceController()
        ctrl.get_libimobiledevice_udid = AsyncMock(return_value=None)     # not on USB

        assert await ctrl.crash_service_udid(core) == PHONE

    async def test_usb_comes_first(self, monkeypatch):
        from server.device import controller as controller_mod

        ctrl = controller_mod.DeviceController()
        ctrl.get_libimobiledevice_udid = AsyncMock(return_value="LIBUDID")
        assert await ctrl.crash_service_udid("anything") == "LIBUDID"

    async def test_nothing_known_is_none(self, monkeypatch):
        from server.device import controller as controller_mod
        from server.device.ios import devicectl

        monkeypatch.setattr(devicectl, "_identity_aliases", {})
        ctrl = controller_mod.DeviceController()
        ctrl.get_libimobiledevice_udid = AsyncMock(return_value=None)
        assert await ctrl.crash_service_udid("B34C4EE9-AF48-53C6-BD13-2BFA66E7EE91") is None


def test_unavailable_is_an_exception_callers_can_catch():
    assert issubclass(NativeTunnelUnavailable, Exception)
