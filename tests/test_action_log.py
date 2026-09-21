"""One log entry per completed quern action.

The spine of the combined trace: an entry naming the operation, the device it
actually went to, what happened and how long it took. It replaces the
`[PERF] START` / `[PERF] SUCCESS` pairs, which put two lines in the log for
one thing that happened and expressed the duration as text nobody could
filter on.

See docs/proposals/logging-spec.md.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from server.models import DeviceError, LogLevel
from server.sources.server_log import ServerLogAdapter


async def _capture(coro_factory, *, level=logging.INFO) -> list:
    """Run something with the buffer adapter installed; return its entries."""
    captured: list = []

    async def on_entry(entry):
        captured.append(entry)

    root = logging.getLogger()
    previous = root.level
    root.setLevel(level)
    adapter = ServerLogAdapter(on_entry=on_entry)
    await adapter.start()
    try:
        try:
            await coro_factory()
        except Exception:
            pass  # the entry is the subject, not the exception
        for _ in range(10):
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
    finally:
        await adapter.stop()
        root.setLevel(previous)
    return [e for e in captured if e.action]


def _controller(**overrides):
    controller = MagicMock()
    controller.resolve_udid = AsyncMock(return_value="RESOLVED-UDID-1111")
    controller._is_physical = MagicMock(return_value=False)
    for k, v in overrides.items():
        setattr(controller, k, v)
    return controller


@pytest.fixture(autouse=True)
def _restore_get_controller():
    """Put `_get_controller` back.

    An earlier version of this file replaced it and never restored it, which
    leaks a MagicMock controller into every test that runs afterwards -- the
    kind of failure that shows up as an unrelated test breaking in CI.
    """
    from server.api import device_ui

    original = device_ui._get_controller  # noqa: SLF001
    yield
    device_ui._get_controller = original  # noqa: SLF001


def _request(controller):
    from server.api import device_ui

    device_ui._get_controller = lambda request: controller  # noqa: SLF001
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))


class TestOneEntryPerAction:
    async def test_a_tap_logs_exactly_one_entry(self):
        """Not a pair. A START and a SUCCESS make the trace twice as long as
        the thing it describes, which is what [PERF] did."""
        from server.api import device_ui
        from server.models import TapRequest

        controller = _controller(tap=AsyncMock(return_value="RESOLVED-UDID-1111"))
        request = _request(controller)

        entries = await _capture(
            lambda: device_ui.tap(request=request, body=TapRequest(x=1.0, y=2.0)),
        )

        assert len(entries) == 1, [e.message for e in entries]

    async def test_the_entry_carries_the_four_fields(self):
        from server.api import device_ui
        from server.models import TapRequest

        controller = _controller(tap=AsyncMock(return_value="RESOLVED-UDID-1111"))
        request = _request(controller)

        entry = (await _capture(
            lambda: device_ui.tap(request=request, body=TapRequest(x=1.0, y=2.0)),
        ))[0]

        assert entry.action == "tap"
        assert entry.udid == "RESOLVED-UDID-1111"
        assert entry.outcome == "ok"
        assert entry.duration_ms is not None
        assert entry.category == "device.action"
        assert entry.level == LogLevel.INFO

    async def test_the_duration_is_a_number_not_a_string(self):
        """`[PERF]` put the duration in the message, so "everything slower
        than two seconds" meant parsing text."""
        from server.api import device_ui
        from server.models import TapRequest

        controller = _controller(tap=AsyncMock(return_value="RESOLVED-UDID-1111"))
        request = _request(controller)

        entry = (await _capture(
            lambda: device_ui.tap(request=request, body=TapRequest(x=1.0, y=2.0)),
        ))[0]

        assert isinstance(entry.duration_ms, int)


