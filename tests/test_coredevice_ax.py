"""The CoreDevice accessibility-tree reader for physical iPhones (#664).

The fixtures are real pushes from an iPhone 11 on iOS 27.2.1, captured with
`clientNeedsAccessibility:` on `remoteAXService`: Settings' root screen and
Safari on example.com. The Wi-Fi network's name and another tab's title were
replaced with neutral text, inside the nested archives too, before they were
committed. What each test asserts was checked against WebDriverAgent's tree
for the same screen (the comparison is on #664).
"""

from __future__ import annotations

import asyncio
import plistlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.device.controller import DeviceController
from server.device.ios import coredevice_ax
from server.device.ios.coredevice_ax import (
    PUSH_SELECTOR,
    AxTreeError,
    CoreDeviceAxReader,
    _element_type,
    parse_push,
    read_tree,
)
from server.models import DeviceError, DeviceType

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "coredevice_ax"
SETTINGS = (FIXTURES / "settings_root.bin").read_bytes()
SAFARI = (FIXTURES / "safari_example.bin").read_bytes()

PHONE = "B34C4EE9-AF48-53C6-BD13-2BFA66E7EE91"


def _as(dump_type: str, data: bytes) -> bytes:
    """The same push relabelled as another dump type."""
    archive = plistlib.loads(data)
    objects = archive["$objects"]
    for i, o in enumerate(objects):
        if o == "AXPTreeDumpTypeAdditionalData":
            objects[i] = f"AXPTreeDumpType{dump_type}"
    return plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)


def _by_id(elements: list[dict], ident: str) -> dict:
    return next(e for e in elements if e["AXUniqueId"] == ident)


class TestParsingSettings:
    """Settings' root: rows, a switch, a header and a search field."""

    def test_it_is_the_complete_dump(self):
        dump_type, elements = parse_push(SETTINGS)
        assert dump_type == "AdditionalData"
        assert len(elements) == 14

    def test_the_application_comes_first_and_covers_the_screen(self):
        _, elements = parse_push(SETTINGS)
        app = elements[0]
        assert app["type"] == "Application"
        assert app["frame"] == {"x": 0.0, "y": 0.0, "width": 414.0, "height": 896.0}

    def test_identifiers_and_frames_match_what_wda_read(self):
        _, elements = parse_push(SETTINGS)
        wifi = _by_id(elements, "com.apple.settings.wifi")
        # WDA: {20, 420, 374x53} -- it rounds; the push is the exact point value.
        assert wifi["frame"] == {"x": 20.0, "y": 420.5, "width": 374.0, "height": 52.0}
        assert wifi["type"] == "Button"
        # A row's inner texts arrive merged into its label, as VoiceOver reads it.
        assert wifi["AXLabel"] == "Wi-Fi, ExampleNetwork"

    def test_the_airplane_mode_switch(self):
        _, elements = parse_push(SETTINGS)
        switch = _by_id(elements, "com.apple.settings.airplaneMode")
        # The accessibility tree's name for a UISwitch, as on a simulator;
        # WDA calls the same element `Switch` (a safe pair).
        assert switch["type"] == "CheckBox"
        assert switch["AXValue"] == "0"

    def test_the_title_is_a_heading(self):
        _, elements = parse_push(SETTINGS)
        assert [e["type"] for e in elements if e["AXLabel"] == "Settings"] == [
            "Application", "Heading"]

    def test_the_search_field(self):
        _, elements = parse_push(SETTINGS)
        field = next(e for e in elements if e["AXValue"] == "Search")
        assert field["type"] == "TextField"
        assert field["role_description"] == "AXSearchField"

    def test_a_disabled_row_reads_as_disabled(self):
        """Personal Hotspot, with no cellular plan, carries the not-enabled trait."""
        _, elements = parse_push(SETTINGS)
        assert _by_id(elements, "com.apple.settings.personalHotspot")["enabled"] is False
        assert _by_id(elements, "com.apple.settings.wifi")["enabled"] is True

    def test_it_parses_into_ui_elements(self):
        """The dicts are what every backend's describe_all returns."""
        from server.device.ui_elements import parse_elements

        _, raw = parse_push(SETTINGS)
        parsed = parse_elements(raw)
        assert [e.identifier for e in parsed if e.identifier][:3] == [
            "com.apple.settings.primaryAppleAccount",
            "com.apple.settings.followUpPane.com.apple.purplebuddy.revisitSkippedSteps",
            "com.apple.settings.airplaneMode",
        ]


