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
import time
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
from server.models import DeviceError, DeviceType, WdaNotSetUpError

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "coredevice_ax"
SETTINGS = (FIXTURES / "settings_root.bin").read_bytes()
SAFARI = (FIXTURES / "safari_example.bin").read_bytes()
#: The probe app's Text tab, with its tab bar.
PROBE_TEXT = (FIXTURES / "probe_text_fields.bin").read_bytes()
#: The probe app's Lazy screen: a UICollectionView list, its rows with no
#: traits, from the SwiftUI-hosted build (#664).
PROBE_LAZY = (FIXTURES / "probe_lazy.bin").read_bytes()

PHONE = "B34C4EE9-AF48-53C6-BD13-2BFA66E7EE91"


def _as(dump_type: str, data: bytes) -> bytes:
    """The same push relabelled as another dump type."""
    archive = plistlib.loads(data)
    objects = archive["$objects"]
    for i, o in enumerate(objects):
        if o == "AXPTreeDumpTypeAdditionalData":
            objects[i] = f"AXPTreeDumpType{dump_type}"
    return plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)


def _relabelled(data: bytes, old: str, new: str) -> bytes:
    """The same push with one string replaced: a distinguishable stage."""
    archive = plistlib.loads(data)
    archive["$objects"] = [new if o == old else o for o in archive["$objects"]]
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


class TestTheSelectedTab:
    """A tab-bar item's value is a number in the push: 1 in front, 0 for the
    others. Read as text only, it was dropped, so every landmark with
    `selected: true` -- every tab landmark -- missed (#664)."""

    def test_the_tab_in_front_and_only_it(self):
        _, elements = parse_push(PROBE_TEXT)
        tabs = {e["AXUniqueId"]: e["AXValue"] for e in elements
                if (e["AXUniqueId"] or "").startswith("tab_")}
        assert tabs == {"tab_text": "1", "tab_controls": "0",
                        "tab_scroll": "0", "tab_links": "0"}

    @pytest.mark.parametrize(("value", "expected"), [
        (1, "1"), (0, "0"), (True, "1"), (False, "0"), (42, "42"),
        ("40%", "40%"), (None, None),
        (50.0, None),           # a slider's: WDA says "50%", so not guessed at
    ])
    def test_numbers_read_as_text(self, value, expected):
        assert coredevice_ax._value(value) == expected

    def test_a_tab_landmark_matches_the_pushed_tree(self):
        """The probe knowledge base's own tab landmark, recorded through WDA."""
        from server.device.ui_elements import parse_elements
        from server.knowledge.landmarks import match_landmark
        from server.models import Landmark

        _, raw = parse_push(PROBE_TEXT)
        elements = parse_elements(raw)
        assert match_landmark(elements, Landmark(
            element="RadioButton", identifier="tab_text", selected=True))
        assert not match_landmark(elements, Landmark(
            element="RadioButton", identifier="tab_links", selected=True))


class TestACollectionViewRow:
    """A list row with no traits reads Group in the push; WDA calls it Cell and
    a simulator StaticText, so a row landmark recorded on either matched
    nothing there (#664)."""

    def test_it_reads_as_a_group(self):
        _, elements = parse_push(PROBE_LAZY)
        row = _by_id(elements, "lazy_row_0")
        assert (row["type"], row["AXLabel"]) == ("Group", "Lazy row 0")

    @pytest.mark.parametrize("recorded", ["Cell", "StaticText", "Button"])
    def test_a_row_landmark_from_any_reader_matches_it(self, recorded):
        from server.device.ui_elements import parse_elements
        from server.knowledge.landmarks import match_landmark
        from server.models import Landmark

        _, raw = parse_push(PROBE_LAZY)
        assert match_landmark(parse_elements(raw),
                              Landmark(element=recorded, identifier="lazy_row_0"))

    def test_but_not_by_its_label(self):
        from server.device.ui_elements import parse_elements
        from server.knowledge.landmarks import match_landmark
        from server.models import Landmark

        _, raw = parse_push(PROBE_LAZY)
        assert not match_landmark(parse_elements(raw),
                                  Landmark(element="Cell", label="Lazy row 0"))


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
        tree, _, _ = await asyncio.wait_for(coredevice_ax._newest_tree(queue), 5)
        return tree.dump_type, tree.elements

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
        tree, _, _ = await asyncio.wait_for(coredevice_ax._newest_tree(queue), 5)
        elements = tree.elements
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
            tree, _, _ = await asyncio.wait_for(coredevice_ax._newest_tree(queue), 4)
            dump_type = tree.dump_type
        finally:
            task.cancel()
        assert dump_type == "AdditionalData"

    @staticmethod
    async def _pair_completing_at(delay: float):
        """Settings complete; Safari's InitialDump at once and its
        AdditionalData `delay` seconds later. Returns the label of the answer."""
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait((PUSH_SELECTOR, [SETTINGS]))
        queue.put_nowait((PUSH_SELECTOR, [_as("InitialDump", SAFARI)]))

        async def later():
            await asyncio.sleep(delay)
            queue.put_nowait((PUSH_SELECTOR, [SAFARI]))

        task = asyncio.create_task(later())
        try:
            tree, _, _ = await asyncio.wait_for(coredevice_ax._newest_tree(queue), 5)
        finally:
            task.cancel()
        return tree.elements[0]["AXLabel"]

    @pytest.fixture
    def short_windows(self, monkeypatch):
        """Settle windows far shorter than the pair below takes, so only the
        pair's own budget can see it through."""
        monkeypatch.setattr(coredevice_ax, "SETTLE_MAX_S", 0.2)
        monkeypatch.setattr(coredevice_ax, "UNSETTLED_QUIET_S", 0.2)
        monkeypatch.setattr(coredevice_ax, "QUIET_S", 0.1)

    async def test_a_pair_under_way_finishes_past_the_settle_window(self, short_windows,
                                                                    monkeypatch):
        """Measured after a tap into Settings > Battery (#676): the newer pair
        completed after the settle window, and the read answered with the
        page still loading."""
        monkeypatch.setattr(coredevice_ax, "PAIR_BASE_S", 0.6)
        monkeypatch.setattr(coredevice_ax, "PAIR_PER_ELEMENT_S", 0.0)
        assert await self._pair_completing_at(0.45) == "Safari"

    async def test_the_pairs_budget_grows_with_its_tree(self, short_windows, monkeypatch):
        """Safari's InitialDump carries 13 elements: at 30 ms each its pair
        has 0.49 s, past the 0.45 s it takes; with none, 0.1 s, short of it."""
        monkeypatch.setattr(coredevice_ax, "PAIR_BASE_S", 0.1)
        monkeypatch.setattr(coredevice_ax, "PAIR_PER_ELEMENT_S", 0.03)
        assert await self._pair_completing_at(0.45) == "Safari"
        monkeypatch.setattr(coredevice_ax, "PAIR_PER_ELEMENT_S", 0.0)
        assert await self._pair_completing_at(0.45) == "Settings"

    async def test_a_small_pair_keeps_the_settle_windows_time(self, monkeypatch):
        """A pair budget shorter than the settle window left does not cut the
        wait the window already gave."""
        monkeypatch.setattr(coredevice_ax, "SETTLE_MAX_S", 1.0)
        monkeypatch.setattr(coredevice_ax, "UNSETTLED_QUIET_S", 1.0)
        monkeypatch.setattr(coredevice_ax, "PAIR_BASE_S", 0.1)
        monkeypatch.setattr(coredevice_ax, "PAIR_PER_ELEMENT_S", 0.0)
        assert await self._pair_completing_at(0.5) == "Safari"

    async def test_a_pair_slower_than_its_budget_is_given_up(self, short_windows,
                                                             monkeypatch):
        monkeypatch.setattr(coredevice_ax, "PAIR_BASE_S", 0.3)
        monkeypatch.setattr(coredevice_ax, "PAIR_PER_ELEMENT_S", 0.0)
        assert await self._pair_completing_at(0.8) == "Settings"

    async def test_pairs_that_never_finish_still_answer_by_the_hard_cap(self, short_windows,
                                                                       monkeypatch):
        """A new InitialDump every 0.25 s, never its pair: each one renews the
        pair budget, so only SETTLE_HARD_S ends the read."""
        monkeypatch.setattr(coredevice_ax, "PAIR_BASE_S", 0.5)
        monkeypatch.setattr(coredevice_ax, "PAIR_PER_ELEMENT_S", 0.0)
        monkeypatch.setattr(coredevice_ax, "SETTLE_HARD_S", 1.0)
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait((PUSH_SELECTOR, [SETTINGS]))

        async def churn():
            while True:
                queue.put_nowait((PUSH_SELECTOR, [_as("InitialDump", SAFARI)]))
                await asyncio.sleep(0.25)

        task = asyncio.create_task(churn())
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            tree, _, _ = await asyncio.wait_for(coredevice_ax._newest_tree(queue), 4)
        finally:
            task.cancel()
        assert tree.elements[0]["AXLabel"] == "Settings"
        assert 0.9 <= loop.time() - started <= 1.6

    def test_the_budget_is_sized_by_the_dump_that_started_the_pair(self):
        """Not the tree in hand: the newer screen's size is what it costs."""
        small, big = coredevice_ax.parse_tree(SAFARI), coredevice_ax.parse_tree(SETTINGS)
        small = coredevice_ax.Tree("InitialDump", small.elements[:2], small.app_pid)
        assert coredevice_ax._pair_budget(small) == pytest.approx(
            coredevice_ax.PAIR_BASE_S + 2 * coredevice_ax.PAIR_PER_ELEMENT_S)
        assert coredevice_ax._pair_budget(big) > coredevice_ax._pair_budget(small)

    async def test_a_page_loading_in_stages_is_waited_for_stage_by_stage(
            self, short_windows, monkeypatch):
        """Three stages, the second completing after the settle window: the
        read answers with the third, not the stage between (#677 review).
        Each newer InitialDump lands well inside the quiet spell before it."""
        monkeypatch.setattr(coredevice_ax, "PAIR_BASE_S", 0.5)
        monkeypatch.setattr(coredevice_ax, "PAIR_PER_ELEMENT_S", 0.0)
        stage3 = _relabelled(SETTINGS, "Settings", "Stage three")
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait((PUSH_SELECTOR, [SETTINGS]))                      # stage 1, at 0

        async def stages():
            await asyncio.sleep(0.03)
            queue.put_nowait((PUSH_SELECTOR, [_as("InitialDump", SAFARI)]))
            await asyncio.sleep(0.27)
            queue.put_nowait((PUSH_SELECTOR, [SAFARI]))                    # stage 2, at 0.3
            await asyncio.sleep(0.03)
            queue.put_nowait((PUSH_SELECTOR, [_as("InitialDump", stage3)]))
            await asyncio.sleep(0.27)
            queue.put_nowait((PUSH_SELECTOR, [stage3]))                    # stage 3, at 0.6

        task = asyncio.create_task(stages())
        try:
            tree, _, _ = await asyncio.wait_for(coredevice_ax._newest_tree(queue), 5)
        finally:
            task.cancel()
        assert tree.elements[0]["AXLabel"] == "Stage three"

    async def test_the_hard_cap_counts_from_the_first_complete_tree(self, short_windows,
                                                                    monkeypatch):
        """A later complete tree does not restart it: SETTLE_HARD_S after the
        first, whatever follows (#677 review)."""
        # Spread wide, and churned slowly: at a 30 ms churn and a 0.4 s gap
        # between the two outcomes, a loaded CI runner took 2.54 s once (#695).
        monkeypatch.setattr(coredevice_ax, "QUIET_S", 0.4)       # longer than the churn's gap
        monkeypatch.setattr(coredevice_ax, "PAIR_BASE_S", 1.2)   # outlasts the 1.0 s gap
        monkeypatch.setattr(coredevice_ax, "PAIR_PER_ELEMENT_S", 0.0)
        monkeypatch.setattr(coredevice_ax, "SETTLE_HARD_S", 2.0)
        queue: asyncio.Queue = asyncio.Queue()
        queue.put_nowait((PUSH_SELECTOR, [SETTINGS]))

        async def churn():
            await asyncio.sleep(0.05)
            queue.put_nowait((PUSH_SELECTOR, [_as("InitialDump", SAFARI)]))
            await asyncio.sleep(0.95)
            # A second complete tree, at 1.0 s.
            queue.put_nowait((PUSH_SELECTOR, [SAFARI]))
            while True:
                await asyncio.sleep(0.15)
                queue.put_nowait((PUSH_SELECTOR, [_as("InitialDump", SETTINGS)]))

        task = asyncio.create_task(churn())
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            await asyncio.wait_for(coredevice_ax._newest_tree(queue), 6)
        finally:
            task.cancel()
        elapsed = loop.time() - started
        assert 1.9 <= elapsed < 2.7, elapsed     # renewed at 1.0 s it would run to 3.0

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

    class AppService:
        def __init__(self, rsd):
            pass

        async def __aenter__(self):
            if state.processes is None:
                raise ConnectionRefusedError(61, "refused")
            return self

        async def __aexit__(self, *a):
            return False

        async def list_processes(self):
            return [{"processIdentifier": pid} for pid in state.processes]

    state.processes = [617, 618]   # Settings and Safari, as captured
    return (SimpleNamespace(RemoteServiceDiscoveryService=RSD, RemoteAX=RemoteAX,
                            AppService=AppService), state)


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

    async def test_a_deadline_with_a_tree_in_hand_answers_with_it(
            self, monkeypatch, phone_address):
        """A short source_timeout ending the wait for a newer pair is not "no
        complete tree": one was in hand (#677 review)."""
        monkeypatch.setattr(coredevice_ax, "PAIR_BASE_S", 30.0)    # the pair outlasts the deadline
        lib, _ = _fake_lib([SETTINGS, _as("InitialDump", SAFARI)])
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        elements = await asyncio.wait_for(read_tree(PHONE, timeout=0.5), 5)

        assert elements[0]["AXLabel"] == "Settings"

    async def test_a_screen_that_never_completes_is_a_device_error(
            self, monkeypatch, phone_address):
        lib, state = _fake_lib([_as("InitialDump", SETTINGS)])
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        with pytest.raises(DeviceError, match="no complete accessibility tree") as caught:
            # Bounded on its own: if read_tree stopped enforcing its deadline,
            # this must fail rather than wait forever.
            await asyncio.wait_for(read_tree(PHONE, timeout=0.5), 5)

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

        async def fake_read(uuid, *, timeout, **_):
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