class TestTheUdidIsTheResolvedOne:
    """The spec's load-bearing requirement.

    A trace keyed on the empty string because the caller omitted a udid joins
    to nothing, and "which device did this actually go to" is a question this
    project has had to answer by hand more than once.
    """

    async def test_a_tap_with_no_udid_still_names_the_device(self):
        from server.api import device_ui
        from server.models import TapRequest

        controller = _controller(tap=AsyncMock(return_value="RESOLVED-UDID-1111"))
        request = _request(controller)

        # No udid in the request at all -- the caller left it to quern.
        entry = (await _capture(
            lambda: device_ui.tap(request=request, body=TapRequest(x=1.0, y=2.0)),
        ))[0]

        assert entry.udid == "RESOLVED-UDID-1111", (
            "the action entry recorded the requested udid rather than the "
            "resolved one, so the trace cannot be joined to anything"
        )

    async def test_tap_element_names_the_resolved_device_too(self):
        from server.api import device_ui
        from server.models import TapElementRequest

        controller = _controller(
            tap_element=AsyncMock(return_value={"status": "ok", "tapped": {}}),
        )
        request = _request(controller)

        entry = (await _capture(
            lambda: device_ui.tap_element(
                request=request, body=TapElementRequest(label="Map"),
            ),
        ))[0]

        assert entry.udid == "RESOLVED-UDID-1111"


class TestOutcomesCarryTheLevelPolicy:
    async def test_a_failure_is_an_error(self):
        from server.api import device_ui
        from server.models import TapRequest

        controller = _controller(
            tap=AsyncMock(side_effect=DeviceError("nope", tool="simctl")),
        )
        request = _request(controller)

        entry = (await _capture(
            lambda: device_ui.tap(request=request, body=TapRequest(x=1.0, y=2.0)),
        ))[0]

        assert entry.outcome == "failed"
        assert entry.level == LogLevel.ERROR

    async def test_a_failed_action_is_still_logged(self):
        """A failure is the entry most worth having in a trace, which is why
        it is emitted from `finally` rather than the success path."""
        from server.api import device_ui
        from server.models import TapRequest

        controller = _controller(
            tap=AsyncMock(side_effect=DeviceError("nope", tool="simctl")),
        )
        request = _request(controller)

        entries = await _capture(
            lambda: device_ui.tap(request=request, body=TapRequest(x=1.0, y=2.0)),
        )

        assert len(entries) == 1

    async def test_not_found_is_not_an_error(self):
        """The element genuinely was not there. That is an answer to the
        question asked, and logging it as an error trains the reader to
        ignore errors."""
        from server.api import device_ui
        from server.models import TapElementRequest

        controller = _controller(
            tap_element=AsyncMock(return_value={"status": "not_found"}),
        )
        request = _request(controller)

        entry = (await _capture(
            lambda: device_ui.tap_element(
                request=request, body=TapElementRequest(label="Nope"),
            ),
        ))[0]

        assert entry.outcome == "not_found"
        assert entry.level == LogLevel.INFO


class TestTheBeginEntryIsDebugOnly:
    async def test_no_begin_entry_at_the_default_level(self):
        from server.api import device_ui
        from server.models import TapRequest

        controller = _controller(tap=AsyncMock(return_value="RESOLVED-UDID-1111"))
        request = _request(controller)

        entries = await _capture(
            lambda: device_ui.tap(request=request, body=TapRequest(x=1.0, y=2.0)),
            level=logging.INFO,
        )

        assert [e.outcome for e in entries] == ["ok"]

    async def test_debug_brings_the_pair_back(self):
        """The one case a completion entry cannot cover: an action that starts
        and never finishes leaves no completion entry at all."""
        from server.api import device_ui
        from server.models import TapRequest

        controller = _controller(tap=AsyncMock(return_value="RESOLVED-UDID-1111"))
        request = _request(controller)

        entries = await _capture(
            lambda: device_ui.tap(request=request, body=TapRequest(x=1.0, y=2.0)),
            level=logging.DEBUG,
        )

        assert [e.outcome for e in entries] == ["started", "ok"]
        assert entries[0].category == entries[1].category, (
            "both halves must carry the category, or a category filter "
            "returns only one of them"
        )


