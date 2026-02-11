"""UI element parsing, search helpers, and screen summary generation.

Maps idb describe-all output (flat JSON array) to UIElement models and
provides search/filter functions for the tap-element workflow.
"""

from __future__ import annotations

from collections import Counter

from server.models import UIElement


def parse_elements(raw: list[dict]) -> list[UIElement]:
    """Map idb describe-all output dicts to UIElement models.

    Handles the field names from real idb output:
    - AXLabel → label
    - AXUniqueId → identifier
    - AXValue → value
    - frame → frame (already a dict with x/y/width/height)
    """
    elements: list[UIElement] = []
    for item in raw:
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

        elements.append(UIElement(
            type=item.get("type", "Unknown"),
            label=item.get("AXLabel") or "",
            identifier=item.get("AXUniqueId"),
            value=item.get("AXValue"),
            frame=frame,
            enabled=item.get("enabled", True),
            role=item.get("role", ""),
            role_description=item.get("role_description", ""),
            help=item.get("help"),
            custom_actions=item.get("custom_actions", []),
        ))
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


def generate_screen_summary(elements: list[UIElement]) -> dict:
    """Generate a template-based LLM-optimized screen description.

    Returns a dict with:
    - summary: human-readable prose
    - element_count: total elements
    - element_types: {type: count}
    - interactive_elements: list of interactive element dicts
    """
    type_counts: Counter[str] = Counter()
    interactive: list[dict] = []

    for el in elements:
        type_counts[el.type] += 1
        if el.type.lower() in _INTERACTIVE_TYPES:
            entry: dict = {"type": el.type, "label": el.label}
            if el.identifier:
                entry["identifier"] = el.identifier
            if el.value:
                entry["value"] = el.value
            interactive.append(entry)

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
    }
