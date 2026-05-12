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
    JobState,
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

    # Mock mode short-circuits the purpose check too. The real purpose
    # is enforced in production; the demo workflow lets the existing
    # 'follow_up_question'-scoped launch token flow into the upload
    # path without forcing the operator to re-launch.
    import os as _mock_os
    _mock_active = _mock_os.environ.get("COPILOT_ALLOW_MOCK", "").lower() == "true"

    if not _mock_active and not claims.is_purpose_authorized("document_ingest"):
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
    # deployment skips Postgres + ClamAV + the queued worker so the
    # chat demo works on a laptop without those services.
    #
    # We DO still push the document into OpenEMR's documents store so
    # the demo flow ("drag a PDF into chat → see it in the patient's
    # profile in OpenEMR") works end-to-end. The push uses the
    # `cli-store-document.php` CLI helper invoked via `docker exec`,
    # which calls OpenEMR's own `\Document::createDocument` — same code
    # path the Documents UI uses, so storage / hashing / categories /
    # ACL boundaries match the rest of OpenEMR.
    #
    # Why CLI-over-docker-exec instead of a REST/FHIR call:
    #   - OpenEMR's FHIR DocumentReference advertises `create` in the
    #     CapabilityStatement but the controller has no POST handler;
    #     all requests return 404 "Route not found".
    #   - OpenEMR's standard /api/patient/{pid}/document POST does
    #     work, but it requires a user-context token (ACL check is
    #     `patients/docs/write` against the user the token was issued
    #     to). The sidecar's client_credentials grant produces system
    #     tokens that don't satisfy that ACL. Switching the sidecar to
    #     a user-context grant is a much bigger refactor.
    #
    # Failure of the OpenEMR push is logged but not propagated — the
    # chat UI gets the mock UploadResult so the user can keep typing,
    # and the failure mode is obvious from sidecar stdout (look for
    # 'mock_openemr_push_failed').
    import os as _os
    if _os.environ.get("COPILOT_ALLOW_MOCK", "").lower() == "true":
        result = _mock_upload_result(body=body, mime_hint=file.content_type)
        # Cache the bytes so the W2 chat's intake extractor can read the
        # actual PDF content when the user references this document_id.
        # Without this, the chat falls back to placeholder claims that
        # don't reflect what the user uploaded — see _mock_upload_cache
        # for the rationale and bounded-size policy.
        try:
            from sidecar.api import _mock_upload_cache
            _mock_upload_cache.store(
                document_id=result.document_id,
                body=body,
                mime_hint=file.content_type or "application/octet-stream",
                filename=file.filename or "document.pdf",
            )
        except Exception as cache_exc:  # noqa: BLE001 — never break upload path
            logger.warning(
                "mock_upload_cache_store_failed",
                extra={
                    "error_type": type(cache_exc).__name__,
                    "error_message": str(cache_exc),
                    "document_id": result.document_id,
                    "hint": (
                        "Bytes were not cached for the W2 chat extractor. "
                        "The chat will fall back to a placeholder claim. "
                        "Check sidecar.api._mock_upload_cache for any "
                        "exceptions raised during store()."
                    ),
                },
            )
        try:
            await _push_to_openemr_documents_store(
                body=body,
                patient_id=context.patient_id,
                filename=(file.filename or "document.pdf"),
                mime_hint=file.content_type or "application/pdf",
            )
        except Exception as push_exc:  # noqa: BLE001 — converted to HTTP below
            # Surface push failures to the chat client instead of
            # silently returning a mock UploadResult that promises the
            # doc is in OpenEMR when it isn't. The previous behavior
            # (catch-log-and-continue) hid Hetzner-vs-local regressions
            # for days — that is exactly the failure mode this turn was
            # filed to fix.
            #
            # The RuntimeError raised by _push_to_openemr_documents_store
            # already contains a comprehensive diagnostic message naming
            # the exact env var to check, status code meaning, etc. We
            # forward that message verbatim so the chat UI / ops can act
            # without having to tail sidecar logs.
            import traceback

            traceback.print_exc()
            logger.error(
                "openemr_push_failed_propagating",
                extra={
                    "error_type": type(push_exc).__name__,
                    "error_message": str(push_exc),
                    "patient_id": context.patient_id,
                },
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "error": "openemr_push_failed",
                    "message": (
                        "Upload was accepted by the sidecar but pushing "
                        "the document into OpenEMR failed. The chat will "
                        "not be able to reference this document. "
                        f"Cause: {type(push_exc).__name__}: {push_exc!s}"
                    ),
                    "code": "openemr_push",
                },
            ) from push_exc
        return result

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