class TestTypingThatDidNotTakeIsSuspectNotFailed:
    async def test_unverified_typing_is_a_warning(self):
        """Quern did what was asked and the result is not to be trusted --
        the WARNING row of the level policy, not the ERROR one."""
        from server.api import device_ui
        from server.models import TypeTextRequest

        controller = _controller(
            type_text=AsyncMock(
                return_value={"udid": "RESOLVED-UDID-1111", "verified": False},
            ),
        )
        request = _request(controller)

        entry = (await _capture(
            lambda: device_ui.type_text(
                request=request, body=TypeTextRequest(text="hello"),
            ),
        ))[0]

        assert entry.outcome == "suspect"
        assert entry.level == LogLevel.WARNING

    async def test_verified_typing_is_plain_ok(self):
        from server.api import device_ui
        from server.models import TypeTextRequest

        controller = _controller(
            type_text=AsyncMock(
                return_value={"udid": "RESOLVED-UDID-1111", "verified": True},
            ),
        )
        request = _request(controller)

        entry = (await _capture(
            lambda: device_ui.type_text(
                request=request, body=TypeTextRequest(text="hello"),
            ),
        ))[0]

        assert entry.outcome == "ok"
        assert entry.level == LogLevel.INFO

    async def test_the_text_itself_is_never_recorded(self):
        """This is how passwords get typed, and a trace is a thing people
        paste into bug reports."""
        from server.api import device_ui
        from server.models import TypeTextRequest

        controller = _controller(
            type_text=AsyncMock(
                return_value={"udid": "RESOLVED-UDID-1111", "verified": True},
            ),
        )
        request = _request(controller)

        entry = (await _capture(
            lambda: device_ui.type_text(
                request=request, body=TypeTextRequest(text="hunter2-secret"),
            ),
        ))[0]

        assert "hunter2" not in entry.message
        assert "hunter2" not in entry.raw


class TestActionsBeyondTheUiLayer:
    """A trace that only covers taps is not a trace.

    The first pass wrapped `device/ui` and nothing else, which left the
    `proxy` category with eighteen tools and no entries at all -- including a
    CA certificate install that changes the device and takes seconds. What
    counts as an action is anything quern did on a caller's behalf, judged by
    who invoked it rather than which module it lives in.
    """

    async def test_installing_a_cert_logs_one_entry_per_device(self):
        """Per device, not per call: this can touch several at once, and a
        trace joins on a single resolved udid."""
        from server.api import proxy_certs

        controller = _controller()
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(device_controller=controller)),
        )

        async def fake_install(ctrl, udid, *, force, device_name):
            return True

        entries = await _capture(
            lambda: _install(proxy_certs, request, ["SIM-A", "SIM-B"], fake_install),
        )

        assert sorted(e.udid for e in entries) == ["SIM-A", "SIM-B"], (
            [e.message for e in entries]
        )
        assert {e.category for e in entries} == {"proxy"}
        assert {e.action for e in entries} == {"install_proxy_cert"}

    async def test_a_device_that_fails_is_recorded_as_failed(self):
        """The loop swallows the exception so the other devices still get
        their turn -- so the action entry is the only record that this one
        did not work."""
        from server.api import proxy_certs

        controller = _controller()
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(device_controller=controller)),
        )

        async def fake_install(ctrl, udid, *, force, device_name):
            if udid == "SIM-B":
                raise RuntimeError("no trust store")
            return True

        entries = await _capture(
            lambda: _install(proxy_certs, request, ["SIM-A", "SIM-B"], fake_install),
        )

        by_udid = {e.udid: e for e in entries}
        assert by_udid["SIM-A"].outcome == "ok"
        assert by_udid["SIM-B"].outcome == "failed"
        assert by_udid["SIM-B"].level == LogLevel.ERROR