class TestParsingWebContent:
    """Safari on example.com: the page's elements come from the web process,
    in the same push as Safari's own."""

    def test_page_text_and_its_link(self):
        _, elements = parse_push(SAFARI)
        link = next(e for e in elements if e["AXLabel"] == "Learn more")
        assert link["type"] == "Link"
        assert link["frame"] == {"x": 166.0, "y": 855.0, "width": 82.0, "height": 21.0}
        texts = [e for e in elements if e["type"] == "StaticText"]
        assert len(texts) == 6
        assert texts[0]["AXLabel"].startswith("This domain is for use in documentation")

    def test_the_chrome_keeps_its_identifiers(self):
        _, elements = parse_push(SAFARI)
        assert _by_id(elements, "ReloadButton")["type"] == "Button"
        # No history to go back to.
        assert _by_id(elements, "BackButton")["enabled"] is False

    def test_the_address_field(self):
        _, elements = parse_push(SAFARI)
        field = _by_id(elements, "TabBarItemTitle")
        assert field["type"] == "TextField"
        assert field["AXLabel"] == "Address"
        assert field["AXValue"].endswith("example.com")

    def test_custom_actions_are_named(self):
        _, elements = parse_push(SAFARI)
        assert "Show Reader View" in _by_id(elements, "TabBarItemTitle")["custom_actions"]


class TestTypes:
    """Only confirmed traits name a type; anything else stays generic."""

    @pytest.mark.parametrize(("traits", "expected"), [
        (1 << 0, "Button"),
        (1 << 1 | 1 << 0, "Link"),          # a link wins over its button bit
        (1 << 16 | 1 << 0, "Heading"),
        (1 << 53 | 1 << 0 | 1 << 6, "CheckBox"),
        (1 << 18 | 1 << 33, "TextField"),
        (1 << 10, "TextField"),
        (1 << 12, "Slider"),
        (1 << 2, "Image"),
        (1 << 6, "StaticText"),
        (1 << 33, "Group"),                 # a private bit alone names nothing
        (0, "Group"),
    ])
    def test_the_traits_decide(self, traits, expected):
        assert _element_type(traits, is_root=False) == expected

    def test_the_root_is_the_application_whatever_its_traits(self):
        assert _element_type(1 << 0, is_root=True) == "Application"


class TestBadPushes:
    """A push that is not a tree is an error, never an empty screen."""

    @pytest.mark.parametrize("data", [
        b"",
        b"not a plist at all",
        plistlib.dumps({"a": 1}, fmt=plistlib.FMT_BINARY),          # no archive
        plistlib.dumps({"$objects": ["$null"], "$top": {}}, fmt=plistlib.FMT_BINARY),
    ])
    def test_garbage_is_refused(self, data):
        with pytest.raises(AxTreeError):
            parse_push(data)

    def test_an_archive_without_a_tree_is_refused(self):
        archive = plistlib.loads(SETTINGS)
        objects = archive["$objects"]
        objects[objects.index("treeDump")] = "somethingElse"
        with pytest.raises(AxTreeError, match="no tree dump"):
            parse_push(plistlib.dumps(archive, fmt=plistlib.FMT_BINARY))

    def test_a_dangling_reference_is_refused_not_raised_raw(self):
        archive = plistlib.loads(SETTINGS)
        archive["$top"]["root"] = plistlib.UID(len(archive["$objects"]) + 5)
        with pytest.raises(AxTreeError):
            parse_push(plistlib.dumps(archive, fmt=plistlib.FMT_BINARY))


