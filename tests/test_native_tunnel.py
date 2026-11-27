"""Apple's own tunnel to a phone quern's tunneld has none to (#781)."""

from __future__ import annotations

import asyncio
import gc
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from server.device.ios import native_tunnel
from server.device.ios.native_tunnel import NativeTunnelUnavailable, TunnelAddress, rsd_for
from server.models import NoTunnelError

PHONE = "00008030-000C59623A69802E"
IDENTITY = "remoted-identity"
CORE = "53DA57AA-COREDEVICE"
USB = ("fd35::1", 61952)
TUNNELD = "server.device.ios.tunneld"
SERVER = Path(__file__).resolve().parent.parent / "server"


class FakeTunnel:
    """A held native tunnel: open and closed are all a test needs to see."""

    def __init__(self, port: int = 62589) -> None:
        self.port = port
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FakeRsd:
    made: list[FakeRsd] = []

    def __init__(self, address, handshake_uuid=None):
        self.address, self.handshake_uuid = address, handshake_uuid
        self.fail_connect: BaseException | None = None
        self.stall = False
        self.closes = 0
        FakeRsd.made.append(self)

    async def connect(self):
        if self.stall:
            await asyncio.sleep(10)
        if self.fail_connect is not None:
            raise self.fail_connect

    async def close(self):
        self.closes += 1


LIB = SimpleNamespace(RemoteServiceDiscoveryService=FakeRsd)


@pytest.fixture
def pool(_no_native_tunnels, monkeypatch):
    """The suite's pool, with `_open` handing out fake tunnels and counting."""
    opened: list[FakeTunnel] = []

    async def _open(udid, serial):
        await asyncio.sleep(0)     # yield, so concurrent callers overlap here
        tunnel = FakeTunnel(62589 + len(opened))
        opened.append(tunnel)
        pool.serials.append(serial)
        return native_tunnel._Held(udid=udid, tunnel=tunnel, host="fd22::1",
                                   port=tunnel.port, identity=IDENTITY)

    pool = _no_native_tunnels
    pool.opened, pool.serials = opened, []
    monkeypatch.setattr(pool, "_open", _open)
    return pool


async def _settle():
    """Let tasks `close_soon` started run."""
    for _ in range(3):
        await asyncio.sleep(0)


def _own_connections(root: Path) -> list[str]:
    """Every RSD built under `root` without naming its identity: outside
    `rsd_for`, a call must pass `handshake_uuid=` itself, as the crash delete
    script does in its own process (`rsd_handshake_uuid`)."""
    found = []
    for path in root.rglob("*.py"):
        if path.name == "native_tunnel.py":
            continue
        text = path.read_text()
        for match in re.finditer(r"RemoteServiceDiscoveryService\s*\(", text):
            if "handshake_uuid=" not in _arguments(text, match.end()):
                line = text.count("\n", 0, match.start()) + 1
                found.append(f"{path.relative_to(root)}:{line}")
    return found


def _arguments(text: str, start: int) -> str:
    """The text of a call's arguments, from just past its "(" to its own ")"."""
    depth = 1
    for i in range(start, len(text)):
        depth += {"(": 1, ")": -1}.get(text[i], 0)
        if depth == 0:
            return text[start:i]
    return text[start:]


