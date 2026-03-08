"""ElementSelector — chainable async query builder for WDA elements.

Provides a fluent DSL for finding and interacting with elements on physical
iOS devices via WebDriverAgent. Uses WdaBackend._request() under the hood,
so all queries get free session auto-recovery and typed error parsing.

Usage:
    # Find by accessibility id (fastest)
    await backend.element(udid, name="Login").tap()

    # Find by label + type
    el = await backend.element(udid, label="Submit", type="Button").get()

    # Scoped child query
    await backend.element(udid, type="NavigationBar").child(label="Back").tap()

    # Wait for element to appear
    await backend.element(udid, name="Success").wait(timeout=5)

    # Get all matches
    buttons = await backend.element(udid, type="Button").find()
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from server.models import WdaElementNotFoundError

if TYPE_CHECKING:
    from server.device.wda_client import WdaBackend

logger = logging.getLogger("quern-debug-server.wda-selector")


def _escape(val: str) -> str:
    """Escape single quotes for NSPredicate string literals."""
    return val.replace("'", "\\'")


def _build_query(
    *,
    name: str | None = None,
    label: str | None = None,
    type: str | None = None,
    predicate: str | None = None,
    class_chain: str | None = None,
) -> tuple[str, str]:
    """Build a (using, value) pair from selector criteria.

    Returns the most efficient WDA locator strategy for the given criteria.
    """
    # Explicit strategy overrides
    if class_chain:
        return "class chain", class_chain
    if predicate:
        return "predicate string", predicate

    xcui_type = f"XCUIElementType{type}" if type else None

    # Fastest: direct accessibility id lookup (only when name is the sole criterion)
    if name and not label and not xcui_type:
        return "accessibility id", name

    # Build NSPredicate string for combined criteria
    clauses: list[str] = []
    if name:
        clauses.append(f"name == '{_escape(name)}'")
    if label:
        clauses.append(f"label ==[c] '{_escape(label)}'")
    if xcui_type:
        clauses.append(f"type == '{xcui_type}'")

    if not clauses:
        raise ValueError("ElementSelector requires at least one criterion")

    return "predicate string", " AND ".join(clauses)


class ElementSelector:
    """Lazy, chainable query builder for WDA elements.

    Accumulates criteria and only hits WDA when a terminal operation is called
    (find, get, wait, tap, clear).
    """

    def __init__(
        self,
        backend: WdaBackend,
        udid: str,
        *,
        name: str | None = None,
        label: str | None = None,
        type: str | None = None,
        predicate: str | None = None,
        class_chain: str | None = None,
        _parent_element_id: str | None = None,
    ) -> None:
        self._backend = backend
        self._udid = udid
        self._name = name
        self._label = label
        self._type = type
        self._predicate = predicate
        self._class_chain = class_chain
        self._parent_element_id = _parent_element_id

    def child(
        self,
        *,
        name: str | None = None,
        label: str | None = None,
        type: str | None = None,
        predicate: str | None = None,
        class_chain: str | None = None,
    ) -> ElementSelector:
        """Create a scoped child selector that searches within this element's subtree.

        Requires that the parent element has a WDA element ID — call find()/get()
        on the parent first if you need to resolve it, or use this directly and
        the parent will be resolved automatically.
        """
        return _ChildSelector(
            backend=self._backend,
            udid=self._udid,
            parent=self,
            name=name,
            label=label,
            type=type,
            predicate=predicate,
            class_chain=class_chain,
        )

    async def find(self, *, timeout: float | None = None) -> list[dict]:
        """Execute the query and return all matching elements.

        Returns idb-format dicts. Empty list if no matches.
        """
        return await self._execute(timeout=timeout)

    async def get(self, *, timeout: float | None = None) -> dict:
        """Execute the query and return the first matching element.

        Raises WdaElementNotFoundError if no element matches.
        """
        results = await self._execute(timeout=timeout)
        if not results:
            using, value = self._build_query()
            raise WdaElementNotFoundError(
                f"No element found: {using}={value!r} on {self._udid[:8]}",
            )
        return results[0]

    async def wait(
        self, *, timeout: float = 5.0, interval: float = 0.5,
    ) -> dict:
        """Poll until at least one element matches, then return the first.

        Raises WdaElementNotFoundError if no match within timeout.
        """
        deadline = time.monotonic() + timeout
        last_results: list[dict] = []
        while True:
            last_results = await self._execute()
            if last_results:
                return last_results[0]
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(interval)

        using, value = self._build_query()
        raise WdaElementNotFoundError(
            f"Timed out waiting for element: {using}={value!r} on {self._udid[:8]} "
            f"(waited {timeout}s)",
        )

    async def tap(self, *, timeout: float | None = None) -> dict:
        """Find the first matching element and tap its center.

        Returns the tapped element dict. Raises WdaElementNotFoundError if not found.
        """
        el = await self.get(timeout=timeout)
        frame = el.get("frame", {})
        x = frame.get("x", 0) + frame.get("width", 0) / 2
        y = frame.get("y", 0) + frame.get("height", 0) / 2
        await self._backend.tap(self._udid, x, y)
        return el

    async def clear(self, *, timeout: float | None = None) -> dict:
        """Find the first matching element and clear its text.

        Uses WDA's native /element/{id}/clear endpoint. Returns the element dict.
        Raises WdaElementNotFoundError if not found.
        """
        el = await self.get(timeout=timeout)
        wda_id = el.get("_wda_element_id")
        if wda_id:
            from server.device.wda_client import ACTION_TIMEOUT
            await self._backend._request(
                "post", self._udid, f"/element/{wda_id}/clear",
                use_session=True, timeout=ACTION_TIMEOUT,
            )
        else:
            # Fallback: tap center then select all + delete
            frame = el.get("frame", {})
            x = frame.get("x", 0) + frame.get("width", 0) / 2
            y = frame.get("y", 0) + frame.get("height", 0) / 2
            await self._backend.select_all_and_delete(self._udid, x, y)
        return el

    def _build_query(self) -> tuple[str, str]:
        """Build the (using, value) pair for this selector."""
        return _build_query(
            name=self._name,
            label=self._label,
            type=self._type,
            predicate=self._predicate,
            class_chain=self._class_chain,
        )

    async def _execute(self, *, timeout: float | None = None) -> list[dict]:
        """Execute the query against WDA."""
        using, value = self._build_query()
        return await self._backend.find_elements_by_query(
            self._udid, using, value,
            scope_element_id=self._parent_element_id,
            timeout=timeout,
        )


class _ChildSelector(ElementSelector):
    """A selector scoped to a parent element's subtree.

    Resolves the parent element first (to get its WDA element ID), then
    queries children within that scope.
    """

    def __init__(
        self,
        backend: WdaBackend,
        udid: str,
        parent: ElementSelector,
        **kwargs,
    ) -> None:
        super().__init__(backend, udid, **kwargs)
        self._parent = parent

    async def _execute(self, *, timeout: float | None = None) -> list[dict]:
        """Resolve parent, then query children within its scope."""
        parent_el = await self._parent.get(timeout=timeout)
        parent_wda_id = parent_el.get("_wda_element_id")
        if not parent_wda_id:
            raise WdaElementNotFoundError(
                f"Parent element has no WDA element ID for scoped query on {self._udid[:8]}",
            )
        using, value = self._build_query()
        return await self._backend.find_elements_by_query(
            self._udid, using, value,
            scope_element_id=parent_wda_id,
            timeout=timeout,
        )
