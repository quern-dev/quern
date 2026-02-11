"""Screenshot post-processing: scaling and format conversion."""

from __future__ import annotations

import io

from PIL import Image


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
