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


def _draw_grid(
    draw: ImageDraw.Draw,
    img_width: int,
    img_height: int,
    retina_scale: float,
    display_scale: float,
    spacing_pt: int,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    """Draw a coordinate grid overlay in point coordinates.

    Grid lines are drawn at every ``spacing_pt`` points with coordinate
    labels along the top and left edges.  Coordinates match the point
    system used by the ``tap`` tool so agents can read positions directly.
    """
    pt_width = img_width / retina_scale
    pt_height = img_height / retina_scale

    line_color = (128, 128, 128, 80)
    line_w = max(1, int(1 * display_scale))

    # Smaller font for grid labels
    grid_font_size = max(10, int(10 * display_scale))
    try:
        grid_font = ImageFont.truetype(
            "/System/Library/Fonts/Helvetica.ttc", grid_font_size,
        )
    except OSError:
        grid_font = font  # fallback to whatever was loaded

    # When spacing is very small, only label every Nth line to avoid overlap
    label_every = 1
    if spacing_pt < 25:
        label_every = max(1, 50 // spacing_pt)

    pad = max(2, int(2 * display_scale))
    label_bg = (0, 0, 0, 140)
    label_fg = (255, 255, 255, 220)

    # Vertical lines + top-edge labels
    idx = 0
    for pt in range(0, int(pt_width) + 1, spacing_pt):
        x_px = pt * retina_scale
        draw.line([(x_px, 0), (x_px, img_height)], fill=line_color, width=line_w)
        if idx % label_every == 0:
            text = str(pt)
            bbox = draw.textbbox((0, 0), text, font=grid_font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            lx = x_px + pad
            # Keep label on-screen
            if lx + tw + pad > img_width:
                lx = x_px - tw - pad * 2
            draw.rectangle([lx, pad, lx + tw + pad, pad + th + pad], fill=label_bg)
            draw.text((lx + pad // 2, pad + pad // 2), text, fill=label_fg, font=grid_font)
        idx += 1

    # Horizontal lines + left-edge labels
    idx = 0
    for pt in range(0, int(pt_height) + 1, spacing_pt):
        y_px = pt * retina_scale
        draw.line([(0, y_px), (img_width, y_px)], fill=line_color, width=line_w)
        if idx % label_every == 0:
            text = str(pt)
            bbox = draw.textbbox((0, 0), text, font=grid_font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            ly = y_px + pad
            # Keep label on-screen
            if ly + th + pad > img_height:
                ly = y_px - th - pad * 2
            draw.rectangle([pad, ly, pad + tw + pad, ly + th + pad], fill=label_bg)
            draw.text((pad + pad // 2, ly + pad // 2), text, fill=label_fg, font=grid_font)
        idx += 1


def annotate_screenshot(
    raw_png: bytes,
    elements: list[UIElement],
    scale: float = 0.5,
    quality: int = 85,
    grid: int | None = None,
) -> tuple[bytes, str]:
    """Draw red bounding boxes and labels on interactive elements.

    When no interactive elements are found, a coordinate grid is
    automatically overlaid so agents can still identify tap positions.
    Pass ``grid=<spacing>`` to force the grid, or ``grid=0`` to disable
    the auto-fallback.

    Args:
        raw_png: Raw PNG bytes from simctl screenshot.
        elements: Parsed UI accessibility elements.
        scale: Output scale factor (0.1–1.0).
        quality: Ignored (always PNG output for annotation clarity).
        grid: Grid spacing in points.  None = auto (grid when 0 elements),
              0 = off, positive int = forced grid at that spacing.

    Returns:
        Tuple of (annotated_png_bytes, "image/png").
    """
    img = Image.open(io.BytesIO(raw_png)).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    # Detect Retina scale factor: accessibility frames are in points,
    # but the screenshot is in pixels (e.g. 3x on iPhone 16 Pro).
    # Find the full-screen element (Application) to determine point width.
    # Android elements report in raw pixels, so retina_scale stays 1.0 but
    # we still need readable font sizes based on actual screen resolution.
    point_width: float | None = None
    for el in elements:
        if el.type == "Application" and el.frame:
            point_width = el.frame["width"]
            break
    if point_width and point_width > 0:
        retina_scale = img.width / point_width
    else:
        retina_scale = 1.0

    # Scale font and line width relative to screen resolution.
    # iOS at 3x retina: retina_scale ~3.0, font = 42px — readable.
    # Android at 1x (raw pixels): retina_scale 1.0, but 1080px wide needs
    # similar visual weight. Use screen width as a baseline.
    display_scale = max(retina_scale, img.width / 400)
    font_size = max(12, int(14 * display_scale))
    line_width = max(2, int(2 * display_scale))
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
    except OSError:
        font = ImageFont.load_default()

    drawn_count = 0
    for el in elements:
        if not _is_interactive(el):
            continue
        if not el.frame:
            continue

        drawn_count += 1

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
        pad = int(4 * display_scale)
        text_bbox = draw.textbbox((0, 0), label_text, font=font)
        text_w = text_bbox[2] - text_bbox[0]
        text_h = text_bbox[3] - text_bbox[1]
        label_y = max(0, y - text_h - pad)
        draw.rectangle(
            [x, label_y, x + text_w + pad, label_y + text_h + pad],
            fill=(255, 0, 0, 180),
        )
        draw.text(
            (x + pad // 2, label_y + pad // 2), label_text,
            fill=(255, 255, 255, 255), font=font,
        )

    # Coordinate grid: auto when no interactive elements, forced when grid > 0
    if grid is None and drawn_count == 0:
        _draw_grid(draw, img.width, img.height, retina_scale, display_scale, 50, font)
    elif grid is not None and grid > 0:
        _draw_grid(draw, img.width, img.height, retina_scale, display_scale, grid, font)

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
