"""Data Transfer Objects (DTOs) shared across the document ingest pipeline.

Every DTO is a frozen Pydantic model with ``extra='forbid'``. The MIME
whitelist is a closed enum (``AcceptedMimeType``) so a typo in a handler
fails at import time rather than producing a silent miss.

Why frozen:

- The DTO is passed across thread boundaries (the queue worker reads
  rows the upload handler wrote). Frozen-by-default means a downstream
  caller cannot accidentally mutate state another caller is reading.
- The DTO is hashed into a deterministic FHIR resource identifier in
  Phase 3. Mutability would invalidate the hash.

Acronyms used in this module:

- ``MIME`` — Multipurpose Internet Mail Extensions, the content type
  taxonomy.
- ``SHA-256`` — Secure Hash Algorithm 256, used for content addressing.
- ``UUID`` — Universally Unique Identifier, used as the queue primary key.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AcceptedMimeType(str, Enum):
    """The MIME types the upload pipeline accepts.

    Closed enum: any value the MIME sniffer returns that is not in this
    set raises ``UploadMimeError``. Adding a new format requires a code
    change AND a new adversarial fixture in ``evals/golden_w2/fixtures``,
    so the surface area review is enforced.
    """

    PDF = "application/pdf"
    PNG = "image/png"
    JPEG = "image/jpeg"
    TIFF = "image/tiff"


class DocType(str, Enum):
    """The kind of clinical document the upload represents.

    The supervisor uses ``doc_type`` to pick the extractor (lab vs intake
    vs referral). The choice is supplied by the caller because the user
    interface knows it from the upload widget; we do not infer it from
    file content.
    """

    LAB_PDF = "lab_pdf"
    INTAKE_FORM = "intake_form"
    REFERRAL_FAX = "referral_fax"


class UploadSource(str, Enum):
    """How the document arrived at the system.

    The source attribute appears on the FHIR DocumentReference and on
    every span; it changes how the audit log groups events.
    """

    UPLOAD = "upload"
    FAX = "fax"
    PORTAL = "portal"


class JobState(str, Enum):
    """Queue state machine. Mirrors the database CHECK constraint.

    The mirror is intentional duplication: the database enforces the
    contract for any client, the enum enforces it inside the worker.
    Dropping the database CHECK would not change behavior, but dropping
    the enum would silently allow a typo in worker code.
    """

    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"
    CANCELLED = "cancelled"


class UploadResult(BaseModel):
    """Response body returned to the BFF after an accepted upload.

    Carries the queue job identifier so the user interface can poll for
    status; carries the SHA-256 so a deduplication test can match the
    upload to a known fixture without re-hashing.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    document_id: str
    sha256: str = Field(min_length=64, max_length=64)
    page_count: int = Field(ge=0)
    status: JobState
    job_id: UUID
    enqueued_at: datetime
    byte_size: int = Field(ge=1)


class QueuedJob(BaseModel):
    """A row from ``agent_jobs`` the worker has just leased.

    The worker constructs this DTO from a ``SELECT ... FOR UPDATE SKIP
    LOCKED`` row before doing any work, so every downstream call (FHIR
    fetch, extractor, persist) accepts a typed parameter rather than a
    bag of column values.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    job_id: UUID
    document_id: str
    patient_id: str
    doc_type: DocType
    source: UploadSource
    sha256: str = Field(min_length=64, max_length=64)
    byte_size: int = Field(ge=1)
    attempt_count: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    enqueued_at: datetime


__all__ = [
    "AcceptedMimeType",
    "DocType",
    "JobState",
    "QueuedJob",
    "UploadResult",
    "UploadSource",
]