class TestOneWayToConnect:
    def test_no_call_site_builds_a_connection_of_its_own(self):
        """A connection on Apple's tunnel with quern's ordinary identity makes
        the phone close every service listener on it, so every RSD quern makes
        goes through `rsd_for`, which picks the identity."""
        assert _own_connections(SERVER) == []

    def test_the_scan_finds_a_site_that_builds_its_own(self, tmp_path):
        """The scan above can fail: a planted site, spelled as the old ones were."""
        (tmp_path / "device").mkdir()
        (tmp_path / "device" / "planted.py").write_text(
            "async def f(lib, address):\n    rsd = lib.RemoteServiceDiscoveryService(address)\n")
        (tmp_path / "native_tunnel.py").write_text("RemoteServiceDiscoveryService(address)\n")
        (tmp_path / "named.py").write_text(
            "rsd = RemoteServiceDiscoveryService(\n    (h, int(p)), handshake_uuid=who(h))\n")
        # Named later in the file, outside the call: still a bare one.
        (tmp_path / "device" / "later.py").write_text(
            "rsd = lib.RemoteServiceDiscoveryService((h, p))\nx = f(handshake_uuid=None)\n")
        assert sorted(_own_connections(tmp_path)) == ["device/later.py:1", "device/planted.py:2"]

    def test_a_tunneld_address_is_connected_to_as_before(self):
        rsd = rsd_for(LIB, ("fd35::1", 61952))
        assert rsd.address == ("fd35::1", 61952)
        assert rsd.handshake_uuid is None


class TestTheLease:
    async def test_a_native_address_connects_as_remoted_and_holds_the_tunnel(self, pool):
        address = await native_tunnel.native_address(PHONE)
        rsd = rsd_for(LIB, address)
        held = address.held

        assert rsd.handshake_uuid == IDENTITY
        assert rsd.address == ("fd22::1", 62589)
        assert held.leases == 1
        await rsd.connect()
        await rsd.close()
        await rsd.close()          # twice: the lease is given back once
        assert held.leases == 0
        assert rsd.closes == 2     # the connection's own close still ran

    async def test_a_connection_dropped_without_close_gives_its_lease_back(self, pool):
        address = await native_tunnel.native_address(PHONE)
        rsd_for(LIB, address)
        assert address.held.leases == 1
        FakeRsd.made.clear()
        gc.collect()
        assert address.held.leases == 0

    async def test_a_connection_that_fails_to_open_retires_the_tunnel(self, pool):
        first = await native_tunnel.native_address(PHONE)
        rsd = rsd_for(LIB, first)
        rsd.fail_connect = ConnectionRefusedError(61, "Connect call failed")
        with pytest.raises(ConnectionRefusedError):
            await rsd.connect()
        assert first.held.retired

        second = await native_tunnel.native_address(PHONE)
        assert second.held is not first.held
        assert len(pool.opened) == 2
        assert not pool.opened[0].closed      # its connection is still open
        await rsd.close()
        await _settle()
        assert pool.opened[0].closed          # closed with its last connection
        assert not pool.opened[1].closed

    async def test_a_connect_that_stalls_retires_it(self, pool, monkeypatch):
        """A caller's own deadline arrives as a cancellation, which says
        nothing; the wrapper's shorter one says the tunnel is stale."""
        monkeypatch.setattr(native_tunnel, "CONNECT_S", 0.05)
        address = await native_tunnel.native_address(PHONE)
        rsd = rsd_for(LIB, address)
        rsd.stall = True
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(rsd.connect(), 5)
        assert address.held.retired

    async def test_an_address_taken_before_its_tunnel_retired_uses_the_new_one(self, pool):
        """A retried subscription reuses the address it was given."""
        old = await native_tunnel.native_address(PHONE)
        old.held.retired = True
        new = await native_tunnel.native_address(PHONE)

        rsd = rsd_for(LIB, old)

        assert rsd.address == (new.held.host, new.held.port) != (old.held.host, old.held.port)
        assert new.held.leases == 1 and old.held.leases == 0

    async def test_a_cancelled_connect_does_not_retire_it(self, pool):
        address = await native_tunnel.native_address(PHONE)
        rsd = rsd_for(LIB, address)
        rsd.fail_connect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await rsd.connect()
        assert not address.held.retired


