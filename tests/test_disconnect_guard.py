"""`_run_until_client_leaves`: the guard's own logic, and where it is applied.

A caveat these tests cannot remove, stated so a green run is not over-read: the
guard does not fire in production today. `APIKeyMiddleware` is a Starlette
`BaseHTTPMiddleware`, and through it `request.is_disconnected()` never reports a
disconnect — the review of #204 isolated that by running the guard behind each
middleware in turn. What is tested here is that the guard is *correct* given a
request that can report a disconnect, so it works unchanged once the middleware
is fixed. It is not a claim that disconnects are handled end to end.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from server.api.device_ui import _run_until_client_leaves, tap_element
from server.models import TapElementRequest


def _request(disconnected):
    request = MagicMock()
    request.is_disconnected = AsyncMock(side_effect=disconnected)
    return request


@pytest.mark.asyncio
async def test_a_disconnect_cancels_the_work_and_returns_499():
    """M6.

    Awaited under a timeout, so a guard that never notices the disconnect
    fails here with a TimeoutError instead of hanging the suite.
    """
    started = asyncio.Event()
    cancelled: list[bool] = []

    async def work():
        started.set()
        try:
            await asyncio.Event().wait()      # never finishes by itself
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    request = _request(lambda: started.is_set())

    with pytest.raises(HTTPException) as excinfo:
        await asyncio.wait_for(
            _run_until_client_leaves(request, work(), what="probe", poll_s=0.01),
            timeout=2.0,
        )

    assert excinfo.value.status_code == 499
    assert cancelled == [True], "the work kept running after the client left"


@pytest.mark.asyncio
async def test_work_that_finishes_returns_its_result():
    async def work():
        return {"status": "ok"}

    request = _request(lambda: False)
    result = await _run_until_client_leaves(request, work(), what="probe", poll_s=0.01)
    assert result == {"status": "ok"}


@pytest.mark.asyncio
async def test_work_that_raises_propagates_its_own_error():
    """A failure inside the work is the work's error, not a disconnect."""
    async def work():
        raise ValueError("device said no")

    request = _request(lambda: False)
    with pytest.raises(ValueError, match="device said no"):
        await _run_until_client_leaves(request, work(), what="probe", poll_s=0.01)


@pytest.mark.asyncio
async def test_tap_element_runs_under_the_guard():
    """The common entry point to the sweep must be guarded too.

    With `scroll_to_find` on — its default — `tap_element` runs the same sweep
    as `scroll_to_element`. Guarding only the dedicated scroll endpoint left the
    path most callers use unbounded.
    """
    seen: list[str] = []

    async def spy(_request, coro, *, what, poll_s=2.0):
        seen.append(what)
        return await coro

    controller = MagicMock()
    controller.tap_element = AsyncMock(return_value={"status": "ok"})

    with (
        patch("server.api.device_ui._run_until_client_leaves", spy),
        patch("server.api.device_ui._get_controller", return_value=controller),
    ):
        await tap_element(MagicMock(), TapElementRequest(identifier="button"))

    assert seen == ["tap_element"], f"tap_element was not guarded: {seen}"
    controller.tap_element.assert_awaited_once()
