"""Screenshot post-processing: scaling, format conversion, and annotation."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

from server.models import UIElement


def process_screenshot(
    raw_png: bytes,
    format: str = "png",
    scale: float = 0.5,
    quality: int = 85,
) -> tuple[bytes, str]:
    """Process a raw PNG screenshot: scale and optionally convert format.

    Args:
        raw_png: Raw PNG bytes from simctl screenshot.
        format: Output format — "png" or "jpeg".
        scale: Scale factor (0.1–1.0). Default 0.5 halves dimensions.
        quality: JPEG quality (1–100). Ignored for PNG.

    Returns:
        Tuple of (processed_bytes, media_type_string).
    """
    img = Image.open(io.BytesIO(raw_png))

    if scale != 1.0:
        new_w = max(1, int(img.width * scale))
        new_h = max(1, int(img.height * scale))
        img = img.resize((new_w, new_h), Image.LANCZOS)

    buf = io.BytesIO()
    fmt = format.upper()
    if fmt == "JPEG":
        # JPEG doesn't support alpha — convert RGBA → RGB
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=quality)
        media_type = "image/jpeg"
    else:
        img.save(buf, format="PNG")
        media_type = "image/png"

    return buf.getvalue(), media_type


# Interactive element types that get annotated with bounding boxes
_INTERACTIVE_TYPES = frozenset({
    "Button", "TextField", "SecureTextField", "Switch", "Slider",
    "Stepper", "Picker", "Link", "Tab", "MenuItem", "Toggle",
    "SearchField", "TextEditor", "DatePicker", "ColorWell",
})

# role_description values for container elements that should also be annotated
# (e.g. nav bars and tab bars whose child buttons aren't listed individually)
_INTERACTIVE_ROLES = frozenset({
    "Nav bar", "Tab bar", "Toolbar", "Navigation bar",
})


def _is_interactive(el: UIElement) -> bool:
    """Check if an element should be annotated."""
    if el.type in _INTERACTIVE_TYPES:
        return True
    if el.role_description in _INTERACTIVE_ROLES:
        return True
    # Tab bar groups often come through with label "Tab Bar"
    if el.type == "Group" and el.label and "tab bar" in el.label.lower():
        return True
    return False


def annotate_screenshot(
    raw_png: bytes,
    elements: list[UIElement],
    scale: float = 0.5,
    quality: int = 85,
) -> tuple[bytes, str]:
    """Draw red bounding boxes and labels on interactive elements.

    Args:
        raw_png: Raw PNG bytes from simctl screenshot.
        elements: Parsed UI accessibility elements.
        scale: Output scale factor (0.1–1.0).
        quality: Ignored (always PNG output for annotation clarity).

    Returns:
        Tuple of (annotated_png_bytes, "image/png").
    """
    img = Image.open(io.BytesIO(raw_png)).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Detect Retina scale factor: accessibility frames are in points,
    # but the screenshot is in pixels (e.g. 3x on iPhone 16 Pro).
    # Find the full-screen element (Application) to determine point width.
    point_width: float | None = None
    for el in elements:
        if el.type == "Application" and el.frame:
            point_width = el.frame["width"]
            break
    if point_width and point_width > 0:
        retina_scale = img.width / point_width
    else:
        retina_scale = 1.0

    font_size = max(12, int(14 * retina_scale))
    line_width = max(2, int(2 * retina_scale))
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
    except (OSError, IOError):
        font = ImageFont.load_default()

    for el in elements:
        if not _is_interactive(el):
            continue
        if not el.frame:
            continue

        # Scale point coordinates to pixel coordinates
        x = el.frame["x"] * retina_scale
        y = el.frame["y"] * retina_scale
        w = el.frame["width"] * retina_scale
        h = el.frame["height"] * retina_scale

        # Draw red bounding box
        draw.rectangle(
            [x, y, x + w, y + h],
            outline=(255, 0, 0, 200),
            width=line_width,
        )

        # Label text
        label_text = f"{el.type}"
        if el.label:
            label_text += f": {el.label}"

        # Draw label background + text above the box
        pad = int(4 * retina_scale)
        text_bbox = draw.textbbox((0, 0), label_text, font=font)
        text_w = text_bbox[2] - text_bbox[0]
        text_h = text_bbox[3] - text_bbox[1]
        label_y = max(0, y - text_h - pad)
        draw.rectangle(
            [x, label_y, x + text_w + pad, label_y + text_h + pad],
            fill=(255, 0, 0, 180),
        )
        draw.text((x + pad // 2, label_y + pad // 2), label_text, fill=(255, 255, 255, 255), font=font)

    # Composite overlay onto original
    img = Image.alpha_composite(img, overlay)
    img = img.convert("RGB")

    # Scale
    if scale != 1.0:
        new_w = max(1, int(img.width * scale))
        new_h = max(1, int(img.height * scale))
        img = img.resize((new_w, new_h), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), "image/png"