async def _push_to_openemr_documents_store(
    *,
    body: bytes,
    patient_id: str,
    filename: str,
    mime_hint: str,
) -> dict:
    """Insert the uploaded document into OpenEMR's documents store.

    Posts to ``interface/clinical_copilot/store-document.php`` over
    HTTP. That endpoint validates a shared secret in the
    ``X-Copilot-Token`` header and calls OpenEMR's
    ``\\Document::createDocument`` — same code path the Documents UI
    uses — so storage, hashing, encryption, and category linkage are
    OpenEMR-correct.

    The endpoint accepts ``patient_fhir_uuid`` and resolves the legacy
    numeric pid in PHP, so we do not need a second round trip (which
    previously required ``docker exec mysql``, also a local-dev hack).

    Why HTTP instead of OpenEMR's standard REST/FHIR endpoints:

      - OpenEMR's FHIR ``DocumentReference`` advertises ``create`` in
        the CapabilityStatement but the controller has no POST handler;
        every POST returns 404 "Route not found".

      - OpenEMR's standard ``/api/patient/{pid}/document`` POST works
        but requires a user-context token (ACL ``patients/docs/write``
        is checked against the OAuth user). The sidecar runs the
        client_credentials grant which produces system tokens; the
        ACL check fails. Switching grants is a much bigger refactor.

      - The earlier implementation used ``docker compose exec`` against
        a CLI helper. That path was inherently local-dev-only: the
        sidecar container on production has no docker socket, no
        compose file, and no docker binary. The HTTP endpoint replaces
        that.

    Required env vars (sidecar):

      ``COPILOT_OPENEMR_DOC_PUSH_URL``    — full URL to the endpoint,
          e.g. ``https://localhost:9300/interface/clinical_copilot/store-document.php``
          locally or ``https://5-161-253-237.sslip.io/interface/clinical_copilot/store-document.php``
          on Hetzner.
      ``COPILOT_OPENEMR_DOC_PUSH_SECRET`` — must match
          ``COPILOT_STORE_DOCUMENT_SECRET`` set on the OpenEMR side.
      ``COPILOT_FHIR_VERIFY_SSL`` — reused; set ``false`` to accept
          self-signed certs (local dev, Hetzner pre-Caddy).

    Returns the JSON dict from OpenEMR on success (contains
    ``document_id``, ``filename``, ``size``, ``category``,
    ``category_id``, ``patient_pid``).

    Raises ``RuntimeError`` with a comprehensive cause on any failure
    path so the caller can present an actionable message to the user
    and the sidecar log contains the diagnosis without further digging.
    """
    import base64
    import os

    import httpx

    push_url = os.environ.get("COPILOT_OPENEMR_DOC_PUSH_URL", "").strip()
    push_secret = os.environ.get("COPILOT_OPENEMR_DOC_PUSH_SECRET", "").strip()
    if not push_url:
        raise RuntimeError(
            "COPILOT_OPENEMR_DOC_PUSH_URL is missing or empty in the "
            "sidecar environment. The sidecar cannot push uploaded "
            "documents into OpenEMR without it. Set it in "
            "clinical-copilot/.env (local) or the deployed .env (Hetzner). "
            "Example: "
            "https://localhost:9300/interface/clinical_copilot/store-document.php"
        )
    if not push_secret:
        raise RuntimeError(
            "COPILOT_OPENEMR_DOC_PUSH_SECRET is missing or empty in the "
            "sidecar environment. The sidecar cannot authenticate to "
            "OpenEMR's store-document.php endpoint without it. Set it in "
            "clinical-copilot/.env (local) or the deployed .env (Hetzner), "
            "and set the matching COPILOT_STORE_DOCUMENT_SECRET on the "
            "OpenEMR container side."
        )

    if not patient_id.startswith("Patient/"):
        raise RuntimeError(
            f"patient_id must start with 'Patient/' (got {patient_id!r}). "
            "The launch token contract guarantees this prefix; if it's "
            "missing, the upstream code at the route handler is wrong."
        )
    fhir_uuid = patient_id.split("/", 1)[1]

    verify_ssl = (
        os.environ.get("COPILOT_FHIR_VERIFY_SSL", "true").strip().lower()
        != "false"
    )

    payload = {
        "patient_fhir_uuid": fhir_uuid,
        "category": "Lab Report",
        "filename": filename,
        "mime": mime_hint,
        "bytes_base64": base64.b64encode(body).decode("ascii"),
    }
    headers = {
        "X-Copilot-Token": push_secret,
        "Content-Type": "application/json",
        # Suppress browser-style XSRF + accept-language defaults so the
        # request reads as machine-to-machine in the OpenEMR Apache log.
        "User-Agent": "clinical-copilot-sidecar/store-document",
    }

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            verify=verify_ssl,
            follow_redirects=False,
        ) as client:
            resp = await client.post(push_url, json=payload, headers=headers)
    except httpx.RequestError as exc:
        raise RuntimeError(
            f"Could not reach OpenEMR store-document.php at {push_url}. "
            f"httpx error: {type(exc).__name__}: {exc!s}. "
            "Confirm: (a) the URL is reachable from the sidecar's network "
            "(curl it from inside the sidecar container if running in "
            "docker), (b) the OpenEMR container is running and serving "
            "/interface/clinical_copilot/store-document.php, (c) "
            "COPILOT_FHIR_VERIFY_SSL=false if the cert is self-signed."
        ) from exc

    if resp.status_code != 200:
        body_text = resp.text[:1000] if resp.text else ""
        raise RuntimeError(
            f"OpenEMR store-document.php returned HTTP {resp.status_code}. "
            f"URL: {push_url}. Response body: {body_text!r}. "
            "Common causes by status: "
            "401 = bad X-Copilot-Token (sidecar secret != OpenEMR secret); "
            "404 = patient_fhir_uuid not in patient_data; "
            "422 = the named category does not exist in OpenEMR; "
            "503 = OpenEMR's COPILOT_STORE_DOCUMENT_SECRET is unset "
            "(endpoint refuses to start without it); "
            "Apache HTML page = the script is missing from the deployed "
            "OpenEMR (check the auto-deploy log)."
        )

    try:
        result = resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"OpenEMR store-document.php returned non-JSON on 200: "
            f"{resp.text[:500]!r}. URL: {push_url}."
        ) from exc

    if not isinstance(result, dict) or "document_id" not in result:
        raise RuntimeError(
            f"OpenEMR store-document.php response missing document_id: "
            f"{result!r}"
        )

    logger.info(
        "mock_openemr_push_ok",
        extra={
            "document_id": result.get("document_id"),
            "patient_id": patient_id,
            "patient_pid": result.get("patient_pid"),
            # NOTE: don't put a key called 'filename' here — that name
            # is reserved by Python's LogRecord and the logger raises
            # 'Attempt to overwrite filename in LogRecord' if we try.
            "document_filename": filename,
            "byte_size": len(body),
            "category": result.get("category"),
        },
    )
    return result


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
