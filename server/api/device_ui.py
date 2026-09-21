"""API routes for device UI automation (idb-dependent)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Coroutine
from typing import Any, TypeVar

from fastapi import APIRouter, HTTPException, Query, Request

from server.api.actions import action as _action
from server.api.actions import logged_action
from server.api.device import (
    _capture_action_screenshot,
    _capture_screen_context,
    _get_controller,
    _handle_device_error,
)
from server.device.controller import DeviceController
from server.device.landmarks import needs_page_urls
from server.models import (
    ClearTextRequest,
    DeviceError,
    PressButtonRequest,
    RestoreInputRequest,
    ScrollToElementRequest,
    SwipeRequest,
    TapElementRequest,
    TapRequest,
    TypeTextRequest,
    WaitForElementRequest,
    WaitSettledRequest,
    WebContentRequest,
)

logger = logging.getLogger(__name__)

def _with_input_warning(
    controller: DeviceController, udid: str | None, payload: dict,
) -> dict:
    """Attach the Device Hub advisory to a write that may have gone nowhere.

    Suppressed input is accepted and discarded, so every one of these handlers
    returns `{"status": "ok"}` for a tap the device never saw. The controller
    already knows -- it just logs it server-side, where the caller is not
    looking. Carrying it on the response puts the explanation on the call that
    is failing.

    Never overwrites an existing `warning`: a handler that already has
    something to say about this call is more specific than this is.
    """
    if not udid:
        return payload
    warning = controller.input_warning(udid)
    if warning and "warning" not in payload:
        payload["warning"] = warning
    return payload



router = APIRouter(prefix="/api/v1/device", tags=["device"])


@router.get("/ui")
async def get_ui_elements(
    request: Request,
    udid: str | None = Query(default=None),
    children_of: str | None = Query(
        default=None,
        description="Only return children of the element with this identifier or label",
    ),
    snapshot_depth: int | None = Query(
        default=None, ge=1, le=50,
        description=(
            "WDA accessibility tree depth (1-50, default 10). "
            "Only affects physical devices."
        ),
    ),
    strategy: str | None = Query(
        default=None,
        description=(
            "Use 'skeleton' to skip /source timeout on "
            "complex screens. Physical devices only."
        ),
    ),
    source_timeout: float | None = Query(
        default=None, ge=1, le=60,
        description="Override WDA /source timeout in seconds. Physical devices only.",
    ),
    mode: str | None = Query(
        default=None, pattern=r"^(flat)$",
        description="'flat' uses flat idb output with custom companion. Default uses nested.",
    ),
    include_raw: bool = Query(
        default=False,
        description=(
            "Include the raw source attributes (extra_attrs) from the underlying "
            "accessibility provider on each element. Useful for debugging the "
            "normalizer when you want to see what got collapsed (e.g., Android "
            "selected= or checkable= attributes that map into our value field). "
            "Stripped by default to keep payloads small. Currently only Android "
            "populates extra_attrs; iOS responses are unchanged either way."
        ),
    ),
):
    """Get all UI accessibility elements from the current screen.

    Optionally scope to children of a specific element using the `children_of` parameter.
    """
    controller = _get_controller(request)
    with _action("get_ui_tree", category="device.read") as act:
        act.detail = f"mode={mode}" + (f" children_of={children_of}" if children_of else "")
        try:
            if strategy == "skeleton":
                resolved_udid = await controller.resolve_udid(udid)
                if controller._is_physical(resolved_udid):
                    raw = await controller.wda_client.build_screen_skeleton(resolved_udid)
                    from server.device.ui_elements import parse_elements
                    elements = parse_elements(raw)
                else:
                    elements, resolved_udid = await controller.get_ui_elements(
                        udid=udid, snapshot_depth=snapshot_depth,
                        source_timeout=source_timeout, mode=mode,
                    )
            elif children_of:
                elements, resolved_udid = await controller.get_ui_elements_children_of(
                    children_of=children_of, udid=udid, snapshot_depth=snapshot_depth,
                )
            else:
                elements, resolved_udid = await controller.get_ui_elements(
                    udid=udid, snapshot_depth=snapshot_depth,
                    source_timeout=source_timeout, mode=mode,
                )

            act.udid = resolved_udid
            act.detail += f", {len(elements)} elements"
            dump_kwargs = {} if include_raw else {"exclude": {"extra_attrs"}}
            return {
                "elements": [e.model_dump(**dump_kwargs) for e in elements],
                "element_count": len(elements),
                "udid": resolved_udid,
            }
        except DeviceError as e:
            raise _handle_device_error(e)


@router.get("/ui/element")
@logged_action("get_element", category="device.read")
async def get_element(
    request: Request,
    label: str | None = Query(default=None),
    label_contains: str | None = Query(default=None),
    label_prefix: str | None = Query(default=None),
    identifier: str | None = Query(default=None),
    element_type: str | None = Query(default=None, alias="type"),
    udid: str | None = Query(default=None),
):
    """Get a single element's state without fetching the entire UI tree.

    Query params:
    - label: Element label (case-insensitive exact match)
    - label_contains: Substring match on label (case-insensitive)
    - label_prefix: Prefix match on label (case-insensitive)
    - identifier: Element identifier (case-sensitive)
    - type: Element type to narrow results (optional)
    - udid: Device UDID (auto-resolves if omitted)

    Only one of label, label_contains, or label_prefix may be provided.

    Returns:
    - 200 with element dict (includes match_count if ambiguous)
    - 404 if no element found
    """
    label_params = [p for p in (label, label_contains, label_prefix) if p is not None]
    if len(label_params) > 1:
        raise HTTPException(
            status_code=400,
            detail="Only one of label, label_contains, or label_prefix may be provided",
        )

    controller = _get_controller(request)
    try:
        element, resolved_udid = await controller.get_element(
            label=label,
            label_contains=label_contains,
            label_prefix=label_prefix,
            identifier=identifier,
            element_type=element_type,
            udid=udid,
        )
        return {"element": element, "udid": resolved_udid}
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/ui/wait-for-element")
async def wait_for_element(request: Request, body: WaitForElementRequest):
    """Wait for an element to satisfy a condition (server-side polling).

    Always returns 200 with matched field to distinguish success/timeout.
    Only non-200 responses are validation errors (400) or server errors (500/503).

    Request body:
    - label or identifier: Element search criteria (at least one required)
    - type: Optional element type to narrow results
    - condition: Condition to wait for (exists, enabled, value_equals, etc.)
    - value: Required for value_* conditions
    - timeout: Max wait time in seconds (default 10, max 60)
    - interval: Poll interval in seconds (default 0.5)
    - udid: Device UDID (auto-resolves if omitted)

    Response:
    - matched: bool - whether condition was satisfied
    - element: dict | None - element state if matched
    - last_state: dict | None - last seen state if timeout
    - elapsed_seconds: float - time spent polling
    - polls: int - number of polls performed
    """
    controller = _get_controller(request)

    # Validation
    if body.timeout > 60:
        raise HTTPException(status_code=400, detail="Timeout cannot exceed 60 seconds")

    if body.condition in ("value_equals", "value_contains") and body.value is None:
        raise HTTPException(
            status_code=400,
            detail=f"Condition '{body.condition}' requires a value parameter",
        )

    with _action("wait_for_element", category="device.read") as act:
        act.detail = f"{body.condition} timeout={body.timeout}s"
        try:
            result, resolved_udid = await controller.wait_for_element(
                condition=body.condition,
                label=body.label,
                label_contains=body.label_contains,
                label_prefix=body.label_prefix,
                identifier=body.identifier,
                element_type=body.element_type,
                value=body.value,
                timeout=body.timeout,
                interval=body.interval,
                udid=body.udid,
                mode=body.mode,
            )
            result["udid"] = resolved_udid
            act.udid = resolved_udid
            # A wait that timed out is not a failure -- the caller asked
            # whether the condition held within a window, and "no" is an
            # answer. But it is the answer worth finding in a trace.
            if not result.get("matched"):
                act.outcome = "not_found"
            return result
        except DeviceError as e:
            raise _handle_device_error(e)


@router.get("/screen-summary")
@logged_action("get_screen_summary", category="device.read")
async def get_screen_summary(
    request: Request,
    max_elements: int = Query(default=20, ge=0, le=500),
    udid: str | None = Query(default=None),
    snapshot_depth: int | None = Query(
        default=None, ge=1, le=50,
        description=(
            "WDA accessibility tree depth (1-50, default 10). "
            "Only affects physical devices."
        ),
    ),
    strategy: str | None = Query(
        default=None,
        description=(
            "Use 'skeleton' to skip /source timeout on "
            "complex screens. Physical devices only."
        ),
    ),
    source_timeout: float | None = Query(
        default=None, ge=1, le=60,
        description=(
            "Override WDA /source timeout in seconds. "
            "Use for slow screens on older devices. Physical devices only."
        ),
    ),
    mode: str | None = Query(
        default=None, pattern=r"^(flat)$",
        description="'flat' uses flat idb output with custom companion. Default uses nested.",
    ),
    identify: bool = Query(
        default=False,
        description="Identify screen against loaded landmarks. Adds identified_as/confidence.",
    ),
):
    """Get an LLM-optimized screen description with smart truncation.

    Query params:
    - max_elements: Maximum interactive elements to include (0 = unlimited, default 20)
    - udid: Device UDID (auto-resolves if omitted)
    - snapshot_depth: WDA accessibility tree depth (1-50, default 10).
      Only affects physical devices.
    - strategy: 'skeleton' to skip /source timeout on complex screens (physical devices only)
    - source_timeout: Override WDA /source timeout in seconds (1-60). Physical devices only.
    - mode: 'flat' to use flat idb output with custom companion. Default uses nested.
    - identify: Match screen against loaded landmarks.

    Returns summary with truncated, total_interactive_elements fields.
    """
    controller = _get_controller(request)
    try:
        summary, elements, resolved_udid = await controller.get_screen_summary(
            max_elements=max_elements,
            udid=udid,
            snapshot_depth=snapshot_depth,
            strategy=strategy,
            source_timeout=source_timeout,
            mode=mode,
        )
        summary["udid"] = resolved_udid

        if identify:
            registry = request.app.state.landmark_registry
            # Only reach for the page listing when a loaded landmark needs it,
            # so a knowledge base with no URL landmarks costs nothing extra.
            page_urls = (
                await controller.web_page_urls(resolved_udid)
                if needs_page_urls(registry.all_screens()) else None
            )
            result = registry.identify(elements, page_urls=page_urls)
            summary["identified_as"] = result["matched"]
            summary["confidence"] = result["confidence"]

        return summary
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/ui/restore-input")
@logged_action("restore_input", category="device.lifecycle")
async def restore_input(request: Request, body: RestoreInputRequest):
    """Take a simulator's input services back from Xcode 27's Device Hub.

    Xcode 27 attaches a guest HID daemon to booted simulators, and backboardd
    answers by disconnecting the legacy touch, button and keyboard services --
    the ones quern drives. Taps and keystrokes are then accepted and discarded,
    so the device looks healthy and the screen never changes.

    **Restarts SpringBoard, so apps running on the simulator are killed.** That
    is why this is a call rather than something quern does silently; the one
    place it is automatic is a boot quern performed itself, where nothing is
    running yet.
    """
    from server.device import sim_input

    controller = _get_controller(request)
    try:
        udid = await controller.resolve_udid(body.udid)
        if controller._is_android(udid) or controller._is_physical(udid):
            # A 400, not a DeviceError: the mapper turns an unmatched
            # DeviceError into a 500, and asking a phone for a thing only
            # simulators have is the caller's mistake, not a server fault.
            raise HTTPException(
                status_code=400,
                detail="Only simulators have the legacy input services this restores.",
            )
        was_suppressed = await sim_input.legacy_input_is_suppressed(udid)
        await sim_input.restore_legacy_input(udid)
        controller._input_checked[udid] = True
        return {
            "status": "ok",
            "udid": udid,
            # False means the services were already the guest's, and this was a
            # SpringBoard restart for nothing -- worth saying rather than
            # reporting an indistinguishable success. None means the state
            # could not be read.
            "was_suppressed": was_suppressed,
            "detail": "SpringBoard was restarted; any running app was killed.",
        }
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/ui/tap")
async def tap(request: Request, body: TapRequest):
    """Tap at specific coordinates."""
    controller = _get_controller(request)
    with _action("tap") as act:
        act.detail = f"({body.x}, {body.y})"
        try:
            udid = await controller.tap(x=body.x, y=body.y, udid=body.udid)
            act.udid = udid
            return _with_input_warning(
                controller, udid,
                {"status": "ok", "udid": udid, "x": body.x, "y": body.y},
            )
        except DeviceError as e:
            raise _handle_device_error(e)


@router.post("/ui/tap-element")
async def tap_element(request: Request, body: TapElementRequest):
    """Find an element by label/identifier and tap its center.

    Returns:
    - 200 with status "ok" and tapped element info for single match
    - 200 with status "ambiguous" and match list for multiple matches
    - 404 when no element matches
    """
    controller = _get_controller(request)
    with _action("tap_element") as act:
        act.detail = body.label or body.identifier or body.label_contains or ""
        try:
            # Resolved up front so the action entry names the device the tap
            # actually went to, not the one the caller may have omitted.
            act.udid = await controller.resolve_udid(body.udid)
            resolved = act.udid
            if body.capture_screenshots:
                before = await _capture_action_screenshot(controller, resolved, "tap_before")

            # Guarded like scroll_to_element, and for the same reason: with
            # `scroll_to_find` on -- the default -- an off-screen target runs
            # the same sweep, and this is the path most callers reach it by.
            # Guarding only the dedicated scroll endpoint left the common one
            # unbounded.
            result = await _run_until_client_leaves(
                request,
                controller.tap_element(
                    label=body.label,
                    label_contains=body.label_contains,
                    label_prefix=body.label_prefix,
                    identifier=body.identifier,
                    element_type=body.element_type,
                    # `resolved`, not `body.udid`: each of these calls
                    # resolves the active device independently, so a
                    # concurrent request that changes it between them would
                    # let the tap, the screenshots and the advisory describe
                    # different devices (CodeRabbit, #250).
                    udid=resolved,
                    skip_stability_check=body.skip_stability_check,
                    source_timeout=body.source_timeout,
                    value=body.value,
                    scroll_to_find=body.scroll_to_find,
                ),
                what="tap_element",
            )

            # Element not found — return 404 with screen context. The 404 is
            # what tells _action this was `not_found` rather than a failure.
            if result.get("status") == "not_found":
                raise HTTPException(status_code=404, detail=result)

            if result.get("status") == "ambiguous":
                act.outcome = "ambiguous"

            if body.capture_screenshots:
                await asyncio.sleep(body.settle_delay)
                after = await _capture_action_screenshot(controller, resolved, "tap_after")
                result["screenshots"] = {"before": before, "after": after}

            if body.include_screen_context and result.get("status") not in (
                "not_found", "ambiguous",
            ):
                result["screen_context"] = await _capture_screen_context(controller, resolved)

            return _with_input_warning(controller, resolved, result)
        except DeviceError as e:
            raise _handle_device_error(e)


@router.post("/ui/web-content")
async def get_web_content(request: Request, body: WebContentRequest) -> dict:
    """Read web content that the accessibility tree cannot see.

    On iOS simulators a WKWebView is absent from the UI tree entirely, so a
    screen built around one appears to hold only its native chrome. This reads
    the page through the simulator's Web Inspector and returns its elements with
    real screen frames, so they can be tapped like any other element.
    """
    controller = _get_controller(request)
    with _action("get_web_content", category="device.read") as act:
        act.detail = body.bundle_id or ""
        try:
            # Matching is by page URL, so every loaded app's hints are equally
            # usable and the knowledge-base app name does not need to be known
            # here.
            registry = getattr(request.app.state, "landmark_registry", None)
            hints = registry.web_content() if registry is not None else None
            result = await controller.get_web_content(
                udid=body.udid, bundle_id=body.bundle_id, hints=hints,
            )
            act.detail += (
                f", anchored={result.get('anchored')}"
                f", {len(result.get('elements', []))} elements"
                f", probes={result.get('probes')}"
            )
            # `elapsed_ms` is part of this endpoint's response contract, so it
            # is read off the same clock the action entry uses rather than a
            # second one that could disagree with it.
            return {"status": "ok", "elapsed_ms": act.duration_ms, **result}
        except DeviceError as e:
            raise _handle_device_error(e)


@router.post("/ui/wait-settled")
async def wait_settled(request: Request, body: WaitSettledRequest) -> dict:
    """Wait until the screen stops changing.

    Answers "has drawing stopped", not "has content arrived": a blank page still
    loading is perfectly still and settles in under two seconds. Waiting out a
    slow load means waiting on the content first, then settling.

    Returns `settled: false` with a reason when the timeout expires, which means
    something is animating rather than loading — a spinner, a video, a carousel.
    """
    controller = _get_controller(request)
    with _action("wait_for_settle", category="device.read") as act:
        try:
            result = await controller.wait_for_settle(udid=body.udid, timeout=body.timeout)
            act.detail = f"settled={result['settled']}, frames={result['frames']}"
            # Not settling is an answer, not a failure: something is animating.
            if not result["settled"]:
                act.outcome = "not_found"
            return {"status": "ok", **result}
        except DeviceError as e:
            raise _handle_device_error(e)


@router.post("/ui/swipe")
async def swipe(request: Request, body: SwipeRequest):
    """Perform a swipe gesture."""
    controller = _get_controller(request)
    with _action("swipe") as act:
        act.detail = (
            f"({body.start_x}, {body.start_y}) -> ({body.end_x}, {body.end_y})"
        )
        try:
            udid = await controller.swipe(
                start_x=body.start_x,
                start_y=body.start_y,
                end_x=body.end_x,
                end_y=body.end_y,
                duration=body.duration,
                udid=body.udid,
            )
            act.udid = udid
            return _with_input_warning(
                controller, udid, {"status": "ok", "udid": udid},
            )
        except DeviceError as e:
            raise _handle_device_error(e)



#: What the wrapped coroutine returns, so a caller keeps its own type
#: rather than being handed Any back.
T = TypeVar("T")


async def _run_until_client_leaves(
    request: Request,
    coro: Coroutine[Any, Any, T],
    *,
    what: str,
    poll_s: float = 2.0,
) -> T:
    """Run a long device operation, and abandon it if the caller disconnects.

    Uvicorn does not cancel a handler when its client goes away, so a request
    the caller timed out of at 180s goes on driving the device. Measured on
    #84: sweeps of 413s and 523s continued against a client that had left,
    holding a simulator the next test was trying to use and, worse, swiping it
    while that test ran.

    The coroutine is run as a task and polled against `is_disconnected()`
    rather than wrapped in a timeout, because there is no single right timeout
    -- the operation's own deadline belongs to the operation. This only answers
    the narrower question of whether anyone is still listening.

    Cancellation is cooperative: the task stops at its next await. A device
    command already in flight completes, which is correct -- half-sending one
    is worse than finishing it.
    """
    task = asyncio.ensure_future(coro)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=poll_s)
            if done:
                return task.result()
            if await request.is_disconnected():
                task.cancel()
                logger.info(
                    "%s: client disconnected — cancelling rather than "
                    "continuing to drive the device", what,
                )
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                raise HTTPException(
                    status_code=499, detail=f"{what} cancelled: client disconnected",
                )
    except asyncio.CancelledError:
        # This coroutine was cancelled, not the client's connection. Cancel the
        # work and let the cancellation propagate rather than converting it into
        # a 499, which would report the caller as having disconnected when it
        # was the server shutting the request down.
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise
    finally:
        if not task.done():
            task.cancel()

@router.post("/ui/scroll-to-element")
@logged_action("scroll_to_element", category="device.action")
async def scroll_to_element(request: Request, body: ScrollToElementRequest):
    """Scroll a scrollable container until the target element is in view.

    Supported on Android and iOS (physical + simulator) via a bounded swipe
    loop. Returns 404 when the element never appears after scrolling.
    """
    controller = _get_controller(request)
    try:
        result = await _run_until_client_leaves(
            request,
            controller.scroll_to_element(
                label=body.label,
                identifier=body.identifier,
                udid=body.udid,
                max_swipes=body.max_swipes,
            ),
            what="scroll_to_element",
        )
        if result.get("status") == "not_found":
            raise HTTPException(status_code=404, detail=result)
        return result
    except DeviceError as e:
        raise _handle_device_error(e)


@router.post("/ui/type")
async def type_text(request: Request, body: TypeTextRequest):
    """Type text into the focused field."""
    controller = _get_controller(request)
    with _action("type_text") as act:
        # The text itself is deliberately not recorded: this is how passwords
        # get typed, and a trace is a thing people paste into bug reports.
        act.detail = f"{len(body.text)} chars"
        try:
            # Resolved once, then used for everything: the before screenshot
            # used to resolve separately from the typing, so a concurrent
            # request changing the active device produced a "before" image of
            # one device and text typed into another.
            resolved = await controller.resolve_udid(body.udid)
            if body.capture_screenshots:
                before = await _capture_action_screenshot(controller, resolved, "type_before")
            typed = await controller.type_text(
                text=body.text, udid=resolved,
                label=body.label, identifier=body.identifier,
            )
            udid = typed["udid"]
            act.udid = udid
            # Typing that did not take is the bug this field exists to make
            # visible -- the call succeeded and the field is still empty.
            if not typed["verified"]:
                act.outcome = "suspect"
                act.detail += ", unverified"
            result: dict = {"status": "ok", "udid": udid, "verified": typed["verified"]}
            if body.capture_screenshots:
                await asyncio.sleep(body.settle_delay)
                after = await _capture_action_screenshot(controller, udid, "type_after")
                result["screenshots"] = {"before": before, "after": after}
            if body.include_screen_context:
                result["screen_context"] = await _capture_screen_context(controller, udid)
            return _with_input_warning(controller, udid, result)
        except DeviceError as e:
            raise _handle_device_error(e)


@router.post("/ui/clear")
async def clear_text(request: Request, body: ClearTextRequest):
    """Clear a text field: triple-tap to select, then Backspace.

    Pass `label` or `identifier` to say which field. Without one this clears the
    first field that has a value, which is not the field the caller just tapped:
    on a sign-in form it finds the email field rather than the password one.
    Focus cannot be detected — the accessibility tree does not report it.
    """
    controller = _get_controller(request)
    with _action("clear_text") as act:
        act.detail = body.label or body.identifier or ""
        try:
            resolved = await controller.clear_text(
                udid=body.udid, label=body.label, identifier=body.identifier,
            )
            act.udid = resolved
            return _with_input_warning(
                controller, resolved, {"status": "ok", "udid": resolved},
            )
        except DeviceError as e:
            raise _handle_device_error(e)


@router.post("/ui/press")
async def press_button(request: Request, body: PressButtonRequest):
    """Press a hardware button."""
    controller = _get_controller(request)
    with _action("press_button") as act:
        act.detail = body.button
        try:
            udid = await controller.press_button(button=body.button, udid=body.udid)
            act.udid = udid
            return _with_input_warning(
                controller, udid, {"status": "ok", "udid": udid},
            )
        except DeviceError as e:
            raise _handle_device_error(e)
