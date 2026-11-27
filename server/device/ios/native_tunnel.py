"""Apple's own tunnel to an iPhone quern's tunneld has none to (#781).

quern's tunneld builds tunnels over USB only, so a phone on Wi-Fi alone had
no address, and every CoreDevice read and action failed with "no tunnel".
macOS's `remoted` keeps a tunnel to such a phone anyway -- Xcode and devicectl
use it -- and pymobiledevice3's `NativeRemotedTunnel` rides it with no root:
the tunnel lives while its handle is held, and its address is routable by the
kernel, so quern's ordinary connections reach it.

Measured on an iPhone 11 on iOS 27.2.1 over Wi-Fi only:

- Display info, the process list, HID and the accessibility push all worked
  through it, and devicectl to the same phone kept working throughout.
- Every connection must present remoted's handshake identity
  (`remoted_handshake_uuid`). One with quern's ordinary identity is accepted
  and then the phone closes *every* service listener on the tunnel, ours
  included, for about 3 s. So `rsd_for` is the one way quern builds a
  connection from an address, and `tests/test_native_tunnel.py` fails on a
  call site that builds its own.
- Two handles to one phone coexist (quern's here, and the Screen Mirror
  bridge's in its own process): same address, both work.

A tunnel is held while a connection made through it is open (a lease, given
back by the connection's own `close`), and given up `IDLE_S` after the last
one closes. A connection that fails to open retires its tunnel: the phone may
have moved its listener, and the next call opens a fresh one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import weakref
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: How long a tunnel with no connection open is kept for the next call.
IDLE_S = 60.0
#: How long opening one may take: browsing the pairing daemon and the RSD
#: handshake. Measured at 0.8-1.4 s to a phone on the network. pymobiledevice3
#: waits 10 s for the pairing daemon to report a phone it does not know, and
#: that wait is the whole of a miss, so this bounds it: a phone that is not
#: there must not cost every call 10 s.
OPEN_S = 5.0
#: How long a failed open is answered from memory, so a phone that is not
#: there costs one attempt per window, not one per call.
FAILED_S = 30.0
#: How long closing one may take before it is abandoned.
CLOSE_S = 5.0
#: How long a connection through one may take to open before the tunnel is
#: given up as stale. Measured at 0.1-0.3 s; under the callers' own bounds
#: (5 s for HID), so a stall reaches this one first and retires the tunnel --
#: a caller's deadline arrives as a cancellation, which says nothing.
CONNECT_S = 4.0


#: A hardware UDID: "00008030-000C59623A69802E", or 40 hex digits before 2018.
_HARDWARE_UDID = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{16}|[0-9A-Fa-f]{40}")


def hardware_udid(udid: str) -> str | None:
    """The phone's hardware UDID ("00008030-..."), from the spellings
    devicectl recorded for it, or None when none is known."""
    from server.device.ios.devicectl import canonical_device_id, spellings_of

    for spelling in (udid, *spellings_of(canonical_device_id(udid))):
        if _HARDWARE_UDID.fullmatch(spelling):
            return spelling
    return None


def _key_and_serial(udid: str) -> tuple[str, str]:
    """The phone's canonical id, to hold one tunnel per phone whatever it is
    called, and its hardware UDID, which the pairing daemon is asked for."""
    from server.device.ios.devicectl import canonical_device_id, spellings_of

    canonical = canonical_device_id(udid)
    for spelling in (udid, *spellings_of(canonical)):
        if _HARDWARE_UDID.fullmatch(spelling):
            return canonical, spelling
    return canonical, udid


def _paired_here(udid: str) -> bool:
    """Whether devicectl listed this phone, or has listed nothing yet.

    A phone it does not list -- one never paired with this Mac, one before
    iOS 17 on USB -- costs a whole failed open to ask about, so it is not
    asked about. Before any listing nothing can be told, and it is asked.
    """
    from server.device.ios import devicectl

    known = devicectl._identity_aliases
    return not known or udid in known


class NativeTunnelUnavailable(Exception):
    """Apple's tunnel to the phone could not be opened; the message says why."""


class TunnelAddress(tuple):
    """A phone's (host, port), and the native tunnel it is on, if it is on one.

    A tuple, so every caller that takes an address takes this one unchanged;
    `rsd_for` reads `held` to connect with the identity the tunnel needs.
    """

    held: _Held | None

    def __new__(cls, host: str, port: int, held: _Held | None = None) -> TunnelAddress:
        self = super().__new__(cls, (host, port))
        self.held = held
        return self


