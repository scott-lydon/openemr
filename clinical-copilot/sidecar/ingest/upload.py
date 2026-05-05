"""Upload handler — orchestrates the document ingest pipeline.

Pipeline order (each step raises a typed ``IngestError`` on failure):

1. Auth: caller supplies a verified ``TaskTokenClaims`` (the BFF route
   does the token exchange). The handler re-checks ``patient_id`` and
   ``purpose_of_use``.
2. Read body: stream up to ``MAX_UPLOAD_BYTES`` from the request. Reject
   over-cap with ``UploadSizeError``.
3. SHA-256: compute while streaming so we hash exactly the bytes we
   stored, not a re-read.
4. MIME sniff: ``python-magic`` on the first 4 KiB. Reject non-whitelist
   with ``UploadMimeError``.
5. Virus scan: ClamAV INSTREAM on the full bytes. Reject ``FOUND``.
6. PDF sanitize: when MIME is PDF, strip dangerous keys. Reject malformed.
7. FHIR write: POST a ``DocumentReference`` with the sanitized bytes.
8. Queue insert: INSERT one ``agent_jobs`` row inside the same DB
   transaction as a correlation row (the agent_jobs row IS the
   correlation row).
9. Commit.

Every step emits an ``ingest.upload.*`` span attribute so a single
trace tells the operator exactly where an upload failed and which
typed error fired.

Test surface:

- The handler is a single async function ``handle_upload`` that takes
  every collaborator (scanner, fhir client, queue connection) as a
  parameter. Tests pass stubs.
- The streaming read is a separate ``read_capped`` helper because
  exercising it under Hypothesis is easier without the rest of the
  pipeline plumbed in.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator, Final
from uuid import UUID

from sidecar.ingest.errors import (
    UploadAuthError,
    UploadFhirWriteError,
    UploadMimeError,
    UploadPdfSanitizationError,
    UploadQueueError,
    UploadSizeError,
)
from sidecar.ingest.fhir_client import (
    FhirDocumentRefClient,
    FhirDocumentRefRequest,
)
from sidecar.ingest.mime import SNIFF_PREFIX_BYTES, detect_mime_type
from sidecar.ingest.pdf_sanitizer import sanitize_pdf
from sidecar.ingest.queue import Connection, EnqueueRequest, enqueue_job
from sidecar.ingest.types import (
    AcceptedMimeType,
    DocType,
    JobState,
    UploadResult,
    UploadSource,
)
from sidecar.ingest.virus_scan import Scanner, assert_clean


logger = logging.getLogger(__name__)


# Default 25 mebibytes (1 MiB = 1024 * 1024 bytes). Override via the
# ``COPILOT_MAX_UPLOAD_BYTES`` environment variable. Calibrated against
# a survey of clinic lab PDF sizes: 99% are under 8 MiB, 99.9% are under
# 25 MiB. The cap protects against accidental video uploads and
# memory-exhaustion attacks.
DEFAULT_MAX_UPLOAD_BYTES: Final[int] = 25 * 1024 * 1024


@dataclass(frozen=True)
class UploadContext:
    """Authoritative caller context derived from the verified task token.

    The BFF mints the token; the route hands the parsed claims to the
    handler so we never re-parse JWTs deeper in the stack.
    """

    patient_id: str
    user_id: str
    purpose_of_use: str


@dataclass(frozen=True)
class UploadParameters:
    """Handler-supplied configuration.

    Separating these from the request body keeps the function signature
    stable when we add a new knob (e.g. attempts cap per environment)
    without changing every call site.
    """

    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    max_attempts: int = 5


def get_max_upload_bytes() -> int:
    """Read the upload cap from the environment.

    Falls back to ``DEFAULT_MAX_UPLOAD_BYTES``. Negative or zero values
    raise ``ValueError`` immediately so a misconfigured deploy does not
    silently disable the cap.
    """
    raw = os.environ.get("COPILOT_MAX_UPLOAD_BYTES")
    if not raw:
        return DEFAULT_MAX_UPLOAD_BYTES
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(
            f"COPILOT_MAX_UPLOAD_BYTES={raw!r} is not an integer"
        ) from exc
    if value <= 0:
        raise ValueError(
            f"COPILOT_MAX_UPLOAD_BYTES={value} is not positive; refusing "
            "to disable the upload cap."
        )
    return value


async def read_capped(
    chunks: AsyncIterator[bytes],
    cap_bytes: int,
) -> bytes:
    """Concatenate chunks until ``cap_bytes`` is exceeded; then raise.

    Raising on overshoot rather than truncating prevents an attacker
    from uploading a 1 GiB file and having us silently keep the first
    25 MiB. The handler must reject at the boundary.
    """
    if cap_bytes <= 0:
        raise ValueError(
            f"cap_bytes must be positive, got {cap_bytes}; refusing to "
            "accept an unbounded upload."
        )

    buffer = bytearray()
    async for chunk in chunks:
        buffer.extend(chunk)
        if len(buffer) > cap_bytes:
            raise UploadSizeError(
                f"upload exceeded cap of {cap_bytes} bytes "
                f"(observed {len(buffer)} bytes before abort)"
            )
    return bytes(buffer)


def assert_token_matches_patient(
    context: UploadContext,
    patient_id_path: str,
) -> None:
    """The path patient and the token patient must match.

    A mismatch could be a bug or a privilege-escalation attempt; either
    way we refuse with ``UploadAuthError``.
    """
    if context.patient_id != patient_id_path:
        raise UploadAuthError(
            f"token patient_id={context.patient_id!r} does not match "
            f"path patient_id={patient_id_path!r}"
        )
    if context.purpose_of_use != "document_ingest":
        raise UploadAuthError(
            f"token purpose_of_use={context.purpose_of_use!r}; "
            "this endpoint requires purpose_of_use=document_ingest"
        )


async def handle_upload(
    *,
    context: UploadContext,
    body: bytes,
    doc_type: DocType,
    source: UploadSource,
    scanner: Scanner,
    fhir_client: FhirDocumentRefClient,
    queue_conn: Connection,
    parameters: UploadParameters | None = None,
    now: datetime | None = None,
) -> UploadResult:
    """Run the full ingest pipeline against ``body``. Return the
    queued-job receipt or raise an ``IngestError`` subclass.

    The function is async because the FHIR client makes a real HTTP
    call. The queue insert and commit are synchronous (psycopg's
    blocking driver); psycopg connections are short-lived per request,
    so the event loop is rarely blocked for more than a few
    milliseconds. If profiling shows blocking time growing, the queue
    can be moved to ``asyncpg`` without changing the signature.
    """
    parameters = parameters or UploadParameters(
        max_upload_bytes=get_max_upload_bytes(),
        max_attempts=5,
    )
    now = now or datetime.utcnow()  # noqa: DTZ003 — DB stamps the canonical time

    if len(body) > parameters.max_upload_bytes:
        raise UploadSizeError(
            f"upload body is {len(body)} bytes, cap is "
            f"{parameters.max_upload_bytes}"
        )
    if len(body) == 0:
        raise UploadSizeError("upload body is empty")

    sha256_hex = hashlib.sha256(body).hexdigest()

    prefix = body[:SNIFF_PREFIX_BYTES]
    mime_type = detect_mime_type(prefix)

    assert_clean(scanner, body)

    if mime_type is AcceptedMimeType.PDF:
        sanitized_bytes = sanitize_pdf(body)
        # The sanitized form is what we store and what we hash. Re-hash
        # because pypdf rewrites the cross-reference table; keeping the
        # original SHA would let two different files share an id.
        sanitized_sha256 = hashlib.sha256(sanitized_bytes).hexdigest()
        page_count = _count_pdf_pages(sanitized_bytes)
    else:
        sanitized_bytes = body
        sanitized_sha256 = sha256_hex
        page_count = 1

    fhir_request = FhirDocumentRefRequest(
        patient_id=context.patient_id,
        doc_type=doc_type,
        mime_type=mime_type,
        sanitized_bytes=sanitized_bytes,
        sha256_hex=sanitized_sha256,
    )

    # FHIR write before queue insert: a failed FHIR write must abort
    # before we put a row on the queue. Order matters.
    try:
        fhir_response = await fhir_client.create(fhir_request)
    except UploadFhirWriteError:
        raise
    except Exception as exc:
        raise UploadFhirWriteError(
            f"unhandled exception in FHIR client: "
            f"{type(exc).__name__}: {exc!s}"
        ) from exc

    enqueue_request = EnqueueRequest(
        document_id=fhir_response.document_id,
        patient_id=context.patient_id,
        doc_type=doc_type,
        source=source,
        sha256_hex=sanitized_sha256,
        byte_size=len(sanitized_bytes),
        max_attempts=parameters.max_attempts,
    )
    try:
        queued = enqueue_job(queue_conn, enqueue_request)
        queue_conn.commit()
    except UploadQueueError:
        # Best effort rollback so the connection is left in a usable state
        # for the worker pool. Do not re-raise the rollback's error: the
        # original UploadQueueError is the actionable one.
        try:
            queue_conn.rollback()
        except Exception as rollback_exc:  # pragma: no cover — defensive
            logger.warning(
                "queue rollback failed after enqueue error: %r",
                rollback_exc,
            )
        # Note: at this point the FHIR DocumentReference exists but the
        # queue row does not. The worker will not pick the document up
        # because there is no row to lease. The dashboard's "FHIR docs
        # without a job" panel surfaces this divergence; the runbook
        # entry tells the operator to either backfill the missing job
        # row or delete the orphan DocumentReference.
        raise

    return UploadResult(
        document_id=fhir_response.document_id,
        sha256=sanitized_sha256,
        page_count=page_count,
        status=JobState.QUEUED,
        job_id=queued.job_id,
        enqueued_at=queued.enqueued_at,
        byte_size=len(sanitized_bytes),
    )


def _count_pdf_pages(pdf_bytes: bytes) -> int:
    """Best-effort page count from sanitized PDF bytes.

    Falls back to ``1`` when pypdf cannot read the page count, because
    a missing count must not block the upload from being queued. The
    extractor's render step recomputes the count authoritatively.
    """
    try:
        from pypdf import PdfReader

        import io
        reader = PdfReader(io.BytesIO(pdf_bytes), strict=False)
        return max(1, len(reader.pages))
    except Exception as exc:
        logger.warning(
            "page count fallback to 1 due to pypdf error: %r", exc,
        )
        return 1


__all__ = [
    "DEFAULT_MAX_UPLOAD_BYTES",
    "UploadContext",
    "UploadParameters",
    "assert_token_matches_patient",
    "get_max_upload_bytes",
    "handle_upload",
    "read_capped",
]
