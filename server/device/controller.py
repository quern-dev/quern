"""DeviceController — orchestrates device backends and tracks active device."""

from __future__ import annotations

import asyncio
import logging
import time

from server.device.adb import AdbBackend
from server.device.controller_ui import DeviceControllerUI
from server.device.devicectl import DevicectlBackend
from server.device.idb import IdbBackend
from server.device.pmd3 import Pmd3Backend
from server.device.screenshots import process_screenshot
from server.device.sim_bridge import SimBridgeBackend, SimBridgeManager
from server.device.simctl import SimctlBackend
from server.device.u2_client import U2Backend
from server.device.usbmux import UsbmuxBackend
from server.device.wda_client import WdaBackend
from server.lifecycle.state import read_active_udid, write_active_udid
from server.models import AppInfo, DeviceError, DeviceInfo, DeviceState, DeviceType, UIElement

logger = logging.getLogger("quern-debug-server.device")


def _display_name(name: str | None, kind: str | None) -> str | None:
    """Turn an AVD name into something meant for a person to read.

    AVD names cannot contain spaces, so the emulator reports "Pixel_7". The
    underscores are a naming-rule artefact rather than anyone's choice, and
    the sidecar feeds the menu bar, which shows the value verbatim.

    Deliberately restricted to emulators. An underscore in a simulator's or a
    physical device's name was typed by a person -- "J_iPhone" is a name, not
    an encoding -- and rewriting it would be wrong. The canonical AVD name is
    untouched either way: it keys the AVD config lookup, the duplicate
    suppression against list_avds(), and boot-by-name, none of which would
    match a prettified string.
    """
    if not name or kind != DeviceType.ANDROID_EMULATOR.value:
        return name
    return name.replace("_", " ")