async def _install(proxy_certs, request, udids, fake_install):
    """Drive the real cert-install loop over a stubbed device list."""
    from unittest.mock import patch

    from server.models import DeviceInfo, DeviceState, DeviceType

    devices = [
        DeviceInfo(
            udid=u, name=f"Sim {u}", state=DeviceState.BOOTED,
            device_type=DeviceType.SIMULATOR, os_version="iOS 18.6",
        )
        for u in udids
    ]
    controller = request.app.state.device_controller
    controller.list_devices = AsyncMock(return_value=devices)
    # No active device, so the handler installs on every booted simulator.
    controller._active_udid = None  # noqa: SLF001

    with patch.object(proxy_certs.cert_manager, "install_cert", fake_install):
        return await proxy_certs.install_cert(request=request, body=None)


class TestTheDecoratorFormActuallyEmits:
    """The coverage test only sees that a decorator is *present*.

    FastAPI reads the endpoint's signature to build its request model, and a
    wrapper that hid it would break the route rather than the logging -- but a
    wrapper that logged nothing would pass the coverage test and the suite
    both, silently. So this drives the decorated function and reads the entry.
    """

    async def test_a_decorated_handler_emits_one_entry(self):
        from server.api.actions import current_action, logged_action

        @logged_action("pretend_action", category="proxy")
        async def handler():
            current_action().udid = "SIM-FROM-INSIDE"
            return {"ok": True}

        entries = await _capture(handler)

        assert len(entries) == 1
        assert entries[0].action == "pretend_action"
        assert entries[0].category == "proxy"
        assert entries[0].outcome == "ok"

    async def test_the_handler_can_name_its_device_from_inside(self):
        """A ContextVar, so a handler deep in a long function does not have to
        thread a parameter out to the decorator."""
        from server.api.actions import current_action, logged_action

        @logged_action("pretend_action", category="proxy")
        async def handler():
            current_action().udid = "SIM-FROM-INSIDE"
            return {"ok": True}

        entries = await _capture(handler)

        assert entries[0].udid == "SIM-FROM-INSIDE"

    async def test_a_raising_handler_is_recorded_as_failed(self):
        from server.api.actions import logged_action

        @logged_action("pretend_action", category="proxy")
        async def handler():
            raise RuntimeError("boom")

        entries = await _capture(handler)

        assert entries[0].outcome == "failed"
        assert entries[0].level == LogLevel.ERROR

    async def test_concurrent_handlers_do_not_share_a_udid(self):
        """The reason it is a ContextVar and not a global: requests interleave
        on one event loop, and two boots would otherwise overwrite each
        other's device."""
        from server.api.actions import current_action, logged_action

        @logged_action("pretend_action", category="proxy")
        async def handler(name):
            current_action().udid = name
            await asyncio.sleep(0)  # let the other one run in between
            current_action().udid = name
            return name

        entries = await _capture(
            lambda: asyncio.gather(handler("SIM-A"), handler("SIM-B")),
        )

        assert sorted(e.udid for e in entries) == ["SIM-A", "SIM-B"], (
            [e.udid for e in entries]
        )

    def test_the_decorator_preserves_the_signature(self):
        """FastAPI builds its request model from the signature, so a wrapper
        that hid it would change what the endpoint accepts."""
        import inspect

        from server.api.actions import logged_action

        @logged_action("pretend_action", category="proxy")
        async def handler(udid: str, count: int = 3) -> dict:
            return {}

        params = inspect.signature(handler).parameters
        assert list(params) == ["udid", "count"]
        assert params["count"].default == 3

    def test_a_sync_handler_is_wrapped_without_awaiting_it(self):
        """FastAPI accepts sync endpoints, and `await`ing one raises.

        Every route decorated today is async, so this guards the next one
        rather than fixing a current bug -- a TypeError at request time is a
        bad way to discover it.
        """
        from server.api.actions import logged_action

        @logged_action("pretend_sync", category="proxy")
        def handler():
            return {"ok": True}

        assert handler() == {"ok": True}
