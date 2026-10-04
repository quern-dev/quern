"""DeviceController — orchestrates device backends and tracks active device."""

from __future__ import annotations

import asyncio
import logging
import time

from server import logging_ext
from server.device.adb import AdbBackend
from server.device.controller_ui import DeviceControllerUI
from server.device.devicectl import DevicectlBackend, canonical_device_id, spellings_of
from server.device.idb import IdbBackend
from server.device.pmd3 import Pmd3Backend
from server.device.screenshots import process_screenshot
from server.device.sim_bridge import SimBridgeBackend, SimBridgeManager
from server.device.simctl import SimctlBackend
from server.device.u2_client import U2Backend
from server.device.usbmux import UsbmuxBackend
from server.device.wda_client import WdaBackend
from server.lifecycle.state import read_active_udid, write_active_udid
from server.logging_ext import current_action
from server.models import (
    AppInfo,
    BootIncompleteError,
    DeviceError,
    DeviceInfo,
    DeviceOperationUnsupportedError,
    DeviceState,
    DeviceType,
    EraseIncompleteError,
    UIElement,
)

logger = logging.getLogger(__name__)


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
        #: Backend name per device, written by `get_ui_elements` at the
        #: moment it selects one. See DeviceControllerUI._last_read_backend.
        self._last_read_backend: dict[str, str] = {}
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
        # Device type cache: udid -> DeviceType (populated by list_devices)
        self._device_type_cache: dict[str, DeviceType] = {}
        # Simulators whose input services have been checked this boot.
        # See server/device/sim_input.py; the check costs a `simctl
        # spawn` (~0.5s), so it is paid once per device rather than per
        # tap.
        self._input_checked: dict[str, bool] = {}
        # When each device was last asked about an input-service state that
        # could not be read; see _INPUT_PROBE_COOLDOWN_S.
        self._input_probe_cooldown: dict[str, float] = {}
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
        # Canonicalised here, not at the ten places that assign it.
        #
        # `resolve_udid` canonicalises what it returns, but the active device
        # is also written directly by `POST /device/active` and by four paths
        # in `DevicePool`. Those stored the raw udid, so a caller who set the
        # active device by the spelling Xcode shows got it back uncanonicalised
        # from branch 2 of `_resolve_udid` -- and once `GET /trace?udid=`
        # started canonicalising its query, *both* spellings returned nothing.
        # That is worse than before the canonicalisation existed, and it is the
        # "empty is indistinguishable from quiet" failure the trace exists to
        # prevent.
        #
        # Fixing the callers would have left the eleventh. This is the one
        # place the value lands, and it is also what the sidecar persists.
        value = canonical_device_id(value) if value else value
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
        from server.models import ToolSiteInfo
        from server.tooling.tool_versions import collect_sites, upgrade_note

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

    def _device_type(self, udid: str) -> DeviceType | None:
        """What kind of device this is, or None if quern has not been told.

        `None` rather than a default, because the default was `SIMULATOR` and
        it was a guess stated as a fact. Callers then dispatched an unknown
        UDID -- including the empty string -- to `simctl`, which on an
        Android-only host or the Linux target of `docs/linux-support-plan.md`
        means a toolchain that is not installed, so "unsupported device"
        surfaced as "xcrun not found" (#263).

        It is not hypothetical. Hours after #305 shipped, `apply=true` on a
        physically attached Pixel 3 XL was refused with "apply is only
        supported on Android" because the cache was cold; two earlier runs had
        passed only because something else warmed it first.

        Use `_ensure_device_type_cached()` first if the answer matters.
        """
        return self._device_type_cache.get(udid)

    def _is_physical(self, udid: str) -> bool:
        """Whether this is a physical *iOS* device, positively known to be.

        False for an unknown udid, which is a change: `_device_type` used to
        answer `SIMULATOR` for a cache miss and this compared against `DEVICE`,
        so an unknown device read as "not physical" for the same reason it
        read as "a simulator" -- a guess. It is now the absence of an answer.
        Warm with `_ensure_device_type_cached` where that distinction matters.
        """
        return self._device_type(udid) == DeviceType.DEVICE

    def _is_android(self, udid: str) -> bool:
        """Whether this is an Android device or emulator, positively known.

        Both kinds, because almost every caller wants "does this go to adb?".
        Where the two differ -- `emu kill`, `geo fix` -- the question is really
        about the transport, and `adb.is_console_serial` answers that (#299).
        """
        return self._device_type(udid) in (DeviceType.ANDROID_EMULATOR, DeviceType.ANDROID_DEVICE)

    #: What an Android caller should do instead, keyed by the operation name
    #: passed to `_require_simulator`. Three distinct situations, and a
    #: refusal that conflates them is worth little: *no equivalent exists*,
    #: *an equivalent exists and quern has not built it*, and *an equivalent
    #: exists with different semantics*. Saying "no Android equivalent" for
    #: the second is simply false, and it was -- `run-as <pkg> cat
    #: shared_prefs/<name>.xml` reads an app's preferences on an unrooted
    #: phone today, measured.
    #:
    #: Absent from this map means the first case: nothing to point at.
    #: What an Android caller should reach for instead: a noun phrase for
    #: the capability, the mechanism that provides it, and the issue tracking
    #: it. Rendered into a sentence below rather than written as one.
    #:
    #: Structured, not prose, and that is the whole point. The first version
    #: of this table was a tutorial -- exact commands, storage layouts,
    #: caveats -- and three separate reviews each found another sentence in
    #: it that was not quite true: that `inotifyd` on the prefs *file* works
    #: (it goes deaf after SharedPreferences' rename-based write), that
    #: preferences are either SharedPreferences or DataStore (an app can have
    #: both, and a third can be encrypted), that `-wipe-data` "restarts" an
    #: emulator (it is a launch flag).
    #:
    #: Shortening it was not enough: a mutant replacing an entry with
    #: "`inotifyd` reports every write reliably, so polling is unnecessary"
    #: -- a claim already disproved on the hardware -- survived the suite,
    #: because no test can check prose for truth. Leaving only a noun phrase
    #: and a mechanism removes the room to assert anything. Operational
    #: detail belongs in #314, where being wrong fails a test instead of
    #: reaching a caller.
    _ANDROID_ALTERNATIVE: dict[str, tuple[str, str, int]] = {
        "read_app_plist": ("reading an app's own preference files", "run-as", 314),
        "set_app_plist_value": ("writing an app's own preference files", "run-as", 314),
        "set_app_plist_values": ("writing an app's own preference files", "run-as", 314),
        "delete_app_plist_key": ("editing an app's own preference files", "run-as", 314),
        "diff_app_plist": ("reading an app's own preference files", "run-as", 314),
        "start_plist_watch": ("watching an app's own files for changes", "inotifyd", 314),
        "save_app_state": ("archiving an app's own data directory", "run-as", 314),
        "restore_app_state": ("restoring an app's own data directory", "run-as", 314),
        # `hw.keyboard` is read at boot, so honouring this means restarting the
        # emulator, where the iOS call is instant -- and the runtime route,
        # switching to quern's own input method, is exactly what left a phone
        # stranded on it in March. #356 holds the question. This pointed at
        # #263 until it closed as fixed, which sent callers to "both defects
        # are fixed" for work that was never part of it.
        "Set hardware keyboard": (
            "the hardware-keyboard setting", "the hw.keyboard AVD property", 356,
        ),
    }

    #: Entries whose mechanism is `run-as`, which the platform refuses for a
    #: package that is not debuggable -- measured: a release build answers
    #: `run-as: package not debuggable`. Derived rather than listed, so the
    #: two collections cannot drift: an entry that uses `run-as` is scoped by
    #: construction.
    @classmethod
    def _needs_debuggable(cls, operation: str) -> bool:
        entry = cls._ANDROID_ALTERNATIVE.get(operation)
        return bool(entry) and entry[1] == "run-as"

    def _require_simulator(self, udid: str, operation: str) -> None:
        """Refuse anything that is not known to be an iOS simulator.

        Tests *for* `SIMULATOR` rather than *against* `DEVICE`. The old form
        rejected only physical iOS, so both Android kinds -- and any unknown
        UDID -- walked through a guard whose entire purpose was to stop them
        and were handed to `simctl` with an adb serial. Verified before the
        change: `ANDROID_DEVICE`, `ANDROID_EMULATOR` and `''` all passed.

        Testing for the allowed kind means a device type added later is
        refused by default rather than admitted by default, which is the half
        of this that keeps being true after today (#263).
        """
        kind = self._device_type(udid)
        if kind == DeviceType.SIMULATOR:
            return
        # The leading clause is load-bearing: `_handle_device_error` matches
        # the literal string "only supported on simulators" to return 400, so
        # rewording it wholesale would silently turn every one of these
        # refusals into a 500. The detail is appended rather than substituted.
        if kind in (DeviceType.ANDROID_DEVICE, DeviceType.ANDROID_EMULATOR):
            entry = self._ANDROID_ALTERNATIVE.get(operation)
            if entry:
                capability, mechanism, issue = entry
                scope = (
                    "For a debuggable app, or any app on a rootable emulator, "
                    if self._needs_debuggable(operation) else ""
                )
                # Capitalised only when it starts the sentence, since the
                # scope prefix is itself a sentence opener.
                phrase = capability if scope else capability[0].upper() + capability[1:]
                detail = (
                    f" {udid} is Android. {scope}{phrase} is available "
                    f"through {mechanism}; quern does not expose it yet -- "
                    f"see #{issue}."
                )
            else:
                # A claim about quern, not about Android. The previous
                # wording -- "there is no Android equivalent for this, it is a
                # simulator-only concept" -- asserted something about the
                # world, and was false for every operation that reached it:
                # `Set hardware keyboard` has `hw.keyboard` and
                # `show_ime_with_hard_keyboard`, and the operations that only
                # `if` ordering keeps away from here include `Clear app data`,
                # whose own docstring in this file records being burned by
                # exactly this claim. quern not having a path is checkable and
                # stays true; the platform lacking a feature is neither.
                detail = (
                    f" {udid} is Android, and quern has no Android path for "
                    "this -- it is implemented through simctl."
                )
        elif kind == DeviceType.DEVICE:
            detail = f" {udid} is a physical iOS device."
        else:
            detail = (
                f" quern does not recognise {udid or '(empty udid)'}; list "
                "devices first so it can be identified."
            )
        raise DeviceError(
            f"{operation} is only supported on simulators.{detail}",
            tool="simctl",
        )

    async def _ensure_device_type_cached(self, udid: str) -> None:
        """Populate device type cache if this UDID isn't known yet.

        Called lazily when a UDID is used that hasn't been seen via
        list_devices(). Without this the type stays unknown -- it used to
        default to simulator (#263) -- so physical devices get routed to idb
        instead of WDA, and operations that ask the type are refused rather
        than dispatched.
        """
        if udid not in self._device_type_cache:
            logger.debug("Device type unknown for %s, refreshing device list...", udid[:8])
            await self.list_devices()

    async def resolve_udid(
        self, udid: str | None = None, *, set_active: bool = True,
    ) -> str:
        """Resolve which device to target, and tell the action log about it.

        This is the one place that *decides* which device a call goes to, so
        it is where the action entry learns its udid. Recording it in each
        handler instead was tried and left most of them blank: the handlers
        wrapped in a `with action(...)` block set it, and the ~78 decorated
        with `@logged_action` did not, so a per-device trace silently lost
        every one of them and their flows fell back to matching on time alone.

        The assignment is a no-op when no action is being recorded.
        """
        resolved = await self._resolve_udid(udid, set_active=set_active)
        current_action().udid = resolved
        logging_ext.note_action_device(resolved)
        return resolved

    async def _resolve_udid(
        self, udid: str | None = None, *, set_active: bool = True,
    ) -> str:
        """Resolve which device to target.

        If a DevicePool is attached, attempts pool-based resolution for
        claim-aware, multi-device-friendly behavior. If pool resolution
        fails for any reason, silently falls back to the original logic.

        `set_active=False` resolves without changing which device subsequent
        unqualified calls go to. It exists so a read that names its own device
        does not have to bypass this function to dodge the side effect --
        bypassing is what `screenshot` did, and it silently cost the action log
        its udid and physical devices their routing.

        Resolution order:
        1. Explicit udid parameter → canonicalise it, use it, update active
        2. Stored active_udid → use it
        3. Pool resolution (if pool attached) → best available booted device
        4. Fallback: simple auto-detect (original logic, unchanged)
        """
        if udid:
            # Warm the caches first: `_ensure_device_type_cached` refreshes the
            # device list when it does not recognise the udid, and that refresh
            # is what learns a physical device's other spelling. Canonicalising
            # before it would look the alias up in an empty map.
            await self._ensure_device_type_cached(udid)
            canonical = canonical_device_id(udid)
            if canonical != udid:
                logger.debug(
                    "Resolved %s to its canonical identifier %s",
                    udid[:8], canonical[:8],
                )
                # The type cache is keyed on the canonical spelling only, so
                # `_ensure_device_type_cached(udid)` above is a guaranteed miss
                # every time a caller names the hardware udid -- a full
                # simctl+devicectl+usbmux+adb enumeration per call, for a device
                # already known. Teaching the cache the other spelling is what
                # stops that; re-running `ensure` on the canonical, which is
                # what this used to do, was a no-op in every reachable path
                # (deleting it left all 168 tests green).
                known = self._device_type_cache.get(canonical)
                if known is not None:
                    self._device_type_cache[udid] = known
            if set_active:
                self._active_udid = canonical
            return canonical

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
            # The canonical spelling, not `restored`. `__init__` writes the
            # persisted udid straight into the backing field -- deliberately,
            # since restoring is not a change worth writing -- so it bypasses
            # the setter that canonicalises. A sidecar holding the hardware
            # udid therefore survives a restart, and this branch returned it
            # raw on the first call: `resolve_udid` records that on the action,
            # and trace ownership compares udids exactly, so the action reads
            # FOREIGN against everything recorded canonically.
            #
            # Reproduced: sidecar = hardware udid, `resolve_udid()` returned
            # the hardware udid while `_active_udid` held the canonical one.
            return self._active_udid

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
        # OSError alongside DeviceError, on every one of these. The backends
        # raise DeviceError for a tool that ran and refused; a tool that is not
        # installed never runs, and asyncio.create_subprocess_exec raises
        # FileNotFoundError -- an OSError, and not a DeviceError. So the handler
        # that says "simctl unavailable" did not catch simctl being unavailable,
        # which is the one case it names. On a Mac every binary is present and
        # nothing noticed; a host without Xcode took the exception through
        # resolve_udid and out of whatever call warmed the cache.
        try:
            sim_devices = await self.simctl.list_devices()
        except (DeviceError, OSError):
            logger.debug("simctl list_devices failed (simctl unavailable)", exc_info=True)
            sim_devices = []
        try:
            physical_devices = await self.devicectl.list_devices()
        except (DeviceError, OSError):
            logger.debug("devicectl list_devices failed", exc_info=True)
            physical_devices = []
        try:
            usbmux_devices = await self.usbmux.list_devices()
        except (DeviceError, OSError):
            logger.debug("usbmux list_devices failed", exc_info=True)
            usbmux_devices = []
        try:
            android_devices = await self.adb.list_devices()
        except (DeviceError, OSError):
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

        # Build CoreDevice UUID -> libimobiledevice UDID mapping. Exactly,
        # through the identity aliases devicectl records: its hardware UDID
        # *is* the USB UDID, so a phone is on USB when usbmux lists one of its
        # spellings. When usbmux could not be asked at all (pymobiledevice3
        # missing or timing out), devicectl's own "wired" transport stands in
        # -- every phone used to read "not on USB" then. Only then: when usbmux
        # answered without the phone, that answer wins, or a pull would go
        # ahead against a UDID usbmux does not have.
        # It used to correlate names, and two phones sharing one ("iPhone" is
        # the default) could map to each other's UDID -- so a crash pull filed
        # one phone's reports under the other, on disk once pulls kept a
        # directory per phone. A name fallback survived that change for phones
        # devicectl listed without a hardware UDID, pending a measurement on
        # Xcode 27 (#323). Measured, and removed: devicectl 642.16 reports
        # `hardwareProperties.udid` for every paired physical device, as 518.31
        # did, so the fallback was unreachable -- confirmed at runtime, each
        # phone having an alias and so skipping it. Across all three states a
        # paired device can be in: wired, on Wi-Fi, and disconnected. The last
        # was checked by taking a phone off the network, which devicectl then
        # lists with no `transportType` at all and `tunnelState: unavailable`,
        # and it still carried its UDID. That state matters most, because the
        # alias has to come from pairing rather than from a live connection for
        # any of this to hold.
        #
        # A listed phone that matches nothing now loses its old mapping: one
        # unplugged since, and now on Wi-Fi, kept it and was pulled over a USB
        # connection that no longer existed. A phone absent from this listing
        # keeps it, since a failed devicectl call is not evidence of anything.
        usb_answer = await self.usbmux.get_usb_devices() if physical_devices else []
        usbmux_failed = usb_answer is None
        usb_devices = usb_answer or []
        usb_udids = {udid for udid, _ in usb_devices}
        hardware = {d.udid: [s for s in spellings_of(d.udid) if s != d.udid]
                    for d in physical_devices}
        matched: dict[str, str] = {}
        for d in physical_devices:
            exact = next((s for s in hardware[d.udid] if s in usb_udids), None)
            if (exact is None and usbmux_failed and d.connection_type == "usb"
                    and len(hardware[d.udid]) == 1):
                exact = hardware[d.udid][0]
            if exact:
                matched[d.udid] = exact
        for d in physical_devices:
            if d.udid in matched:
                self._usbmux_udid_map[d.udid] = matched[d.udid]
            else:
                self._usbmux_udid_map.pop(d.udid, None)

        return sim_devices + physical_devices + usbmux_devices + android_devices

    async def get_libimobiledevice_udid(self, coredevice_udid: str) -> str | None:
        """Look up the libimobiledevice UDID for a CoreDevice UUID.

        For pre-iOS 17 devices discovered via usbmux, the UDID is already in
        libimobiledevice format (40-char hex) — return it directly.

        Returns None if the device is not USB-connected (e.g. network-only).
        Refreshes the mapping if the UDID isn't found on first lookup.

        Any spelling of the device is accepted. The map is keyed by CoreDevice
        UUID, and a caller holding the hardware UDID -- the one `idevice_id`,
        Xcode and Finder show -- was told a phone plugged in over USB was not
        connected. The alias is re-read after the refresh, because the refresh
        is what records it on a server that has not listed devices yet.
        """
        # Check the CoreDevice -> libimobiledevice mapping
        udid = self._usbmux_udid_map.get(canonical_device_id(coredevice_udid))
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

        udid = self._usbmux_udid_map.get(canonical_device_id(coredevice_udid))
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
            # Warm first. `boot` is the one device entry point that does not
            # go through `resolve_udid`, so nothing else populates the cache
            # here -- and since `_device_type` stopped guessing `SIMULATOR`
            # (#263), an unwarmed cache made a perfectly valid simulator
            # unrecognised and refused by the guard below. `shutdown` and
            # `erase` are fine because they resolve first.
            await self._ensure_device_type_cached(udid)
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

        # A previous boot of this udid may have left a verdict behind, and it
        # describes a device that no longer exists. Cleared before the probe,
        # so a boot that cannot read the state leaves nothing stale: otherwise
        # an old True survives, the first input call skips its probe, and a
        # simulator whose services were taken never warns.
        self._input_checked.pop(udid, None)
        self._input_probe_cooldown.pop(udid, None)

        try:
            # Device Hub attaches a few seconds after the boot returns, so a
            # repair applied immediately is undone by an attachment that has
            # not happened yet. Wait for it, but only when Device Hub is
            # running -- otherwise there is nothing to wait for.
            hub_running = await sim_input.device_hub_is_running()
            if hub_running:
                suppressed = await sim_input.wait_for_device_hub_to_attach(udid)
            else:
                suppressed = await sim_input.legacy_input_is_suppressed(udid)
            if suppressed:
                logging_ext.info(
                    logger,
                    "Input services on %s are held by Device Hub; restoring "
                    "them now, while nothing is running", udid[:8],
                    category="device.lifecycle", udid=udid,
                )
                await sim_input.restore_legacy_input(udid)
            elif suppressed is None:
                logging_ext.warning(
                    logger,
                    "Could not read the input-service state on %s; if taps do "
                    "nothing, see POST /api/v1/device/ui/restore-input", udid[:8],
                    category="device.lifecycle", udid=udid,
                )
            elif hub_running:
                # Device Hub is up and never attached within the wait. Either
                # this runtime predates the handover, or the daemon crashed on
                # startup and every event will be discarded with no error
                # (idb's case, which nothing here can distinguish) -- or it is
                # simply slower than the wait today.
                logger.info(
                    "Device Hub is running but never claimed the input services "
                    "on %s; if taps do nothing, that is where to look", udid[:8],
                )

            # Cached only where the answer is settled: a repair that worked, or
            # a healthy simulator on a machine with no Device Hub to change its
            # mind. An unreadable state, or a wait that timed out with Device
            # Hub running, leaves it unset so the first input call asks again
            # -- the cache exists to skip a ~0.5s probe, not to stand in for an
            # answer nobody got.
            if suppressed is True or (suppressed is False and not hub_running):
                self._input_checked[udid] = True
        except (DeviceError, OSError) as exc:
            # Left unrecorded on purpose: the next input call re-reads the
            # state, and a repair that failed partway puts it back to
            # suppressed, so the warning still fires.
            logger.warning("Could not restore input services on %s: %s", udid[:8], exc)
            self._input_checked.pop(udid, None)

    async def shutdown(self, udid: str) -> None:
        """Shutdown a simulator or Android emulator."""
        if self._is_android(udid):
            # The *console*, not the device kind. `adb emu kill` travels only
            # over the local `emulator-NNNN` serial, so the same AVD reached
            # over TCP cannot be killed this way even though it is every bit
            # an emulator -- measured, `adb emu` returns empty there. Asking
            # the type here would have started issuing console commands down a
            # connection that cannot carry them the moment the classifier
            # began recognising TCP-attached emulators correctly.
            if self.adb.is_console_serial(udid):
                await self.adb._run_adb_for_device(udid, "emu", "kill")
                if self._active_udid == udid:
                    self._active_udid = None
                return
            if self._device_type(udid) == DeviceType.ANDROID_EMULATOR:
                raise DeviceOperationUnsupportedError(
                    f"{udid} is an emulator, but it is attached over TCP and "
                    "`adb emu kill` needs the local console serial. Shut it "
                    "down through its `emulator-NNNN` serial, or stop the "
                    "process hosting it.",
                    tool="adb",
                )
            raise DeviceOperationUnsupportedError(
                "Shutdown not supported for physical Android devices", tool="adb",
            )
        self._require_simulator(udid, "Shutdown")
        await self.simctl.shutdown(udid)
        if self._active_udid == udid:
            self._active_udid = None

    #: An erased emulator boots cold -- the wipe discards the snapshot and
    #: the first boot of fresh userdata is the slow one.
    #: Boot and boot-completed share this one budget. With the kill's, the
    #: worst case stays under the MCP client's 300s request timeout, so a slow
    #: boot is reported by the server rather than as a dropped connection.
    _ERASE_BOOT_TIMEOUT = 240.0
    _ERASE_KILL_TIMEOUT = 30.0

    async def erase(self, udid: str) -> str:
        """Reset a simulator or Android emulator to factory state.

        Returns the udid the erased device is now at. For a simulator that is
        the one passed in, shut down. An Android emulator comes back *running*,
        because `-wipe-data` is a launch flag: erasing one means killing it and
        booting the same AVD again with the flag, and the boot may land on a
        different console port. Nobody calls erase by accident, so the session
        ending is the point rather than a side effect (#356).
        """
        if self._is_android(udid):
            return await self._erase_android_emulator(udid)
        if udid.startswith("avd:"):
            # A shut-down AVD, listed under its name because it has no serial
            # yet. Nothing to kill: booting it with `-wipe-data` *is* the erase.
            return await self._erase_android_emulator(udid)
        self._require_simulator(udid, "Erase")
        # simctl erase requires the simulator to be shutdown
        try:
            await self.simctl.shutdown(udid)
        except DeviceError:
            pass  # already shutdown
        await self.simctl.erase(udid)
        if self._active_udid == udid:
            self._active_udid = None
        return udid

    async def _erase_android_emulator(self, udid: str) -> str:
        """Kill an emulator and boot its AVD again with `-wipe-data`.

        Everything that can refuse runs before the kill: the device kind, the
        AVD name, whether this server can launch that AVD at all, and whether
        something else is already booting or erasing it. After the kill there is
        no undo, so a failure from there on says the emulator is gone.
        """
        shut_down = udid.startswith("avd:")
        if not shut_down and not self.adb.is_console_serial(udid):
            # The console, not the device kind -- the same reasoning, and the
            # same refusals, as `shutdown`: `adb emu` travels only over the
            # local `emulator-NNNN` serial.
            if self._device_type(udid) == DeviceType.ANDROID_EMULATOR:
                raise DeviceOperationUnsupportedError(
                    f"{udid} is an emulator, but it is attached over TCP, and "
                    "erasing one needs its local console to learn the AVD name "
                    "and kill it. Erase it through its `emulator-NNNN` serial.",
                    tool="adb",
                )
            raise DeviceOperationUnsupportedError(
                f"Erase is not possible on a physical Android device ({udid}): a "
                "factory reset needs input on the device itself -- in Settings, "
                "or in recovery mode's menu -- so adb cannot perform one.",
                tool="adb",
            )

        avd = udid.removeprefix("avd:") if shut_down else await self.adb.avd_name(udid)
        if avd in self.adb._booting_avds:
            raise DeviceOperationUnsupportedError(
                f"AVD '{avd}' is already being booted or erased; try again once "
                "that has finished.",
                tool="emulator",
            )
        headless = (
            False if shut_down else await self.adb.emulator_was_headless(avd)
        )
        await self.adb.check_can_boot(avd)
        was_active = self._active_udid == udid

        # Held across the whole kill-and-boot, so the AVD does not read as
        # shut down -- and so bootable by anyone else -- in the gap between.
        self.adb._booting_avds.add(avd)
        serial: str | None = None
        try:
            if not shut_down:
                try:
                    await self.adb._run_adb_for_device(udid, "emu", "kill")
                except DeviceError:
                    # A kill is a write, not retryable, and a non-zero exit
                    # after the command was sent can still mean it ran. So the
                    # exit status decides nothing: `_wait_until_gone` watches
                    # what actually happened, and either sees it go or says it
                    # was not wiped (review).
                    logger.warning("`emu kill` on %s reported failure; "
                                   "checking whether it went anyway", udid)
                await self._wait_until_gone(udid, avd, self._ERASE_KILL_TIMEOUT)

            loop = asyncio.get_running_loop()
            deadline = loop.time() + self._ERASE_BOOT_TIMEOUT
            try:
                serial = await self.adb.boot_emulator(
                    avd, timeout=self._ERASE_BOOT_TIMEOUT,
                    headless=headless, wipe_data=True,
                )
                # Follow the device before waiting on it, so a slow
                # boot-completed leaves quern pointing at the emulator that
                # exists rather than at a serial that is gone.
                self._adopt_erased_serial(udid, serial, was_active)
                # Report the outcome, not the request: listed by adb is not
                # started. Measured: about nine seconds apart on an erase.
                await self.adb.wait_for_boot_completed(
                    serial, max(deadline - loop.time(), 0.0),
                )
            except DeviceError as e:
                if serial is None and isinstance(e, BootIncompleteError):
                    # It came back -- adb lists it -- and only Android is
                    # slow. Follow it, rather than reporting it gone.
                    serial = e.serial
                    self._adopt_erased_serial(udid, serial, was_active)
                if serial is None:
                    self._device_type_cache.pop(udid, None)
                    if was_active:
                        self._active_udid = None
                what = (
                    f"{udid} was shut down for the erase and did not come back"
                    if serial is None else
                    f"{udid} was relaunched as {serial} for the erase, but"
                    " Android did not finish starting"
                )
                raise EraseIncompleteError(
                    f"{what}: {e}. It may already be wiped.",
                    previous_udid=udid, udid=serial, tool=e.tool,
                ) from e
        finally:
            self.adb._booting_avds.discard(avd)
        return serial

    def _adopt_erased_serial(self, old: str, new: str, was_active: bool) -> None:
        """Move what quern knows about the device to the serial it came back on."""
        if new != old:
            self._device_type_cache.pop(old, None)
        self._device_type_cache[new] = DeviceType.ANDROID_EMULATOR
        # Per-serial state describes the device as it was before the wipe: a
        # UI tree of screens that no longer exist, and a cached uiautomator2
        # connection to an agent the wipe removed.
        for stale in {old, new}:
            self._invalidate_ui_cache(stale)
            self._input_checked.pop(stale, None)
            self.u2._disconnect(stale)
        if was_active:
            # Unlike a simulator, which an erase leaves shut down, this device
            # is up and is the one the caller was using -- possibly on a new
            # serial, which they would otherwise have to go and find.
            self._active_udid = new

    async def _wait_until_gone(self, serial: str, avd: str, timeout: float) -> None:
        """Wait until the emulator is gone -- from adb, and as a process.

        adb alone is not enough: `list_devices` answers an empty list when
        `adb devices` itself fails, which reads exactly like "gone". The
        process check covers that. Measured, the process exits before adb drops
        the serial, so on a healthy kill this costs nothing extra; when `ps`
        cannot be run, adb's answer is all there is.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            unlisted = serial not in {d.udid for d in await self.adb.list_devices()}
            running = await self.adb.emulator_running(avd)
            if unlisted and running is not True:
                return
            await asyncio.sleep(1)
        raise DeviceError(
            f"{serial} was told to shut down for the erase but was still running "
            f"after {timeout:.0f}s; it has not been wiped.",
            tool="adb",
        )

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

    #: How long to wait for a launched app to become the application on
    #: screen. A healthy launch is frontmost well inside this; a refused one
    #: never is, and the pid decides once it expires.
    _LAUNCH_FRONTMOST_TIMEOUT_S = 3.0
    _LAUNCH_FRONTMOST_INTERVAL_S = 0.25

    async def launch_app(
        self,
        bundle_id: str,
        udid: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[str, dict]:
        """Launch an app. Returns the resolved udid and, when `env` was
        given, what became of it: `env_applied`, and `restarted` -- whether a
        running instance was stopped so the variables could apply.

        An environment reaches only a process that is starting. Both iOS
        routes bring a running app forward instead, keeping the environment
        it started with, and say nothing (measured on each), so with `env`
        quern restarts a running app rather than report variables it never
        delivered. Without `env` a running app is brought forward as before.

        A physical iPhone launches through WDA (XCUIApplication), which
        takes an environment; a simulator through `simctl launch`. Both get
        QUERN_AUTOMATION=YES whenever quern starts the process. Android apps
        take no environment variables, so there `env` is reported as not
        applied rather than dropped silently.
        """
        resolved = await self.resolve_udid(udid)
        info: dict = {}
        if self._is_android(resolved):
            await self.adb.launch_app(resolved, bundle_id)
            if env:
                info = {"env_applied": False,
                        "warning": "Android apps do not receive environment variables, so "
                                   "env was not applied. Pass values the app reads from its "
                                   "launch intent or settings instead."}
        elif self._is_physical(resolved):
            info = await self._launch_on_device(resolved, bundle_id, env)
        else:
            restarted: bool | None = None
            if env:
                # Read before the launch: after a restart the new process is
                # always running, so a read afterwards would always say yes.
                try:
                    restarted = await asyncio.wait_for(
                        self.simctl.running_pid(resolved, bundle_id),
                        self._LAUNCH_STATE_READ_TIMEOUT_S) is not None
                except Exception:  # noqa: BLE001 - only the report loses; the launch goes on
                    restarted = None
            pid = await self.simctl.launch_app(resolved, bundle_id, env=env, restart=bool(env))
            self._invalidate_ui_cache(resolved)
            await self._confirm_the_app_came_up(resolved, bundle_id, pid)
            if env:
                info = {"env_applied": True, "restarted": restarted}
        self._invalidate_ui_cache(resolved)  # UI changed
        return resolved, info

    #: Bound on the state reads around a launch: they inform the report, and
    #: a wedged one must cost the report, never the launch.
    _LAUNCH_STATE_READ_TIMEOUT_S = 5.0
    #: How long a launched app has to reach the foreground before the launch
    #: is called failed. XCUIApplication.launch normally returns with it
    #: there; this covers a momentary background state on the way.
    _LAUNCH_FRONT_GRACE_S = 2.0
    _LAUNCH_FRONT_INTERVAL_S = 0.25

    async def _launch_on_device(
        self, udid: str, bundle_id: str, env: dict[str, str] | None,
    ) -> dict:
        """Launch on a physical iPhone through WDA.

        Without `env`, a running app is activated as before, and one known
        to be stopped is started with QUERN_AUTOMATION=YES, as a simulator's
        is; one whose state is unknown is activated, which starts it without
        the variable. With `env`, a running app -- or one whose state is
        unknown, since terminating a stopped app is harmless -- is
        terminated first, because WDA's launch only activates a running app
        and its variables would never arrive.

        The caller's `env` can override QUERN_AUTOMATION, as on a simulator.
        """
        try:
            state: int | None = await asyncio.wait_for(
                self.wda_client.app_state(udid, bundle_id), self._LAUNCH_STATE_READ_TIMEOUT_S)
        except Exception:  # noqa: BLE001 - unknown, and handled as unknown below
            state = None
        if state == 0:          # XCUIApplicationStateUnknown
            state = None
        launch_env = {"QUERN_AUTOMATION": "YES", **(env or {})}
        if env:
            running = None if state is None else state in (2, 3, 4)
            if running is not False:
                try:
                    terminated = await self.wda_client.terminate_app(udid, bundle_id)
                except DeviceError as exc:
                    raise DeviceError(
                        f"could not stop the running {bundle_id} so env would apply: {exc}",
                        tool="wda",
                    ) from exc
                if running is None and terminated is not None:
                    # WDA answers true only for an app it found running.
                    running = terminated
            await self.wda_client.launch_app(udid, bundle_id, launch_env)
            return {"env_applied": True, "restarted": running,
                    **await self._confirm_in_front_on_device(udid, bundle_id)}
        if state == 1:
            await self.wda_client.launch_app(udid, bundle_id, launch_env)
            return await self._confirm_in_front_on_device(udid, bundle_id)
        await self.wda_client.activate_app(udid, bundle_id)
        return {}

    async def _confirm_in_front_on_device(self, udid: str, bundle_id: str) -> dict:
        """Fail a launch WDA accepted when the app does not reach the front.

        Polled for `_LAUNCH_FRONT_GRACE_S`, so a background state on the way
        is not a failure. Reads that fail are not a verdict either way: the
        launch stands, and the response says the check could not be made."""
        deadline = time.monotonic() + self._LAUNCH_FRONT_GRACE_S
        state: int | None = None
        error = ""
        while True:
            try:
                state = await asyncio.wait_for(
                    self.wda_client.app_state(udid, bundle_id),
                    self._LAUNCH_STATE_READ_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001 - reported, not a verdict
                error = str(exc) or type(exc).__name__
            else:
                if state == 4:
                    return {}
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(self._LAUNCH_FRONT_INTERVAL_S)
        if state is None:
            return {"launch_confirmed": None,
                    "launch_check_error": f"could not read the app's state: {error}"}
        raise DeviceError(
            f"{bundle_id} was launched and is not in the foreground "
            f"(XCUIApplication state {state})",
            tool="wda",
        )

    async def _confirm_the_app_came_up(
        self, udid: str, bundle_id: str, pid: int | None,
    ) -> None:
        """Fail when the launch was accepted and the app never ran.

        `simctl launch` reports the launch it *requested*. It exits 0 and
        prints a pid for an app the system then refuses, which on iOS 27 is
        every app without a scene manifest: UIKit logs "UIScene life cycle is
        required for apps built with this SDK" and kills it. Quern answered
        `launched`, the screen stayed on SpringBoard, and every later call
        failed as "no element found" -- a reason with nothing to do with the
        cause (#235).

        Waiting on the pid alone cannot be cheap: measured on an iOS 27
        simulator, the refused process stays alive **2.3s** before the system
        takes it, so a liveness check that runs before that reports success
        and one that waits for it costs every launch 2.5s.

        So the signal is the app becoming frontmost, which a healthy launch
        does in well under a second and a refused one never does. The pid is
        the tie-breaker for the case that cannot be told apart otherwise: an
        app still starting looks exactly like one that never will, until you
        ask whether its process is there.
        """
        deadline = time.monotonic() + self._LAUNCH_FRONTMOST_TIMEOUT_S
        while True:
            if await self._is_frontmost(udid, bundle_id):
                return
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(self._LAUNCH_FRONTMOST_INTERVAL_S)

        if self.simctl.process_is_alive(pid):
            # Slow to draw, not dead. Saying nothing is right: the caller has
            # `wait_for_element` for readiness, and refusing here would fail
            # every cold start on a loaded machine.
            return
        raise DeviceError(
            f"{bundle_id} was launched and is not running"
            f"{await self.simctl.why_launch_failed(udid, bundle_id)}",
            tool="simctl",
        )

    async def _is_frontmost(self, udid: str, bundle_id: str) -> bool:
        """Is that bundle the application on screen?

        Compared on the Application element's label against the app's own
        `CFBundleName`/`CFBundleDisplayName`, because the accessibility tree
        names an app the way a person would, not by bundle id. A read that
        fails answers False; it is retried until the deadline, and the pid
        decides after that.
        """
        name = await self.simctl.app_display_name(udid, bundle_id)
        if not name:
            # Not frontmost as far as this check can tell -- which lets the
            # deadline expire and hands the decision to the pid. Answering
            # True instead would skip that, and a dead process would report a
            # successful launch: "cannot tell from the screen" is not the
            # same as "nothing is wrong", and the process is evidence the
            # screen is not.
            return False
        try:
            elements, _ = await self.get_ui_elements(
                udid, use_cache=False, filter_type="Application",
                probe_containers=False,
            )
        except Exception:        # noqa: BLE001 - a read that fails is not a verdict
            return False
        return any((e.label or "") == name for e in elements)

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
        # Through `resolve_udid`, not around it. This short-circuited when
        # the caller named a device, to avoid changing the active one, and so
        # skipped everything else that function does: the action log never
        # learned the udid (fixed once by repeating the assignment here, which
        # left the bypass in place), and a physical device's second spelling
        # was never canonicalised, which routed a connected iPhone to simctl.
        # `set_active=False` buys the same thing without the bypass.
        resolved = await self.resolve_udid(udid, set_active=False)
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
            # Only wake physical devices — emulator screencap works with the
            # screen off. Asks the device type rather than the serial, so an
            # emulator reached over TCP is not woken needlessly; the prefix
            # test called that one physical.
            if self._device_type(resolved) != DeviceType.ANDROID_EMULATOR:
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

    #: How long `open_url` waits for the expected app to come to the front. A
    #: link hands over in well under a second; the rest is a cold start's
    #: margin.
    _OPEN_URL_FRONTMOST_TIMEOUT_S = 5.0
    _OPEN_URL_FRONTMOST_INTERVAL_S = 0.25
    #: How long after the open before a sighting counts. Two measured cases
    #: need it. On an iPhone 12 (iOS 26.5) Safari took the front 0.84-1.23s
    #: after WDA's `/url` returned, and a read in that gap still saw the app
    #: that was about to lose it -- an unclaimed path was reported as opened.
    #: On a Pixel 5 (Android 14) an activity that crashes on the link was in
    #: front for ~0.4s and force-finished at ~1.0s, and a read in that gap
    #: reported a crash as a success.
    _OPEN_URL_SETTLE_S = 2.0
    #: Bound on the reads taken before the open. They are side checks: a
    #: device slow to answer one must cost the check, never the open.
    _OPEN_URL_PREREAD_TIMEOUT_S = 3.0

    async def open_url(
        self, url: str, udid: str | None = None, bundle_id: str | None = None,
        direct: bool = False,
    ) -> tuple[str, dict]:
        """Open a URL the way a tapped link arrives, and say where it went.

        Returns the resolved udid and an outcome: `via` (simctl, wda or adb),
        `route` (system or direct), and with `bundle_id` whether that app
        ended up in front -- and on Android whether it crashed.

        The default is the system's own routing everywhere, because that is
        what a user's tap does and the only route that tests it: universal
        links checked against apple-app-site-association on iOS, App Links
        against assetlinks.json on Android. Measured on a Pixel 5 against a
        production build, it is also the only route that found a crash -- a
        `/dl/` path claimed by two activities went, as a tap, to one that
        cannot start, while package-addressed delivery showed a chooser.

        The route follows the kind of device, never the UI backend. A
        simulator goes through `simctl openurl` whether sim-bridge, idb or a
        WDA runner reads its screen -- as `launch_app` does. A physical
        iPhone goes through WDA's `/url` without a bundle id (see
        `WdaBackend.open_url`). Android sends the VIEW intent with no
        package, and for an http(s) link the BROWSABLE category a browser tap
        carries. Only for http(s): an intent with a category reaches only
        filters that declare it, and a `content:` URI's viewer, or an app's
        scheme registered without BROWSABLE, does not -- those were opened
        without it before and still are.

        `direct` is the opt-in for links the system will not route to the
        app -- a staging build's, which are not verified App Links. On
        Android it addresses the intent to `bundle_id`, as the app's Espresso
        tests do. iOS has no equivalent, and saying so beats quietly taking
        the system route for a caller who believes it bypassed verification.

        `bundle_id` otherwise delivers nothing: it names the app the link
        should open in, and quern reports whether it did.
        """
        resolved = await self.resolve_udid(udid)
        android = self._is_android(resolved)
        physical = not android and self._is_physical(resolved)
        if not android and not physical:
            self._require_simulator(resolved, "Open URL")
        if direct and not android:
            raise DeviceOperationUnsupportedError(
                "direct=true is Android-only: iOS has no way to hand a URL to an app "
                "that keeps universal-link routing, so every iOS open takes the system "
                "route. Drop direct to open it the way a tap does.",
                tool="wda" if physical else "simctl",
            )
        if direct and not bundle_id:
            raise DeviceOperationUnsupportedError(
                "direct=true delivers the intent to an app package, so it needs "
                "bundle_id -- the package to deliver to.",
                tool="adb",
            )
        kind = "android" if android else "device" if physical else "simulator"
        # Everything the check needs is read before the open: afterwards the
        # app in front may be the one leaving, and a crash window has to
        # start before the crash. None of it may stop the open itself.
        expected: str | None = None
        unknown: dict | None = None
        before: str | None = None
        crash_since: str | None = None
        crash_check_error: str | None = None
        if bundle_id:
            if kind == "simulator":
                try:
                    expected = await self.simctl.app_display_name(resolved, bundle_id)
                except Exception as exc:  # noqa: BLE001 - the check fails, not the open
                    unknown = {"opened_in_app": None,
                               "opened_in_app_error": f"could not read {bundle_id}'s "
                                                      f"display name: {exc}"}
                else:
                    if not expected:
                        unknown = {"opened_in_app": None,
                                   "opened_in_app_error": f"could not read {bundle_id}'s "
                                                          "display name on this simulator "
                                                          "-- is it installed?"}
            else:
                expected = bundle_id
            if expected:
                try:
                    before, _ = await asyncio.wait_for(
                        self._foreground_app(resolved, kind, expected),
                        self._OPEN_URL_PREREAD_TIMEOUT_S)
                except Exception:  # noqa: BLE001 - unknown just means "not known to be there"
                    before = None
            if android:
                try:
                    crash_since = await self.adb.device_time(resolved)
                except Exception as exc:  # noqa: BLE001 - reported as an unchecked crash
                    crash_check_error = f"could not read the device clock: {exc}"
        if android:
            web = url.lower().startswith(("http://", "https://"))
            await self.adb.open_url(resolved, url, package=bundle_id if direct else None,
                                    browsable=web and not direct)
            via = "adb"
        elif physical:
            await self.wda_client.open_url(resolved, url)
            via = "wda"
        else:
            await self.simctl.open_url(resolved, url)
            via = "simctl"
        self._invalidate_ui_cache(resolved)
        outcome: dict = {"via": via, "route": "direct" if direct else "system"}
        if unknown:
            outcome.update(unknown)
        elif expected:
            outcome.update(await self._url_landed_in(
                resolved, kind, expected, already_in_front=before == expected))
            if android:
                outcome.update(await self._crash_since_open(
                    resolved, bundle_id, crash_since, crash_check_error))
            if outcome.get("opened_in_app") is False:
                outcome["warning"] = self._landed_elsewhere(bundle_id, kind, direct, outcome)
        return resolved, outcome

    async def _crash_since_open(
        self, serial: str, package: str, since: str | None, error: str | None,
    ) -> dict:
        """Whether `package` crashed after the open, from the crash buffer.

        The screen cannot say: a crash leaves the home screen in front, or
        the app's own previous activity when it had a task, and that reads
        as the link having opened. `crashed` is True or False only when the
        buffer was read; otherwise it is None with `crash_check_error`."""
        if since is None:
            return {"crashed": None, "crash_check_error": error or "no crash window"}
        try:
            exception = await self.adb.crashed_since(serial, package, since)
        except Exception as exc:  # noqa: BLE001 - reported, not a verdict
            return {"crashed": None, "crash_check_error": str(exc) or type(exc).__name__}
        if exception is None:
            return {"crashed": False}
        # The app is not showing the link, whatever is in front.
        return {"crashed": True, "crash": exception, "opened_in_app": False}

    async def _url_landed_in(
        self, udid: str, kind: str, expected: str, *, already_in_front: bool,
    ) -> dict:
        """Watch the front after an open, and say whether `expected` ended up
        there.

        Nothing is read until `_OPEN_URL_SETTLE_S` has passed: before it the
        app may be about to lose the front to the link, or may have taken the
        link and be about to crash on it, so no earlier sighting could be
        believed. After it, the expected app in front is the answer.
        Something else in front is an answer at once only when the expected
        app was known to be in front before the open -- it was there and has
        gone; otherwise reads continue to the deadline, since a cold start
        may simply not have arrived yet. Each read is bounded by the time
        left, so the deadline holds against a slow read.

        `opened_in_app` is True or False only on the strength of a read that
        worked: when none did it is None, with the reason, so "could not
        tell" never reads as either answer. `foreground_app` is what was seen
        in front -- a bundle id or package on a device, the app's display
        name on a simulator, where only the accessibility tree is there to
        ask -- and on Android `foreground_activity` names the component too.
        """
        start = time.monotonic()
        deadline = start + max(self._OPEN_URL_FRONTMOST_TIMEOUT_S, self._OPEN_URL_SETTLE_S)
        await asyncio.sleep(self._OPEN_URL_SETTLE_S)
        seen: dict | None = None
        error = "no read was taken after the hand-over"
        while True:
            left = deadline - time.monotonic()
            try:
                app, activity = await asyncio.wait_for(
                    self._foreground_app(udid, kind, expected), max(left, 0.5))
            except Exception as exc:  # noqa: BLE001 - a failed read is reported, not a verdict
                error = str(exc) or type(exc).__name__
            else:
                seen = {"foreground_app": app}
                if activity:
                    seen["foreground_activity"] = activity
                if app == expected:
                    return {"opened_in_app": True, **seen}
                if already_in_front and app is not None:
                    # It was there and has gone: whatever is in front now
                    # took the link. Nothing in front at all is not that --
                    # Android reports no resumed activity mid-transition, so
                    # it waits for a read that names something (CodeRabbit).
                    return {"opened_in_app": False, **seen}
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(self._OPEN_URL_FRONTMOST_INTERVAL_S)
        if seen is None:
            return {"opened_in_app": None,
                    "opened_in_app_error": f"could not read the app in front: {error}"}
        return {"opened_in_app": False, **seen}

    async def _foreground_app(
        self, udid: str, kind: str, expected: str | None = None,
    ) -> tuple[str | None, str | None]:
        """What is in front, and on Android which activity: the resumed
        activity's package there, WDA's bundle id on an iPhone, the
        Application element's label on a simulator, through whichever
        backend reads it -- `expected` if any labelled Application element
        carries it, as `_is_frontmost` asks, else the first. Raises when it
        cannot be read."""
        if kind == "android":
            resumed = await self.adb.resumed_activity(udid)
            return resumed if resumed else (None, None)
        if kind == "device":
            return await self.wda_client.active_app(udid), None
        elements, _ = await self.get_ui_elements(
            udid, use_cache=False, filter_type="Application", probe_containers=False,
        )
        labels = [e.label for e in elements if e.label]
        if expected is not None and expected in labels:
            return expected, None
        return (labels[0] if labels else None), None

    @staticmethod
    def _landed_elsewhere(bundle_id: str, kind: str, direct: bool, outcome: dict) -> str:
        """Say where the link went instead, and what that usually means.
        On the response, not only in the log: the caller deciding what to do
        next is the one who needs it."""
        front = outcome.get("foreground_app") or "another app"
        activity = outcome.get("foreground_activity") or ""
        if outcome.get("crashed"):
            return (f"{bundle_id} crashed on the link: {outcome.get('crash')}. "
                    "get_latest_crash has the full report.")
        if activity.endswith("ResolverActivity"):
            return (f"The URL did not open in {bundle_id}: Android showed its app "
                    "chooser, because more than one activity claims this URL. A user "
                    "tapping the link sees the same chooser.")
        said = f"The URL did not open in {bundle_id}; {front} is in front."
        if kind != "android":
            return (said + " For an https link this usually means the app's associated "
                    "domains (apple-app-site-association) do not claim this path.")
        if direct:
            return (said + " The intent was delivered to the app, which handed the link "
                    "on.")
        return (said + " The link is not a verified App Link for this app; for a staging "
                "or other unverified link, pass direct=true.")

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
        """Clear all app data for an app. Returns the resolved udid.

        Follows the branch-on-Android-first pattern the rest of this class
        uses, rather than falling through to a simulator guard. Inverting that
        guard for #263 turned this from a cryptic simctl failure into a
        confident false claim -- "only supported on simulators" about a device
        whose `pm clear` answers `Success`.
        """
        resolved = await self.resolve_udid(udid)
        if self._is_android(resolved):
            await self.adb.clear_app_data(resolved, bundle_id)
            return resolved
        self._require_simulator(resolved, "Clear app data")
        try:
            await self.simctl.terminate_app(resolved, bundle_id)
        except DeviceError:
            pass  # app wasn't running, proceed anyway
        await self.simctl.clear_app_data(resolved, bundle_id)
        return resolved