class TestAnAppThatDiesUnderTheRead:
    """#669: on iOS 27 a push read crashes some apps. The read must say so,
    not hand back the dying app's identifier-less tree."""

    def test_each_tree_names_its_app(self):
        assert coredevice_ax.parse_tree(SETTINGS).app_pid == 617
        assert coredevice_ax.parse_tree(SAFARI).app_pid == 618

    async def test_an_app_that_died_is_reported_by_name(self, monkeypatch, phone_address):
        lib, state = _fake_lib([_as("InitialDump", SETTINGS), SETTINGS,
                                _as("InitialDump", SAFARI), SAFARI])
        state.processes = [618]                       # Settings is gone
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        with pytest.raises(coredevice_ax.AppExitedDuringRead) as caught:
            await asyncio.wait_for(read_tree(PHONE, timeout=5), 10)

        assert (caught.value.app, caught.value.pid) == ("Settings", 617)
        assert "#669" in str(caught.value)
        assert state.provider_closed and state.rsd_closed

    async def test_a_death_seen_only_as_the_next_apps_first_dump(self, monkeypatch,
                                                                 phone_address):
        """As measured: the dying app's last complete tree, then SpringBoard's
        InitialDump, with SpringBoard's complete tree arriving after the wait
        has ended. The read returned the dying app's tree, with no warning."""
        lib, state = _fake_lib([_as("InitialDump", SETTINGS), SETTINGS,
                                _as("InitialDump", SAFARI)])
        state.processes = [618]
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        monkeypatch.setattr(coredevice_ax, "SETTLE_MAX_S", 0.3)
        monkeypatch.setattr(coredevice_ax, "UNSETTLED_QUIET_S", 0.3)

        with pytest.raises(coredevice_ax.AppExitedDuringRead):
            await asyncio.wait_for(read_tree(PHONE, timeout=5), 10)

    async def test_an_app_switch_is_not_a_crash(self, monkeypatch, phone_address):
        lib, state = _fake_lib([SETTINGS, _as("InitialDump", SAFARI), SAFARI])
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        elements = await asyncio.wait_for(read_tree(PHONE, timeout=5), 10)

        assert elements[0]["AXLabel"] == "Safari"

    async def test_a_process_list_it_cannot_read_is_not_taken_as_a_death(
            self, monkeypatch, phone_address):
        lib, state = _fake_lib([SETTINGS, _as("InitialDump", SAFARI), SAFARI])
        state.processes = None
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        elements = await asyncio.wait_for(read_tree(PHONE, timeout=5), 10)

        assert elements[0]["AXLabel"] == "Safari"

    async def test_notifications_lengthen_the_quiet_spell(self, monkeypatch):
        """The crash replaces the app about a second after its last tree: the
        default quiet spell ended first and handed back the dying app's tree."""
        monkeypatch.setattr(coredevice_ax, "QUIET_S", 0.1)
        # Wide enough that a loaded CI runner's late `later()` still lands
        # inside it: at 0.6 s it missed once on GitHub's macOS runners. Still
        # a test: without the note, the 0.1 s spell ends before Safari arrives.
        monkeypatch.setattr(coredevice_ax, "UNSETTLED_QUIET_S", 2.0)
        note = plistlib.dumps({"$archiver": "NSKeyedArchiver", "$version": 100000,
                               "$top": {"root": plistlib.UID(1)},
                               "$objects": ["$null", {"$class": plistlib.UID(2), "notification": 7},
                                            {"$classname": "AXPTranslatorResponse"}]},
                              fmt=plistlib.FMT_BINARY)
        queue: asyncio.Queue = asyncio.Queue()
        for push in (_as("InitialDump", SETTINGS), note, SETTINGS):
            queue.put_nowait((PUSH_SELECTOR, [push]))

        async def later():
            await asyncio.sleep(0.3)
            queue.put_nowait((PUSH_SELECTOR, [_as("InitialDump", SAFARI)]))
            queue.put_nowait((PUSH_SELECTOR, [SAFARI]))

        task = asyncio.create_task(later())
        tree, first, _ = await asyncio.wait_for(coredevice_ax._newest_tree(queue), 5)
        await task
        assert (first.app_pid, tree.app_pid) == (617, 618)


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


