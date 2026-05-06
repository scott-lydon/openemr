"""Tests for the Phase 6 citation preview pipeline.

Coverage:

- Signed URL: mint-then-verify round-trip recovers the citation id
  and patient id; stale tokens raise ``SignedUrlExpired``; tampered
  tokens raise ``SignedUrlSignatureInvalid``; weak signing keys raise
  at mint time.
- ``bbox_to_pixel_rect`` scales correctly for typical normalized
  bboxes; clamps negative or >1 inputs onto the page; rejects
  degenerate rectangles upstream (covered by BoundingBox schema).
- ``render_bbox_overlay`` produces a PNG; the cited region has
  detectable orange pixels; an out-of-range page raises
  ``PreviewRenderError``; no anchor at all raises
  ``PreviewRenderError`` (defense in depth — schema also forbids).
- Fuzzy quote search returns a non-empty rectangle when the quote
  matches; ``None`` when it does not.
- Persistence round-trip: insert then get returns the same row
  values, including the bbox.
"""

from __future__ import annotations

import io
import time
import uuid
from datetime import datetime
from typing import Any

import pytest

from sidecar.citations import (
    CitationRowNotFound,
    PreviewRenderError,
    SignedUrlExpired,
    SignedUrlSignatureInvalid,
    bbox_to_pixel_rect,
    fuzzy_quote_search,
    get_citation,
    insert_citation,
    mint_signed_url,
    render_bbox_overlay,
    verify_signed_url,
)
from sidecar.schemas.w2.bbox import BoundingBox
from sidecar.schemas.w2.citation import Citation, CitationSourceType


SIGNING_KEY = "x" * 64  # 64-char dev key meets the >=16 requirement


# ─── Signed URL ───────────────────────────────────────────────────────


def test_mint_then_verify_round_trips() -> None:
    url = mint_signed_url(
        base_url="https://example.com/preview.png",
        citation_id="cit-123",
        patient_id="Patient/87413",
        signing_key=SIGNING_KEY,
        ttl_seconds=300,
    )
    assert "?token=" in url
    token = url.split("?token=")[1]
    decoded = verify_signed_url(token=token, signing_key=SIGNING_KEY)
    assert decoded.citation_id == "cit-123"
    assert decoded.patient_id == "Patient/87413"


def test_verify_rejects_tampered_payload() -> None:
    url = mint_signed_url(
        base_url="https://example.com/preview.png",
        citation_id="cit-123",
        patient_id="Patient/87413",
        signing_key=SIGNING_KEY,
        ttl_seconds=300,
    )
    token = url.split("?token=")[1]
    payload, sig = token.split(".")
    # Flip a character in the payload.
    tampered_payload = ("Z" + payload[1:]) if payload[0] != "Z" else ("A" + payload[1:])
    tampered = f"{tampered_payload}.{sig}"
    with pytest.raises(SignedUrlSignatureInvalid):
        verify_signed_url(token=tampered, signing_key=SIGNING_KEY)


def test_verify_rejects_expired() -> None:
    url = mint_signed_url(
        base_url="https://example.com/preview.png",
        citation_id="cit-123",
        patient_id="Patient/87413",
        signing_key=SIGNING_KEY,
        ttl_seconds=300,
        now_unix=1_000_000,
    )
    token = url.split("?token=")[1]
    with pytest.raises(SignedUrlExpired):
        verify_signed_url(token=token, signing_key=SIGNING_KEY, now_unix=1_000_999)


def test_mint_rejects_weak_key() -> None:
    with pytest.raises(ValueError):
        mint_signed_url(
            base_url="https://example.com",
            citation_id="cit",
            patient_id="Patient/X",
            signing_key="short",
        )


def test_mint_rejects_zero_ttl() -> None:
    with pytest.raises(ValueError):
        mint_signed_url(
            base_url="https://example.com",
            citation_id="cit",
            patient_id="Patient/X",
            signing_key=SIGNING_KEY,
            ttl_seconds=0,
        )


# ─── Bbox to pixel rect ───────────────────────────────────────────────


def test_bbox_to_pixel_rect_scales() -> None:
    bbox = BoundingBox(page=0, x0=0.1, y0=0.2, x1=0.5, y1=0.4)
    rect = bbox_to_pixel_rect(bbox=bbox, width_px=1000, height_px=2000)
    assert rect.x0 == 100
    assert rect.y0 == 400
    assert rect.x1 == 500
    assert rect.y1 == 800


def test_bbox_to_pixel_rect_clamps_at_edges() -> None:
    # x1 maxes at 1.0 -> page width.
    bbox = BoundingBox(page=0, x0=0.0, y0=0.0, x1=1.0, y1=1.0)
    rect = bbox_to_pixel_rect(bbox=bbox, width_px=300, height_px=400)
    assert rect.x0 == 0 and rect.y0 == 0
    assert rect.x1 == 300 and rect.y1 == 400


# ─── Renderer ─────────────────────────────────────────────────────────


