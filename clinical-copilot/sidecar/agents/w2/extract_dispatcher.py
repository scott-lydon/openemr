"""Bind the queue worker's ``ExtractFn`` to the render → extract → persist
chain.

The Phase 2 worker accepts a single ``ExtractFn`` callable. This module
provides ``build_extract_fn`` which wires up:

1. **Read the source bytes** for ``job.document_id`` from the OpenEMR
   ``DocumentReference`` resource.
2. **Render** the PDF to per-page images plus native text.
3. **Dispatch** to the lab or intake extractor based on ``job.doc_type``.
4. **Persist** the extracted fields to FHIR.

The dispatcher is the only place that knows how to glue the four
modules together. Everything below it (render, extractors, persist) is
testable in isolation; everything above (the worker) accepts the
callable and does not know what is inside.

Reading the source bytes:

- The worker has a job, but only the document id (not the bytes). The
  bytes were written to the OpenEMR FHIR ``DocumentReference`` at
  upload time. The dispatcher fetches them back via ``DocumentSource``
  (a thin protocol; production reads from FHIR, tests read from a
  fixture map).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from sidecar.agents.w2.intake_extractor import extract_intake_pdf
from sidecar.agents.w2.lab_extractor import extract_lab_pdf
from sidecar.agents.w2.vlm_client import VlmClient
from sidecar.ingest.errors import IngestError, _ErrorMeta
from sidecar.ingest.persist import (
    FhirPersistClient,
    persist_intake_extraction,
    persist_lab_extraction,
)
from sidecar.ingest.render import extract_native_text, render_pages
from sidecar.ingest.types import DocType, QueuedJob


logger = logging.getLogger(__name__)


class DocumentSourceError(IngestError):
    META = _ErrorMeta(
        code="document_source_failed",
        http_status=502,
        debug_hint=(
            "Could not read source bytes for the document_id named in the "
            "job. The DocumentReference may have been deleted, or the FHIR "
            "server is unavailable. The job will retry."
        ),
    )


class UnsupportedDocTypeError(IngestError):
    META = _ErrorMeta(
        code="unsupported_doc_type",
        http_status=400,
        debug_hint=(
            "The job's doc_type is not handled by any registered extractor. "
            "Either register a new extractor in extract_dispatcher or update "
            "the upload route's doc_type whitelist."
        ),
    )


class DocumentSource(Protocol):
    """Read the sanitized source bytes of a stored document.

    Production fetches from OpenEMR's FHIR ``DocumentReference`` and
    base64-decodes ``content[0].attachment.data``. Tests substitute a
    fixture-backed implementation.
    """

    async def fetch(self, document_id: str) -> bytes:
        ...


@dataclass
class StubDocumentSource:
    """In-memory fixture map used by unit tests."""

    by_id: dict[str, bytes]

    async def fetch(self, document_id: str) -> bytes:
        if document_id not in self.by_id:
            raise DocumentSourceError(
                f"document_id={document_id!r} not in stub fixture map"
            )
        return self.by_id[document_id]


def build_extract_fn(
    *,
    document_source: DocumentSource,
    vlm_client: VlmClient,
    persist_client: FhirPersistClient,
) -> Callable[[QueuedJob], Awaitable[None]]:
    """Return an ``ExtractFn`` the worker can call.

    The returned coroutine fetches the document bytes, renders, runs the
    matching extractor, and persists. Any typed ``IngestError`` raised
    propagates to the worker, which then schedules the queue retry per
    Phase 2's exponential-backoff contract.
    """

    async def extract(job: QueuedJob) -> None:
        try:
            pdf_bytes = await document_source.fetch(job.document_id)
        except IngestError:
            raise
        except Exception as exc:
            raise DocumentSourceError(
                f"document_id={job.document_id!r} fetch failed: "
                f"{type(exc).__name__}: {exc!s}"
            ) from exc

        pages = render_pages(pdf_bytes)
        native_text = extract_native_text(pdf_bytes)

        if job.doc_type is DocType.LAB_PDF:
            extraction = await extract_lab_pdf(
                document_id=job.document_id,
                document_sha256=job.sha256,
                patient_id=job.patient_id,
                pages=pages,
                page_native_text=native_text,
                vlm_client=vlm_client,
            )
            resource_ids = await persist_lab_extraction(
                extraction=extraction, client=persist_client
            )
            logger.info(
                "lab extraction persisted document_id=%s resource_count=%d",
                job.document_id, len(resource_ids),
            )
            return

        if job.doc_type is DocType.INTAKE_FORM:
            intake = await extract_intake_pdf(
                document_id=job.document_id,
                document_sha256=job.sha256,
                patient_id=job.patient_id,
                pages=pages,
                page_native_text=native_text,
                vlm_client=vlm_client,
            )
            resource_ids = await persist_intake_extraction(
                extraction=intake, client=persist_client
            )
            logger.info(
                "intake extraction persisted document_id=%s resource_count=%d",
                job.document_id, len(resource_ids),
            )
            return

        if job.doc_type is DocType.REFERRAL_FAX:
            # Referral fax extractor is the Phase 11 extension. Until it
            # lands, mark the job permanently failed (retrying will not
            # help) so it goes to dead-letter on the first attempt and
            # operators see it explicitly.
            raise UnsupportedDocTypeError(
                "referral_fax is a Phase 11 extension; the extractor is "
                "not yet wired. Either disable referral uploads in the "
                "BFF doc_type whitelist or land Phase 11."
            )

        raise UnsupportedDocTypeError(
            f"doc_type={job.doc_type.value!r} has no registered extractor"
        )

    return extract


__all__ = [
    "DocumentSource",
    "DocumentSourceError",
    "StubDocumentSource",
    "UnsupportedDocTypeError",
    "build_extract_fn",
]
