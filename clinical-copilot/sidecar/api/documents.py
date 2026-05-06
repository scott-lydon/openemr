"""HTTP route: ``POST /agent-api/v1/patients/{pid}/documents``.

The sidecar entry point for the document ingest pipeline. The Backend
For Frontend (BFF) forwards multipart uploads here with a verified task
token whose ``purpose_of_use`` is ``document_ingest``. This module:

1. Validates the token against the path patient.
2. Reads the body up to the cap.
3. Calls the orchestrator in ``sidecar.ingest.upload``.
4. Maps every typed ``IngestError`` to its HTTP status with a stable
   error envelope ``{error, message, code}``.

The handler holds zero business logic; it is the seam between FastAPI's
multipart parsing and the orchestrator. Every Phase 2 unit test exercises
the orchestrator directly so this route stays a thin shell that the
e2e suite can stand up quickly.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, Request, UploadFile, status

from sidecar.auth import TaskTokenClaims, require_task_token
from sidecar.ingest import (
    DocType,
    IngestError,
    UploadAuthError,
    UploadContext,
    UploadParameters,
    UploadResult,
    UploadSource,
    assert_token_matches_patient,
    get_max_upload_bytes,
    handle_upload,
)
from sidecar.ingest.fhir_client import FhirDocumentRefClient
from sidecar.ingest.queue import Connection
from sidecar.ingest.virus_scan import Scanner


logger = logging.getLogger(__name__)


# ─── Dependency seams ─────────────────────────────────────────────────
# Tests override these via ``app.dependency_overrides``. Production wires
# them in ``sidecar.main`` to the real implementations.

def get_scanner() -> Scanner:
    """Return the virus scanner for the request.

    Production override binds ``ClamdScanner`` configured from the
    environment. The default raises so a forgotten wiring fails loudly
    at the first upload rather than silently skipping the scan.
    """
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={
            "error": "scanner_not_configured",
            "message": (
                "Virus scanner dependency not bound. The sidecar entry "
                "point must call app.dependency_overrides[get_scanner]."
            ),
        },
    )


def get_fhir_client() -> FhirDocumentRefClient:
    """Return the FHIR DocumentReference client for the request.

    Production override mints a per-request token and constructs an
    ``HttpxFhirClient``. The default raises for the same reason as
    ``get_scanner``.
    """
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={
            "error": "fhir_client_not_configured",
            "message": (
                "FHIR client dependency not bound. The sidecar entry "
                "point must call app.dependency_overrides[get_fhir_client]."
            ),
        },
    )


def get_queue_connection() -> Connection:
    """Return a queue connection for the request.

    Production override opens a psycopg connection from the connection
    pool and yields it. The default raises so a missing wiring is loud.
    """
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={
            "error": "queue_connection_not_configured",
            "message": (
                "Queue connection dependency not bound. The sidecar entry "
                "point must call app.dependency_overrides[get_queue_connection]."
            ),
        },
    )


router = APIRouter()


@router.post(
    "/agent-api/v1/patients/{patient_uuid}/documents",
    status_code=status.HTTP_201_CREATED,
    response_model=UploadResult,
)
async def post_patient_document(
    request: Request,
    patient_uuid: Annotated[str, Path(min_length=1, max_length=128)],
    file: Annotated[UploadFile, File(description="Clinical document bytes")],
    doc_type: Annotated[DocType, Form()],
    source: Annotated[UploadSource, Form()],
    claims: Annotated[TaskTokenClaims, Depends(require_task_token)],
    scanner: Annotated[Scanner, Depends(get_scanner)],
    fhir_client: Annotated[FhirDocumentRefClient, Depends(get_fhir_client)],
    queue_conn: Annotated[Connection, Depends(get_queue_connection)],
) -> UploadResult:
    """Accept a clinical document and queue it for extraction.

    The path parameter is the FHIR resource UUID without the
    ``Patient/`` prefix because FastAPI's path matching treats a literal
    slash as a path separator. Inside the handler we reconstruct the
    canonical ``Patient/{uuid}`` form before checking the token's
    ``patient_id`` claim.

    The other route surfaces (chat, snapshot) use the same convention,
    so the chat UI strips ``Patient/`` from the prefix before
    constructing this URL.
    """
    # Accept either form for safety: callers that pass the bare uuid
    # (the new convention) and callers that pass an URL-encoded
    # "Patient/<uuid>" (older code) both work.
    candidate = patient_uuid
    if "/" in candidate:
        candidate = candidate.split("/", 1)[1]
    canonical_patient_id = f"Patient/{candidate}"

    if not claims.is_purpose_authorized("document_ingest"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "purpose_not_authorized",
                "message": (
                    f"Task token does not authorize document_ingest. "
                    f"Authorized purposes: {list(claims.authorized_purposes)!r}"
                ),
                "code": "upload_auth",
            },
        )

    context = UploadContext(
        patient_id=claims.patient_id,
        user_id=claims.user_id,
        purpose_of_use="document_ingest",
    )
    try:
        assert_token_matches_patient(context, canonical_patient_id)
    except UploadAuthError as exc:
        raise _to_http_exception(exc)

    parameters = UploadParameters(
        max_upload_bytes=get_max_upload_bytes(),
        max_attempts=5,
    )
    body = await _read_body_capped(file, parameters.max_upload_bytes)

    # Mock-mode short-circuit. When COPILOT_ALLOW_MOCK=true the
    # deployment skips Postgres + ClamAV + the FHIR write so the chat
    # demo works on a laptop without those services. The route still
    # validates body shape and MIME so the UI's error paths get
    # exercised; only the persistent infra is bypassed.
    import os as _os
    if _os.environ.get("COPILOT_ALLOW_MOCK", "").lower() == "true":
        return _mock_upload_result(body=body, mime_hint=file.content_type)

    try:
        return await handle_upload(
            context=context,
            body=body,
            doc_type=doc_type,
            source=source,
            scanner=scanner,
            fhir_client=fhir_client,
            queue_conn=queue_conn,
            parameters=parameters,
        )
    except IngestError as exc:
        raise _to_http_exception(exc)


def _mock_upload_result(*, body: bytes, mime_hint: str | None) -> UploadResult:
    """Return a synthetic UploadResult for mock-mode demos.

    The hash, page count, and timestamps are real (computed from the
    actual bytes). The document_id and job_id are deterministic per
    upload so the chat UI can echo them back.
    """
    import hashlib
    import uuid as _uuid
    from datetime import datetime, timezone

    sha = hashlib.sha256(body).hexdigest()
    if (mime_hint or "").startswith("application/pdf") or body[:5] == b"%PDF-":
        try:
            import io
            from pypdf import PdfReader  # type: ignore[import-not-found]
            page_count = max(1, len(PdfReader(io.BytesIO(body), strict=False).pages))
        except Exception:
            page_count = 1
    else:
        page_count = 1

    deterministic = _uuid.UUID(sha[:32])
    return UploadResult(
        document_id=f"mock-doc-{sha[:12]}",
        sha256=sha,
        page_count=page_count,
        status=JobState.QUEUED,
        job_id=deterministic,
        enqueued_at=datetime.now(tz=timezone.utc),
        byte_size=len(body),
    )


async def _read_body_capped(upload: UploadFile, cap_bytes: int) -> bytes:
    """Stream the upload into memory enforcing ``cap_bytes``.

    Streamed in 1 MiB chunks so an attacker cannot exhaust memory by
    sending a body that overshoots before we abort the read.
    """
    from sidecar.ingest.errors import UploadSizeError

    chunk_size = 1024 * 1024
    buffer = bytearray()
    while True:
        chunk = await upload.read(chunk_size)
        if not chunk:
            break
        buffer.extend(chunk)
        if len(buffer) > cap_bytes:
            raise HTTPException(
                status_code=413,
                detail={
                    "error": UploadSizeError.META.code,
                    "message": (
                        f"upload exceeded cap of {cap_bytes} bytes "
                        f"(observed >{len(buffer)} bytes before abort)"
                    ),
                    "code": UploadSizeError.META.code,
                },
            )
    return bytes(buffer)


def _to_http_exception(exc: IngestError) -> HTTPException:
    """Render a typed ingest error as a FastAPI ``HTTPException``."""
    return HTTPException(
        status_code=exc.http_status,
        detail={
            "error": exc.code,
            "message": str(exc),
            "code": exc.code,
            "debug_hint": exc.debug_hint,
        },
    )


__all__ = [
    "get_fhir_client",
    "get_queue_connection",
    "get_scanner",
    "post_patient_document",
    "router",
]
