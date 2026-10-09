"""A WDA write is never sent twice (#407).

`WdaBackend._request` re-sends a request once on a fresh connection after a
timeout or a read error. That is right for a read and wrong for a write: both
errors can arrive *after* WDA has the request, so re-sending runs it again -- a
tap lands twice, text is typed twice, a second Home press opens the app
switcher. Only a refused connection, which never reached WDA, may be retried.

These tests drive the real `_request` and fault the transport underneath it,
counting what actually reached "WDA". The tests this replaces for launch and
open_url mocked `_request` itself, which is where the retry lives, so they
could not see a re-send at all.
"""

from __future__ import annotations

import httpx
import pytest

from server.device.ios import wda_client as wda_mod
from server.device.ios.wda_client import WdaBackend
from server.models import DeviceError

SIM = "11111111-2222-3333-4444-555555555555"


class FakeWDA:
    """An httpx.AsyncClient stand-in that counts what reaches WDA.

    `faults` maps a path suffix to the exceptions to raise on successive
    requests to it; once they run out, the request succeeds.
    """

    def __init__(self, faults: dict[str, list[Exception]] | None = None):
        self.faults = {k: list(v) for k, v in (faults or {}).items()}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def reached(self, suffix: str) -> int:
        return sum(1 for _, url in self.calls if url.endswith(suffix))

    async def _do(self, method: str, url: str, **kw):
        self.calls.append((method, url))
        for suffix, queue in self.faults.items():
            if url.endswith(suffix) and queue:
                raise queue.pop(0)
        body = {"value": None}
        if url.endswith("/session"):
            body = {"sessionId": "s1", "value": {"sessionId": "s1"}}
        if url.endswith("/window/size"):
            body = {"value": {"width": 400.0, "height": 800.0}}
        if url.endswith("/elements"):
            body = {"value": []}
        return httpx.Response(200, json=body, request=httpx.Request(method.upper(), url))

    async def post(self, url, **kw):
        return await self._do("post", url, **kw)

    async def get(self, url, **kw):
        return await self._do("get", url, **kw)


@pytest.fixture
def wda(monkeypatch):
    backend = WdaBackend()
    backend.register_simulator(SIM, 8200)

    def install(faults=None) -> FakeWDA:
        fake = FakeWDA(faults)
        monkeypatch.setattr(wda_mod.httpx, "AsyncClient", fake)
        return fake

    backend.install = install
    return backend


def _req():
    return httpx.Request("POST", "http://wda/x")


#: Every write that must not run twice: (name, path WDA sees, how to call it).
WRITES = [
    ("tap", "/wda/tap", lambda w: w.tap(SIM, 10, 10)),
    ("long press", "/wda/touchAndHold", lambda w: w.tap(SIM, 10, 10, hold=0.5)),
    ("swipe", "/wda/dragfromtoforduration", lambda w: w.swipe(SIM, 10, 10, 10, 300)),
    ("type_text", "/wda/keys", lambda w: w.type_text(SIM, "hello")),
    ("press_button", "/wda/pressButton", lambda w: w.press_button(SIM, "home")),
    ("launch_app", "/wda/apps/launch", lambda w: w.launch_app(SIM, "com.example", {})),
    ("open_url", "/url", lambda w: w.open_url(SIM, "https://example.com/x")),
]

#: Errors that can arrive after WDA already has the request.
MAYBE_DELIVERED = [
    pytest.param(lambda: httpx.ReadError("reset", request=_req()), id="read-error"),
    pytest.param(lambda: httpx.ReadTimeout("slow", request=_req()), id="read-timeout"),
    pytest.param(lambda: httpx.RemoteProtocolError("hung up", request=_req()),
                 id="remote-protocol"),
]


@pytest.mark.parametrize("make_error", MAYBE_DELIVERED)
@pytest.mark.parametrize(("name", "path", "call"), WRITES, ids=[w[0] for w in WRITES])
async def test_a_write_that_may_have_landed_is_not_sent_again(wda, name, path, call, make_error):
    """The behaviour, and nothing else: one request reached WDA.

    Kept apart from the wording below so a failure here always means a write
    was sent twice. Measured against the code before #407: tap, swipe,
    type_text and press_button failed on a read error and a read timeout,
    launch_app and open_url on a read error; a remote-protocol error was
    already not re-sent, and passed."""
    fake = wda.install({path: [make_error()]})
    with pytest.raises(DeviceError):
        await call(wda)
    assert fake.reached(path) == 1, f"{name} reached WDA {fake.reached(path)} times"


@pytest.mark.parametrize("make_error", MAYBE_DELIVERED)
@pytest.mark.parametrize(("name", "path", "call"), WRITES, ids=[w[0] for w in WRITES])
async def test_the_refusal_says_the_write_may_have_run(wda, name, path, call, make_error):
    """What the caller needs in order to decide whether to repeat it."""
    wda.install({path: [make_error()]})
    with pytest.raises(DeviceError) as caught:
        await call(wda)
    text = str(caught.value)
    assert any(phrase in text for phrase in (
        "may already have been performed", "may already have launched",
        "may still be starting", "may have opened anyway",
    )), text


@pytest.mark.parametrize(("name", "path", "call"), WRITES, ids=[w[0] for w in WRITES])
async def test_a_refused_connection_is_still_retried(wda, name, path, call):
    """A refused connection never reached WDA, so sending it again is safe --
    and keeps a write working across a forward that has just been replaced."""
    fake = wda.install({path: [httpx.ConnectError("refused", request=_req())]})
    await call(wda)
    assert fake.reached(path) == 2


async def test_the_backspace_fallback_of_a_clear_is_not_sent_twice(wda, monkeypatch):
    """select_all_and_delete's last resort: three taps, then one backspace.
    Re-sent, the backspace deletes a character the caller did not ask about."""
    fake = wda.install({"/wda/keys": [httpx.ReadError("reset", request=_req())]})
    with pytest.raises(DeviceError):
        await wda.select_all_and_delete(SIM, 10, 10)
    assert fake.reached("/wda/keys") == 1


async def test_a_gesture_that_may_have_landed_is_not_sent_again(wda):
    from server.device.gestures import plan as plan_gesture

    plan = plan_gesture("pinch", 200, 400, scale=0.5)
    fake = wda.install({"/actions": [httpx.ReadError("reset", request=_req())]})
    with pytest.raises(DeviceError, match="not sent again"):
        await wda.perform_gesture(SIM, plan)
    assert fake.reached("/actions") == 1


async def test_a_read_is_still_retried_after_a_read_error(wda):
    """The fix is for writes. A read sent as a POST is safe to repeat, and must
    keep its retry: losing it would turn every dropped connection into a
    failed tree read."""
    fake = wda.install({"/elements": [httpx.ReadError("reset", request=_req())]})
    await wda.find_elements_by_query(SIM, "accessibility id", "x")
    assert fake.reached("/elements") == 2


async def test_a_clear_is_still_retried(wda):
    """Clearing a field twice leaves it empty, so it keeps the retry."""
    fake = wda.install({"/element/E1/clear": [httpx.ReadError("reset", request=_req())]})
    await wda._request("post", SIM, "/element/E1/clear", use_session=True)
    assert fake.reached("/element/E1/clear") == 2
