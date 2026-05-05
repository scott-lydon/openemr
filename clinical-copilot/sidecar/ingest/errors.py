"""Typed errors raised by the document ingest pipeline.

Every error carries three fields:

- ``code`` — machine readable, stable across releases. Matched by the eval
  suite, dashboards, and the queue's ``last_error->>'code'`` column.
- ``http_status`` — the HTTP status code the BFF should surface to the
  caller. Picked once here so handlers do not have to remember the
  mapping.
- ``debug_hint`` — a short string a developer can read in a trace and
  immediately know what to look at. Never includes Personal Health
  Information (PHI); the caller may safely log it.

The base class ``IngestError`` extends ``Exception`` (not ``ValueError``)
so callers using ``except Exception`` catch it without ambiguity, and
callers using ``except ValueError`` for input parsing do not accidentally
swallow a virus-scan failure.

Test invariant: every concrete subclass carries a non-empty ``code``,
a 4xx or 5xx ``http_status``, and a non-empty ``debug_hint``. The repo
hygiene test in ``tests/sidecar/w2/test_upload.py`` enforces this.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class _ErrorMeta:
    """Class-level metadata for a typed ingest error.

    ``code`` is the stable identifier surfaced to clients and dashboards.
    ``http_status`` is the HTTP code the BFF returns. ``debug_hint`` is
    a human-readable suggestion for a developer reading the stack trace.
    """

    code: str
    http_status: int
    debug_hint: str


class IngestError(Exception):
    """Base class for every typed error in the document ingest pipeline.

    Subclasses set ``META`` once; ``__init__`` then composes a single
    formatted message that includes the code, the HTTP status, and the
    debug hint, plus an optional caller-supplied detail string.

    The ``detail`` parameter is for context that varies per occurrence
    (the rejected MIME, the byte size, the file name). Never put PHI in
    ``detail`` — exceptions are logged.
    """

    META: _ErrorMeta = _ErrorMeta(
        code="ingest_error",
        http_status=500,
        debug_hint="Subclass IngestError before raising.",
    )

    def __init__(self, detail: str = "") -> None:
        self.detail: Final[str] = detail
        message = (
            f"[{self.META.code} http={self.META.http_status}] "
            f"{self.META.debug_hint}"
        )
        if detail:
            message = f"{message} | {detail}"
        super().__init__(message)

    @property
    def code(self) -> str:
        return self.META.code

    @property
    def http_status(self) -> int:
        return self.META.http_status

    @property
    def debug_hint(self) -> str:
        return self.META.debug_hint


class UploadAuthError(IngestError):
    """The task token is missing, malformed, expired, or scope-mismatched.

    Most often: the BFF minted a token without
    ``purpose_of_use="document_ingest"``, or the token's ``patient_id``
    claim does not match the path parameter.
    """

    META = _ErrorMeta(
        code="upload_auth",
        http_status=401,
        debug_hint=(
            "Authorization failed. Confirm the BFF minted a token with "
            "purpose_of_use=document_ingest and the patient_id claim "
            "matches the URL. Check sidecar.auth verifier for the exact "
            "subclass (TaskTokenExpiredError, TaskTokenSignatureError, etc.)."
        ),
    )


class UploadMimeError(IngestError):
    """python-magic sniffed a Multipurpose Internet Mail Extensions (MIME)
    type the upload pipeline does not accept.

    Whitelist: ``application/pdf``, ``image/png``, ``image/jpeg``,
    ``image/tiff``. The decision uses the file content (magic bytes), not
    the client-supplied ``Content-Type``, because the latter is trivially
    spoofed.
    """

    META = _ErrorMeta(
        code="upload_mime_rejected",
        http_status=415,
        debug_hint=(
            "MIME sniffer rejected the upload. Whitelist is "
            "application/pdf, image/png, image/jpeg, image/tiff. "
            "Confirm the client is sending raw bytes (not a JSON-wrapped "
            "base64 blob), and that the file is what the client claims."
        ),
    )


class UploadSizeError(IngestError):
    """The upload exceeds the configured byte cap.

    Default cap is 25 mebibytes (MAX_UPLOAD_BYTES env var overrides).
    The cap is enforced while streaming so an attacker cannot exhaust
    memory by sending an unbounded body.
    """

    META = _ErrorMeta(
        code="upload_size_exceeded",
        http_status=413,
        debug_hint=(
            "Upload exceeds the configured byte cap. Raise "
            "COPILOT_MAX_UPLOAD_BYTES if a legitimate clinical workflow "
            "needs larger files; otherwise confirm the client is not "
            "uploading raw scanner output without compression."
        ),
    )


class UploadVirusError(IngestError):
    """ClamAV's INSTREAM scan returned ``FOUND``.

    The signature name is captured into ``detail`` for the audit log and
    the dashboard, but never returned to the client (returning the
    signature could leak information about the detection ruleset).
    """

    META = _ErrorMeta(
        code="upload_virus_detected",
        http_status=422,
        debug_hint=(
            "ClamAV detected a known signature. Inspect the detail field "
            "for the signature name; confirm clamd is reachable on its "
            "configured Unix socket; investigate the source of the upload "
            "(an authenticated session producing virus uploads is itself "
            "an incident)."
        ),
    )


class UploadPdfSanitizationError(IngestError):
    """The Portable Document Format (PDF) sanitizer found a payload it
    cannot strip safely.

    PDFs with JavaScript actions, embedded files, or external entity
    references are stripped via a pypdf rewrite. If the rewrite itself
    raises (corrupt PDF, encrypted document with no password, malformed
    xref table) the upload is rejected because we will not store a PDF
    we cannot sanitize.
    """

    META = _ErrorMeta(
        code="upload_pdf_sanitization_failed",
        http_status=422,
        debug_hint=(
            "PDF sanitization failed. Likely causes: corrupt xref table, "
            "encrypted PDF with no password, or a malformed object stream. "
            "Inspect the underlying pypdf exception in the trace; do not "
            "store the unsanitized bytes."
        ),
    )


class UploadFhirWriteError(IngestError):
    """The OpenEMR Fast Healthcare Interoperability Resources (FHIR)
    DocumentReference create call failed.

    Two-phase commit hazard: if the FHIR write succeeds but the queue
    insert fails, we end up with an orphan DocumentReference. The
    upload handler runs the queue insert in the same database
    transaction as the FHIR write's correlation row, and only commits
    after the FHIR write returns 201, so the orphan window is bounded
    by the time between the FHIR ack and the local commit.
    """

    META = _ErrorMeta(
        code="upload_fhir_write_failed",
        http_status=502,
        debug_hint=(
            "OpenEMR FHIR DocumentReference create failed. Check "
            "openemr-fhir reachability, OAuth token validity, and the "
            "server-side audit log for a 4xx that names the rejected "
            "field. If the server is healthy and the request is valid, "
            "look for a transient network blip and retry."
        ),
    )


class UploadQueueError(IngestError):
    """The agent_jobs queue insert failed.

    Almost always a Postgres reachability or schema issue. The queue is
    Postgres so we get atomic inserts; if the queue is down the upload
    fails closed (no DocumentReference is committed) rather than
    accepting work the system cannot eventually process.
    """

    META = _ErrorMeta(
        code="upload_queue_insert_failed",
        http_status=503,
        debug_hint=(
            "Queue insert failed. Check Postgres reachability "
            "(COPILOT_DATABASE_URL), confirm the agent_jobs migration ran, "
            "and verify the connection pool is not exhausted. The upload "
            "rolls back the FHIR write to avoid orphan documents."
        ),
    )


__all__ = [
    "IngestError",
    "UploadAuthError",
    "UploadFhirWriteError",
    "UploadMimeError",
    "UploadPdfSanitizationError",
    "UploadQueueError",
    "UploadSizeError",
    "UploadVirusError",
]
