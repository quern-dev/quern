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

    async def _do(self, method: str, url: str, **kw):
        body = kw.get("json") or {}
        if url.endswith("/appium/settings"):
            self.depth = body["settings"]["snapshotMaxDepth"]
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
