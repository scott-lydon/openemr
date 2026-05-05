"""HTTP route: ``GET /agent-api/v1/citations/{citation_id}/preview.png``.

Renders the cited page with the bounding box overlaid. Two
authentication paths:

- **Bearer task token** — when the caller holds a valid task token
  scoped to the citation's patient. Used by the chat layer's own
  fetches.
- **Signed URL** — when the citation appears as ``<img src=...>`` in
  the HTML and the browser cannot send a bearer header. The token in
  the query string is verified for signature, expiry, and
  citation-id match.

Both paths converge on the same scope check: the caller must have
read access to the citation's patient. Any mismatch returns 403.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, status
from fastapi.responses import Response

from sidecar.auth import (
    TaskTokenClaims,
    TaskTokenError,
    verify_task_token,
)
from sidecar.citations.persistence import (
    CitationRowNotFound,
    CitationsConnection,
    get_citation,
)
from sidecar.citations.preview_renderer import (
    PreviewRenderError,
    render_bbox_overlay,
)
from sidecar.citations.signing import (
    SignedUrlError,
    SignedUrlExpired,
    SignedUrlSignatureInvalid,
    verify_signed_url,
)
from sidecar.config import get_settings
from sidecar.ingest.fhir_client import FhirDocumentRefClient


logger = logging.getLogger(__name__)


router = APIRouter()


# ─── Dependency seams (overridden in sidecar/main.py) ───────────────


def get_citations_connection() -> CitationsConnection:
    raise HTTPException(
        status_code=500,
        detail={"error": "citations_connection_not_configured"},
    )


def get_pdf_source_for_citation():
    """Return a callable that, given a ``DocumentReference`` id, returns
    the sanitized PDF bytes.

    Production binds to the FHIR DocumentReference fetch; tests inject
    a fixture-backed callable.
    """
    raise HTTPException(
        status_code=500,
        detail={"error": "pdf_source_not_configured"},
    )


# ─── Route ──────────────────────────────────────────────────────────


def _try_decode_bearer(authorization: str | None) -> TaskTokenClaims | None:
    """Decode a Bearer Authorization header; return ``None`` if absent
    or malformed.

    Unlike ``require_task_token`` this never raises: the citations
    preview endpoint accepts EITHER a bearer or a signed URL, so a
    missing bearer is normal when the signed URL path is in use.
    """
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    settings = get_settings()
    try:
        return verify_task_token(parts[1].strip(), signing_key=settings.bff_jwt_signing_key)
    except TaskTokenError:
        return None


@router.get(
    "/agent-api/v1/citations/{citation_id}/preview.png",
)
async def get_citation_preview(
    request: Request,
    citation_id: Annotated[str, Path(min_length=1, max_length=128)],
    token: Annotated[str | None, Query()] = None,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    conn: Annotated[CitationsConnection, Depends(get_citations_connection)] = None,  # type: ignore[assignment]
    pdf_source=Depends(get_pdf_source_for_citation),
) -> Response:
    """Serve the rendered preview Portable Network Graphics (PNG)."""

    bearer_claims = _try_decode_bearer(authorization)

    try:
        citation_uuid = uuid.UUID(citation_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "citation_id_malformed",
                "message": f"citation_id={citation_id!r} is not a valid UUID",
            },
        ) from exc

    try:
        row = get_citation(conn, citation_id=citation_uuid)
    except CitationRowNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail={"error": "citation_not_found", "message": str(exc)},
        ) from exc

    # Authorization: bearer OR signed URL. Either path establishes
    # the caller's claim to the citation's patient.
    settings = get_settings()
    authorized = False

    if token:
        try:
            verified = verify_signed_url(
                token=token,
                signing_key=settings.bff_jwt_signing_key,
            )
        except SignedUrlExpired as exc:
            raise HTTPException(
                status_code=401,
                detail={"error": "signed_url_expired", "message": str(exc)},
            ) from exc
        except SignedUrlSignatureInvalid as exc:
            raise HTTPException(
                status_code=401,
                detail={"error": "signed_url_invalid", "message": str(exc)},
            ) from exc
        if verified.citation_id != citation_id or verified.patient_id != row.patient_id:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "signed_url_scope_mismatch",
                    "message": (
                        "Signed URL token's citation/patient does not match "
                        "the requested citation."
                    ),
                },
            )
        authorized = True
    elif bearer_claims is not None:
        if bearer_claims.patient_id != row.patient_id:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "bearer_scope_mismatch",
                    "message": (
                        f"Bearer token patient_id={bearer_claims.patient_id!r} "
                        f"does not match citation patient_id={row.patient_id!r}"
                    ),
                },
            )
        authorized = True

    if not authorized:
        raise HTTPException(
            status_code=401,
            detail={"error": "unauthorized", "message": "no valid auth"},
        )

    # Document citations only: render with bbox overlay.
    if row.source_type.value != "DocumentReference":
        raise HTTPException(
            status_code=400,
            detail={
                "error": "preview_not_supported",
                "message": (
                    f"source_type={row.source_type.value!r} citations do not "
                    "have a page-image preview. Use the citation card "
                    "renderer instead."
                ),
            },
        )

    try:
        pdf_bytes = await pdf_source(row.source_id)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "pdf_source_unreachable",
                "message": f"{type(exc).__name__}: {exc!s}",
            },
        ) from exc

    page_index = row.page if row.page is not None else 0
    try:
        png_bytes = render_bbox_overlay(
            pdf_bytes=pdf_bytes,
            page_index=page_index,
            bbox=row.bbox,
            quote_fallback=row.quote_or_value,
        )
    except PreviewRenderError as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "preview_render_failed", "message": str(exc)},
        ) from exc

    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Citation-Id": citation_id,
        },
    )


__all__ = [
    "get_citation_preview",
    "get_citations_connection",
    "get_pdf_source_for_citation",
    "router",
]