class TestDecoderLimits:
    @staticmethod
    def _archive(objects, root=1):
        return plistlib.dumps({"$archiver": "NSKeyedArchiver", "$version": 100000,
                               "$top": {"root": plistlib.UID(root)}, "$objects": objects},
                              fmt=plistlib.FMT_BINARY)

    def test_a_string_that_names_itself_is_refused_not_recursed(self):
        objects = ["$null",
                   {"$class": plistlib.UID(2), "NS.string": plistlib.UID(1)},
                   {"$classname": "NSString", "$classes": ["NSString", "NSObject"]}]
        with pytest.raises(AxTreeError):
            parse_push(self._archive(objects))

    def test_nesting_too_deep_is_refused_not_raised_raw(self):
        depth = 3000
        objects: list = ["$null"]
        for i in range(depth):
            nxt = [plistlib.UID(i + 2)] if i + 1 < depth else []
            objects.append({"$class": plistlib.UID(depth + 1), "NS.objects": nxt})
        objects.append({"$classname": "NSArray", "$classes": ["NSArray", "NSObject"]})
        with pytest.raises(AxTreeError, match="too deeply"):
            parse_push(self._archive(objects))

    def test_a_frame_in_exponent_notation(self):
        assert coredevice_ax._rect("{{1.5e-05, 2}, {3, 4E2}}") == {
            "x": 1.5e-05, "y": 2.0, "width": 3.0, "height": 400.0}


class TestWaitingForTheTree:
    """Which push a read settles on."""

    @staticmethod
    async def _run(*items):
        queue: asyncio.Queue = asyncio.Queue()
        for item in items:
            queue.put_nowait(item)
        return await asyncio.wait_for(coredevice_ax._newest_tree(queue), 5)

    async def test_it_takes_the_additional_data_not_the_initial_dump(self):
        dump_type, elements = await self._run(
            (PUSH_SELECTOR, [_as("InitialDump", SETTINGS)]),
            (PUSH_SELECTOR, [SETTINGS]),
        )
        assert dump_type == "AdditionalData"
        assert len(elements) == 14

    async def test_notifications_and_other_selectors_are_skipped(self):
        dump_type, _ = await self._run(
            (PUSH_SELECTOR, [plistlib.dumps({"$objects": [], "$top": {}},
                                            fmt=plistlib.FMT_BINARY)]),
            ("hostAPIVersion", [2]),
            (PUSH_SELECTOR, []),
            (PUSH_SELECTOR, [SETTINGS]),
        )
        assert dump_type == "AdditionalData"

    async def test_a_changing_screen_waits_for_its_newer_tree(self, monkeypatch):
        """Settings is complete, then Safari's InitialDump arrives and its
        AdditionalData only after the quiet period: the read waits for it,
        rather than answering with the screen that has already gone."""
        monkeypatch.setattr(coredevice_ax, "QUIET_S", 0.1)
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait((PUSH_SELECTOR, [SETTINGS]))
        queue.put_nowait((PUSH_SELECTOR, [_as("InitialDump", SAFARI)]))

        async def later():
            await asyncio.sleep(0.4)
            queue.put_nowait((PUSH_SELECTOR, [SAFARI]))

        task = asyncio.create_task(later())
        _, elements = await asyncio.wait_for(coredevice_ax._newest_tree(queue), 5)
        await task
        assert elements[0]["AXLabel"] == "Safari"

    async def test_a_change_that_never_completes_answers_the_last_tree(self, monkeypatch):
        """An InitialDump with no pair: once the settle window ends, the
        complete tree in hand is the answer, not a timeout."""
        monkeypatch.setattr(coredevice_ax, "SETTLE_MAX_S", 0.3)
        _, elements = await self._run(
            (PUSH_SELECTOR, [SETTINGS]),
            (PUSH_SELECTOR, [_as("InitialDump", SAFARI)]),
        )
        assert elements[0]["AXLabel"] == "Settings"

    async def test_a_screen_that_never_stops_changing_still_answers(self, monkeypatch):
        """A clock or a spinner: a new pair every 0.45 s, forever. Without a
        settle cap every read ran out its deadline with trees in hand."""
        monkeypatch.setattr(coredevice_ax, "SETTLE_MAX_S", 1.0)
        queue: asyncio.Queue = asyncio.Queue()

        async def churn():
            while True:
                queue.put_nowait((PUSH_SELECTOR, [_as("InitialDump", SETTINGS)]))
                queue.put_nowait((PUSH_SELECTOR, [SETTINGS]))
                await asyncio.sleep(0.45)

        task = asyncio.create_task(churn())
        try:
            dump_type, _ = await asyncio.wait_for(coredevice_ax._newest_tree(queue), 4)
        finally:
            task.cancel()
        assert dump_type == "AdditionalData"

    async def test_an_initial_dump_alone_never_completes(self):
        """No identifiers yet, so not an answer: the read's deadline ends it."""
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(self._run(
                (PUSH_SELECTOR, [_as("InitialDump", SETTINGS)])), 1.5)


