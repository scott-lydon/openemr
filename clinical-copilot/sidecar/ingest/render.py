"""Multi-page Portable Document Format (PDF) rasterization and native
text extraction.

The Vision Language Model (VLM) extractor in
``sidecar/agents/intake_extractor.py`` calls this module once per
document to get:

- ``list[PageImage]`` — one Pillow image per page at the requested
  resolution. Used as the visual half of the multimodal extractor call.
- ``list[str]`` — the page's native (selectable) text when the document
  is born digital, or an empty string when the page is a pure scan. Used
  as the textual half of the extractor call so the model has both
  signals to reason over.

Why two halves:

- Pure VLM extraction works on scans but is unnecessarily expensive on
  born-digital PDFs where the text is already correct.
- Pure text extraction works on born-digital PDFs but produces nothing
  useful on scanned pages (no text layer).
- Sending both forces the VLM to reconcile them, which catches cases
  where the text layer disagrees with the rendered glyphs (a known
  attack vector for phishing PDFs).

Optional steps the renderer applies:

- **Deskew** when the page is rotated more than 0.5 degrees (only when
  OpenCV is available; falls through silently otherwise — deskew is
  best-effort, not required for correctness).
- **Unsharp mask** on born-digital pages to sharpen the glyph edges
  before the VLM sees them. Skipped on scans because unsharp masking a
  noisy scan amplifies the noise.

The unit-test surface for this module focuses on:

- ``render_pages`` returns one image per page with the requested DPI.
- ``extract_native_text`` returns one string per page (possibly empty).
- Empty/garbage input raises a typed error rather than crashing.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Final

from sidecar.ingest.errors import UploadPdfSanitizationError


logger = logging.getLogger(__name__)


# 300 dots-per-inch (DPI) is the standard OCR research minimum. Lower
# DPI loses small glyphs, especially the dotted-i and the period.
DEFAULT_RENDER_DPI: Final[int] = 300

# Minimum rotation that triggers a deskew. Below 0.5 degrees the visual
# difference is negligible and the rotation operation introduces its own
# resampling artifacts.
DESKEW_MIN_DEGREES: Final[float] = 0.5

# Pages whose native text exceeds this character threshold are treated
# as "born digital". Below this threshold we assume a scan and skip the
# unsharp mask.
BORN_DIGITAL_TEXT_THRESHOLD: Final[int] = 200


@dataclass(frozen=True)
class PageImage:
    """One page rendered as a Pillow image.

    ``page_index`` is zero-based. ``dpi`` records the resolution the
    image was rendered at, so a downstream caller can convert pixel
    coordinates to PDF user-space coordinates without guessing.
    """

    page_index: int
    dpi: int
    width_px: int
    height_px: int
    png_bytes: bytes


def render_pages(pdf_bytes: bytes, *, dpi: int = DEFAULT_RENDER_DPI) -> list[PageImage]:
    """Rasterize every page of ``pdf_bytes`` to a Pillow image.

    Returns one ``PageImage`` per page, in document order. Empty input
    or a corrupt PDF raises ``UploadPdfSanitizationError`` (the same
    typed error the sanitizer uses, because the failure mode is the
    same: we cannot read the PDF). The render step is downstream of the
    sanitizer in the pipeline, so a sanitization-tier failure here is a
    bug in the sanitizer or the writer that produced the bytes.
    """
    if not pdf_bytes:
        raise UploadPdfSanitizationError("renderer received zero bytes")
    if dpi < 72 or dpi > 1200:
        raise ValueError(
            f"dpi must be between 72 and 1200, got {dpi}; lower than 72 "
            "loses sub-pixel glyph detail, higher than 1200 produces "
            "images larger than the VLM's input limit."
        )

    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise UploadPdfSanitizationError(
            "PyMuPDF (fitz) is not installed; install the w2_ingest extra. "
            "Underlying ImportError: " + repr(exc)
        ) from exc

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise UploadPdfSanitizationError(
            f"PyMuPDF failed to open the PDF: {type(exc).__name__}: {exc!s}"
        ) from exc

    pages: list[PageImage] = []
    try:
        zoom = dpi / 72.0  # PDF user space is 72 dpi by definition.
        matrix = fitz.Matrix(zoom, zoom)
        for index, page in enumerate(doc):
            pixmap = page.get_pixmap(matrix=matrix, alpha=False)
            png = pixmap.tobytes(output="png")
            png = _maybe_deskew(png)
            png = _maybe_unsharp(png, page_text=page.get_text("text") or "")
            pages.append(
                PageImage(
                    page_index=index,
                    dpi=dpi,
                    width_px=pixmap.width,
                    height_px=pixmap.height,
                    png_bytes=png,
                )
            )
    finally:
        doc.close()
    return pages


def extract_native_text(pdf_bytes: bytes) -> list[str]:
    """Return the native (selectable) text per page, in document order.

    Returns ``""`` for pages with no native text layer. The empty string
    signals "scanned page" to the extractor, not "no content".
    """
    if not pdf_bytes:
        raise UploadPdfSanitizationError("native-text extractor received zero bytes")

    try:
        import fitz
    except ImportError as exc:
        raise UploadPdfSanitizationError(
            "PyMuPDF (fitz) is not installed; install the w2_ingest extra. "
            "Underlying ImportError: " + repr(exc)
        ) from exc

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise UploadPdfSanitizationError(
            f"PyMuPDF failed to open the PDF: {type(exc).__name__}: {exc!s}"
        ) from exc

    try:
        return [page.get_text("text") or "" for page in doc]
    finally:
        doc.close()


def _maybe_deskew(png_bytes: bytes) -> bytes:
    """Best-effort deskew via OpenCV's Hough transform.

    Falls through unchanged when OpenCV is not installed. The deskew is
    a quality improvement, not a correctness requirement, so a missing
    dependency degrades gracefully.
    """
    try:
        import cv2  # type: ignore[import-untyped]
        import numpy as np  # type: ignore[import-untyped]
    except ImportError:
        return png_bytes

    try:
        from PIL import Image  # type: ignore[import-untyped]
    except ImportError:
        return png_bytes

    try:
        image = Image.open(io.BytesIO(png_bytes)).convert("L")
        arr = np.array(image)
        edges = cv2.Canny(arr, 50, 150)
        lines = cv2.HoughLinesP(
            edges, rho=1, theta=np.pi / 720, threshold=200,
            minLineLength=100, maxLineGap=10,
        )
        if lines is None or len(lines) == 0:
            return png_bytes

        angles = []
        for line in lines:
            x1, y1, x2, y2 = line[0]
            if x2 == x1:
                continue
            angle_deg = np.degrees(np.arctan2(y2 - y1, x2 - x1))
            # Only consider near-horizontal lines (text baselines).
            if abs(angle_deg) < 15:
                angles.append(angle_deg)
        if not angles:
            return png_bytes
        median_angle = float(np.median(angles))
        if abs(median_angle) < DESKEW_MIN_DEGREES:
            return png_bytes

        rotated = image.rotate(-median_angle, resample=Image.Resampling.BICUBIC, fillcolor=255)
        out = io.BytesIO()
        rotated.save(out, format="PNG")
        return out.getvalue()
    except Exception as exc:
        logger.warning("deskew skipped due to error: %r", exc)
        return png_bytes


def _maybe_unsharp(png_bytes: bytes, *, page_text: str) -> bytes:
    """Apply a Pillow unsharp mask to born-digital pages.

    A page is "born digital" when its native text layer exceeds
    ``BORN_DIGITAL_TEXT_THRESHOLD`` characters. Scanned pages skip the
    sharpen because amplifying scanner noise hurts more than it helps.
    """
    if len(page_text) < BORN_DIGITAL_TEXT_THRESHOLD:
        return png_bytes

    try:
        from PIL import Image, ImageFilter
    except ImportError:
        return png_bytes

    try:
        image = Image.open(io.BytesIO(png_bytes))
        sharpened = image.filter(ImageFilter.UnsharpMask(radius=1.0, percent=120, threshold=2))
        out = io.BytesIO()
        sharpened.save(out, format="PNG")
        return out.getvalue()
    except Exception as exc:
        logger.warning("unsharp skipped due to error: %r", exc)
        return png_bytes


__all__ = [
    "BORN_DIGITAL_TEXT_THRESHOLD",
    "DEFAULT_RENDER_DPI",
    "DESKEW_MIN_DEGREES",
    "PageImage",
    "extract_native_text",
    "render_pages",
]
