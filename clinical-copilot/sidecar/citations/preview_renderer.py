"""Render a single page of a Portable Document Format (PDF) with a
translucent overlay around the cited bounding box.

Two paths:

- **Native bounding box.** The Vision Language Model returned
  normalized ``BoundingBox`` coordinates at extraction time. We
  rasterize the page at 200 dots per inch (DPI) and draw a translucent
  rectangle scaled from the normalized coordinates.
- **Fuzzy quote fallback.** When the bounding box is missing (poor
  scan, model regression), ``fuzzy_quote_search`` uses PyMuPDF's
  ``page.search_for`` to locate the quote text on the page and returns
  its rectangle. The renderer then overlays that rectangle.

Why translucent rather than solid:

- A solid box hides the text it is meant to highlight. The clinician
  needs to read the value; the overlay's job is to draw the eye to it.
- Alpha is 64/255 (~25% opacity) and the color is the OpenEMR brand
  orange.

Performance:

- Rendering takes ~150 ms on a developer laptop for a typical lab PDF
  page. We render on demand (not at extraction time) because the
  citation count per encounter is small and the bytes are cheap to
  cache at the Hypertext Transfer Protocol (HTTP) layer.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Final

from sidecar.schemas.w2.bbox import BoundingBox


logger = logging.getLogger(__name__)


PREVIEW_DPI: Final[int] = 200
OVERLAY_RGBA: Final[tuple[int, int, int, int]] = (255, 140, 0, 96)
OVERLAY_BORDER_RGBA: Final[tuple[int, int, int, int]] = (255, 140, 0, 230)
OVERLAY_BORDER_WIDTH: Final[int] = 4


class PreviewRenderError(Exception):
    """The preview could not be produced.

    Always carries a clear cause (no PDF source, page index out of
    range, no anchor to highlight). The endpoint maps to a 404 or 500
    based on which case fired.
    """


@dataclass(frozen=True)
class PixelRect:
    """A rectangle in the rasterized image's pixel space."""

    x0: int
    y0: int
    x1: int
    y1: int

    def is_empty(self) -> bool:
        return self.x1 <= self.x0 or self.y1 <= self.y0


def render_bbox_overlay(
    *,
    pdf_bytes: bytes,
    page_index: int,
    bbox: BoundingBox | None,
    quote_fallback: str | None,
    dpi: int = PREVIEW_DPI,
) -> bytes:
    """Render the indicated page with the cited region highlighted.

    ``bbox`` takes precedence; ``quote_fallback`` is used when the bbox
    is None. If both are missing, raises ``PreviewRenderError`` so the
    endpoint never serves a meaningless plain page render (a clinician
    seeing the page with no highlight would not know where to look).
    """
    if not pdf_bytes:
        raise PreviewRenderError("preview renderer received zero bytes")
    if page_index < 0:
        raise PreviewRenderError(f"page_index {page_index} is negative")
    if bbox is None and not (quote_fallback and quote_fallback.strip()):
        raise PreviewRenderError(
            "preview cannot be rendered: neither a bounding box nor a "
            "fuzzy-quote anchor is available. The Vision Language Model "
            "did not return either anchor; reject the citation upstream."
        )

    try:
        import fitz
    except ImportError as exc:
        raise PreviewRenderError(
            "PyMuPDF (fitz) is not installed; install the w2_ingest extra. "
            "Underlying ImportError: " + repr(exc)
        ) from exc

    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise PreviewRenderError(
            "Pillow is not installed; install the w2_ingest extra. "
            "Underlying ImportError: " + repr(exc)
        ) from exc

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise PreviewRenderError(
            f"PyMuPDF failed to open the PDF: {type(exc).__name__}: {exc!s}"
        ) from exc

    try:
        if page_index >= len(doc):
            raise PreviewRenderError(
                f"page_index={page_index} but document has {len(doc)} page(s)"
            )

        page = doc[page_index]
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pixmap = page.get_pixmap(matrix=matrix, alpha=False)
        page_png = pixmap.tobytes(output="png")
        image = Image.open(io.BytesIO(page_png)).convert("RGBA")
        width, height = image.size

        if bbox is not None:
            rect = bbox_to_pixel_rect(
                bbox=bbox, width_px=width, height_px=height
            )
        else:
            rect = fuzzy_quote_search(
                page=page, quote=quote_fallback or "", dpi=dpi
            )

        if rect is None or rect.is_empty():
            raise PreviewRenderError(
                "preview anchor could not be resolved on the page; the "
                "fuzzy quote did not match and no bbox was provided."
            )

        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        draw.rectangle((rect.x0, rect.y0, rect.x1, rect.y1), fill=OVERLAY_RGBA)
        draw.rectangle(
            (rect.x0, rect.y0, rect.x1, rect.y1),
            outline=OVERLAY_BORDER_RGBA,
            width=OVERLAY_BORDER_WIDTH,
        )

        composited = Image.alpha_composite(image, overlay)
        out = io.BytesIO()
        composited.convert("RGB").save(out, format="PNG", optimize=True)
        return out.getvalue()
    finally:
        doc.close()


def bbox_to_pixel_rect(
    *,
    bbox: BoundingBox,
    width_px: int,
    height_px: int,
) -> PixelRect:
    """Convert a normalized bounding box to a pixel rectangle.

    The schema enforces ``0 <= x0 < x1 <= 1`` and the same for y, so
    the multiplication-then-clamp produces a valid rect.
    """
    x0 = max(0, min(width_px, int(round(bbox.x0 * width_px))))
    y0 = max(0, min(height_px, int(round(bbox.y0 * height_px))))
    x1 = max(0, min(width_px, int(round(bbox.x1 * width_px))))
    y1 = max(0, min(height_px, int(round(bbox.y1 * height_px))))
    return PixelRect(x0=x0, y0=y0, x1=x1, y1=y1)


def fuzzy_quote_search(
    *,
    page,  # PyMuPDF Page; typed loose because fitz is optional
    quote: str,
    dpi: int = PREVIEW_DPI,
) -> PixelRect | None:
    """Locate ``quote`` on the page; return its pixel rectangle.

    Falls back to a substring search when ``page.search_for`` returns
    nothing (the quote uses different whitespace or punctuation than
    the rendered text). Returns ``None`` when no match.
    """
    if not quote.strip():
        return None

    needle = quote.strip()
    rects = page.search_for(needle, quads=False)
    if not rects:
        # Trim the needle to the first 60 characters; a long quote is
        # rarely an exact match.
        rects = page.search_for(needle[:60], quads=False)
    if not rects:
        return None

    zoom = dpi / 72.0
    rect = rects[0]
    return PixelRect(
        x0=int(round(rect.x0 * zoom)),
        y0=int(round(rect.y0 * zoom)),
        x1=int(round(rect.x1 * zoom)),
        y1=int(round(rect.y1 * zoom)),
    )


__all__ = [
    "OVERLAY_BORDER_RGBA",
    "OVERLAY_BORDER_WIDTH",
    "OVERLAY_RGBA",
    "PREVIEW_DPI",
    "PixelRect",
    "PreviewRenderError",
    "bbox_to_pixel_rect",
    "fuzzy_quote_search",
    "render_bbox_overlay",
]