class _FakeControl:
    def __init__(self, on_subscribe, fail_unsubscribe=False):
        self.calls: list[tuple] = []
        self._on_subscribe = on_subscribe
        self._fail_unsubscribe = fail_unsubscribe

    async def invoke(self, selector, *args, expects_reply=True):
        self.calls.append((selector, args))
        if args == (True,):
            self._on_subscribe()
        elif self._fail_unsubscribe:
            raise ConnectionResetError("gone")


def _fake_lib(pushes, *, connect_error=None, fail_unsubscribe=False):
    """A pymobiledevice3 stand-in: the subscribe call fills the queue."""
    state = SimpleNamespace(rsd_closed=False, provider_closed=False, control=None)

    class RSD:
        def __init__(self, address):
            state.address = address

        async def connect(self):
            if connect_error:
                raise connect_error

        async def close(self):
            state.rsd_closed = True

    class RemoteAX:
        def __init__(self, rsd):
            ctx: dict = {}
            self.dtx = SimpleNamespace(ctx=ctx)

            def deliver():
                for p in pushes:
                    ctx["control_dispatch_queue"].put_nowait((PUSH_SELECTOR, [p]))

            state.control = self.dtx._ctrl_channel = _FakeControl(
                deliver, fail_unsubscribe)

        async def connect(self):
            pass

        async def close(self):
            state.provider_closed = True

    return SimpleNamespace(RemoteServiceDiscoveryService=RSD, RemoteAX=RemoteAX), state


@pytest.fixture
def phone_address(monkeypatch):
    monkeypatch.setattr("server.device.performance._device_address",
                        AsyncMock(return_value=("fd00::1", 51000)))