class TestThePool:
    async def test_one_tunnel_per_phone_whatever_it_is_called(self, pool, monkeypatch):
        from server.device.ios import devicectl

        for spelling in (CORE, PHONE):
            monkeypatch.setitem(devicectl._identity_aliases, spelling, CORE)

        a = await native_tunnel.native_address(CORE)
        b = await native_tunnel.native_address(PHONE)

        assert a.held is b.held
        assert pool.serials == [PHONE]         # the pairing daemon is asked by hardware UDID
        assert native_tunnel.is_held(PHONE) and native_tunnel.is_held(CORE)

    async def test_concurrent_calls_open_it_once(self, pool):
        results = await asyncio.wait_for(
            asyncio.gather(*(native_tunnel.native_address(PHONE) for _ in range(5))), 5)
        assert len(pool.opened) == 1
        assert len({id(r.held) for r in results}) == 1

    async def test_a_failed_open_is_remembered_for_a_while(self, _no_native_tunnels, monkeypatch):
        calls = []

        async def _open(udid, serial):
            calls.append(udid)
            raise NativeTunnelUnavailable("Apple's tunnel to it could not be opened: no device")

        monkeypatch.setattr(_no_native_tunnels, "_open", _open)
        for _ in range(3):
            with pytest.raises(NativeTunnelUnavailable, match="no device"):
                await native_tunnel.native_address(PHONE)
        assert len(calls) == 1

        clock = native_tunnel.time.monotonic() + native_tunnel.FAILED_S + 1
        with patch.object(native_tunnel.time, "monotonic", return_value=clock):
            with pytest.raises(NativeTunnelUnavailable):
                await native_tunnel.native_address(PHONE)
        assert len(calls) == 2

    async def test_an_idle_tunnel_is_given_up(self, pool, monkeypatch):
        monkeypatch.setattr(native_tunnel, "IDLE_S", 0.04)
        address = await native_tunnel.native_address(PHONE)
        rsd = rsd_for(LIB, address)
        await asyncio.sleep(0.1)
        assert not pool.opened[0].closed       # a connection is open: held
        await rsd.close()
        await asyncio.sleep(0.1)
        await _settle()
        assert pool.opened[0].closed
        assert not native_tunnel.is_held(PHONE)

    async def test_a_tunnel_whose_reaper_ended_gets_one_again(self, pool, monkeypatch):
        """Opened under the image mount's own `asyncio.run`, whose end
        cancelled the reaper: the next lookup starts one."""
        monkeypatch.setattr(native_tunnel, "IDLE_S", 0.04)
        await native_tunnel.native_address(PHONE)
        pool._reaper.cancel()
        await _settle()
        assert pool._reaper.done()

        await native_tunnel.native_address(PHONE)
        await asyncio.sleep(0.1)
        await _settle()
        assert pool.opened[0].closed

    async def test_shutdown_gives_every_tunnel_up(self, pool):
        await native_tunnel.native_address(PHONE)
        await native_tunnel.native_address("00008101-000A1B2C3D4E5F60")
        await native_tunnel.close_all()
        assert [t.closed for t in pool.opened] == [True, True]
        assert not native_tunnel.is_held(PHONE)


class TestOpening:
    """`_open` itself, against a fake pymobiledevice3."""

    async def _open(self, monkeypatch, tunnel_cls):
        import pymobiledevice3.remote.native_tunnel as pmd3_native
        import pymobiledevice3.remote.remotexpc as remotexpc

        monkeypatch.setattr(pmd3_native, "NativeRemotedTunnel", tunnel_cls)
        monkeypatch.setattr(remotexpc, "remoted_handshake_uuid", lambda: IDENTITY)
        return await native_tunnel._Pool()._open(PHONE, PHONE)

    async def test_it_reads_the_address_and_identity(self, monkeypatch):
        class Tunnel(FakeTunnel):
            def __init__(self, serial):
                super().__init__()
                self.serial = serial

            async def aopen(self):
                return SimpleNamespace(service=SimpleNamespace(address=("fd22::1", 62589, 0, 0)))

        held = await self._open(monkeypatch, Tunnel)
        assert (held.host, held.port, held.identity) == ("fd22::1", 62589, IDENTITY)
        assert held.tunnel.serial == PHONE

    async def test_a_failure_says_why_and_closes_what_it_opened(self, monkeypatch):
        made = []

        class Tunnel(FakeTunnel):
            def __init__(self, serial):
                super().__init__()
                made.append(self)

            async def aopen(self):
                raise RuntimeError("Could not find remoted's RSD connection")

        with pytest.raises(NativeTunnelUnavailable, match="could not be opened: Could not find"):
            await self._open(monkeypatch, Tunnel)
        assert made[0].closed

    async def test_one_that_never_opens_is_bounded(self, monkeypatch):
        monkeypatch.setattr(native_tunnel, "OPEN_S", 0.05)

        class Tunnel(FakeTunnel):
            def __init__(self, serial):
                super().__init__()

            async def aopen(self):
                await asyncio.sleep(10)

        with pytest.raises(NativeTunnelUnavailable, match="did not open within"):
            await self._open(monkeypatch, Tunnel)


