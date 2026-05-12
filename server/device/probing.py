"""Shared accessibility-tree probing helpers.

Both idb and sim-bridge surface UI containers (tab bars, nav bars, toolbars)
whose `children` array is empty even though the container clearly has
interactive subviews visible on screen. The workaround is to issue a series
of hit-test queries across the container's bounds and treat each unique
result as a discovered child.

The probing logic here is backend-agnostic — it takes a `describe_point`
callable so the caller can plug in either `IdbBackend.describe_point`
(subprocess to `idb ui describe-point`) or
`SimBridgeBackend.describe_point` (AXPTranslator's `objectAtPoint:` via
the sim-bridge subprocess).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

# Container role_descriptions whose children are often missing from describe-all
PROBEABLE_ROLES = frozenset({
    "Nav bar", "Tab bar", "Toolbar", "Navigation bar",
})

# Probe interval in points — smaller than iOS minimum tap target (44pt)
PROBE_STEP = 20


def is_probeable_container(item: dict) -> bool:
    """Check if an item is an interactive container with no enumerated children."""
    children = item.get("children", [])
    if children:
        return False

    role_desc = item.get("role_description", "")
    if role_desc in PROBEABLE_ROLES:
        return True

    label = item.get("AXLabel") or ""
    if item.get("type") == "Group" and "tab bar" in label.lower():
        return True

    return False


def find_empty_containers(items: list[dict]) -> list[dict]:
    """Walk the nested tree and find containers that need probing."""
    containers: list[dict] = []
    for item in items:
        if is_probeable_container(item):
            containers.append(item)
        children = item.get("children", [])
        if children:
            containers.extend(find_empty_containers(children))
    return containers


def flatten_nested(items: list[dict]) -> list[dict]:
    """Flatten a nested element tree, mutating items to drop their children key."""
    result: list[dict] = []
    for item in items:
        children = item.pop("children", [])
        result.append(item)
        if children:
            result.extend(flatten_nested(children))
    return result


def _frame_key(frame: dict | None) -> tuple[int, int, int, int] | None:
    if not frame:
        return None
    return (
        int(frame.get("x", 0)), int(frame.get("y", 0)),
        int(frame.get("width", 0)), int(frame.get("height", 0)),
    )


DescribePointFn = Callable[[str, float, float], Awaitable[dict | None]]


async def probe_container(
    udid: str, container: dict, describe_point: DescribePointFn,
) -> list[dict]:
    """Probe a single container's interior to discover hidden children.

    Calls `describe_point` at a regular X interval across the container's
    width (at its vertical center) and returns deduplicated hits, excluding
    the container itself.
    """
    frame = container.get("frame")
    if not frame:
        return []

    x_start = float(frame.get("x", 0))
    width = float(frame.get("width", 0))
    y_center = float(frame.get("y", 0)) + float(frame.get("height", 0)) / 2

    probe_xs: list[float] = []
    x = x_start + PROBE_STEP / 2
    while x < x_start + width:
        probe_xs.append(x)
        x += PROBE_STEP
    if not probe_xs:
        return []

    results = await asyncio.gather(*(describe_point(udid, px, y_center) for px in probe_xs))

    container_key = _frame_key(frame)
    seen: set[tuple[int, int, int, int]] = set()
    discovered: list[dict] = []
    for element in results:
        if element is None:
            continue
        key = _frame_key(element.get("frame"))
        if key is None or key == container_key or key in seen:
            continue
        seen.add(key)
        discovered.append(element)
    return discovered


def merge_probed_into_flat(flat: list[dict], probed: list[dict]) -> None:
    """Append probed elements to `flat`, skipping ones whose frame already exists."""
    if not probed:
        return
    existing: set[tuple[int, int, int, int]] = set()
    for item in flat:
        key = _frame_key(item.get("frame"))
        if key is not None:
            existing.add(key)
    for el in probed:
        key = _frame_key(el.get("frame"))
        if key is None or key in existing:
            continue
        flat.append(el)
        existing.add(key)