class DeviceController(DeviceControllerUI):
    """High-level device management: resolves active device, delegates to backends."""

    def __init__(self) -> None:
        self.simctl = SimctlBackend()
        self.idb = IdbBackend()
        self.devicectl = DevicectlBackend()
        self.pmd3 = Pmd3Backend()
        self.usbmux = UsbmuxBackend()
        self.wda_client = WdaBackend()
        self.adb = AdbBackend()
        self.u2 = U2Backend()
        self.sim_bridge_manager = SimBridgeManager()
        self.sim_bridge = SimBridgeBackend(self.sim_bridge_manager)
        self._sim_bridge_ok = False
        #: When `_sim_bridge_ok` was last established, or None if never.
        #:
        #: Latching it at startup and never re-checking is #179: Xcode 27 moved
        #: SimulatorKit under a running server, and it went on routing every tap
        #: to a backend that could no longer work, while `/tools` correctly
        #: reported it unavailable.
        #:
        #: None rather than 0.0, because `time.monotonic()` counts from boot and
        #: so legitimately *is* near zero on a machine that just started. With
        #: 0.0 as the sentinel, "never checked" and "checked at boot" are the
        #: same value, and the staleness test reads an unpopulated cache as
        #: fresh for the first `max_age` seconds of uptime. CI caught this; no
        #: developer machine has an uptime short enough to see it.
        self._sim_bridge_checked_at: float | None = None
        self._tools_cache: tuple[float, dict[str, bool]] | None = None
        #: Serialises probe-and-adopt. /tools and the periodic refresh can run
        #: at once, and without this an older sample can land after a newer one
        #: -- leaving `_ui_backend` on the wrong backend until the next refresh,
        #: up to 300s of taps going somewhere they should not.
        self._sim_bridge_lock = asyncio.Lock()
        self.__active_udid: str | None = None
        # What was last persisted, so an assignment that changes nothing can
        # skip the write entirely. Separate from __active_udid, which is
        # assigned before the comparison runs. See the setter.
        self.__active_name_key: str | None = None
        self.__active_name: str | None = None
        self.__active_kind: str | None = None
        self._pool = None  # Set by main.py after pool is created; None = no pool

        # Restore active device from its sidecar file (lives separately
        # from state.json so it survives `quern stop` and stop/start cycles).
        persisted = read_active_udid()
        if persisted:
            self.__active_udid = persisted
            logger.info("Restored active device: %s", persisted[:8])
        # UI tree cache: {udid: (elements, timestamp)}
        self._ui_cache: dict[str, tuple[list[UIElement], float]] = {}
        # One long-lived Web Inspector connection. Reconnecting per request cost
        # ~3.4s of handshake, and webinspectord did not re-report its connected
        # applications to a connection opened immediately after the previous one
        # closed, so alternate calls saw no apps at all.
        self._web_inspector: object | None = None
        self._web_inspector_lock = asyncio.Lock()
        # Held for a whole web-content transaction. The connection is shared and
        # the protocol interleaves replies, so two concurrent collections would
        # read each other's messages -- and one finding an empty result would
        # close the connection out from under the other.
        self._web_inspector_op_lock = asyncio.Lock()
        # Web elements from the last get_web_content, merged into UI reads so
        # tap_element can resolve them. Kept beside the UI cache rather than in
        # it, so a cached native tree is never polluted with web content that
        # may already be stale.
        self._web_overlay: dict[str, tuple[list[UIElement], float]] = {}
        self._cache_ttl: float = 0.3  # 300ms cache TTL
        self._cache_hits: int = 0
        self._cache_misses: int = 0
        # Device info cache for screen dimensions
        self._device_info_cache: dict[str, DeviceInfo] = {}
        # Device type cache: udid -> DeviceType (populated by list_devices)
        self._device_type_cache: dict[str, DeviceType] = {}
        # Simulators whose input services have been checked this boot.
        # See server/device/sim_input.py; the check costs a `simctl
        # spawn` (~0.5s), so it is paid once per device rather than per
        # tap.
        self._input_checked: dict[str, bool] = {}
        # Device name cache: udid -> human-readable name (populated by
        # list_devices). Only consumer is the active-device sidecar, so that
        # readers outside the server can show a name instead of a UDID.
        self._device_name_cache: dict[str, str] = {}
        # CoreDevice UUID -> libimobiledevice UDID mapping (populated by list_devices)
        self._usbmux_udid_map: dict[str, str] = {}

    @property
    def _active_udid(self) -> str | None:
        return self.__active_udid

    @_active_udid.setter
    def _active_udid(self, value: str | None) -> None:
        # Best-effort name: the cache is filled by list_devices(), which
        # every resolve path runs before landing here, but the pool and the
        # set-active-device API can assign a UDID directly. A miss writes no
        # name and readers fall back to the UDID -- the previous behaviour.
        name = self._device_name_cache.get(value) if value else None
        # The cache, not _device_type(), which answers SIMULATOR for an
        # unknown UDID. A guess persisted to the sidecar would have the menu
        # bar label real hardware as a simulator, so an unknown type is
        # written as absent and the reader shows no qualifier at all.
        cached_kind = self._device_type_cache.get(value) if value else None
        kind = cached_kind.value if cached_kind else None
        name = _display_name(name, kind)
        self.__active_udid = value

        # Write only on an actual change. resolve_udid() assigns this on every
        # call that names a device, which is most tool calls, so the sidecar
        # was being rewritten -- taking LOCK_EX on the event loop each time --
        # to store the value it already held. The active device changes rarely,
        # so this takes the I/O off the hot path altogether rather than moving
        # it to a thread, which a property setter cannot await anyway and which
        # would make two rapid switches race to land out of order.
        #
        # The name and the type are part of the comparison, not just the UDID:
        # both caches are warmed by list_devices() and can arrive after the
        # first assignment, and that later fill is exactly when the sidecar
        # needs rewriting.
        if (
            value == self.__active_name_key
            and name == self.__active_name
            and kind == self.__active_kind
        ):
            return
        self.__active_name_key = value
        self.__active_name = name
        self.__active_kind = kind
        write_active_udid(value, name, kind)

    def refresh_active_device(self) -> None:
        """Rewrite the active-device sidecar from the warmed caches.

        `__init__` restores the persisted UDID straight into the backing field
        rather than through the setter, deliberately -- restoring a device is
        not a change worth writing. The consequence is that the sidecar keeps
        whatever the previous server left there, which for anything written
        before the name and type existed is a bare UDID.

        Nothing else reliably refreshes it. `resolve_udid()` does, but the
        tool everyone actually uses to pick a device goes through the pool,
        and the pool's sticky-active path returns `controller._active_udid`
        without assigning it -- so no setter runs, and the menu bar shows an
        identifier for the whole session. Only an explicit resolve by UDID or
        by name repaired it, which is a strange thing to have to know.

        Called once at startup after `list_devices()` has filled the name and
        type caches. The dedup guard in the setter makes it a no-op whenever
        the sidecar already agrees.
        """
        udid = self._active_udid
        if not udid:
            return
        # Only when the caches actually know this device. An empty cache is
        # not evidence that the device has no name -- list_devices() swallows
        # DeviceError per backend, so a simctl or adb failure yields exactly
        # the same empty cache as "nothing is connected". Writing on that
        # replaces a good name with a bare UDID, which is the symptom this
        # method exists to prevent, caused by this method.
        if udid not in self._device_name_cache and udid not in self._device_type_cache:
            logger.debug(
                "Not refreshing the active-device sidecar: %s is not in the "
                "device caches, so any name it already holds is better than "
                "what this would write", udid[:8],
            )
            return
        self._active_udid = udid

    async def check_tools(
        self, *, adopt: bool = False, max_age: float = 0.0,
    ) -> dict[str, bool]:
        """Check availability of CLI tools.

        Every probe is bounded and runs concurrently. Sequentially, the shared
        budget would be per tool and `/tools` could take seven times as long to
        answer on a machine where several are wedged -- and `/tools` not
        answering is the bug this came from (#180).

        The values are booleans, so "installed but not responding" arrives here
        as False, indistinguishable from "not installed". The probe logs the
        difference; expressing it is #181.
        """
        import time

        from server.device.tunneld import is_tunneld_running

        # `max_age` exists because this is no longer cheap. Seven probes, six of
        # them subprocesses, and `GET /api/v1/device/list` calls it on every
        # request to report tool availability alongside the devices -- which for
        # an agent driving the MCP tool is a hot path. Callers that *report*
        # health (/tools, startup) pass 0 and always measure; callers that
        # merely include it in a larger response can accept a few seconds old.
        if max_age > 0 and self._tools_cache is not None:
            cached_at, cached = self._tools_cache
            if time.monotonic() - cached_at < max_age:
                return dict(cached)

        names = (
            "simctl", "idb", "devicectl", "pymobiledevice3",
            "tunneld", "adb", "sim_bridge",
        )
        results = await asyncio.gather(
            self.simctl.is_available(),
            self.idb.is_available(),
            self.devicectl.is_available(),
            self.pmd3.is_available(),
            is_tunneld_running(),
            self.adb.is_available(),
            self._probe_sim_bridge(adopt=adopt),
            return_exceptions=True,
        )
        tools: dict[str, bool] = {}
        for name, result in zip(names, results, strict=True):
            if isinstance(result, BaseException):
                # One probe raising must not take the health endpoint down with
                # it -- reporting six tools and an error beats reporting none.
                logger.warning("%s availability probe failed: %r", name, result)
                tools[name] = False
            else:
                tools[name] = bool(result)

        self._tools_cache = (time.monotonic(), dict(tools))
        return tools

    async def _probe_sim_bridge(self, *, adopt: bool) -> bool:
        """Probe the sim-bridge backend, optionally adopting the result.

        Adopting is opt-in rather than a side effect of measuring. It is the
        cheap half of #179 -- a /tools call re-syncs a server left routing to a
        backend the same response calls unavailable -- but it decides which
        backend serves every subsequent tap, and no operator associates
        *listing devices* with re-selecting one.

        The probe and the adoption are one critical section. They are not the
        only caller: the periodic refresh runs the same pair, and interleaved,
        a slower older probe can land after a faster newer one and leave
        `_ui_backend` wrong until the next refresh.
        """
        async with self._sim_bridge_lock:
            ok = await self.sim_bridge_manager.is_available()
            if adopt:
                self._adopt_sim_bridge_state(ok)
            return ok

    def _adopt_sim_bridge_state(self, ok: bool) -> None:
        """Record a freshly measured sim-bridge availability, loudly on change.

        A backend flipping under a running server is not routine: it means the
        toolchain moved, and the log line is the only warning anyone gets
        before gestures start going somewhere else.
        """
        import time

        # Only a *change* is worth a warning, and only after a first answer
        # exists to change from. Every server boot establishes this from False,
        # so warning on that too would put a WARNING in every startup log for
        # the most ordinary event there is -- and a warning that always fires
        # is one nobody reads when it matters.
        established = self._sim_bridge_checked_at is not None
        if established and ok != self._sim_bridge_ok:
            logger.warning(
                "sim-bridge backend became %s under a running server; UI "
                "automation will now use %s. The toolchain moved: this is the "
                "only notice before gestures start going somewhere else.",
                "available" if ok else "unavailable",
                "sim-bridge" if ok else "idb",
            )
        self._sim_bridge_ok = ok
        self._sim_bridge_checked_at = time.monotonic()

    async def refresh_sim_bridge_availability(self, max_age: float = 300.0) -> bool:
        """Re-probe the sim-bridge backend when the cached answer is stale.

        `max_age` exists so this is safe to call often: the probe spawns
        `xcode-select`, which is cheap but not free, and UI operations select a
        backend on a synchronous path that cannot await.
        """
        import time

        if (
            self._sim_bridge_checked_at is not None
            and time.monotonic() - self._sim_bridge_checked_at < max_age
        ):
            return self._sim_bridge_ok
        await self._probe_sim_bridge(adopt=True)
        return self._sim_bridge_ok

    async def tool_sites(self) -> list:
        """Every install site quern uses, with versions and provenance.

        Separate from `check_tools()` rather than folded into it. That returns
        one boolean per tool name and is consumed by truthiness -- `main.py`
        decides whether to use the sim-bridge backend from it -- so widening
        the values to dictionaries would make every tool read as available,
        including the missing ones.

        The names also do not line up: `pymobiledevice3` is two installs at
        different versions serving different code paths, which is the confusion
        this reports its way out of.
        """
        from server.device.tool_versions import collect_sites, upgrade_note
        from server.models import ToolSiteInfo

        return [
            ToolSiteInfo(
                name=site.name,
                role=site.role,
                available=site.available,
                version=site.version,
                path=site.path,
                source=site.source,
                detail=site.detail,
                volatile_path=site.volatile_path,
                requested=site.requested,
                required_by=site.required_by,
                upgrade_note=upgrade_note(site),
                diagnostic=site.diagnostic,
            )
            for site in await collect_sites()
        ]

    def _device_type(self, udid: str) -> DeviceType:
        """Look up device type from cache. Defaults to simulator if unknown."""
        return self._device_type_cache.get(udid, DeviceType.SIMULATOR)

    def _is_physical(self, udid: str) -> bool:
        return self._device_type(udid) == DeviceType.DEVICE

    def _is_android(self, udid: str) -> bool:
        return self._device_type(udid) in (DeviceType.ANDROID_EMULATOR, DeviceType.ANDROID_DEVICE)

    def _require_simulator(self, udid: str, operation: str) -> None:
        """Raise DeviceError if the device is physical (operation not supported)."""
        if self._is_physical(udid):
            raise DeviceError(
                f"{operation} is only supported on simulators",
                tool="simctl",
            )

    async def _ensure_device_type_cached(self, udid: str) -> None:
        """Populate device type cache if this UDID isn't known yet.

        Called lazily when a UDID is used that hasn't been seen via
        list_devices(). Without this, _is_physical() defaults to simulator
        and physical devices get routed to idb instead of WDA.
        """
        if udid not in self._device_type_cache:
            logger.debug("Device type unknown for %s, refreshing device list...", udid[:8])
            await self.list_devices()

    async def resolve_udid(self, udid: str | None = None) -> str:
        """Resolve which device to target.

        If a DevicePool is attached, attempts pool-based resolution for
        claim-aware, multi-device-friendly behavior. If pool resolution
        fails for any reason, silently falls back to the original logic.

        Resolution order:
        1. Explicit udid parameter → use it, update active
        2. Stored active_udid → use it
        3. Pool resolution (if pool attached) → best available booted device
        4. Fallback: simple auto-detect (original logic, unchanged)
        """
        if udid:
            await self._ensure_device_type_cached(udid)
            self._active_udid = udid
            return udid

        if self._active_udid:
            restored = self._active_udid
            await self._ensure_device_type_cached(restored)
            # Reassign through the setter now the caches are warm. __init__
            # puts the persisted UDID straight into the backing field, on
            # purpose -- restoring a device is not a change worth writing --
            # so nothing has run the setter yet on this path, and it is the
            # path a restart takes. Returning early left a sidecar written by
            # an older server holding only its UDID however many tool calls
            # ran, and the menu bar showed the UDID. The dedup guard makes
            # this a no-op once the name and type have landed.
            self._active_udid = restored
            return restored

        # Step 3: try pool-based resolution (silent upgrade)
        if self._pool is not None:
            try:
                resolved = await self._pool.resolve_device()
                self._active_udid = resolved
                return resolved
            except Exception as e:
                logger.debug("Pool resolution failed, falling back: %s", e)

        # Step 4: fallback — auto-detect from all backends
        devices = await self.list_devices()
        booted = [d for d in devices if d.state == DeviceState.BOOTED]

        if len(booted) == 0:
            raise DeviceError("No booted device found", tool="simctl")
        if len(booted) > 1:
            names = ", ".join(f"{d.name} ({d.udid[:8]})" for d in booted)
            raise DeviceError(
                f"Multiple devices booted ({names}), specify udid",
                tool="simctl",
            )

        self._active_udid = booted[0].udid
        return self._active_udid

    def _invalidate_ui_cache(self, udid: str | None = None) -> None:
        """Invalidate UI tree cache for a device (or all devices if udid=None)."""
        if udid:
            self._ui_cache.pop(udid, None)
            # Anything that changed the native tree can have moved, replaced or
            # dismissed the page too, and a web element's position is only
            # meaningful for the layout it was measured against.
            self._web_overlay.pop(udid, None)
            logger.debug(f"UI cache invalidated for device {udid[:8]}")
        else:
            self._ui_cache.clear()
            self._web_overlay.clear()
            logger.debug("UI cache cleared for all devices")

    def get_cache_stats(self) -> dict:
        """Return cache statistics for observability."""
        total = self._cache_hits + self._cache_misses
        hit_rate = (self._cache_hits / total * 100) if total > 0 else 0

        # Add per-device cache age info
        cache_ages = {}
        now = time.time()
        for udid, (elements, timestamp) in self._ui_cache.items():
            age_ms = (now - timestamp) * 1000
            cache_ages[udid[:8]] = f"{age_ms:.1f}ms"

        return {
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "hit_rate_percent": round(hit_rate, 1),
            "cached_devices": len(self._ui_cache),
            "ttl_ms": int(self._cache_ttl * 1000),
            "cache_ages": cache_ages,
        }

    async def list_devices(self) -> list[DeviceInfo]:
        """List all devices (simulators + physical + pre-iOS 17 USB + Android)."""
        try:
            sim_devices = await self.simctl.list_devices()
        except DeviceError:
            logger.debug("simctl list_devices failed (simctl unavailable)", exc_info=True)
            sim_devices = []
        try:
            physical_devices = await self.devicectl.list_devices()
        except DeviceError:
            logger.debug("devicectl list_devices failed", exc_info=True)
            physical_devices = []
        try:
            usbmux_devices = await self.usbmux.list_devices()
        except DeviceError:
            logger.debug("usbmux list_devices failed", exc_info=True)
            usbmux_devices = []
        try:
            android_devices = await self.adb.list_devices()
        except DeviceError:
            logger.debug("adb list_devices failed", exc_info=True)
            android_devices = []

        # Populate device type cache and WDA os_version cache
        for d in sim_devices:
            self._device_type_cache[d.udid] = DeviceType.SIMULATOR
        for d in physical_devices:
            # The backend's own classification, not a hardcoded DEVICE. This
            # loop runs after the simctl one, so stamping DEVICE here overwrote
            # the correct SIMULATOR for any UDID appearing in both -- which is
            # every simulator, once Xcode 26 started registering them as
            # CoreDevices. devicectl now filters simulators out, and this stops
            # the mistake being reintroduced if anything else ever returns one.
            self._device_type_cache[d.udid] = d.device_type
            if d.os_version:
                self.wda_client._device_os_versions[d.udid] = d.os_version
            if d.name:
                self.wda_client._device_names[d.udid] = d.name
        for d in usbmux_devices:
            self._device_type_cache[d.udid] = DeviceType.DEVICE
            if d.os_version:
                self.wda_client._device_os_versions[d.udid] = d.os_version
            if d.name:
                self.wda_client._device_names[d.udid] = d.name
        for d in android_devices:
            self._device_type_cache[d.udid] = d.device_type
        # One pass over every backend rather than four: the name is wanted
        # for all device kinds and nothing else here varies by kind.
        for d in sim_devices + physical_devices + usbmux_devices + android_devices:
            if d.name:
                self._device_name_cache[d.udid] = d.name

        # Build CoreDevice UUID -> libimobiledevice UDID mapping
        # by correlating device names between devicectl and usbmux
        usb_name_map = await self.usbmux.get_usb_udid_map()
        for d in physical_devices:
            if d.name in usb_name_map:
                self._usbmux_udid_map[d.udid] = usb_name_map[d.name]

        return sim_devices + physical_devices + usbmux_devices + android_devices

    async def get_libimobiledevice_udid(self, coredevice_udid: str) -> str | None:
        """Look up the libimobiledevice UDID for a CoreDevice UUID.

        For pre-iOS 17 devices discovered via usbmux, the UDID is already in
        libimobiledevice format (40-char hex) — return it directly.

        Returns None if the device is not USB-connected (e.g. network-only).
        Refreshes the mapping if the UDID isn't found on first lookup.
        """
        # Check the CoreDevice -> libimobiledevice mapping
        udid = self._usbmux_udid_map.get(coredevice_udid)
        if udid is not None:
            return udid

        # Pre-iOS 17 devices already use libimobiledevice UDIDs as their
        # primary identifier (from usbmux). Check if this UDID belongs to
        # a usbmux-discovered device and return it as-is.
        device_type = self._device_type_cache.get(coredevice_udid)
        if device_type == DeviceType.DEVICE:
            # It's a known physical device — check if it's a usbmux UDID
            # (40-char hex, not a CoreDevice UUID format)
            if len(coredevice_udid) == 40 and all(c in "0123456789abcdef" for c in coredevice_udid):
                return coredevice_udid

        # Refresh and try again
        await self.list_devices()

        udid = self._usbmux_udid_map.get(coredevice_udid)
        if udid is not None:
            return udid

        # Re-check after refresh for usbmux devices
        device_type = self._device_type_cache.get(coredevice_udid)
        if device_type == DeviceType.DEVICE:
            if len(coredevice_udid) == 40 and all(c in "0123456789abcdef" for c in coredevice_udid):
                return coredevice_udid

        return None

    async def boot(
        self, udid: str | None = None,
        name: str | None = None, headless: bool = False,
    ) -> str:
        """Boot a simulator or Android emulator by udid or name.

        Returns the udid that was booted.
        """
        if udid:
            if self._is_android(udid):
                raise DeviceError(
                    "Cannot boot Android emulator by serial — use name (AVD name) instead",
                    tool="adb",
                )
            self._require_simulator(udid, "Boot")
            await self.simctl.boot(udid)
            self._active_udid = udid
            await self._restore_input_after_boot(udid)
            return udid

        if name:
            # Check if name matches an Android AVD first
            if self.adb.is_installed():
                avds = await self.adb.list_avds()
                if name in avds:
                    serial = await self.adb.boot_emulator(name, headless=headless)
                    self._device_type_cache[serial] = DeviceType.ANDROID_EMULATOR
                    self._active_udid = serial
                    return serial

            # Fall back to iOS simulator
            devices = await self.simctl.list_devices()
            matches = [d for d in devices if d.name == name]
            if not matches:
                raise DeviceError(f"No simulator or AVD found with name '{name}'", tool="simctl")
            target = matches[0]
            await self.simctl.boot(target.udid)
            self._active_udid = target.udid
            await self._restore_input_after_boot(target.udid)
            return target.udid

        raise DeviceError("Either udid or name is required to boot", tool="simctl")

    async def _restore_input_after_boot(self, udid: str) -> None:
        """Take the input services back, if Xcode 27's Device Hub has them.

        Done here because a simulator quern has just booted is running
        nothing, so the SpringBoard restart the repair needs costs the caller
        nothing. On a device that was already booted the same repair would
        kill whatever the user has open, so there it is offered rather than
        taken (see `_require_input_can_land`).

        Never fatal to a boot: a simulator that cannot receive input is worth
        far more than no simulator, and the next input call says so plainly.
        """
        from server.device import sim_input

        try:
            # Device Hub attaches a few seconds after the boot returns, so a
            # repair applied immediately is undone by an attachment that has
            # not happened yet. Wait for it, but only when Device Hub is
            # running -- otherwise there is nothing to wait for.
            if await sim_input.device_hub_is_running():
                suppressed = await sim_input.wait_for_device_hub_to_attach(udid)
            else:
                suppressed = await sim_input.legacy_input_is_suppressed(udid)
            if suppressed:
                logger.info(
                    "Input services on %s are held by Device Hub; restoring "
                    "them now, while nothing is running", udid[:8],
                )
                await sim_input.restore_legacy_input(udid)
            elif suppressed is None:
                logger.warning(
                    "Could not read the input-service state on %s; if taps do "
                    "nothing, see POST /api/v1/device/ui/restore-input", udid[:8],
                )
            elif await sim_input.device_hub_is_running():
                # Device Hub is up and never attached. Either this runtime
                # predates the handover, or the daemon crashed on startup and
                # every event will be discarded with no error (idb's case,
                # which nothing here can distinguish).
                logger.info(
                    "Device Hub is running but never claimed the input services "
                    "on %s; if taps do nothing, that is where to look", udid[:8],
                )
            self._input_checked[udid] = suppressed is False
        except (DeviceError, OSError) as exc:
            # Left unrecorded on purpose: the next input call re-reads the
            # state, and a repair that failed partway puts it back to
            # suppressed, so the warning still fires.
            logger.warning("Could not restore input services on %s: %s", udid[:8], exc)
            self._input_checked.pop(udid, None)

    async def shutdown(self, udid: str) -> None:
        """Shutdown a simulator or Android emulator."""
        if self._is_android(udid):
            if self._device_type(udid) == DeviceType.ANDROID_EMULATOR:
                await self.adb._run_adb_for_device(udid, "emu", "kill")
                if self._active_udid == udid:
                    self._active_udid = None
                return
            raise DeviceError("Shutdown not supported for physical Android devices", tool="adb")
        self._require_simulator(udid, "Shutdown")
        await self.simctl.shutdown(udid)
        if self._active_udid == udid:
            self._active_udid = None

    async def erase(self, udid: str) -> None:
        """Erase a simulator, resetting it to factory state. Shuts down first if booted."""
        self._require_simulator(udid, "Erase")
        # simctl erase requires the simulator to be shutdown
        try:
            await self.simctl.shutdown(udid)
        except DeviceError:
            pass  # already shutdown
        await self.simctl.erase(udid)
        if self._active_udid == udid:
            self._active_udid = None

    def _is_pre_ios17_udid(self, udid: str) -> bool:
        """Return True if this UDID is a pre-iOS 17 libimobiledevice UDID.

        Pre-iOS 17 devices are discovered via usbmux and have 40-character
        lowercase hex UDIDs.  iOS 17+ devices use CoreDevice UUIDs (RFC 4122
        format with dashes and uppercase hex).
        """
        return len(udid) == 40 and all(c in "0123456789abcdef" for c in udid)

    async def _install_app_legacy(self, udid: str, app_path: str) -> None:
        """Install an app on a pre-iOS 17 device via ideviceinstaller / pymobiledevice3."""
        import shutil

        if shutil.which("ideviceinstaller"):
            tool = "ideviceinstaller"
            cmd = ["ideviceinstaller", "-u", udid, "install", app_path]
        else:
            pmd3 = shutil.which("pymobiledevice3")
            if not pmd3:
                raise DeviceError(
                    "Neither ideviceinstaller nor pymobiledevice3 found. "
                    "Install with: brew install ideviceinstaller",
                    tool="install",
                )
            tool = "pymobiledevice3"
            cmd = [pmd3, "apps", "install", "--udid", udid, app_path]

        logger.info("Installing via %s on pre-iOS17 device %s", tool, udid[:8])
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise DeviceError(
                f"{tool} install failed (rc={proc.returncode}): {stderr.decode().strip()}",
                tool=tool,
            )

    async def install_app(self, app_path: str, udid: str | None = None) -> str:
        """Install an app. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        if self._is_android(resolved):
            await self.adb.install_app(resolved, app_path)
        elif self._is_physical(resolved):
            if self._is_pre_ios17_udid(resolved):
                await self._install_app_legacy(resolved, app_path)
            else:
                await self.devicectl.install_app(resolved, app_path)
        else:
            await self.simctl.install_app(resolved, app_path)
        return resolved

    async def launch_app(
        self,
        bundle_id: str,
        udid: str | None = None,
        env: dict[str, str] | None = None,
    ) -> str:
        """Launch an app. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        if self._is_android(resolved):
            await self.adb.launch_app(resolved, bundle_id)
        elif self._is_physical(resolved):
            await self.wda_client.activate_app(resolved, bundle_id)
        else:
            await self.simctl.launch_app(resolved, bundle_id, env=env)
        self._invalidate_ui_cache(resolved)  # UI changed
        return resolved

    async def terminate_app(self, bundle_id: str, udid: str | None = None) -> str:
        """Terminate an app. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        if self._is_android(resolved):
            await self.adb.terminate_app(resolved, bundle_id)
        elif self._is_physical(resolved):
            await self.wda_client.terminate_app(resolved, bundle_id)
        else:
            await self.simctl.terminate_app(resolved, bundle_id)
        return resolved

    async def uninstall_app(self, bundle_id: str, udid: str | None = None) -> str:
        """Uninstall an app. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        if self._is_android(resolved):
            await self.adb.uninstall_app(resolved, bundle_id)
        elif self._is_physical(resolved):
            await self.devicectl.uninstall_app(resolved, bundle_id)
        else:
            await self.simctl.uninstall_app(resolved, bundle_id)
        return resolved

    async def list_apps(self, udid: str | None = None) -> tuple[list[AppInfo], str]:
        """List installed apps. Returns (apps, resolved_udid)."""
        resolved = await self.resolve_udid(udid)
        if self._is_android(resolved):
            apps = await self.adb.list_apps(resolved)
        elif self._is_physical(resolved):
            apps = await self.devicectl.list_apps(resolved)
        else:
            apps = await self.simctl.list_apps(resolved)
        return apps, resolved

    async def _ensure_android_screen_on(self, udid: str) -> None:
        """Wake an Android device screen if it's off."""
        await self.adb.wake_screen(udid)

    async def screenshot(
        self,
        udid: str | None = None,
        format: str = "png",
        scale: float = 0.5,
        quality: int = 85,
    ) -> tuple[bytes, str]:
        """Capture and process a screenshot. Returns (image_bytes, media_type)."""
        # Use resolve_udid for fallback logic but don't change the active device
        if udid:
            await self._ensure_device_type_cached(udid)
            resolved = udid
        else:
            resolved = await self.resolve_udid(None)
        raw_png = await self.raw_screenshot(resolved)
        return process_screenshot(raw_png, format=format, scale=scale, quality=quality)

    async def raw_screenshot(self, resolved: str) -> bytes:
        """The device's own capture, before any re-encoding.

        Separate because a caller that is going to decode the image itself
        should not pay for it being resized and re-encoded first: measured, the
        processing adds ~62ms to a ~114ms capture, and settle detection decodes
        every frame anyway.
        """
        if self._is_android(resolved):
            # Only wake physical devices — emulator screencap works with screen off
            if not resolved.startswith("emulator-"):
                await self._ensure_android_screen_on(resolved)
            return await self.adb.screenshot(resolved)
        if self._is_physical(resolved):
            return await self.pmd3.screenshot(resolved)
        return await self.simctl.screenshot(resolved)

    async def set_location(
        self, latitude: float, longitude: float, udid: str | None = None,
    ) -> str:
        """Set simulated GPS location. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        if self._is_android(resolved):
            await self.adb.set_location(resolved, latitude, longitude)
        else:
            self._require_simulator(resolved, "Set location")
            await self.simctl.set_location(resolved, latitude, longitude)
        return resolved

    async def open_url(
        self, url: str, udid: str | None = None, bundle_id: str | None = None,
    ) -> str:
        """Open a URL on the device. Returns the resolved udid.

        On Android, pass `bundle_id` (the app package) to deliver the URL
        straight to that app — needed for deep links on debug/staging builds
        that aren't verified App Links (otherwise Android opens the browser).
        Ignored on iOS (universal links route to the associated app by the OS).
        """
        resolved = await self.resolve_udid(udid)
        if self._is_android(resolved):
            await self.adb.open_url(resolved, url, package=bundle_id)
        else:
            self._require_simulator(resolved, "Open URL")
            await self.simctl.open_url(resolved, url)
        return resolved

    async def grant_permission(
        self, bundle_id: str, permission: str, udid: str | None = None,
    ) -> str:
        """Grant an app permission. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        if self._is_android(resolved):
            await self.adb.grant_permission(resolved, bundle_id, permission)
        else:
            self._require_simulator(resolved, "Grant permission")
            await self.simctl.grant_permission(resolved, bundle_id, permission)
        return resolved

    async def set_locale(
        self, lang: str, country: str = "", udid: str | None = None,
    ) -> str:
        """Set the system locale. Returns the resolved udid.

        Android: via Quern Driver broadcast receiver or setprop fallback.
        iOS physical (USB): via pymobiledevice3 lockdown language + locale.
        iOS simulators: not yet supported.
        """
        resolved = await self.resolve_udid(udid)
        if self._is_android(resolved):
            await self.adb.set_locale(resolved, lang, country)
        elif self._is_physical(resolved):
            hw_udid = await self.get_libimobiledevice_udid(resolved)
            if not hw_udid:
                raise DeviceError(
                    "Locale change requires a USB connection (device not found via usbmux)",
                    tool="pymobiledevice3",
                )
            # iOS Language key uses SupportedLanguages format: just the
            # language code for most languages (e.g. "ja", "de", "fr"),
            # or lang-region for regional variants (e.g. "en-US", "pt-BR",
            # "zh-Hans"). Locale uses POSIX format (e.g. "ja_JP", "en_US").
            language_tag = f"{lang}-{country}" if country else lang
            locale_tag = f"{lang}_{country}" if country else lang
            await self.pmd3.set_language(hw_udid, language_tag)
            await self.pmd3.set_locale(hw_udid, locale_tag)
            logger.info(
                "iOS locale set: language=%s, locale=%s on %s (reboot may be needed)",
                language_tag, locale_tag, resolved[:8],
            )
        else:
            raise DeviceError(
                "set_locale is not yet supported for iOS simulators",
                tool="simctl",
            )
        return resolved

    async def set_hardware_keyboard(
        self, enabled: bool, udid: str | None = None,
    ) -> str:
        """Attach/detach the simulated hardware keyboard. Returns the resolved udid.

        iOS simulators only; requires the sim-bridge backend. While the
        hardware keyboard is attached the software keyboard stays hidden,
        which keeps UI trees small and screenshots unobstructed. Detaching
        restores the software keyboard for focused text fields.
        """
        resolved = await self.resolve_udid(udid)
        self._require_simulator(resolved, "Set hardware keyboard")
        if not self._sim_bridge_ok:
            raise DeviceError(
                "set_hardware_keyboard requires the sim-bridge backend "
                "(Xcode with SimulatorKit private frameworks)",
                tool="sim-bridge",
            )
        await self.sim_bridge.set_hardware_keyboard(resolved, enabled)
        return resolved

    async def set_font_scale(
        self, scale: float, udid: str | None = None,
    ) -> str:
        """Set the font scale. Android only. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        if not self._is_android(resolved):
            raise DeviceError("set_font_scale is currently Android-only", tool="simctl")
        await self.adb.set_font_scale(resolved, scale)
        return resolved

    async def set_display_density(
        self, dpi: int | None = None, udid: str | None = None,
    ) -> str:
        """Set display density override (or reset). Android only. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        if not self._is_android(resolved):
            raise DeviceError("set_display_density is currently Android-only", tool="simctl")
        await self.adb.set_display_density(resolved, dpi)
        return resolved

    async def clear_app_data(self, bundle_id: str, udid: str | None = None) -> str:
        """Clear all app data for a simulator app. Returns the resolved udid."""
        resolved = await self.resolve_udid(udid)
        self._require_simulator(resolved, "Clear app data")
        try:
            await self.simctl.terminate_app(resolved, bundle_id)
        except DeviceError:
            pass  # app wasn't running, proceed anyway
        await self.simctl.clear_app_data(resolved, bundle_id)
        return resolved