class TestTheDeviceAddress:
    async def test_with_no_tunneld_tunnel_the_native_one_is_used(self, pool):
        from server.device import performance

        with patch(f"{TUNNELD}.is_tunneld_running", AsyncMock(return_value=True)), \
                patch(f"{TUNNELD}.resolve_tunnel_udid", AsyncMock(return_value=None)):
            address = await performance._device_address(PHONE)

        assert isinstance(address, TunnelAddress)
        assert tuple(address) == ("fd22::1", 62589)

    async def test_a_tunneld_tunnel_comes_first(self, pool):
        from server.device import performance

        with patch(f"{TUNNELD}.is_tunneld_running", AsyncMock(return_value=True)), \
                patch(f"{TUNNELD}.resolve_tunnel_udid", AsyncMock(return_value=PHONE)), \
                patch.object(performance, "_tunnel_address", AsyncMock(return_value=USB)):
            address = await performance._device_address(PHONE)

        assert address == USB
        assert getattr(address, "held", None) is None
        assert pool.opened == []

    async def test_with_neither_the_error_says_why_and_what_wifi_needs(self):
        from server.device import performance

        with patch(f"{TUNNELD}.is_tunneld_running", AsyncMock(return_value=True)), \
                patch(f"{TUNNELD}.resolve_tunnel_udid", AsyncMock(return_value=None)), \
                pytest.raises(NoTunnelError) as e:
            await performance._device_address(PHONE)

        message = str(e.value)
        assert "not opened from a test" in message           # the native reason
        assert "paired with this Mac, unlocked, and on the same network" in message


class TestTheListing:
    async def test_a_phone_reached_through_apples_tunnel_says_so(self, pool):
        from server.device.controller import DeviceController
        from server.models import DeviceInfo, DeviceState, DeviceType

        phone = DeviceInfo(udid=PHONE, name="iPhone 11", state=DeviceState.BOOTED,
                           device_type=DeviceType.DEVICE, connection_type="localNetwork")
        other = DeviceInfo(udid="00008101-000A1B2C3D4E5F60", name="iPhone 12",
                           state=DeviceState.BOOTED, device_type=DeviceType.DEVICE,
                           connection_type="usb")
        usb_only = DeviceInfo(udid="00008020-000B1C2D3E4F5061", name="iPhone XS",
                              state=DeviceState.BOOTED, device_type=DeviceType.DEVICE,
                              connection_type="usb")
        await native_tunnel.native_address(PHONE)
        await native_tunnel.native_address(usb_only.udid)
        ctrl = DeviceController()
        ctrl.simctl.list_devices = AsyncMock(return_value=[])
        ctrl.devicectl.list_devices = AsyncMock(return_value=[phone, other])
        ctrl.usbmux.list_devices = AsyncMock(return_value=[usb_only])
        ctrl.adb.list_devices = AsyncMock(return_value=[])

        devices = await ctrl.list_devices()

        wanted = (PHONE, other.udid, usb_only.udid)
        tunnels = {d.udid: d.tunnel for d in devices if d.udid in wanted}
        assert tunnels == {PHONE: "native", other.udid: "", usb_only.udid: "native"}