class TestReadingADevice:
    async def test_a_read_returns_the_tree_and_closes_everything(
            self, monkeypatch, phone_address):
        lib, state = _fake_lib([_as("InitialDump", SETTINGS), SETTINGS])
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        elements = await read_tree(PHONE, timeout=5)

        assert len(elements) == 14
        assert state.address == ("fd00::1", 51000)
        assert state.control.calls == [("clientNeedsAccessibility:", (True,)),
                                       ("clientNeedsAccessibility:", (False,))]
        assert state.provider_closed and state.rsd_closed

    async def test_a_screen_that_never_completes_is_a_device_error(
            self, monkeypatch, phone_address):
        lib, state = _fake_lib([_as("InitialDump", SETTINGS)])
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        with pytest.raises(DeviceError, match="no complete accessibility tree") as caught:
            await read_tree(PHONE, timeout=0.5)

        assert caught.value.tool == "coredevice"
        assert state.provider_closed and state.rsd_closed

    async def test_a_refused_connection_is_a_device_error(self, monkeypatch, phone_address):
        lib, state = _fake_lib([], connect_error=ConnectionRefusedError(61, "refused"))
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        with pytest.raises(DeviceError, match="failed") as caught:
            await read_tree(PHONE, timeout=5)

        assert caught.value.tool == "coredevice"
        assert state.rsd_closed

    async def test_a_failed_unsubscribe_does_not_hide_a_good_read(
            self, monkeypatch, phone_address):
        lib, _ = _fake_lib([SETTINGS], fail_unsubscribe=True)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        assert len(await read_tree(PHONE, timeout=5)) == 14

    async def test_a_hung_unsubscribe_neither_holds_the_read_nor_leaks(
            self, monkeypatch, phone_address):
        lib, state = _fake_lib([SETTINGS])
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        monkeypatch.setattr(coredevice_ax, "CLOSE_S", 0.2)

        async def hang(selector, *args, expects_reply=True):
            if args == (True,):
                state.control._on_subscribe()
                return
            await asyncio.sleep(60)

        original = lib.RemoteAX.__init__

        def init(self, rsd):
            original(self, rsd)
            self.dtx._ctrl_channel.invoke = hang

        monkeypatch.setattr(lib.RemoteAX, "__init__", init)

        elements = await asyncio.wait_for(read_tree(PHONE, timeout=1.0), 3)

        assert len(elements) == 14  # a tree already read is not reported lost
        assert state.provider_closed and state.rsd_closed

    async def test_a_slow_address_lookup_is_inside_the_deadline(self, monkeypatch):
        async def slow(uuid):
            await asyncio.sleep(5)

        monkeypatch.setattr("server.device.performance._device_address", slow)
        lib, _ = _fake_lib([SETTINGS])
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        with pytest.raises(DeviceError, match="within 0.3s"):
            await asyncio.wait_for(read_tree(PHONE, timeout=0.3), 2)

    async def test_any_failure_finding_the_device_is_a_device_error(self, monkeypatch):
        monkeypatch.setattr("server.device.performance._device_address",
                            AsyncMock(side_effect=OSError("devicectl vanished")))
        lib, _ = _fake_lib([SETTINGS])
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        with pytest.raises(DeviceError) as caught:
            await read_tree(PHONE, timeout=5)
        assert caught.value.tool == "coredevice"

    async def test_source_timeout_bounds_the_read(self, monkeypatch):
        seen = {}

        async def fake_read(uuid, *, timeout):
            seen["timeout"] = timeout
            return []

        monkeypatch.setattr(coredevice_ax, "read_tree", fake_read)
        await CoreDeviceAxReader().describe_all(PHONE, source_timeout=7)
        assert seen == {"timeout": 7}

    async def test_no_tunnel_is_a_device_error_before_anything_opens(self, monkeypatch):
        monkeypatch.setattr(
            "server.device.performance._device_address",
            AsyncMock(side_effect=DeviceError("no tunnel to B34C4EE9", tool="pymobiledevice3")))
        lib, state = _fake_lib([SETTINGS])
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        with pytest.raises(DeviceError, match="no tunnel"):
            await read_tree(PHONE, timeout=5)
        assert state.control is None


def _phone_controller(os_version: str | None = "iOS 27.2.1", *, reader: str = "coredevice",
                      monkeypatch) -> DeviceController:
    ctrl = DeviceController()
    ctrl._active_udid = PHONE
    ctrl._device_type_cache[PHONE] = DeviceType.DEVICE
    if os_version:
        ctrl.wda_client._device_os_versions[PHONE] = os_version
    monkeypatch.setattr("server.device.controller_ui.get_device_ui_reader", lambda: reader)
    ctrl.coredevice_ax.describe_all = AsyncMock(return_value=parse_push(SETTINGS)[1])
    ctrl.wda_client.describe_all = AsyncMock(return_value=[{
        "type": "Application", "AXLabel": "from WDA", "AXUniqueId": None,
        "frame": {"x": 0, "y": 0, "width": 414, "height": 896}}])
    ctrl.wda_client.find_elements_by_query = AsyncMock(return_value=[])
    ctrl._mark_on_screen = AsyncMock()
    return ctrl


