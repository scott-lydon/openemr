"""Document ingest pipeline.

Public surface re-exported from this package so handlers can write
``from sidecar.ingest import handle_upload`` without reaching into
module paths. Every error type, every typed parameter, and the upload
entry point are exported.

Pipeline modules:

- ``errors`` — typed exceptions every handler may raise.
- ``types`` — Data Transfer Objects (DTOs) shared across modules.
- ``mime`` — content-type sniffing via libmagic.
- ``virus_scan`` — ClamAV INSTREAM scan.
- ``pdf_sanitizer`` — strips JavaScript and other dangerous PDF features.
- ``fhir_client`` — DocumentReference create on the OpenEMR FHIR endpoint.
- ``queue`` — Postgres-backed agent_jobs table operations.
- ``upload`` — orchestrates steps 1-9 of the upload pipeline.
- ``worker`` — long-lived consumer loop for queued jobs.
"""

from sidecar.ingest.errors import (
    IngestError,
    UploadAuthError,
    UploadFhirWriteError,
    UploadMimeError,
    UploadPdfSanitizationError,
    UploadQueueError,
    UploadSizeError,
    UploadVirusError,
)
from sidecar.ingest.types import (
    AcceptedMimeType,
    DocType,
    JobState,
    QueuedJob,
    UploadResult,
    UploadSource,
)
from sidecar.ingest.upload import (
    DEFAULT_MAX_UPLOAD_BYTES,
    UploadContext,
    UploadParameters,
    assert_token_matches_patient,
    get_max_upload_bytes,
    handle_upload,
    read_capped,
)

__all__ = [
    "AcceptedMimeType",
    "DEFAULT_MAX_UPLOAD_BYTES",
    "DocType",
    "IngestError",
    "JobState",
    "QueuedJob",
    "UploadAuthError",
    "UploadContext",
    "UploadFhirWriteError",
    "UploadMimeError",
    "UploadParameters",
    "UploadPdfSanitizationError",
    "UploadQueueError",
    "UploadResult",
    "UploadSizeError",
    "UploadSource",
    "UploadVirusError",
    "assert_token_matches_patient",
    "get_max_upload_bytes",
    "handle_upload",
    "read_capped",
]
