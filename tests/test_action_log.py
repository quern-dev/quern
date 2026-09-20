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
