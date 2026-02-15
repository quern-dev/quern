"""UI element parsing, search helpers, and screen summary generation.

Maps idb describe-all output (flat JSON array) to UIElement models and
provides search/filter functions for the tap-element workflow.
"""

from __future__ import annotations

import logging
import time
from collections import Counter

from server.models import UIElement

logger = logging.getLogger("quern-debug-server.device")


def parse_elements(raw: list[dict], filter_label: str | None = None,
                   filter_identifier: str | None = None,
                   filter_type: str | None = None) -> list[UIElement]:
    """Map idb describe-all output dicts to UIElement models with optional filtering.

    Handles the field names from real idb output:
    - AXLabel → label
    - AXUniqueId → identifier
    - AXValue → value
    - frame → frame (already a dict with x/y/width/height)

    Performance optimization: When search criteria are provided, only elements matching
    the criteria are parsed (avoids expensive Pydantic validation for irrelevant elements).

    Args:
        raw: List of raw element dicts from idb
        filter_label: If provided, only parse elements with this exact label (case-insensitive)
        filter_identifier: If provided, only parse elements with this exact identifier
        filter_type: If provided, only parse elements with this exact type (case-insensitive)

    Returns:
        List of parsed UIElement objects (only matching elements if filters provided)
    """
    start = time.perf_counter()
    has_filters = filter_label or filter_identifier or filter_type
    logger.info(f"[PERF] parse_elements START: {len(raw)} raw items, filters={'yes' if has_filters else 'no'}")

    elements: list[UIElement] = []
    parsed_count = 0
    skipped_count = 0

    # Pre-compute lowercase versions for case-insensitive matching
    filter_label_lower = filter_label.lower() if filter_label else None
    filter_type_lower = filter_type.lower() if filter_type else None

    for item in raw:
        # Fast string checks before expensive parsing
        if filter_identifier:
            item_id = item.get("AXUniqueId")
            if item_id != filter_identifier:
                skipped_count += 1
                continue  # Skip - identifier doesn't match

        if filter_label_lower:
            item_label = item.get("AXLabel") or ""
            if item_label.lower() != filter_label_lower:
                skipped_count += 1
                continue  # Skip - label doesn't match

        if filter_type_lower:
            item_type = item.get("type") or ""
            if item_type.lower() != filter_type_lower:
                skipped_count += 1
                continue  # Skip - type doesn't match

        # Element matches filters - proceed with full parsing
        frame = item.get("frame")
        if isinstance(frame, dict):
            frame = {
                "x": float(frame.get("x", 0)),
                "y": float(frame.get("y", 0)),
                "width": float(frame.get("width", 0)),
                "height": float(frame.get("height", 0)),
            }
        else:
            frame = None

        # Handle None values explicitly for required string fields
        type_val = item.get("type")
        if type_val is None or type_val == "":
            type_val = "Unknown"

        role_val = item.get("role")
        if role_val is None:
            role_val = ""

        role_desc_val = item.get("role_description")
        if role_desc_val is None:
            role_desc_val = ""

        custom_actions_val = item.get("custom_actions")
        if custom_actions_val is None:
            custom_actions_val = []

        elements.append(UIElement(
            type=type_val,
            label=item.get("AXLabel") or "",
            identifier=item.get("AXUniqueId"),
            value=str(v) if (v := item.get("AXValue")) is not None else None,
            frame=frame,
            enabled=item.get("enabled", True),
            role=role_val,
            role_description=role_desc_val,
            help=item.get("help"),
            custom_actions=custom_actions_val,
        ))
        parsed_count += 1

    end = time.perf_counter()
    logger.info(f"[PERF] parse_elements COMPLETE: {(end-start)*1000:.1f}ms, parsed={parsed_count}, skipped={skipped_count}")

    return elements


def find_by_label(elements: list[UIElement], label: str) -> list[UIElement]:
    """Find elements by exact case-insensitive label match.

    No substring matching — avoids 'Mail' matching 'Voicemail'.
    """
    lower = label.lower()
    return [e for e in elements if e.label.lower() == lower]


def find_by_identifier(elements: list[UIElement], identifier: str) -> list[UIElement]:
    """Find elements by exact identifier match (case-sensitive)."""
    return [e for e in elements if e.identifier == identifier]


