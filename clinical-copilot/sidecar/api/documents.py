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
        try:
            await _push_to_openemr_documents_store(
                body=body,
                patient_id=context.patient_id,
                filename=(file.filename or "document.pdf"),
                mime_hint=file.content_type or "application/pdf",
            )
        except Exception as push_exc:  # noqa: BLE001 — see comment above
            # Inline print so the failure cause is visible in stdout
            # without configuring structured logging. The `extra=` keys
            # below are ignored by the default formatter.
            import traceback
            print(
                f"[mock_openemr_push_failed] {type(push_exc).__name__}: {push_exc!s}\n"
                f"{traceback.format_exc()}",
                flush=True,
            )
            logger.warning(
                "mock_openemr_push_failed",
                extra={
                    "error_type": type(push_exc).__name__,
                    "error_message": str(push_exc),
                    "patient_id": context.patient_id,
                    "hint": (
                        "The chat upload succeeded but the document "
                        "could not be inserted into OpenEMR's documents "
                        "store. Check: (a) the docker compose stack "
                        "is running and the openemr container is "
                        "healthy, (b) the patient_id maps to a real "
                        "patient_data.pid via FHIR uuid lookup, (c) "
                        "the openemr container can read "
                        "/var/www/localhost/htdocs/openemr/interface/"
                        "clinical_copilot/cli-store-document.php."
                    ),
                },
            )
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
) -> None:
    """Insert the uploaded document into OpenEMR's documents store.

    Calls ``interface/clinical_copilot/cli-store-document.php`` via
    ``docker exec``. The CLI uses ``\\Document::createDocument`` —
    same code path the Documents UI uses — so storage, hashing,
    encryption and category linkage are all OpenEMR-correct.

    ``patient_id`` arrives as ``Patient/<fhir-uuid>``. The CLI takes
    the legacy numeric ``pid``, so we run a quick lookup against
    ``patient_data.uuid`` to translate.

    Mock mode bypasses Postgres + ClamAV + the worker queue, but the
    user-visible demo flow ('drag a PDF in chat, see it in the patient
    profile') requires the document to actually land in OpenEMR — that's
    what this function ensures.

    Why ``docker exec`` and not a network call:

      - OpenEMR's FHIR DocumentReference advertises ``create`` in
        the CapabilityStatement but the controller has no POST handler;
        every POST returns 404 "Route not found".

      - OpenEMR's standard ``/api/patient/{pid}/document`` POST works
        but requires a user-context token (ACL ``patients/docs/write``
        is checked against the OAuth user). The sidecar runs the
        client_credentials grant which produces system tokens; the
        ACL check fails. Switching grants is a much bigger refactor.

    Raises a ``RuntimeError`` on any failure path so the caller's
    ``except`` logs the structured cause.
    """
    import asyncio
    import base64
    import binascii
    import json

    if not patient_id.startswith("Patient/"):
        raise RuntimeError(
            f"patient_id must start with 'Patient/' (got {patient_id!r}). "
            "The launch token contract guarantees this prefix; if it's "
            "missing, the upstream code at the route handler is wrong."
        )
    fhir_uuid = patient_id.split("/", 1)[1]

    # Look up the legacy numeric pid for this FHIR uuid. The compose
    # path and database creds are well-known for the development-easy
    # stack; production deployments would parameterize via env.
    repo_root = _resolve_repo_root_for_docker_exec()
    compose_file = (
        repo_root / "docker" / "development-easy" / "docker-compose.yml"
    )
    if not compose_file.is_file():
        raise RuntimeError(
            f"docker-compose file not found at {compose_file!s}. The "
            "sidecar needs the OpenEMR compose stack to insert documents "
            "via cli-store-document.php. Either start the docker stack "
            "or add a non-mock path that uses the production OAuth flow."
        )

    pid = await _resolve_pid_from_fhir_uuid(compose_file, fhir_uuid)
    if pid is None:
        raise RuntimeError(
            f"No patient_data row has uuid matching FHIR uuid {fhir_uuid!r}. "
            "Either the patient was deleted between launch and upload, "
            "or the launch token's patient_id wasn't translated correctly."
        )

    # Encode the body for the CLI. base64 is simpler + safer than passing
    # raw bytes through the docker exec arg list (which doesn't handle
    # NULs or large args well).
    try:
        bytes_b64 = base64.b64encode(body).decode("ascii")
    except (binascii.Error, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Failed to base64-encode upload body: {exc}"
        ) from exc

    cli_path = (
        "/var/www/localhost/htdocs/openemr/interface/"
        "clinical_copilot/cli-store-document.php"
    )
    cmd = [
        "docker",
        "compose",
        "-f", str(compose_file),
        "exec", "-T", "openemr",
        "php", cli_path,
        f"--pid={pid}",
        "--category=Lab Report",
        f"--filename={filename}",
        f"--mime={mime_hint}",
        f"--bytes-base64={bytes_b64}",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        raise RuntimeError(
            f"cli-store-document.php exited {proc.returncode}. "
            f"stderr: {stderr[:500]!r}; stdout: {stdout[:500]!r}"
        )

    # The CLI's last stdout line is a JSON object on success.
    try:
        last_line = stdout.splitlines()[-1] if stdout else ""
        result = json.loads(last_line)
    except (IndexError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"cli-store-document.php exited 0 but stdout was not JSON. "
            f"stdout: {stdout[:500]!r}; stderr: {stderr[:500]!r}"
        ) from exc

    if "document_id" not in result:
        raise RuntimeError(
            f"cli-store-document.php returned JSON without document_id: "
            f"{result!r}"
        )

    logger.info(
        "mock_openemr_push_ok",
        extra={
            "document_id": result.get("document_id"),
            "patient_id": patient_id,
            "patient_pid": pid,
            # NOTE: don't put a key called 'filename' here — that name
            # is reserved by Python's LogRecord and the logger raises
            # 'Attempt to overwrite filename in LogRecord' if we try.
            # Same for 'name', 'message', 'levelname', 'pathname', etc.
            # See logging.LogRecord docs.
            "document_filename": filename,
            "byte_size": len(body),
            "category": result.get("category"),
        },
    )


def _resolve_repo_root_for_docker_exec():
    """Walk up from this file until we find docker/development-easy.

    The sidecar is checked into the OpenEMR repo at
    ``clinical-copilot/`` and the compose file is at
    ``docker/development-easy/docker-compose.yml``. Walking up from
    the current file's path finds the repo root without baking the
    layout into a constant — moving the sidecar inside the repo
    doesn't break this helper.
    """
    from pathlib import Path

    here = Path(__file__).resolve()
    for ancestor in here.parents:
        if (ancestor / "docker" / "development-easy").is_dir():
            return ancestor
    raise RuntimeError(
        "Could not locate the OpenEMR repo root from "
        f"{here!s}. Walked up to {here.parents[-1]!s} without finding "
        "docker/development-easy/. Either run the sidecar from inside "
        "the OpenEMR clone, or wire COPILOT_OPENEMR_REPO_ROOT in env."
    )


async def _resolve_pid_from_fhir_uuid(
    compose_file,
    fhir_uuid: str,
) -> int | None:
    """Look up the numeric pid for a FHIR uuid.

    The patient_data.uuid column stores the binary form (16 bytes); the
    FHIR uuid is the dashed string. We pass the string form as
    ``UNHEX(REPLACE(?, '-', ''))`` so the comparison works against the
    binary column.
    """
    import asyncio

    sql = (
        "SELECT pid FROM patient_data WHERE "
        "uuid = UNHEX(REPLACE('" + fhir_uuid.replace("'", "") + "', '-', '')) "
        "LIMIT 1;"
    )
    cmd = [
        "docker",
        "compose",
        "-f", str(compose_file),
        "exec", "-T", "openemr",
        "mysql", "-u", "openemr", "-popenemr", "openemr",
        "-N",  # skip column names header
        "-e", sql,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    out = stdout_bytes.decode("utf-8", errors="replace").strip()
    # Filter out the mariadb deprecation warning that prefixes stdout.
    for line in out.splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


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
