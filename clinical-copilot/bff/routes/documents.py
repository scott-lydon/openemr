"""BFF documents route: ``POST /agent-api/v1/patients/{pid}/documents``.

The Backend For Frontend (BFF):

1. Authenticates the caller (the user's OpenEMR session via the BFF's
   own auth — verified upstream of this route).
2. Mints a 5-minute task token with ``purpose_of_use=document_ingest``.
3. Streams the multipart body to the sidecar.
4. Returns the sidecar's response verbatim.

The sidecar performs MIME sniffing, virus scanning, PDF sanitization,
the FHIR write, and the queue insert. The BFF's job is bookkeeping:
mint a token, forward, return.

Why a separate router:

- The chat route does its own token minting on a per-message basis;
  the documents route mints a separate token because the
  ``purpose_of_use`` is different.
- Multipart streaming is a different beast from JSON; keeping the
  document router separate avoids growing ``bff/main.py`` past the
  comprehensible-in-one-file threshold.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any, Final

import httpx
from fastapi import APIRouter, Body, File, Form, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import JSONResponse

from sidecar.auth import mint_task_token
from sidecar.config import get_settings


logger = logging.getLogger(__name__)


# Whitelist of doc_type values the BFF forwards. The sidecar's enum is
# the authoritative one; we re-declare here so a client posting a typo
# fails at the BFF rather than tying up a sidecar request slot.
ALLOWED_DOC_TYPES: Final[frozenset[str]] = frozenset(
    {"lab_pdf", "intake_form", "referral_fax"}
)
ALLOWED_SOURCES: Final[frozenset[str]] = frozenset({"upload", "fax", "portal"})


router = APIRouter()


@router.post(
    "/agent-api/v1/patients/{patient_id}/documents",
    status_code=status.HTTP_201_CREATED,
)
async def post_patient_document(
    patient_id: str,
    file: Annotated[UploadFile, File(...)],
    doc_type: Annotated[str, Form(...)],
    source: Annotated[str, Form(...)],
    user_id: Annotated[str, Header(alias="X-Cowork-User", description="OpenEMR user id")],
) -> JSONResponse:
    """Mint a task token, forward the multipart body to the sidecar."""
    if doc_type not in ALLOWED_DOC_TYPES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "doc_type_invalid",
                "message": (
                    f"doc_type={doc_type!r} is not allowed; expected one of "
                    f"{sorted(ALLOWED_DOC_TYPES)!r}"
                ),
            },
        )
    if source not in ALLOWED_SOURCES:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "source_invalid",
                "message": (
                    f"source={source!r} is not allowed; expected one of "
                    f"{sorted(ALLOWED_SOURCES)!r}"
                ),
            },
        )

    settings = get_settings()
    scopes = ["patient/DocumentReference.c", "patient/DocumentReference.r"]
    task_token = mint_task_token(
        signing_key=settings.bff_jwt_signing_key,
        user_id=user_id,
        patient_id=patient_id,
        purposes_of_use=["document_ingest"],
        scopes=scopes,
        lifetime_seconds=settings.task_token_lifetime_seconds,
    )

    body_bytes = await file.read()
    files = {
        "file": (
            file.filename or "upload.bin",
            body_bytes,
            file.content_type or "application/octet-stream",
        ),
    }
    form: dict[str, Any] = {"doc_type": doc_type, "source": source}

    sidecar_url = (
        f"{settings.sidecar_url.rstrip('/')}"
        f"/agent-api/v1/patients/{patient_id}/documents"
    )
    try:
        async with httpx.AsyncClient(
            verify=settings.fhir_verify_ssl,
            timeout=httpx.Timeout(connect=5.0, read=60.0, write=60.0, pool=5.0),
        ) as client:
            upstream = await client.post(
                sidecar_url,
                headers={"Authorization": f"Bearer {task_token}"},
                data=form,
                files=files,
            )
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "sidecar_unreachable",
                "message": (
                    f"BFF could not reach sidecar at {sidecar_url}: "
                    f"{type(exc).__name__}: {exc!s}"
                ),
            },
        ) from exc

    try:
        payload = upstream.json()
    except ValueError:
        payload = {"raw": upstream.text[:2048]}
    return JSONResponse(content=payload, status_code=upstream.status_code)


@router.get("/dev/mint-token")
def mint_dev_token(
    patient: Annotated[str, Query(...)],
    purpose: Annotated[str, Query(...)] = "document_ingest",
    user_id: Annotated[str, Query(...)] = "dev",
) -> dict[str, str]:
    """Development-only convenience endpoint: mint a token for curl testing.

    Production deploys must NOT expose this route. The ``/dev/`` prefix
    is matched in the gateway config and stripped or 404'd in the
    production environment. The endpoint mirrors what
    ``W2_VERIFICATION_CHECKLIST.md`` Phase 2.2 expects.
    """
    settings = get_settings()
    scopes = ["patient/DocumentReference.c", "patient/DocumentReference.r"]
    token = mint_task_token(
        signing_key=settings.bff_jwt_signing_key,
        user_id=user_id,
        patient_id=patient,
        purposes_of_use=[purpose],
        scopes=scopes,
        lifetime_seconds=settings.task_token_lifetime_seconds,
    )
    return {"token": token, "patient_id": patient, "purpose": purpose}


__all__ = ["ALLOWED_DOC_TYPES", "ALLOWED_SOURCES", "router"]