def find_by_type(elements: list[UIElement], element_type: str) -> list[UIElement]:
    """Find elements by exact case-insensitive type match."""
    lower = element_type.lower()
    return [e for e in elements if e.type.lower() == lower]


def find_element(
    elements: list[UIElement],
    label: str | None = None,
    identifier: str | None = None,
    element_type: str | None = None,
) -> list[UIElement]:
    """Find elements matching label/identifier/type filters.

    Combines filters with AND logic. At least one of label or identifier required.
    Returns list of matching elements (may be empty).

    Args:
        elements: List of UI elements to search
        label: Exact case-insensitive label match
        identifier: Exact case-sensitive identifier match
        element_type: Exact case-insensitive type match (narrows results)

    Returns:
        List of matching elements (empty if no matches)
    """
    # Start with label or identifier search
    if label:
        matches = find_by_label(elements, label)
    elif identifier:
        matches = find_by_identifier(elements, identifier)
    else:
        # No search criteria provided
        return []

    # Optional type filter to narrow results
    if element_type and matches:
        matches = find_by_type(matches, element_type)

    return matches


def get_center(element: UIElement) -> tuple[float, float]:
    """Calculate center point from an element's frame.

    Returns (x, y) center coordinates.
    Raises ValueError if element has no frame.
    """
    if element.frame is None:
        raise ValueError(f"Element '{element.label or element.type}' has no frame")
    x = element.frame["x"] + element.frame["width"] / 2
    y = element.frame["y"] + element.frame["height"] / 2
    return round(x, 2), round(y, 2)


# Element types considered interactive for screen summaries
_INTERACTIVE_TYPES = {"button", "textfield", "switch", "slider", "link", "searchfield"}


def _is_navigation_chrome(el: UIElement) -> bool:
    """Detect tab bars, nav bars, back buttons, toolbars.

    Uses type-based heuristic to identify navigation chrome that should
    always be included in summaries regardless of truncation limits.
    """
    el_type_lower = el.type.lower()
    # Common navigation types
    if el_type_lower in {"tabbar", "navigationbar", "toolbar", "navbar"}:
        return True
    # Back buttons and nav items
    if "back" in el.label.lower() and el_type_lower == "button":
        return True
    # Tab bar items
    if "tab" in el_type_lower:
        return True
    return False


def _prioritize_element(el: UIElement) -> int:
    """Assign priority score for truncation.

    Priority levels (higher = more important):
    - 60: Buttons with identifiers (primary actions)
    - 40: Form inputs (text fields, switches, pickers)
    - 20: Buttons without identifiers
    - 5: Static text/labels
    """
    el_type_lower = el.type.lower()

    # Buttons with identifiers = primary actions
    if el_type_lower == "button" and el.identifier:
        return 60

    # Form inputs
    if el_type_lower in {"textfield", "switch", "slider", "searchfield", "picker"}:
        return 40

    # Buttons without identifiers
    if el_type_lower == "button":
        return 20

    # Everything else (static text, labels)
    return 5