class TestACrashAfterTheRead:
    """#669's crash can land after the read has answered: the whole tree at
    4.0 s, the app gone at 4.3 s (iPhone 11). The next read tells, from the
    process list and the phone's crash report for that pid."""

    REPORT_669 = ('{"app_name":"QuernProbeScene"}\n'
                  '{"pid": 100, "frames": "-[UIView _accessibilityWindowSections]"}')
    REPORT_OTHER = '{"app_name":"QuernProbeScene"}\n{"pid": 100, "frames": "abort()"}'

    def _reader(self, monkeypatch, reads, reports=None, *, read_s=0.0):
        """A reader whose reads see `reads` in turn: (pid, app, alive), alive
        being the answer for every earlier pid asked about."""
        calls = []

        async def fake_read(udid, *, timeout, previous_pids=(), seen=None):
            calls.append(previous_pids)
            if read_s:
                await asyncio.sleep(read_s)
            pid, app, alive = reads[len(calls) - 1]
            seen.update(pid=pid, app=app)
            others = [p for p in previous_pids if p != pid]
            if others:
                seen["alive"] = dict.fromkeys(others, alive)
            return [{"type": "Application", "AXLabel": app}]
        monkeypatch.setattr(coredevice_ax, "read_tree", fake_read)
        monkeypatch.setattr(coredevice_ax, "CRASH_REPORT_WAIT_S", 0.0)
        reader = CoreDeviceAxReader()
        if reports is not None:
            reader.crash_reports = AsyncMock(return_value=reports)
        return reader, calls

    async def test_the_next_read_names_it(self, monkeypatch):
        reader, calls = self._reader(
            monkeypatch, [(100, "QuernProbeScene", None), (37, "SpringBoard", False)],
            reports=[(100, self.REPORT_669)])

        await reader.describe_all(PHONE)
        with pytest.raises(coredevice_ax.AppExitedAfterRead) as caught:
            await reader.describe_all(PHONE)

        assert (caught.value.app, caught.value.pid) == ("QuernProbeScene", 100)
        assert isinstance(caught.value, coredevice_ax.AppExitedDuringRead)  # every guard
        assert "crashed after a read" in str(caught.value)
        assert calls == [(), (100,)]
        # Asked for the app's own reports first.
        assert reader.crash_reports.await_args.args[2] == "QuernProbeScene"

    @pytest.mark.parametrize(("reads", "reports"), [
        # Switched away, still running.
        ([(100, "QuernProbeScene", None), (37, "SpringBoard", True)], [(100, REPORT_669)]),
        # Gone, but the phone could not be asked.
        ([(100, "QuernProbeScene", None), (37, "SpringBoard", None)], [(100, REPORT_669)]),
        # Relaunched: the same app under a new pid.
        ([(100, "QuernProbeScene", None), (101, "QuernProbeScene", False)], [(100, REPORT_669)]),
        # Gone with a crash of its own, not #669's.
        ([(100, "QuernProbeScene", None), (37, "SpringBoard", False)], [(100, REPORT_OTHER)]),
        # Gone, and no report for that pid.
        ([(100, "QuernProbeScene", None), (37, "SpringBoard", False)], [(99, REPORT_669)]),
        ([(100, "QuernProbeScene", None), (37, "SpringBoard", False)], []),
    ])
    async def test_anything_else_is_not_claimed(self, monkeypatch, reads, reports):
        reader, _ = self._reader(monkeypatch, reads, reports=reports)

        await reader.describe_all(PHONE)
        await reader.describe_all(PHONE)

        # Reports are read only for an app that is gone and not relaunched.
        dead_elsewhere = reads[1][2] is False and reads[1][1] != reads[0][1]
        assert reader.crash_reports.await_count == (1 if dead_elsewhere else 0)

    async def test_could_not_tell_is_asked_again(self, monkeypatch):
        """Not knowing whether the earlier app lives keeps it, so the question
        is not lost (review)."""
        reader, calls = self._reader(
            monkeypatch, [(100, "QuernProbeScene", None), (37, "SpringBoard", None),
                          (37, "SpringBoard", False)],
            reports=[(100, self.REPORT_669)])

        await reader.describe_all(PHONE)
        await reader.describe_all(PHONE)
        with pytest.raises(coredevice_ax.AppExitedAfterRead):
            await reader.describe_all(PHONE)

        assert calls == [(), (100,), (37, 100)]

    async def test_but_not_for_ever(self, monkeypatch):
        """A phone that never answers stops being asked after a few reads."""
        reads = [(100, "QuernProbeScene", None)] + [(37, "SpringBoard", None)] * 5
        reader, calls = self._reader(monkeypatch, reads, reports=[])

        for _ in reads:
            await reader.describe_all(PHONE)

        assert [100 in c for c in calls] == [False, True, True, True, False, False]

    async def test_a_deadline_before_the_question_keeps_it_too(self, monkeypatch):
        reader, calls = self._reader(
            monkeypatch, [(100, "QuernProbeScene", None)], reports=[])

        async def answered_at_the_deadline(udid, *, timeout, previous_pids=(), seen=None):
            calls.append(previous_pids)
            seen.update(pid=37, app="SpringBoard")       # and nothing about 100
            return [{"type": "Application", "AXLabel": "SpringBoard"}]

        await reader.describe_all(PHONE)
        monkeypatch.setattr(coredevice_ax, "read_tree", answered_at_the_deadline)
        await reader.describe_all(PHONE)

        assert [e.pid for e in reader._front[PHONE]] == [37, 100]

    async def test_a_launch_check_does_not_erase_an_earlier_question(self, monkeypatch):
        """Read A; A crashes after the read; launch_app's check sees B. The
        next read still asks about A (review)."""
        reader, calls = self._reader(
            monkeypatch, [(100, "QuernProbeScene", None), (1583, "Settings", False)],
            reports=[(100, self.REPORT_669)])

        await reader.describe_all(PHONE)
        reader.note_front(PHONE, 1583, "Settings", time.time())
        with pytest.raises(coredevice_ax.AppExitedAfterRead):
            await reader.describe_all(PHONE)

        assert calls[1] == (1583, 100)

    def _reads(self, monkeypatch, reader, pid, app, alive, *, during=None, unasked=None):
        """One read seeing `pid`/`app`, answering `alive` per earlier pid."""
        async def read(udid, *, timeout, previous_pids=(), seen=None):
            if during:
                during()
            seen.update(pid=pid, app=app)
            seen["alive"] = {p: alive.get(p) for p in previous_pids if p != pid}
            if unasked:
                seen["unasked"] = unasked
            return []
        monkeypatch.setattr(coredevice_ax, "read_tree", read)

    async def test_an_app_known_to_be_running_is_let_go(self, monkeypatch):
        """Only an unknown one is kept: a running one would make every read
        after an app switch pay a process listing (review)."""
        reader = CoreDeviceAxReader()
        reader.note_front(PHONE, 200, "B", 0.0)
        reader.note_front(PHONE, 100, "A", 0.0)
        self._reads(monkeypatch, reader, 37, "SpringBoard", {100: True, 200: None})

        await reader.describe_all(PHONE)

        assert [(e.pid, e.unknown) for e in reader._front[PHONE]] == [(37, 0), (200, 1)]

    async def test_the_front_then_the_noted_then_the_unknown(self, monkeypatch):
        """The order decides which the cap lets go: the oldest question."""
        reader = CoreDeviceAxReader()
        reader.note_front(PHONE, 200, "B", 0.0)
        reader.note_front(PHONE, 100, "A", 0.0)
        self._reads(monkeypatch, reader, 37, "SpringBoard", {100: None, 200: None},
                    during=lambda: reader.note_front(PHONE, 1583, "Settings", 0.0))

        await reader.describe_all(PHONE)

        assert [e.pid for e in reader._front[PHONE]] == [37, 1583, 100]

    async def test_the_front_noted_during_its_own_read_is_there_once(self, monkeypatch):
        reader = CoreDeviceAxReader()
        self._reads(monkeypatch, reader, 37, "SpringBoard", {},
                    during=lambda: reader.note_front(PHONE, 37, "SpringBoard", 0.0))

        await reader.describe_all(PHONE)

        assert [e.pid for e in reader._front[PHONE]] == [37]

    async def test_an_app_that_changed_under_the_read_unasked_is_kept(self, monkeypatch):
        """The in-read check could not tell whether it died (review)."""
        reader = CoreDeviceAxReader()
        self._reads(monkeypatch, reader, 37, "SpringBoard", {}, unasked=(617, "Settings"))

        await reader.describe_all(PHONE)

        assert [(e.pid, e.app, e.unknown) for e in reader._front[PHONE]] == [
            (37, "SpringBoard", 0), (617, "Settings", 1)]

    async def test_a_forget_during_the_poll_stands(self, monkeypatch):
        """quern ended an app while this read looked for a report: the
        questions it had kept are dropped with it (review)."""
        monkeypatch.setattr(coredevice_ax, "CRASH_REPORT_WAIT_S", 0.0)
        reader = CoreDeviceAxReader()
        reader.note_front(PHONE, 200, "B", 0.0)
        reader.note_front(PHONE, 100, "QuernProbeScene", 0.0)
        # 100 gone (so a poll runs), 200 unknown (so it would be kept).
        self._reads(monkeypatch, reader, 37, "SpringBoard", {100: False, 200: None})

        async def forgets(udid, since, app):
            reader.forget_front(PHONE)
            return []
        reader.crash_reports = forgets

        await reader.describe_all(PHONE)

        assert [e.pid for e in reader._front[PHONE]] == [37]

    async def test_an_app_noted_during_the_read_is_kept(self, monkeypatch):
        """A launch check that ran while this read was under way."""
        reader = CoreDeviceAxReader()

        async def read(udid, *, timeout, previous_pids=(), seen=None):
            reader.note_front(PHONE, 1583, "Settings", time.time())
            seen.update(pid=37, app="SpringBoard")
            return []
        monkeypatch.setattr(coredevice_ax, "read_tree", read)

        await reader.describe_all(PHONE)

        assert [e.pid for e in reader._front[PHONE]] == [37, 1583]

    async def test_an_in_read_crash_behind_a_slow_connect_is_still_caught(
            self, monkeypatch, phone_address):
        """The in-read check's connect has the read's deadline, not CLOSE_S:
        bounding it hid this crash (review)."""
        lib, state = _fake_lib([_as("InitialDump", SETTINGS), SETTINGS,
                                _as("InitialDump", SAFARI), SAFARI])
        state.processes = [618]                       # Settings is gone
        Service = lib.AppService

        class SlowConnect(Service):
            async def __aenter__(self):
                await asyncio.sleep(0.5)
                return await super().__aenter__()
        lib.AppService = SlowConnect
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        monkeypatch.setattr(coredevice_ax, "CLOSE_S", 0.2)

        with pytest.raises(coredevice_ax.AppExitedDuringRead):
            await asyncio.wait_for(read_tree(PHONE, timeout=5), 10)

    async def test_an_in_read_change_that_cannot_be_told_is_unasked(
            self, monkeypatch, phone_address):
        lib, state = _fake_lib([_as("InitialDump", SETTINGS), SETTINGS,
                                _as("InitialDump", SAFARI), SAFARI])
        state.processes = None                        # the process service refuses
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        seen: dict = {}

        await asyncio.wait_for(read_tree(PHONE, timeout=5, seen=seen), 10)

        assert seen["unasked"] == (617, "Settings")

    async def test_a_process_service_that_hangs_is_could_not_tell(
            self, monkeypatch, phone_address):
        """Its connect too: a hang there once held a read to its deadline."""
        lib, state = _fake_lib([_as("InitialDump", SAFARI), SAFARI])

        class Hangs:
            def __init__(self, rsd):
                pass

            async def __aenter__(self):
                await asyncio.sleep(30)

            async def __aexit__(self, *a):
                return False
        lib.AppService = Hangs
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        monkeypatch.setattr(coredevice_ax, "CLOSE_S", 0.2)
        seen: dict = {}

        await asyncio.wait_for(read_tree(PHONE, timeout=5, previous_pids=(617,), seen=seen), 3)

        assert seen["alive"] == {617: None}

    async def test_a_cancelled_check_leaves_the_question(self, monkeypatch):
        """The caller left during the report poll: the next read asks again."""
        reader, calls = self._reader(
            monkeypatch, [(100, "QuernProbeScene", None), (37, "SpringBoard", False),
                          (37, "SpringBoard", False)])
        started = asyncio.Event()

        async def hangs(udid, since, app):
            started.set()
            await asyncio.sleep(30)
        reader.crash_reports = hangs
        monkeypatch.setattr(coredevice_ax, "CRASH_REPORT_WAIT_S", 30.0)

        await reader.describe_all(PHONE)
        task = asyncio.create_task(reader.describe_all(PHONE))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert [e.pid for e in reader._front[PHONE]] == [100]

    async def test_an_unreadable_listing_is_tried_again(self, monkeypatch):
        """A report still being written fails the pull; the next look has it."""
        reader, _ = self._reader(
            monkeypatch, [(100, "QuernProbeScene", None), (37, "SpringBoard", False)])
        monkeypatch.setattr(coredevice_ax, "CRASH_REPORT_WAIT_S", 5.0)
        monkeypatch.setattr(coredevice_ax, "CRASH_REPORT_POLL_S", 0.01)
        reader.crash_reports = AsyncMock(side_effect=[
            OSError("report still being written"), [(100, self.REPORT_669)]])

        await reader.describe_all(PHONE)
        with pytest.raises(coredevice_ax.AppExitedAfterRead):
            await reader.describe_all(PHONE)

    async def test_a_phone_that_cannot_be_asked_is_asked_once(self, monkeypatch):
        reader, _ = self._reader(
            monkeypatch, [(100, "QuernProbeScene", None), (37, "SpringBoard", False)])
        monkeypatch.setattr(coredevice_ax, "CRASH_REPORT_WAIT_S", 5.0)
        reader.crash_reports = AsyncMock(return_value=None)

        await reader.describe_all(PHONE)
        await reader.describe_all(PHONE)

        assert reader.crash_reports.await_count == 1

    async def test_the_wait_is_bounded_whatever_a_listing_takes(self, monkeypatch):
        reader, _ = self._reader(
            monkeypatch, [(100, "QuernProbeScene", None), (37, "SpringBoard", False)])
        monkeypatch.setattr(coredevice_ax, "CRASH_REPORT_WAIT_S", 0.3)

        async def slow(udid, since, app):
            await asyncio.sleep(30)
        reader.crash_reports = slow

        await reader.describe_all(PHONE)
        started = time.monotonic()
        await reader.describe_all(PHONE)
        assert time.monotonic() - started < 2

    async def test_the_window_opens_when_the_earlier_read_began(self, monkeypatch):
        """Not when it returned: a crash during its close predates that."""
        reader, _ = self._reader(
            monkeypatch, [(100, "QuernProbeScene", None), (37, "SpringBoard", False)],
            reports=[], read_s=0.5)

        began = time.time()
        await reader.describe_all(PHONE)
        await reader.describe_all(PHONE)

        since = reader.crash_reports.await_args.args[1]
        assert abs(since - (began - coredevice_ax.CRASH_REPORT_SLACK_S)) < 0.2

    async def test_unreadable_reports_claim_nothing(self, monkeypatch):
        from server.sources.ios_crash import IosCrashError

        reader, _ = self._reader(
            monkeypatch, [(100, "QuernProbeScene", None), (37, "SpringBoard", False)])
        reader.crash_reports = AsyncMock(side_effect=IosCrashError("crash ls failed"))

        await reader.describe_all(PHONE)
        await reader.describe_all(PHONE)

    async def test_with_no_reports_to_read_nothing_is_claimed(self, monkeypatch):
        reader, _ = self._reader(
            monkeypatch, [(100, "QuernProbeScene", None), (37, "SpringBoard", False)])

        await reader.describe_all(PHONE)
        await reader.describe_all(PHONE)

    async def test_an_app_quern_ended_is_no_crash(self, monkeypatch):
        reader, calls = self._reader(
            monkeypatch, [(100, "QuernProbeScene", None), (37, "SpringBoard", False)],
            reports=[(100, self.REPORT_669)])

        await reader.describe_all(PHONE)
        reader.forget_front(PHONE)
        await reader.describe_all(PHONE)

        assert calls == [(), ()]

    async def test_a_report_written_late_is_waited_for(self, monkeypatch):
        """ReportCrash writes it after the process has gone."""
        reader, _ = self._reader(
            monkeypatch, [(100, "QuernProbeScene", None), (37, "SpringBoard", False)])
        monkeypatch.setattr(coredevice_ax, "CRASH_REPORT_WAIT_S", 5.0)
        monkeypatch.setattr(coredevice_ax, "CRASH_REPORT_POLL_S", 0.01)
        reader.crash_reports = AsyncMock(side_effect=[[], [], [(100, self.REPORT_669)]])

        await reader.describe_all(PHONE)
        with pytest.raises(coredevice_ax.AppExitedAfterRead):
            await reader.describe_all(PHONE)
        assert reader.crash_reports.await_count == 3

    async def test_the_read_asks_about_earlier_apps_on_its_connection(
            self, monkeypatch, phone_address):
        lib, state = _fake_lib([_as("InitialDump", SAFARI), SAFARI])
        state.processes = [618, 500]                  # Safari in front; 617 gone, 500 not
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        seen: dict = {}

        await asyncio.wait_for(
            read_tree(PHONE, timeout=5, previous_pids=(617, 618, 500), seen=seen), 10)

        assert seen == {"pid": 618, "app": "Safari", "alive": {617: False, 500: True}}

    async def test_a_phone_that_cannot_list_says_so(self, monkeypatch, phone_address):
        lib, state = _fake_lib([_as("InitialDump", SAFARI), SAFARI])
        state.processes = None                        # the process service refuses
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        seen: dict = {}

        await asyncio.wait_for(read_tree(PHONE, timeout=5, previous_pids=(617,), seen=seen), 10)

        assert seen["alive"] == {617: None}

    async def test_the_same_app_asks_nothing(self, monkeypatch, phone_address):
        lib, state = _fake_lib([_as("InitialDump", SAFARI), SAFARI])
        state.processes = None                        # a process query would raise
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        seen: dict = {}

        await asyncio.wait_for(read_tree(PHONE, timeout=5, previous_pids=(618,), seen=seen), 10)

        assert "alive" not in seen

    async def test_the_controller_records_it_like_any_read_crash(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=coredevice_ax.AppExitedAfterRead("QuernProbeScene", 100))

        await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        assert ctrl._push_crashed_apps[PHONE] == {"QuernProbeScene"}
        assert "crashed after a read" in ctrl.read_note()