class TestWhenTheReaderServes:
    def test_opted_in_on_ios_27(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        assert ctrl._device_tree_reader(PHONE) is ctrl.coredevice_ax

    @pytest.mark.parametrize(("os_version", "reader"), [
        ("iOS 27.2.1", "wda"),     # not opted in
        ("iOS 26.7", "coredevice"),  # below where it was measured
        (None, "coredevice"),      # version never seen
        ("unknown", "coredevice"),
    ])
    def test_otherwise_wda(self, monkeypatch, os_version, reader):
        ctrl = _phone_controller(os_version, reader=reader, monkeypatch=monkeypatch)
        assert ctrl._device_tree_reader(PHONE) is None

    def test_never_a_simulator_or_android(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl._device_type_cache[PHONE] = DeviceType.SIMULATOR
        assert ctrl._device_tree_reader(PHONE) is None
        ctrl._is_android = lambda udid: True
        ctrl._device_type_cache[PHONE] = DeviceType.DEVICE
        assert ctrl._device_tree_reader(PHONE) is None


class TestReadsThroughTheController:
    async def test_a_read_uses_the_push_and_names_it(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)

        elements, _ = await ctrl.get_ui_elements(PHONE, use_cache=False)

        assert len(elements) == 14
        ctrl.wda_client.describe_all.assert_not_called()
        assert ctrl.backend_that_served(PHONE) == CoreDeviceAxReader.TOOL_NAME
        # Marking asks WDA for the viewport, which would start it for nothing.
        ctrl._mark_on_screen.assert_not_called()

    async def test_an_element_lookup_stays_on_wda(self, monkeypatch):
        """Whole-screen reads only: a row's merged label ("Wi-Fi, Home") would
        match differently from WDA's lookup, so lookups are WDA's as before."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)

        await ctrl.get_ui_elements(PHONE, filter_identifier="com.apple.settings.general")

        ctrl.wda_client.find_elements_by_query.assert_awaited()
        ctrl.coredevice_ax.describe_all.assert_not_called()

    async def test_a_read_with_a_wda_depth_stays_on_wda(self, monkeypatch):
        """Every lookup made for a tool passes a depth; so does a caller who
        asked WDA for one."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)

        await ctrl.get_ui_elements(PHONE, use_cache=False, snapshot_depth=12)

        ctrl.wda_client.describe_all.assert_awaited()
        ctrl.coredevice_ax.describe_all.assert_not_called()

    async def test_a_cached_push_tree_never_answers_a_lookup(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        await ctrl.get_ui_elements(PHONE)            # whole screen, cached

        await ctrl.get_ui_elements(PHONE, filter_label="General")

        ctrl.wda_client.find_elements_by_query.assert_awaited()

    async def test_a_cache_hit_names_the_backend_that_made_the_tree(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        await ctrl.get_ui_elements(PHONE)
        ctrl._last_read_backend[PHONE] = ctrl.wda_client.TOOL_NAME  # a WDA read since

        await ctrl.get_ui_elements(PHONE)                           # served from cache

        assert ctrl.coredevice_ax.describe_all.await_count == 1
        assert ctrl.backend_that_served(PHONE) == CoreDeviceAxReader.TOOL_NAME
        ctrl._mark_on_screen.assert_not_called()

    async def test_a_failed_push_read_falls_back_to_wda_and_says_so(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=DeviceError("refused", tool="coredevice"))

        elements, _ = await ctrl.get_ui_elements(PHONE, use_cache=False)

        assert [e.label for e in elements] == ["from WDA"]
        assert ctrl.backend_that_served(PHONE) == ctrl.wda_client.TOOL_NAME
        ctrl._mark_on_screen.assert_awaited()

    async def test_not_opted_in_is_unchanged(self, monkeypatch):
        ctrl = _phone_controller(reader="wda", monkeypatch=monkeypatch)

        elements, _ = await ctrl.get_ui_elements(PHONE, use_cache=False)

        assert [e.label for e in elements] == ["from WDA"]
        ctrl.coredevice_ax.describe_all.assert_not_called()


class TestTheSetting:
    @pytest.mark.parametrize(("raw", "expected"), [
        ({"device_ui_reader": "coredevice"}, "coredevice"),
        ({"device_ui_reader": "wda"}, "wda"),
        ({"device_ui_reader": "CoreDevice"}, "wda"),   # a typo is not consent
        ({"device_ui_reader": True}, "wda"),
        ({}, "wda"),
    ])
    def test_only_the_literal_opts_in(self, monkeypatch, raw, expected):
        from server import config

        monkeypatch.setattr(config, "read_user_config", lambda **kw: raw)
        assert config.get_device_ui_reader() == expected
