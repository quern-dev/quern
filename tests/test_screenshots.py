"""Tests for screenshot annotation."""

from __future__ import annotations

import io

from PIL import Image

from server.device.screenshots import annotate_screenshot
from server.models import UIElement


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_test_png(width: int = 400, height: int = 800) -> bytes:
    """Create a minimal valid PNG image for testing."""
    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _button(label: str, x: float, y: float, w: float = 80, h: float = 44) -> UIElement:
    return UIElement(
        type="Button",
        label=label,
        frame={"x": x, "y": y, "width": w, "height": h},
    )


# ---------------------------------------------------------------------------
# annotate_screenshot
# ---------------------------------------------------------------------------


class TestAnnotateScreenshot:
    def test_returns_valid_png(self):
        raw = _make_test_png()
        elements = [_button("OK", 100, 200)]
        result, media_type = annotate_screenshot(raw, elements, scale=1.0)
        assert media_type == "image/png"
        img = Image.open(io.BytesIO(result))
        assert img.format == "PNG"
        assert img.size == (400, 800)

    def test_scales_output(self):
        raw = _make_test_png(400, 800)
        elements = [_button("OK", 100, 200)]
        result, _ = annotate_screenshot(raw, elements, scale=0.5)
        img = Image.open(io.BytesIO(result))
        assert img.size == (200, 400)

    def test_skips_non_interactive_types(self):
        raw = _make_test_png()
        elements = [
            UIElement(type="StaticText", label="Hello", frame={"x": 0, "y": 0, "width": 100, "height": 20}),
            UIElement(type="Application", label="App", frame={"x": 0, "y": 0, "width": 400, "height": 800}),
        ]
        # Should not raise — just produces unmodified (scaled) image
        result, media_type = annotate_screenshot(raw, elements, scale=1.0)
        assert media_type == "image/png"

    def test_skips_elements_without_frame(self):
        raw = _make_test_png()
        elements = [UIElement(type="Button", label="NoFrame")]
        result, _ = annotate_screenshot(raw, elements, scale=1.0)
        img = Image.open(io.BytesIO(result))
        assert img.format == "PNG"

    def test_multiple_buttons_annotated(self):
        raw = _make_test_png()
        elements = [
            _button("Save", 50, 100),
            _button("Cancel", 200, 100),
            UIElement(type="TextField", label="Name", frame={"x": 50, "y": 200, "width": 300, "height": 40}),
        ]
        result, _ = annotate_screenshot(raw, elements, scale=1.0)
        img = Image.open(io.BytesIO(result))
        assert img.format == "PNG"

    def test_empty_elements(self):
        raw = _make_test_png()
        result, media_type = annotate_screenshot(raw, [], scale=1.0)
        assert media_type == "image/png"
        img = Image.open(io.BytesIO(result))
        assert img.size == (400, 800)