class TestThePhonesCrashReports:
    def _ctrl(self, monkeypatch, names, bodies, *, fail=None):
        from server.sources import ios_crash

        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.get_libimobiledevice_udid = AsyncMock(return_value="00008030-000C59623A69802E")
        monkeypatch.setattr(ios_crash, "command", lambda: ["pmd3"])
        monkeypatch.setattr(ios_crash, "list_reports", AsyncMock(return_value=names))
        pulled = []

        async def pull(cmd, udid, wanted, out):
            pulled.extend(wanted)
            copied = [n for n in wanted if n != fail]
            for n in copied:
                (out / n).write_text('{"h":1}\n' + bodies.get(n, "{}"))
            if fail in wanted:
                raise ios_crash.IosCrashError("did not copy 1", copied)
            return copied
        monkeypatch.setattr(ios_crash, "pull_reports", pull)
        return ctrl, pulled

    async def test_the_newest_are_pulled_and_read(self, monkeypatch):
        from datetime import datetime

        ctrl, pulled = self._ctrl(monkeypatch, [
            "QuernProbeScene-2026-11-15-100617.ips",
            "QuernProbeScene-2026-11-15-085221.ips",
            "JetsamEvent-2026-11-15-100700.ips",
            "Other-2026-11-15-090000.ips",
            "Old-2026-11-10-100000.ips",                 # days before: not wanted
        ], {"QuernProbeScene-2026-11-15-100617.ips": '{"pid": 1933}'})
        since = datetime(2026, 11, 15, 10, 6, 15).timestamp()

        reports = await ctrl._crash_reports_since(PHONE, since)

        assert pulled == ["JetsamEvent-2026-11-15-100700.ips",
                          "QuernProbeScene-2026-11-15-100617.ips",
                          "Other-2026-11-15-090000.ips",
                          "QuernProbeScene-2026-11-15-085221.ips"]
        assert [pid for pid, _ in reports] == [None, 1933, None, None]

    async def test_the_apps_own_reports_come_first(self, monkeypatch):
        """A burst of newer reports from others does not crowd it out."""
        from datetime import datetime

        names = ["QuernProbeScene-2026-11-15-100617.ips"] + [
            f"JetsamEvent-2026-11-15-1007{i:02d}.ips" for i in range(10)]
        ctrl, pulled = self._ctrl(monkeypatch, names,
                                  {"QuernProbeScene-2026-11-15-100617.ips": '{"pid": 100}'})

        reports = await ctrl._crash_reports_since(
            PHONE, datetime(2026, 11, 15, 10, 6, 15).timestamp(), "QuernProbeScene")

        assert pulled == ["QuernProbeScene-2026-11-15-100617.ips"]   # its own alone
        assert reports[0][0] == 100

    async def test_newest_by_the_phones_clock_whatever_the_zone(self, monkeypatch):
        """A phone hours behind the Mac dates its reports before `since`;
        they are still the newest it has (review)."""
        from datetime import datetime

        ctrl, pulled = self._ctrl(monkeypatch, ["QuernProbeScene-2026-11-15-040617.ips"],
                                  {"QuernProbeScene-2026-11-15-040617.ips": '{"pid": 1933}'})
        since = datetime(2026, 11, 15, 10, 6, 15).timestamp()

        reports = await ctrl._crash_reports_since(PHONE, since)

        assert [pid for pid, _ in reports] == [1933]

    async def test_a_report_that_did_not_copy_leaves_the_others(self, monkeypatch):
        ctrl, _ = self._ctrl(
            monkeypatch,
            ["A-2026-11-15-100617.ips", "B-2026-11-15-100618.ips"],
            {"A-2026-11-15-100617.ips": '{"pid": 1933}'},
            fail="B-2026-11-15-100618.ips")
        from datetime import datetime

        reports = await ctrl._crash_reports_since(
            PHONE, datetime(2026, 11, 15, 10, 6, 0).timestamp())

        assert [pid for pid, _ in reports] == [1933]

    async def test_a_phone_off_usb_cannot_be_asked(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.get_libimobiledevice_udid = AsyncMock(return_value=None)

        assert await ctrl._crash_reports_since(PHONE, 0.0) is None

    async def test_quern_ending_the_app_forgets_it(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.resolve_udid = AsyncMock(return_value=PHONE)
        ctrl.devicectl.terminate_app = AsyncMock(return_value=True)
        ctrl.coredevice_ax.note_front(PHONE, 100, "QuernProbeScene", 0.0)

        await ctrl.terminate_app("com.quern.probe.scene", udid=PHONE)

        assert PHONE not in ctrl.coredevice_ax._front

    async def test_a_terminate_that_stopped_nothing_forgets_nothing(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.resolve_udid = AsyncMock(return_value=PHONE)
        ctrl.devicectl.terminate_app = AsyncMock(return_value=False)
        ctrl.coredevice_ax.note_front(PHONE, 100, "QuernProbeScene", 0.0)

        await ctrl.terminate_app("com.example.other", udid=PHONE)

        assert ctrl.coredevice_ax._front[PHONE][0].pid == 100

    @pytest.mark.parametrize("action", ["install", "uninstall"])
    async def test_an_install_or_uninstall_forgets_it(self, monkeypatch, action):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.resolve_udid = AsyncMock(return_value=PHONE)
        ctrl._is_pre_ios17_udid = lambda udid: False
        ctrl.devicectl.install_app = AsyncMock()
        ctrl.devicectl.uninstall_app = AsyncMock()
        ctrl.coredevice_ax.note_front(PHONE, 100, "QuernProbeScene", 0.0)

        if action == "install":
            await ctrl.install_app("/tmp/Probe.app", udid=PHONE)
        else:
            await ctrl.uninstall_app("com.quern.probe.scene", udid=PHONE)

        assert PHONE not in ctrl.coredevice_ax._front

    def test_the_reader_is_given_them(self):
        ctrl = DeviceController()
        assert ctrl.coredevice_ax.crash_reports == ctrl._crash_reports_since


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

        elements, _ = await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

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

        await ctrl.get_ui_elements(PHONE, use_cache=False, snapshot_depth=12,
                                   whole_screen=True)

        ctrl.wda_client.describe_all.assert_awaited()
        ctrl.coredevice_ax.describe_all.assert_not_called()

    async def test_a_cached_push_tree_never_answers_a_lookup(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        await ctrl.get_ui_elements(PHONE, whole_screen=True)   # pushed, cached

        await ctrl.get_ui_elements(PHONE, filter_label="General")

        ctrl.wda_client.find_elements_by_query.assert_awaited()

    async def test_a_cache_hit_names_the_backend_that_made_the_tree(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        await ctrl.get_ui_elements(PHONE, whole_screen=True)
        ctrl._last_read_backend[PHONE] = ctrl.wda_client.TOOL_NAME  # a WDA read since

        await ctrl.get_ui_elements(PHONE, whole_screen=True)        # served from cache

        assert ctrl.coredevice_ax.describe_all.await_count == 1
        assert ctrl.backend_that_served(PHONE) == CoreDeviceAxReader.TOOL_NAME
        ctrl._mark_on_screen.assert_not_called()

    async def test_a_failed_push_read_falls_back_to_wda_and_says_so(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=DeviceError("refused", tool="coredevice"))

        elements, _ = await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        assert [e.label for e in elements] == ["from WDA"]
        assert ctrl.backend_that_served(PHONE) == ctrl.wda_client.TOOL_NAME
        ctrl._mark_on_screen.assert_awaited()

    async def test_not_opted_in_is_unchanged(self, monkeypatch):
        ctrl = _phone_controller(reader="wda", monkeypatch=monkeypatch)

        elements, _ = await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        assert [e.label for e in elements] == ["from WDA"]
        ctrl.coredevice_ax.describe_all.assert_not_called()


class TestIdentifyingTheScreen:
    """POST /landmarks/identify reads the screen as get_screen_summary does:
    through the push on a phone opted in (#664). It read through WDA, so on a
    phone with no WDA running, identifying a screen started it."""

    def _client(self, ctrl):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from server.api import device_ui, landmarks
        from server.knowledge.landmarks import LandmarkRegistry

        app = FastAPI()
        app.include_router(landmarks.router)
        app.include_router(device_ui.router)
        app.state.landmark_registry = LandmarkRegistry()
        app.state.device_controller = ctrl
        client = TestClient(app)
        client.post("/api/v1/landmarks/load", json={"app": "settings", "landmarks": {
            "root": {"landmarks": [
                {"element": "Button", "identifier": "com.apple.settings.general"}]},
        }}).raise_for_status()
        return client

    def test_it_reads_the_push(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)

        resp = self._client(ctrl).post("/api/v1/landmarks/identify",
                                       json={"app": "settings", "udid": PHONE})

        assert resp.status_code == 200, resp.text
        assert resp.json()["matched"] == "root"
        ctrl.coredevice_ax.describe_all.assert_awaited()
        ctrl.wda_client.describe_all.assert_not_called()

    def test_a_depth_asks_for_wdas_read(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)

        self._client(ctrl).post("/api/v1/landmarks/identify",
                                json={"app": "settings", "udid": PHONE, "snapshot_depth": 12})

        ctrl.wda_client.describe_all.assert_awaited()
        ctrl.coredevice_ax.describe_all.assert_not_called()

    #: Settings' root as WDA records it: the table is a container, which the
    #: push does not carry.
    TABLE_SCREEN = {"tabled": {"landmarks": [
        {"element": "Button", "identifier": "com.apple.settings.general"},
        {"element": "Table", "identifier": "settings_table"}]}}

    def test_a_container_landmark_missed_on_the_push_is_named(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        client = self._client(ctrl)
        client.post("/api/v1/landmarks/load",
                    json={"app": "tabled", "landmarks": self.TABLE_SCREEN}).raise_for_status()

        result = client.post("/api/v1/landmarks/identify",
                             json={"app": "tabled", "udid": PHONE}).json()

        assert result["matched"] is None
        assert "no containers" in result["note"]
        assert "tabled (Table settings_table)" in result["note"]

    def test_and_said_on_the_summary_too(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        client = self._client(ctrl)
        client.delete("/api/v1/landmarks/", params={"app": "settings"}).raise_for_status()
        client.post("/api/v1/landmarks/load",
                    json={"app": "tabled", "landmarks": self.TABLE_SCREEN}).raise_for_status()

        summary = client.get("/api/v1/device/screen-summary",
                             params={"udid": PHONE, "identify": True}).json()

        assert summary["identified_as"] is None
        assert "tabled (Table settings_table)" in summary["identify_note"]

    def test_not_on_a_read_through_wda(self, monkeypatch):
        """WDA has the containers: a miss there is a real one."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        # The same screen as the push serves, so only the reader differs.
        ctrl.wda_client.describe_all = AsyncMock(return_value=parse_push(SETTINGS)[1])
        client = self._client(ctrl)
        client.post("/api/v1/landmarks/load",
                    json={"app": "tabled", "landmarks": self.TABLE_SCREEN}).raise_for_status()

        result = client.post("/api/v1/landmarks/identify", json={
            "app": "tabled", "udid": PHONE, "snapshot_depth": 12}).json()
        # The summary identifies against every set: the matching one goes.
        client.delete("/api/v1/landmarks/", params={"app": "settings"}).raise_for_status()
        summary = client.get("/api/v1/device/screen-summary", params={
            "udid": PHONE, "identify": True, "snapshot_depth": 12}).json()
        assert summary["identified_as"] is None
        assert "identify_note" not in summary

        ctrl.coredevice_ax.describe_all.assert_not_called()
        assert result["partial_matches"][0]["matched"] == 1   # only the Table missed
        assert "note" not in result


class TestIdentifyingAfterAPushCrash:
    """identify reads the push now, which can crash the app (#669): the answer
    says so, and which reader made it, as get_ui_tree does."""

    def test_the_crash_is_on_the_answer(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=coredevice_ax.AppExitedDuringRead("QuernProbeScene", 711))

        result = TestIdentifyingTheScreen()._client(ctrl).post(
            "/api/v1/landmarks/identify", json={"app": "settings", "udid": PHONE}).json()

        assert "QuernProbeScene" in result["warning"]
        assert result["backend"] == ctrl.wda_client.TOOL_NAME

    def test_a_clean_push_read_names_its_reader(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)

        result = TestIdentifyingTheScreen()._client(ctrl).post(
            "/api/v1/landmarks/identify", json={"app": "settings", "udid": PHONE}).json()

        assert result["backend"] == CoreDeviceAxReader.TOOL_NAME
        assert "warning" not in result


class TestTheOtherIdentifyReads:
    """The navigation check and an action's screen context identify too, and
    read through WDA on a phone meant to run without it (#664 review)."""

    async def test_the_navigation_check_reads_the_push(self, monkeypatch):
        from server.api.device_ui import _identify_now
        from server.knowledge.landmarks import LandmarkRegistry

        ctrl = _phone_controller(monkeypatch=monkeypatch)

        await _identify_now(ctrl, PHONE, LandmarkRegistry(), use_cache=False)

        ctrl.coredevice_ax.describe_all.assert_awaited()
        ctrl.wda_client.describe_all.assert_not_called()

    async def test_after_a_crash_it_keeps_the_action_depth(self, monkeypatch):
        """WDA with no depth is the full walk, 34.5 s of a long list."""
        from server.api.device_ui import _identify_now
        from server.knowledge.landmarks import LandmarkRegistry

        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl._push_crashed_apps[PHONE] = {"QuernProbeScene"}

        await _identify_now(ctrl, PHONE, LandmarkRegistry(), use_cache=False)

        ctrl.coredevice_ax.describe_all.assert_not_called()
        assert ctrl.wda_client.describe_all.await_args.kwargs.get("snapshot_depth") == 12

    async def test_a_crash_in_the_context_read_is_on_the_context(self, monkeypatch):
        from server.api.device import _capture_screen_context

        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=coredevice_ax.AppExitedDuringRead("QuernProbeScene", 711))

        context = await _capture_screen_context(ctrl, PHONE)

        assert "QuernProbeScene" in context["warning"]

    async def test_an_actions_screen_context_reads_the_push(self, monkeypatch):
        from server.api.device import _capture_screen_context

        ctrl = _phone_controller(monkeypatch=monkeypatch)

        context = await _capture_screen_context(ctrl, PHONE)

        assert context["backend"] == CoreDeviceAxReader.TOOL_NAME
        ctrl.wda_client.describe_all.assert_not_called()


class TestANavigationCheckThatCrashesTheApp:
    """The navigation check reads the push, which can crash the app (#669),
    and its fallback reads SpringBoard: a tap by label there taps an icon
    (review). Before the tap that is a refusal; after it, a warning."""

    PUSHED_TEXT = [
        {"type": "Application", "AXLabel": "QuernProbeScene", "AXUniqueId": None,
         "frame": {"x": 0, "y": 0, "width": 414, "height": 896}},
        {"type": "Heading", "AXLabel": "Text", "AXUniqueId": None,
         "frame": {"x": 150, "y": 60, "width": 100, "height": 20}},
        {"type": "Button", "AXLabel": "More", "AXUniqueId": None, "enabled": True,
         "frame": {"x": 330, "y": 820, "width": 60, "height": 50}},
    ]
    SPRINGBOARD = [
        {"type": "Application", "AXLabel": "SpringBoard", "AXUniqueId": None,
         "frame": {"x": 0, "y": 0, "width": 414, "height": 896}},
        {"type": "Button", "AXLabel": "More", "AXUniqueId": "More", "enabled": True,
         "frame": {"x": 20, "y": 100, "width": 60, "height": 60}},
    ]

    async def _tap(self, ctrl):
        from httpx import ASGITransport, AsyncClient

        from server.config import ServerConfig
        from server.main import create_app
        from server.models import Landmark, NavEdge, ScreenLandmarks

        app = create_app(config=ServerConfig(api_key="k"), enable_oslog=False,
                         enable_crash=False, enable_proxy=False)
        ctrl.resolve_udid = AsyncMock(return_value=PHONE)
        ctrl.wda_client.describe_all = AsyncMock(return_value=self.SPRINGBOARD)
        ctrl.wda_client.find_elements_by_query = AsyncMock(return_value=self.SPRINGBOARD[1:])
        ctrl.wda_client._set_snapshot_depth = AsyncMock()
        ctrl.wda_client._ensure_session = AsyncMock(return_value="s")
        ctrl.wda_client.tap = AsyncMock()
        app.state.device_controller = ctrl
        app.state.landmark_registry.load("probe", [
            ScreenLandmarks(screen="text", landmarks=[Landmark(element="Heading", label="Text")],
                            leads_to=[NavEdge(target="more", label="More", element_type="Button")]),
            ScreenLandmarks(screen="more", landmarks=[Landmark(element="Heading", label="More")]),
        ])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            return await c.post(
                "/api/v1/device/ui/tap-element",
                json={"label": "More", "udid": PHONE, "verify_navigation": True,
                      "navigation_timeout": 1},
                headers={"Authorization": "Bearer k"})

    async def test_before_the_tap_it_taps_nothing(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=coredevice_ax.AppExitedDuringRead("QuernProbeScene", 711))
        ctrl.coredevice_input.tap = AsyncMock()

        resp = await self._tap(ctrl)

        assert resp.status_code >= 400, resp.text
        assert "Nothing was tapped" in resp.text and "QuernProbeScene" in resp.text
        ctrl.coredevice_input.tap.assert_not_called()
        ctrl.wda_client.tap.assert_not_called()

    async def test_after_the_tap_it_says_so(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        tapped = []

        async def read(*args, **kwargs):
            if tapped:
                raise coredevice_ax.AppExitedDuringRead("QuernProbeScene", 711)
            return self.PUSHED_TEXT
        ctrl.coredevice_ax.describe_all = AsyncMock(side_effect=read)

        async def tap(*args, **kwargs):
            tapped.append(args)
            return "coredevice"
        ctrl.coredevice_input.tap = AsyncMock(side_effect=tap)

        resp = await self._tap(ctrl)

        assert resp.status_code == 200, resp.text
        assert tapped, "the tap went ahead"
        assert "after the tap" in resp.json()["warning"]
        assert "QuernProbeScene" in resp.json()["warning"]


class TestReadsInsideActionsStayOnWda:
    """Only reads made to show the screen use the push. `type_text` and
    `clear_text` find their field with an unfiltered read and no depth, which
    the first version took for a whole-screen read (CodeRabbit on #668)."""

    async def test_an_unfiltered_read_inside_an_action_is_wdas(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)

        elements, _ = await ctrl.get_ui_elements(PHONE, use_cache=False)

        assert [e.label for e in elements] == ["from WDA"]
        ctrl.coredevice_ax.describe_all.assert_not_called()

    async def test_a_cached_push_tree_is_not_served_to_an_action(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        await ctrl.get_ui_elements(PHONE, whole_screen=True)   # pushed, cached

        elements, _ = await ctrl.get_ui_elements(PHONE)

        assert [e.label for e in elements] == ["from WDA"]

    async def test_the_cache_names_the_read_that_made_it(self, monkeypatch):
        """A WDA lookup that lands while the push read awaits rewrites the
        shared last-read name; the cache must still say coredevice, or a
        lookup inside the TTL would be answered from merged labels."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        pushed = parse_push(SETTINGS)[1]

        async def read_while_wda_answers(*a, **kw):
            ctrl._last_read_backend[PHONE] = ctrl.wda_client.TOOL_NAME
            return pushed

        ctrl.coredevice_ax.describe_all = AsyncMock(side_effect=read_while_wda_answers)

        await ctrl.get_ui_elements(PHONE, whole_screen=True)
        assert ctrl._ui_cache_backend[PHONE] == CoreDeviceAxReader.TOOL_NAME

        await ctrl.get_ui_elements(PHONE, filter_label="General")
        ctrl.wda_client.find_elements_by_query.assert_awaited()

    async def test_a_pushed_summary_carries_no_wda_timeout(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.wda_client.source_timed_out = lambda udid: 6.0   # an earlier WDA read

        summary, _, _ = await ctrl.get_screen_summary(udid=PHONE, whole_screen=True)

        assert summary["backend"] == CoreDeviceAxReader.TOOL_NAME
        assert "source_timed_out" not in summary and "degraded" not in summary

    async def test_a_wda_summary_still_reports_its_timeout(self, monkeypatch):
        ctrl = _phone_controller(reader="wda", monkeypatch=monkeypatch)
        ctrl.wda_client.source_timed_out = lambda udid: 6.0

        summary, _, _ = await ctrl.get_screen_summary(udid=PHONE, whole_screen=True)

        assert summary["backend"] == ctrl.wda_client.TOOL_NAME
        assert summary["source_timed_out"] is True


class TestTheControllerAfterACrash:
    async def test_the_read_falls_back_and_says_why(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=coredevice_ax.AppExitedDuringRead("QuernProbeScene", 711))

        elements, _ = await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        assert [e.label for e in elements] == ["from WDA"]
        assert ctrl.backend_that_served(PHONE) == ctrl.wda_client.TOOL_NAME
        assert "QuernProbeScene (pid 711) exited" in ctrl.read_note()
        assert "QuernProbeScene" in ctrl._push_crashed_apps[PHONE]

    async def test_a_crashed_app_is_not_read_through_the_push_again(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl._push_crashed_apps[PHONE] = {"QuernProbeScene"}
        ctrl._last_app[PHONE] = "QuernProbeScene"

        await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        ctrl.coredevice_ax.describe_all.assert_not_called()

    async def test_after_a_crash_the_device_stays_on_wda(self, monkeypatch):
        """The read after a crash sees SpringBoard; relaunching the app must not
        put the next read back on the push (CodeRabbit on #668)."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl._push_crashed_apps[PHONE] = {"QuernProbeScene"}
        ctrl._last_app[PHONE] = " "            # SpringBoard, after the crash

        await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        ctrl.coredevice_ax.describe_all.assert_not_called()

    async def test_another_device_is_unaffected(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl._push_crashed_apps["OTHER-PHONE"] = {"QuernProbeScene"}

        await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        ctrl.coredevice_ax.describe_all.assert_awaited()

    async def test_a_flat_read_never_gets_the_pushed_cache(self, monkeypatch):
        """`mode` asks for WDA's read (CodeRabbit on #668)."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        await ctrl.get_ui_elements(PHONE, whole_screen=True)          # pushed, cached
        ctrl.wda_client.describe_all_flat = AsyncMock(return_value=[{
            "type": "Application", "AXLabel": "flat from WDA", "AXUniqueId": None,
            "frame": {"x": 0, "y": 0, "width": 414, "height": 896}}])

        elements, _ = await ctrl.get_ui_elements(PHONE, whole_screen=True, mode="flat")

        assert [e.label for e in elements] == ["flat from WDA"]

    async def test_every_read_records_the_app_on_screen(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)
        assert ctrl._last_app[PHONE] == "Settings"
        await ctrl.get_ui_elements(PHONE, use_cache=False)          # WDA's read
        assert ctrl._last_app[PHONE] == "from WDA"

    async def test_the_summary_carries_the_warning(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=coredevice_ax.AppExitedDuringRead("QuernProbeScene", 711))

        summary, _, _ = await ctrl.get_screen_summary(udid=PHONE, whole_screen=True)

        assert "exited while its screen was read" in summary["warning"]


BARE = [{"type": "Application", "AXLabel": "", "AXUniqueId": None,
         "frame": {"x": 0, "y": 0, "width": 393, "height": 852}}]


class TestABareApplication:
    """The wedge measured on an iPhone 15 Pro (#664): every app launched after
    it read as one unlabelled Application, while SpringBoard read normally."""

    def test_the_wedge_is_refused(self):
        with pytest.raises(coredevice_ax.BareAppTree, match="restarting the phone") as caught:
            coredevice_ax._refuse_bare(BARE, PHONE)
        assert caught.value.tool == "coredevice"

    @pytest.mark.parametrize("elements", [
        # An app with no accessibility of its own still carries its name.
        [{**BARE[0], "AXLabel": "SomeGame"}],
        # SpringBoard's Application is a space, and holds the icons.
        [{**BARE[0], "AXLabel": " "}, {**BARE[0], "type": "Button", "AXLabel": "Fandango"}],
        # An empty label with something under it is a screen, not the wedge.
        [BARE[0], {**BARE[0], "type": "Button", "AXLabel": "OK"}],
    ])
    def test_a_screen_is_not(self, elements):
        assert coredevice_ax._refuse_bare(elements, PHONE) is elements

    async def test_a_read_refuses_it(self, monkeypatch, phone_address):
        lib, state = _fake_lib([])
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)
        tree = coredevice_ax.Tree("AdditionalData", BARE, 711)

        async def newest(queue, held=None):
            return tree, tree, tree
        monkeypatch.setattr(coredevice_ax, "_newest_tree", newest)
        # No pushes reach the queue here: not a silence to resubscribe over.
        monkeypatch.setattr(coredevice_ax, "_first_push", AsyncMock(return_value=True))

        with pytest.raises(coredevice_ax.BareAppTree):
            await read_tree(PHONE, timeout=5)
        assert state.provider_closed and state.rsd_closed

    async def test_a_deadline_answer_refuses_it(self, monkeypatch, phone_address):
        """The tree in hand at a deadline is checked as the settled one is."""
        lib, _ = _fake_lib([])
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        async def newest(queue, held=None):
            held["latest"] = coredevice_ax.Tree("AdditionalData", BARE, 711)
            await asyncio.sleep(30)
        monkeypatch.setattr(coredevice_ax, "_newest_tree", newest)
        # No pushes reach the queue here: not a silence to resubscribe over.
        monkeypatch.setattr(coredevice_ax, "_first_push", AsyncMock(return_value=True))

        with pytest.raises(coredevice_ax.BareAppTree):
            await asyncio.wait_for(read_tree(PHONE, timeout=0.3), 5)

    async def test_the_controller_reads_through_wda_and_says_why(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(side_effect=coredevice_ax.BareAppTree(PHONE))

        elements, _ = await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        assert [e.label for e in elements] == ["from WDA"]
        assert ctrl.backend_that_served(PHONE) == ctrl.wda_client.TOOL_NAME
        assert "empty tree" in ctrl.read_note() and "WDA's" in ctrl.read_note()
        # Not a crash: the push still serves this device's next read.
        assert not ctrl._push_crashed_apps.get(PHONE)

    async def test_wda_failing_too_reports_the_wedge(self, monkeypatch):
        """Not WDA's error alone, which says nothing about why the push failed;
        and still WDA's exception, so not set up stays a 400 (#681 review)."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(side_effect=coredevice_ax.BareAppTree(PHONE))
        ctrl.wda_client.describe_all = AsyncMock(side_effect=WdaNotSetUpError("WDA is not set up"))

        with pytest.raises(WdaNotSetUpError) as caught:
            await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        assert "empty tree" in str(caught.value)
        assert "WDA could not read the screen instead: WDA is not set up" in str(caught.value)

    async def test_a_lookup_with_no_wda_reports_the_wedge(self, monkeypatch):
        """A tap_element on a wedged phone with no WDA is an error naming the
        wedge, not WDA's error alone. It fails where a missing WDA does in
        production: making the session, which setting the lookup's depth
        does first (#681 review)."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(side_effect=coredevice_ax.BareAppTree(PHONE))
        del ctrl.wda_client.find_elements_by_query          # the real one
        ctrl.wda_client._ensure_session = AsyncMock(
            side_effect=WdaNotSetUpError("WDA is not set up"))

        with pytest.raises(WdaNotSetUpError, match="empty tree"):
            await ctrl.get_ui_elements(PHONE, filter_label="Wi-Fi", use_cache=False, lookup=True)

    async def test_a_miss_through_wda_carries_the_reason(self, monkeypatch):
        """WDA read the screen and the element is not there: the miss still says
        the push answered bare, or it is a plain 404 again (#681 review)."""
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(side_effect=coredevice_ax.BareAppTree(PHONE))
        ctrl.wda_client._set_snapshot_depth = AsyncMock()

        with pytest.raises(DeviceError, match="No element found.*empty tree"):
            await ctrl.get_element(label="Wi-Fi", udid=PHONE)

    async def test_one_bare_read_is_read_again(self, monkeypatch):
        monkeypatch.setattr(coredevice_ax, "BARE_CONFIRM_S", 0)
        reads = AsyncMock(side_effect=[coredevice_ax.BareAppTree(PHONE), [{"AXLabel": "Settings"}]])
        monkeypatch.setattr(coredevice_ax, "read_tree", reads)

        elements = await CoreDeviceAxReader().describe_all(PHONE)

        assert elements == [{"AXLabel": "Settings"}] and reads.await_count == 2

    async def test_two_bare_reads_are_the_wedge(self, monkeypatch):
        monkeypatch.setattr(coredevice_ax, "BARE_CONFIRM_S", 0)
        reads = AsyncMock(side_effect=[coredevice_ax.BareAppTree(PHONE)] * 2)
        monkeypatch.setattr(coredevice_ax, "read_tree", reads)

        with pytest.raises(coredevice_ax.BareAppTree):
            await CoreDeviceAxReader().describe_all(PHONE)

    async def test_an_unrelated_wda_failure_is_unchanged(self, monkeypatch):
        ctrl = _phone_controller(monkeypatch=monkeypatch)
        ctrl.coredevice_ax.describe_all = AsyncMock(
            side_effect=DeviceError("refused", tool="coredevice"))
        ctrl.wda_client.describe_all = AsyncMock(
            side_effect=DeviceError("WDA is not set up", tool="wda"))

        with pytest.raises(DeviceError) as caught:
            await ctrl.get_ui_elements(PHONE, use_cache=False, whole_screen=True)

        assert str(caught.value) == "WDA is not set up"


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


def _silent_first(lib, silent: int, *, late: float | None = None,
                  fail_second: bool = False) -> list:
    """Make the first `silent` subscriptions get no push at all, as one made
    the moment an app is relaunched did on an iPhone 11 (#684). With `late`,
    the first pushes after all, that many seconds after subscribing."""
    made: list = []
    original = lib.RemoteAX

    class Maybe(original):
        def __init__(self, rsd):
            super().__init__(rsd)
            made.append(self)
            channel = self.dtx._ctrl_channel
            if len(made) == 1 and late is not None:
                deliver = channel._on_subscribe
                channel._on_subscribe = (
                    lambda: asyncio.get_running_loop().call_later(late, deliver))
            elif len(made) <= silent:
                channel._on_subscribe = lambda: None

        async def connect(self):
            if fail_second and len(made) == 2:
                raise ConnectionRefusedError(61, "refused")
            await super().connect()
    lib.RemoteAX = Maybe
    return made


def _unsubscribed(ax) -> bool:
    return ("clientNeedsAccessibility:", (False,)) in ax.dtx._ctrl_channel.calls


@pytest.mark.usefixtures("phone_address")
class TestASilentSubscription:
    """#684: no push at all for FIRST_PUSH_S means a fresh connection, once."""

    async def test_it_is_made_again_on_a_fresh_connection(self, monkeypatch):
        monkeypatch.setattr(coredevice_ax, "FIRST_PUSH_S", 0.2)
        lib, state = _fake_lib([SETTINGS])
        made = _silent_first(lib, 1)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        elements = await asyncio.wait_for(read_tree(PHONE, timeout=5), 10)

        assert len(elements) == 14 and len(made) == 2
        # Both were unsubscribed: the silent one once the fresh one pushed,
        # the fresh one when the read was done.
        assert _unsubscribed(made[0]) and _unsubscribed(made[1])

    async def test_a_healthy_read_makes_one_connection(self, monkeypatch):
        lib, _ = _fake_lib([SETTINGS])
        made = _silent_first(lib, 0)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        await read_tree(PHONE, timeout=5)

        assert len(made) == 1

    async def test_once_only_then_the_deadline(self, monkeypatch):
        monkeypatch.setattr(coredevice_ax, "FIRST_PUSH_S", 0.2)
        lib, state = _fake_lib([SETTINGS])
        made = _silent_first(lib, 5)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        with pytest.raises(DeviceError, match="no complete accessibility tree"):
            await asyncio.wait_for(read_tree(PHONE, timeout=1.0), 10)

        # Each one, not just whichever set the fake's shared flags (#692 review).
        assert len(made) == 2 and _unsubscribed(made[0]) and _unsubscribed(made[1])

    async def test_the_app_in_front_recovers_the_same_way(self, monkeypatch):
        monkeypatch.setattr(coredevice_ax, "FIRST_PUSH_S", 0.2)
        lib, _ = _fake_lib([SETTINGS])
        made = _silent_first(lib, 1)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        assert await coredevice_ax.front_app(PHONE, timeout=5) == (617, "Settings")
        assert len(made) == 2

    async def test_a_late_first_push_still_wins(self, monkeypatch):
        """A heavy screen whose first push is merely late is not thrown away
        for a read that starts over (#692 review)."""
        monkeypatch.setattr(coredevice_ax, "FIRST_PUSH_S", 0.2)
        lib, _ = _fake_lib([SETTINGS])
        made = _silent_first(lib, 2, late=0.4)          # the spare stays silent
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        elements = await asyncio.wait_for(read_tree(PHONE, timeout=5), 10)

        assert len(elements) == 14 and len(made) == 2
        assert _unsubscribed(made[1])                   # the spare is closed
        # The first was kept open for its late push, and closed once, at the end.
        calls = made[0].dtx._ctrl_channel.calls
        assert calls.count(("clientNeedsAccessibility:", (False,))) == 1

    async def test_a_spare_that_cannot_connect_leaves_the_first(self, monkeypatch):
        monkeypatch.setattr(coredevice_ax, "FIRST_PUSH_S", 0.2)
        lib, _ = _fake_lib([SETTINGS])
        made = _silent_first(lib, 1, late=0.4, fail_second=True)
        monkeypatch.setattr(coredevice_ax, "_pmd3", lambda: lib)

        elements = await asyncio.wait_for(read_tree(PHONE, timeout=5), 10)

        assert len(elements) == 14 and len(made) == 2
