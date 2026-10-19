"""The WDA client calls the #475 sweep makes: its frames and its element scroll.

Driven through the real `_request`, with the transport faked underneath it, the
way #407's tests drive the writes.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from server.device.ios import wda_client as wda_mod
from server.device.ios.wda_client import WdaBackend
from server.models import DeviceError
from tests.test_wda_writes_not_resent import SIM, FakeWDA


def _el(eid: str, kind: str, x: float, y: float, w: float, h: float) -> dict:
    return {"ELEMENT": eid, "type": f"XCUIElementType{kind}", "name": kind,
            "label": None, "rect": {"x": x, "y": y, "width": w, "height": h}}


class ScreenWDA(FakeWDA):
    """Answers screenshots, the depth setting, and class queries by depth."""

    def __init__(self, containers: dict[int, list[dict]] | None = None, **kw):
        super().__init__(**kw)
        self.containers = containers or {}
        self.depth: int | None = None
        self.queries: list[tuple[int | None, str]] = []
        self.png = b"\x89PNG fake"
        #: The status the settings endpoint answers with.
        self.settings_status = 200
        #: Settings requests to answer "invalid session id" first, as WDA does
        #: once another client has replaced quern's session.
        self.stale_settings = 0
        self.sessions = 0

    async def _do(self, method: str, url: str, **kw):
        body = kw.get("json") or {}
        if url.endswith("/appium/settings"):
            self.calls.append((method, url))
            for suffix, queue in self.faults.items():
                if url.endswith(suffix) and queue:
                    raise queue.pop(0)
            if self.stale_settings:
                self.stale_settings -= 1
                return httpx.Response(404, json={"value": {
                    "error": "invalid session id", "message": "Session does not exist"}},
                    request=httpx.Request("POST", url))
            if self.settings_status != 200:
                return httpx.Response(self.settings_status, json={"value": {"error": "x"}},
                                      request=httpx.Request("POST", url))
            self.depth = body["settings"]["snapshotMaxDepth"]
            return httpx.Response(200, json={"value": None}, request=httpx.Request("POST", url))
        if url.endswith("/elements") and body.get("using") == "class name":
            self.calls.append((method, url))
            for suffix, queue in self.faults.items():
                if url.endswith(suffix) and queue:
                    raise queue.pop(0)
            self.queries.append((self.depth, body["value"]))
            kind = body["value"].removeprefix("XCUIElementType")
            found = [e for e in self.containers.get(self.depth, []) if e["type"].endswith(kind)]
            return httpx.Response(200, json={"value": found},
                                  request=httpx.Request("POST", url))
        if url.endswith("/session"):
            self.sessions += 1
        if url.endswith("/source"):
            self.calls.append((method, url))
            return httpx.Response(200, json={"value": {"type": "XCUIElementTypeApplication",
                                                       "rect": {"x": 0, "y": 0, "width": 400,
                                                                "height": 880},
                                                       "children": []}},
                                  request=httpx.Request("GET", url))
        if url.endswith("/screenshot"):
            self.calls.append((method, url))
            for suffix, queue in self.faults.items():
                if url.endswith(suffix) and queue:
                    raise queue.pop(0)
            return httpx.Response(200, json={"value": base64.b64encode(self.png).decode()},
                                  request=httpx.Request("GET", url))
        return await super()._do(method, url, **kw)


@pytest.fixture
def wda(monkeypatch):
    backend = WdaBackend()
    backend.register_simulator(SIM, 8200)

    def install(**kw) -> ScreenWDA:
        fake = ScreenWDA(**kw)
        monkeypatch.setattr(wda_mod.httpx, "AsyncClient", fake)
        return fake

    backend.install = install
    return backend


def _timeout():
    return httpx.ReadTimeout("slow", request=httpx.Request("GET", "http://wda/x"))


class TestScreenshot:
    async def test_it_returns_the_png(self, wda):
        fake = wda.install()
        assert await wda.screenshot(SIM) == fake.png

    async def test_a_timeout_is_not_re_sent(self, wda):
        """Behind a deep query WDA may still be walking; the transport's
        re-send drops a phone's forward (#470 review, #475 review 4)."""
        fake = wda.install(faults={"/screenshot": [_timeout(), _timeout()]})
        with pytest.raises(DeviceError, match="timed out"):
            await wda.screenshot(SIM)
        assert fake.reached("/screenshot") == 1

    async def test_no_image_is_an_error_not_an_empty_frame(self, wda):
        fake = wda.install()
        fake.png = None

        async def no_value(method, url, **kw):
            fake.calls.append((method, url))
            return httpx.Response(200, json={"value": None}, request=httpx.Request("GET", url))

        fake._do = no_value
        with pytest.raises(DeviceError, match="no screenshot"):
            await wda.screenshot(SIM)