def generate_screen_summary(elements: list[UIElement], max_elements: int = 20) -> dict:
    """Generate a template-based LLM-optimized screen description with smart truncation.

    Args:
        elements: List of UI elements to summarize
        max_elements: Maximum interactive elements to include (0 = unlimited)

    Returns a dict with:
    - summary: human-readable prose
    - element_count: total elements
    - element_types: {type: count}
    - interactive_elements: list of interactive element dicts
    - truncated: bool - whether the list was truncated
    - total_interactive_elements: int - total count before truncation
    - max_elements: int - the limit that was applied
    """
    type_counts: Counter[str] = Counter()
    all_interactive: list[tuple[UIElement, dict, int]] = []  # (element, dict, priority)
    navigation_chrome: list[dict] = []

    for el in elements:
        type_counts[el.type] += 1

        # Check if this is navigation chrome
        if _is_navigation_chrome(el):
            entry: dict = {"type": el.type, "label": el.label}
            if el.identifier:
                entry["identifier"] = el.identifier
            if el.value:
                entry["value"] = el.value
            navigation_chrome.append(entry)
            continue

        # Check if interactive
        if el.type.lower() in _INTERACTIVE_TYPES:
            entry = {"type": el.type, "label": el.label}
            if el.identifier:
                entry["identifier"] = el.identifier
            if el.value:
                entry["value"] = el.value
            priority = _prioritize_element(el)
            all_interactive.append((el, entry, priority))

    # Track counts before truncation
    total_interactive = len(all_interactive)

    # Apply smart truncation if max_elements > 0
    truncated = False
    if max_elements > 0 and len(all_interactive) > max_elements:
        # Sort by priority (descending), take top N
        all_interactive.sort(key=lambda x: x[2], reverse=True)
        all_interactive = all_interactive[:max_elements]
        truncated = True

    # Extract just the dicts from the tuples
    interactive = [entry for _, entry, _ in all_interactive]

    # Append navigation chrome (not counted against limit)
    interactive.extend(navigation_chrome)

    # Build prose summary
    parts: list[str] = []

    # Identify app context from Application element
    app_elements = [e for e in elements if e.type == "Application"]
    if app_elements and app_elements[0].label.strip():
        parts.append(f"{app_elements[0].label.strip()} screen")
    else:
        parts.append("Screen")

    # Count description
    type_desc = []
    for t, count in type_counts.most_common():
        if t == "Application":
            continue
        type_desc.append(f"{count} {t.lower()}{'s' if count > 1 else ''}")
    if type_desc:
        parts[0] += f" with {', '.join(type_desc[:4])}"
        if len(type_desc) > 4:
            parts[0] += f", and {len(type_desc) - 4} more type(s)"
    parts[0] += "."

    # List interactive element labels
    labeled = [e["label"] for e in interactive if e["label"]]
    if labeled:
        parts.append(f"Interactive elements: {', '.join(labeled[:15])}")
        if len(labeled) > 15:
            parts[-1] += f", and {len(labeled) - 15} more"
        parts[-1] += "."

    # Note any elements with values (e.g. search bar text, slider position)
    valued = [(e["label"] or e["type"], e["value"]) for e in interactive if e.get("value")]
    if valued:
        val_strs = [f"{name}: '{val}'" for name, val in valued[:5]]
        parts.append(f"Values: {', '.join(val_strs)}.")

    summary = " ".join(parts)

    return {
        "summary": summary,
        "element_count": len(elements),
        "element_types": dict(type_counts),
        "interactive_elements": interactive,
        "truncated": truncated,
        "total_interactive_elements": total_interactive,
        "max_elements": max_elements,
    }


# ---------------------------------------------------------------------------
# Hierarchy queries (Phase 4b-delta)
# ---------------------------------------------------------------------------


def _find_node(tree: list[dict], identifier: str | None, label: str | None) -> dict | None:
    """Recursively search nested tree for a node matching identifier or label."""
    for node in tree:
        # Check identifier (case-sensitive)
        if identifier and (node.get("AXUniqueId") == identifier):
            return node
        # Check label (case-insensitive)
        node_label = node.get("AXLabel") or ""
        if label and node_label.lower() == label.lower():
            return node
        # Recurse into children
        children = node.get("children", [])
        if children:
            found = _find_node(children, identifier, label)
            if found:
                return found
    return None


def _flatten_children(nodes: list[dict]) -> list[dict]:
    """Flatten nested tree WITHOUT mutating input."""
    result: list[dict] = []
    for node in nodes:
        # Copy without children key to avoid mutation
        flat_node = {k: v for k, v in node.items() if k != "children"}
        result.append(flat_node)
        children = node.get("children", [])
        if children:
            result.extend(_flatten_children(children))
    return result


def find_children_of(
    nested_tree: list[dict],
    parent_identifier: str | None = None,
    parent_label: str | None = None,
) -> list[dict]:
    """Find all descendants of a specific parent in the nested tree.

    Searches by identifier first, falls back to label (case-insensitive).
    Returns flat list of raw dicts (not UIElements). Empty if parent not found.
    """
    parent = _find_node(nested_tree, identifier=parent_identifier, label=parent_label)
    if parent is None:
        return []
    children = parent.get("children", [])
    if not children:
        return []
    return _flatten_children(children)