@dataclass(eq=False)
class _Held:
    udid: str
    tunnel: Any
    host: str
    port: int
    #: remoted's handshake identity, resolved once when the tunnel opened.
    identity: Any
    leases: int = 0
    last_used: float = field(default_factory=time.monotonic)
    #: A connection through it failed to open: no new ones are made on it,
    #: and it closes once the open ones have.
    retired: bool = False

    def address(self) -> TunnelAddress:
        self.last_used = time.monotonic()
        return TunnelAddress(self.host, self.port, self)

    def lease(self) -> Any:
        """Take a lease; the returned function gives it back, once."""
        self.leases += 1
        self.last_used = time.monotonic()
        given = False

        def release() -> None:
            nonlocal given
            if given:
                return
            given = True
            self.leases -= 1
            self.last_used = time.monotonic()
            if self.retired and self.leases == 0:
                _POOL.close_soon(self)

        return release


def rsd_for(lib: Any, address: tuple) -> Any:
    """An unconnected RSD to `address`, made the way its tunnel needs.

    The one place quern makes one: a connection on Apple's tunnel without
    remoted's identity takes down every service on that tunnel. For an
    address on such a tunnel the connection also holds the tunnel open until
    its own `close`.
    """
    held = getattr(address, "held", None)
    if held is None:
        return lib.RemoteServiceDiscoveryService(address)
    if held.retired:
        # An address taken before its tunnel was retired, as a retried
        # subscription reuses one: the tunnel that replaced it, if there is
        # one yet (CodeRabbit on #789).
        held = _POOL.current(held.udid) or held
    rsd = lib.RemoteServiceDiscoveryService((held.host, held.port),
                                            handshake_uuid=held.identity)
    release = held.lease()
    connect, close = rsd.connect, rsd.close

    async def connect_then_check(*args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.wait_for(connect(*args, **kwargs), CONNECT_S)
        except Exception:
            # Cancellation says nothing about the tunnel; a failure might.
            if not held.retired:
                logger.info("[native-tunnel] a connection to %s failed to open; "
                            "the next call opens a fresh tunnel", held.udid[:8])
            held.retired = True
            raise

    async def close_then_release(*args: Any, **kwargs: Any) -> Any:
        try:
            return await close(*args, **kwargs)
        finally:
            release()

    rsd.connect = connect_then_check
    rsd.close = close_then_release
    # A connection dropped without its close still gives the lease back.
    with contextlib.suppress(TypeError):
        weakref.finalize(rsd, release)
    return rsd


def lease(address: tuple) -> tuple[tuple, Any]:
    """Hold the address's tunnel for something that is not an RSD made here
    -- a pymobiledevice3 subprocess given `--rsd` -- until the returned
    function is called (once; again is a no-op). An address on no native
    tunnel is returned as it is, with nothing to give back.

    The tunnel is reachable from another process while it is held: measured,
    `crash ls`, `developer dvt screenshot` and `syslog live` given `--rsd`
    worked through one this process held, and pymobiledevice3 presents
    remoted's identity on such an address itself (`rsd_handshake_uuid`).
    """
    held = getattr(address, "held", None)
    if held is None:
        return address, lambda: None
    if held.retired:
        held = _POOL.current(held.udid) or held
    return TunnelAddress(held.host, held.port, held), held.lease()


@contextlib.asynccontextmanager
async def holding(address: tuple) -> AsyncIterator[tuple]:
    """`lease` for the length of a block."""
    address, release = lease(address)
    try:
        yield address
    finally:
        release()


def rsd_args(address: tuple) -> list[str]:
    """pymobiledevice3's arguments for an RSD address."""
    return ["--rsd", str(address[0]), str(address[1])]


class _Pool:
    """The native tunnels this server holds, one per phone."""

    def __init__(self) -> None:
        self._held: dict[str, _Held] = {}
        self._failed: dict[str, tuple[float, str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._reaper: asyncio.Task | None = None
        self._closing: set[asyncio.Task] = set()

    def current(self, udid: str) -> _Held | None:
        """The phone's tunnel now, if one is held and not retired."""
        current = self._held.get(udid)
        return current if current is not None and not current.retired else None

    def held(self, udid: str) -> bool:
        current = self._held.get(_key_and_serial(udid)[0])
        return current is not None and not current.retired

    async def address(self, udid: str) -> TunnelAddress:
        """The phone's address on Apple's tunnel, opening it if need be, or
        NativeTunnelUnavailable naming why there is none."""
        udid, serial = _key_and_serial(udid)
        current = self._held.get(udid)
        if current is not None and not current.retired:
            # Here too: a tunnel opened on a loop that has since ended (the
            # image mount's `asyncio.run`) lost its reaper with it
            # (CodeRabbit on #789).
            self._ensure_reaper()
            return current.address()
        lock = self._locks.setdefault(udid, asyncio.Lock())
        async with lock:
            current = self._held.get(udid)
            if current is not None and not current.retired:
                return current.address()
            failed = self._failed.get(udid)
            if failed and time.monotonic() - failed[0] < FAILED_S:
                raise NativeTunnelUnavailable(failed[1])
            if not _paired_here(udid):
                raise NativeTunnelUnavailable(
                    "devicectl lists no phone paired with this Mac by that id, so "
                    "Apple's tunnel was not asked for")
            try:
                opened = await self._open(udid, serial)
            except NativeTunnelUnavailable as e:
                self._failed[udid] = (time.monotonic(), str(e))
                raise
            self._failed.pop(udid, None)
            if current is not None:
                # Retired: it closes when its last connection does.
                if current.leases == 0:
                    self.close_soon(current)
            self._held[udid] = opened
            self._ensure_reaper()
            return opened.address()

    async def _open(self, udid: str, serial: str) -> _Held:
        try:
            from pymobiledevice3.remote.native_tunnel import NativeRemotedTunnel
            from pymobiledevice3.remote.remotexpc import remoted_handshake_uuid
        except ImportError as e:
            raise NativeTunnelUnavailable(
                f"this pymobiledevice3 cannot ride Apple's tunnel ({e})") from e
        tunnel = NativeRemotedTunnel(serial=serial)
        started = time.monotonic()
        # One deadline for the whole attempt, and a failed one is closed in
        # the background: the caller waits OPEN_S at most (CodeRabbit on #790).
        deadline = asyncio.timeout(OPEN_S)
        try:
            async with deadline:
                identity = await asyncio.to_thread(remoted_handshake_uuid)
                rsd = await tunnel.aopen()
            host, port = rsd.service.address[:2]
        except TimeoutError:
            self._close_in_background(tunnel, udid)
            if deadline.expired():
                raise NativeTunnelUnavailable(
                    f"Apple's tunnel to it did not open within {OPEN_S:g}s") from None
            raise NativeTunnelUnavailable(
                "Apple's tunnel to it could not be opened: a step timed out") from None
        except Exception as e:  # noqa: BLE001 -- pymobiledevice3 raises its own kinds
            self._close_in_background(tunnel, udid)
            raise NativeTunnelUnavailable(f"Apple's tunnel to it could not be opened: {e}") from e
        logger.info("[native-tunnel] opened Apple's tunnel to %s at [%s]:%d in %.1fs",
                    udid[:8], host, port, time.monotonic() - started)
        return _Held(udid=udid, tunnel=tunnel, host=host, port=port, identity=identity)

    def _close_in_background(self, tunnel: Any, udid: str) -> None:
        task = asyncio.get_running_loop().create_task(self._close_tunnel(tunnel, udid))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    def close_soon(self, held: _Held) -> None:
        if self._held.get(held.udid) is held:
            del self._held[held.udid]
        try:
            task = asyncio.get_running_loop().create_task(
                self._close_tunnel(held.tunnel, held.udid))
        except RuntimeError:
            return  # no loop left: the process is ending, and the tunnel with it
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    @staticmethod
    async def _close_tunnel(tunnel: Any, udid: str) -> None:
        try:
            await asyncio.wait_for(tunnel.aclose(), CLOSE_S)
        except Exception:  # noqa: BLE001 -- giving it up is all that is left
            logger.debug("[native-tunnel] closing the tunnel to %s failed", udid[:8],
                         exc_info=True)

    def _ensure_reaper(self) -> None:
        if self._reaper is None or self._reaper.done():
            self._reaper = asyncio.get_running_loop().create_task(self._reap())

    async def _reap(self) -> None:
        """Give up tunnels idle for IDLE_S; end when none is held."""
        while self._held:
            await asyncio.sleep(IDLE_S / 4)
            now = time.monotonic()
            for held in list(self._held.values()):
                if held.leases == 0 and (held.retired or now - held.last_used >= IDLE_S):
                    logger.info("[native-tunnel] giving up the idle tunnel to %s", held.udid[:8])
                    self.close_soon(held)

    async def close_all(self) -> None:
        """Give up every tunnel, for the server's shutdown."""
        if self._reaper is not None:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reaper
            self._reaper = None
        for held in list(self._held.values()):
            self.close_soon(held)
        if self._closing:
            await asyncio.gather(*list(self._closing), return_exceptions=True)


_POOL = _Pool()


async def native_address(udid: str) -> TunnelAddress:
    """The phone's address on Apple's own tunnel (see the module docstring)."""
    return await _POOL.address(udid)


def is_held(udid: str) -> bool:
    """Whether this server is holding Apple's tunnel to the phone now."""
    return _POOL.held(udid)


async def close_all() -> None:
    await _POOL.close_all()