def _build_pdf() -> bytes:
    try:
        import fitz
    except ImportError:
        pytest.skip("PyMuPDF not installed; renderer tests need it")
    doc = fitz.open()
    page = doc.new_page(width=400, height=600)
    page.insert_text((50, 100), "Lab report HbA1c 6.8 percent")
    out = io.BytesIO()
    doc.save(out)
    doc.close()
    return out.getvalue()


def test_render_bbox_overlay_produces_png_with_overlay() -> None:
    pdf = _build_pdf()
    bbox = BoundingBox(page=0, x0=0.1, y0=0.1, x1=0.6, y1=0.25)
    png = render_bbox_overlay(
        pdf_bytes=pdf, page_index=0, bbox=bbox, quote_fallback=None,
    )
    assert png.startswith(b"\x89PNG")
    # Confirm the overlay pixel is present somewhere — at least one
    # pixel close to the OVERLAY_RGBA color in the cited region.
    try:
        from PIL import Image
    except ImportError:
        pytest.skip("Pillow not installed")
    img = Image.open(io.BytesIO(png)).convert("RGB")
    width, height = img.size
    cited_pixels = []
    for x in range(int(width * 0.15), int(width * 0.55), 5):
        for y in range(int(height * 0.12), int(height * 0.22), 5):
            cited_pixels.append(img.getpixel((x, y)))
    # Some pixel in the cited region should be tinted toward the orange
    # overlay (R noticeably greater than B).
    assert any(p[0] - p[2] > 20 for p in cited_pixels)


def test_render_bbox_overlay_rejects_out_of_range_page() -> None:
    pdf = _build_pdf()
    bbox = BoundingBox(page=0, x0=0.1, y0=0.1, x1=0.5, y1=0.2)
    with pytest.raises(PreviewRenderError):
        render_bbox_overlay(
            pdf_bytes=pdf, page_index=99, bbox=bbox, quote_fallback=None,
        )


def test_render_bbox_overlay_rejects_no_anchor() -> None:
    pdf = _build_pdf()
    with pytest.raises(PreviewRenderError):
        render_bbox_overlay(
            pdf_bytes=pdf,
            page_index=0,
            bbox=None,
            quote_fallback="",
        )


def test_fuzzy_quote_search_finds_text() -> None:
    pdf = _build_pdf()
    try:
        import fitz
    except ImportError:
        pytest.skip("PyMuPDF not installed")
    doc = fitz.open(stream=pdf, filetype="pdf")
    rect = fuzzy_quote_search(page=doc[0], quote="HbA1c 6.8")
    doc.close()
    assert rect is not None
    assert not rect.is_empty()


def test_fuzzy_quote_search_returns_none_on_no_match() -> None:
    pdf = _build_pdf()
    try:
        import fitz
    except ImportError:
        pytest.skip("PyMuPDF not installed")
    doc = fitz.open(stream=pdf, filetype="pdf")
    rect = fuzzy_quote_search(page=doc[0], quote="this text is not on the page")
    doc.close()
    assert rect is None


def test_render_bbox_overlay_uses_quote_fallback_when_bbox_none() -> None:
    pdf = _build_pdf()
    png = render_bbox_overlay(
        pdf_bytes=pdf,
        page_index=0,
        bbox=None,
        quote_fallback="HbA1c 6.8",
    )
    assert png.startswith(b"\x89PNG")


# ─── Persistence ──────────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, row: tuple[Any, ...] | None = None) -> None:
        self._row = row

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._row


class _FakeCitationsConn:
    """In-memory model of the citations table for unit tests."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[Any, ...]] = {}

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> _FakeCursor:
        params = params or ()
        if sql.strip().startswith("INSERT INTO citations"):
            self.rows[str(params[0])] = params
            return _FakeCursor()
        if sql.strip().startswith("SELECT citation_id"):
            citation_id = str(params[0])
            row = self.rows.get(citation_id)
            if row is None:
                return _FakeCursor(None)
            return _FakeCursor(row)
        raise AssertionError(f"unexpected SQL: {sql.strip()[:80]!r}")

    def commit(self) -> None:
        pass


def test_insert_then_get_round_trips() -> None:
    conn = _FakeCitationsConn()
    citation = Citation(
        source_type=CitationSourceType.DOCUMENT_REFERENCE,
        source_id="DocumentReference/doc-x",
        page_or_section=0,
        field_or_chunk_id="HbA1c",
        quote_or_value="HbA1c 6.8 %",
        bbox=BoundingBox(page=0, x0=0.1, y0=0.1, x1=0.5, y1=0.2),
    )
    cid = insert_citation(
        conn,
        encounter_id="enc-1",
        patient_id="Patient/87413",
        citation=citation,
    )
    row = get_citation(conn, citation_id=cid)
    assert row.encounter_id == "enc-1"
    assert row.patient_id == "Patient/87413"
    assert row.field_or_chunk_id == "HbA1c"
    assert row.bbox is not None
    assert row.bbox.page == 0


def test_get_citation_unknown_id_raises() -> None:
    conn = _FakeCitationsConn()
    with pytest.raises(CitationRowNotFound):
        get_citation(conn, citation_id=uuid.uuid4())