class TestFindScrollContainer:
    POINT = (200.0, 440.0)

    async def test_the_innermost_container_under_the_swipe(self, wda):
        """Not the largest: an outer pager scrolls nothing the sweep can see
        (#475 review 7)."""
        wda.install(containers={12: [
            _el("pager", "ScrollView", 0, 0, 400, 880),
            _el("list", "CollectionView", 0, 100, 400, 700),
            _el("side", "Table", 0, 0, 100, 200),   # not under the point
        ]})
        assert await wda.find_scroll_container(SIM, (12, 25), point=self.POINT) == ("list", 12)

    async def test_it_looks_deeper_when_nothing_is_at_the_first_depth(self, wda):
        """Settings' container appears only at 25; the scroll must run there."""
        fake = wda.install(containers={25: [_el("cv", "CollectionView", 0, 0, 400, 880)]})
        assert await wda.find_scroll_container(SIM, (12, 25), point=self.POINT) == ("cv", 25)
        assert fake.depth == 25
        assert {d for d, _ in fake.queries} == {12, 25}

    async def test_nothing_under_the_swipe_is_none(self, wda):
        wda.install(containers={12: [_el("side", "Table", 0, 0, 100, 200)]})
        assert await wda.find_scroll_container(SIM, (12, 25), point=self.POINT) is None

    async def test_a_timeout_ends_the_search(self, wda):
        """No more queries behind a runner that may still be walking."""
        fake = wda.install(
            containers={25: [_el("cv", "CollectionView", 0, 0, 400, 880)]},
            faults={"/elements": [_timeout()]},
        )
        assert await wda.find_scroll_container(SIM, (12, 25), point=self.POINT) is None
        assert len(fake.queries) == 0, "queried again after the timeout"
        assert fake.reached("/elements") == 1


class TestScrollElement:
    async def test_it_scrolls_that_element_in_that_direction_by_that_share(self, wda):
        fake = wda.install()
        sent = {}
        real = fake._do

        async def record(method, url, **kw):
            if url.endswith("/scroll"):
                sent.update(kw.get("json") or {})
            return await real(method, url, **kw)

        fake._do = record
        await wda.scroll_element(SIM, "el-9", "up", 0.4)
        assert fake.reached("/wda/element/el-9/scroll") == 1
        assert sent == {"direction": "up", "distance": 0.4}, json.dumps(sent)


class TestSetSnapshotDepth:
    """#478 review: a failed depth set was recorded as success, or swallowed."""

    async def test_an_error_answer_is_a_failure_and_forgets_the_depth(self, wda):
        fake = wda.install()
        assert await wda._set_snapshot_depth(SIM, 25) is True
        fake.settings_status = 500
        assert await wda._set_snapshot_depth(SIM, 12) is False
        assert SIM not in wda._current_depth, "a failed set left a depth on record"
        fake.settings_status = 200
        sent = fake.reached("/appium/settings")
        assert await wda._set_snapshot_depth(SIM, 12) is True
        assert fake.reached("/appium/settings") == sent + 1
        assert fake.depth == 12

    async def test_after_a_failure_the_same_depth_is_sent_again(self, wda):
        """It used to keep the old value: a set to 25 that failed after an earlier
        25 left the cache at 25, so the retry was skipped."""
        fake = wda.install()
        await wda._set_snapshot_depth(SIM, 12)
        fake.settings_status = 500
        await wda._set_snapshot_depth(SIM, 25)
        fake.settings_status = 200
        sent = fake.reached("/appium/settings")
        await wda._set_snapshot_depth(SIM, 12)
        assert fake.reached("/appium/settings") == sent + 1, "trusted a depth WDA may not have"

    async def test_no_answer_is_a_failure(self, wda):
        # Every settings request goes unanswered: session creation's own, the
        # set, and whatever reconnecting and resending _request adds.
        wda.install(faults={"/appium/settings": [_timeout() for _ in range(10)]})
        assert await wda._set_snapshot_depth(SIM, 12) is False
        assert SIM not in wda._current_depth

    async def test_a_shallow_full_read_at_an_unknown_depth_is_refused(self, wda):
        """A /source at an unknown depth may walk every element (#470)."""
        fake = wda.install()
        fake.settings_status = 500
        with pytest.raises(DeviceError, match="was not read"):
            await wda.describe_all(SIM, snapshot_depth=12)
        assert fake.reached("/source") == 0, "read /source anyway"

    async def test_a_session_whose_settings_were_refused_records_no_depth(self, wda):
        fake = wda.install()
        fake.settings_status = 500
        await wda._ensure_session(SIM)
        assert SIM not in wda._current_depth, "recorded a depth WDA refused"

    async def test_a_session_another_client_replaced_is_made_again(self, wda):
        """Review of the #478 follow-up: the set used its own client, so a stale
        session failed every set -- and every shallow /source behind it -- until
        the driver was restarted."""
        fake = wda.install()
        await wda._ensure_session(SIM)
        wda._current_depth.pop(SIM, None)
        before = fake.sessions
        fake.stale_settings = 1
        assert await wda._set_snapshot_depth(SIM, 12) is True
        assert fake.sessions == before + 1, "the stale session was not replaced"
        assert fake.depth == 12
        await wda.describe_all(SIM, snapshot_depth=12)
        assert fake.reached("/source") == 1

    async def test_a_failed_set_to_the_default_depth_still_reads(self, wda):
        """Only a shallow read is refused: the default depth is the full walk
        anyway, and refusing it would fail every default tree read whenever WDA
        refused a setting."""
        fake = wda.install()
        fake.settings_status = 500
        await wda.describe_all(SIM)
        assert fake.reached("/source") == 1

    async def test_the_nested_read_refuses_too(self, wda):
        fake = wda.install()
        fake.settings_status = 500
        with pytest.raises(DeviceError, match="was not read"):
            await wda.describe_all_nested(SIM, snapshot_depth=12)
        assert fake.reached("/source") == 0
