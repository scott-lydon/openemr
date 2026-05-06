"""Tests for the Week 2 document upload pipeline.

Coverage:

- Typed errors carry the expected HTTP status, code, and debug hint.
- MIME sniffer accepts each whitelist type and rejects others.
- Virus scanner stub catches the EICAR test signature.
- PDF sanitizer strips ``/JavaScript`` and ``/OpenAction``.
- Upload handler happy path: writes one DocumentReference, inserts one
  ``agent_jobs`` row, returns a queued ``UploadResult``.
- Upload handler rejects oversized, empty, wrong-MIME, and infected
  bodies with the matching typed error.
- Upload handler does not commit the queue insert when the FHIR write
  raises (idempotency / transactional safety).
- Queue worker happy path: leases one job, runs the supplied extractor,
  marks it ``done``.
- Queue worker on typed error: marks failure, schedules a retry, then a
  dead letter when ``attempt_count`` hits ``max_attempts``.
- Hypothesis property test: random byte sequences either succeed or
  raise a typed ``IngestError`` — never crash the handler.

The fake queue connection is a thin in-memory model of the agent_jobs
table that exercises every SQL statement the queue module emits without
spinning up a real Postgres. Integration tests against a real database
live in the docker-compose-test stack and run in CI nightly; these unit
tests run on every change.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator

import pytest

from sidecar.ingest import (
    AcceptedMimeType,
    DocType,
    IngestError,
    JobState,
    UploadAuthError,
    UploadContext,
    UploadFhirWriteError,
    UploadMimeError,
    UploadParameters,
    UploadPdfSanitizationError,
    UploadQueueError,
    UploadSizeError,
    UploadSource,
    UploadVirusError,
    assert_token_matches_patient,
    handle_upload,
    read_capped,
)
from sidecar.ingest.errors import _ErrorMeta  # type: ignore[attr-defined]
from sidecar.ingest.fhir_client import StubFhirClient
from sidecar.ingest.mime import detect_mime_type
from sidecar.ingest.pdf_sanitizer import sanitize_pdf
from sidecar.ingest.queue import (
    EnqueueRequest,
    compute_backoff_seconds,
    enqueue_job,
    fail_job,
    lease_one_job,
    complete_job,
)
from sidecar.ingest.virus_scan import (
    EICAR_TEST_BYTES,
    ScanResult,
    Scanner,
    StubScanner,
    assert_clean,
)
from sidecar.ingest.worker import run_one_iteration


# ─── Helpers ──────────────────────────────────────────────────────────


def _minimal_pdf_bytes() -> bytes:
    """A tiny but valid one-page PDF.

    Re-built each test rather than fetched from disk so the unit-test
    runtime stays self-contained and the bytes are deterministic.
    """
    # The simplest legal PDF accepted by pypdf. The xref offsets matter:
    # they are computed by hand to point to the corresponding object
    # blocks. Verified against pypdf 5.x.
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>\nendobj\n"
        b"xref\n0 4\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000111 00000 n \n"
        b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n177\n%%EOF\n"
    )


def _malicious_pdf_bytes() -> bytes:
    """A small PDF declaring a JavaScript ``/OpenAction``."""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R "
        b"/OpenAction << /S /JavaScript /JS (app.alert\\('hi'\\);) >> "
        b">>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>\nendobj\n"
        b"xref\n0 4\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000130 00000 n \n"
        b"0000000183 00000 n \n"
        b"trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n249\n%%EOF\n"
    )


@dataclass
class _FakeRow:
    """One row of the in-memory ``agent_jobs`` model."""

    job_id: str
    document_id: str
    patient_id: str
    doc_type: str
    source: str
    sha256: str
    byte_size: int
    state: str
    enqueued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_error: str | None = None
    attempt_count: int = 0
    max_attempts: int = 5
    next_attempt_at: datetime = field(
        default_factory=lambda: datetime.now(tz=timezone.utc)
    )


class FakeCursor:
    """Cursor stub that lets ``fetchone`` return whatever the connection
    pre-loaded for the most recent execute call."""

    def __init__(self, queued_rows: list[tuple[Any, ...]]) -> None:
        self._rows = list(queued_rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        if not self._rows:
            return None
        return self._rows.pop(0)

    def fetchall(self) -> list[tuple[Any, ...]]:
        rows, self._rows = self._rows, []
        return rows


class FakeQueueConnection:
    """In-memory model of the ``agent_jobs`` table.

    Pattern-matches on the SQL emitted by ``sidecar.ingest.queue``. This
    is intentionally narrow: a change in the queue module's SQL must be
    accompanied by a change here, which is the point — the test enforces
    SQL stability the same way a database CHECK enforces state stability.
    """

    def __init__(self) -> None:
        self.rows: dict[str, _FakeRow] = {}
        self._committed = False
        self._rolled_back = False
        self.commits = 0
        self.rollbacks = 0

    @property
    def is_committed(self) -> bool:
        return self._committed

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> FakeCursor:
        params = params or ()
        sql_compact = " ".join(sql.split())
        if "INSERT INTO agent_jobs" in sql_compact:
            row = _FakeRow(
                job_id=str(params[0]),
                document_id=str(params[1]),
                patient_id=str(params[2]),
                doc_type=str(params[3]),
                source=str(params[4]),
                sha256=str(params[5]),
                byte_size=int(params[6]),
                state="queued",
                enqueued_at=params[7],
                attempt_count=0,
                max_attempts=int(params[8]),
                next_attempt_at=params[9],
            )
            self.rows[row.job_id] = row
            return FakeCursor([])
        if sql_compact.startswith("UPDATE agent_jobs SET state='running'"):
            ready = sorted(
                (r for r in self.rows.values()
                 if r.state == "queued"
                 and r.next_attempt_at <= datetime.now(tz=timezone.utc)),
                key=lambda r: r.next_attempt_at,
            )
            if not ready:
                return FakeCursor([])
            row = ready[0]
            row.state = "running"
            row.started_at = datetime.now(tz=timezone.utc)
            row.attempt_count += 1
            return FakeCursor(
                [(row.job_id, row.document_id, row.patient_id, row.doc_type,
                  row.source, row.sha256, row.byte_size, row.attempt_count,
                  row.max_attempts, row.enqueued_at)]
            )
        if "SET state='done'" in sql_compact:
            job_id = str(params[0])
            row = self.rows.get(job_id)
            if row and row.state == "running":
                row.state = "done"
                row.finished_at = datetime.now(tz=timezone.utc)
            return FakeCursor([])
        if sql_compact.startswith("SELECT attempt_count, max_attempts"):
            job_id = str(params[0])
            row = self.rows.get(job_id)
            if row is None or row.state != "running":
                return FakeCursor([])
            return FakeCursor([(row.attempt_count, row.max_attempts)])
        if "state='dead_letter'" in sql_compact:
            error_payload, job_id = params
            row = self.rows[str(job_id)]
            row.state = "dead_letter"
            row.last_error = error_payload
            row.finished_at = datetime.now(tz=timezone.utc)
            return FakeCursor([])
        if re.search(r"state='queued',\s*last_error", sql_compact):
            error_payload, job_id = params
            row = self.rows[str(job_id)]
            row.state = "queued"
            row.last_error = error_payload
            # Use the SQL's INTERVAL value to stamp next_attempt_at; we
            # parse the embedded literal because the queue module
            # interpolates the backoff inline.
            match = re.search(r"INTERVAL '(\d+) seconds'", sql)
            if match:
                seconds = int(match.group(1))
            else:
                seconds = 1
            row.next_attempt_at = datetime.now(tz=timezone.utc) + timedelta(seconds=seconds)
            return FakeCursor([])
        if "FROM agent_jobs GROUP BY state" in sql_compact:
            counts: dict[str, int] = {}
            for r in self.rows.values():
                counts[r.state] = counts.get(r.state, 0) + 1
            return FakeCursor([(state, count) for state, count in counts.items()])
        raise AssertionError(f"FakeQueueConnection: unexpected SQL {sql_compact!r}")

    def commit(self) -> None:
        self._committed = True
        self.commits += 1

    def rollback(self) -> None:
        self._rolled_back = True
        self.rollbacks += 1


# ─── Typed error invariants ───────────────────────────────────────────


def test_every_typed_error_has_full_metadata() -> None:
    """Every concrete IngestError carries code, http status, debug hint."""
    for cls in [
        UploadAuthError,
        UploadMimeError,
        UploadSizeError,
        UploadVirusError,
        UploadPdfSanitizationError,
        UploadFhirWriteError,
        UploadQueueError,
    ]:
        meta = cls.META  # type: ignore[attr-defined]
        assert isinstance(meta, _ErrorMeta), f"{cls.__name__} META must be _ErrorMeta"
        assert meta.code, f"{cls.__name__} META.code must be non-empty"
        assert 400 <= meta.http_status < 600, (
            f"{cls.__name__} META.http_status must be 4xx or 5xx, got "
            f"{meta.http_status}"
        )
        assert meta.debug_hint, f"{cls.__name__} META.debug_hint must be non-empty"


def test_typed_error_message_includes_code_status_and_detail() -> None:
    """Stringifying the error should be enough to triage from a stack trace."""
    err = UploadMimeError("detected_mime='text/plain'")
    rendered = str(err)
    assert "[upload_mime_rejected http=415]" in rendered
    assert "detected_mime='text/plain'" in rendered


# ─── MIME sniffing ────────────────────────────────────────────────────


# A real 1x1 transparent PNG. libmagic validates the IHDR chunk before
# returning ``image/png``, so we cannot use synthetic ``\x89PNG`` + zeros.
_MINIMAL_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cb\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.mark.parametrize(
    "prefix, expected",
    [
        (b"%PDF-1.4\n%abc\n", AcceptedMimeType.PDF),
        (_MINIMAL_PNG_BYTES, AcceptedMimeType.PNG),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 20, AcceptedMimeType.JPEG),
        (b"II*\x00" + b"\x00" * 20, AcceptedMimeType.TIFF),
        (b"MM\x00*" + b"\x00" * 20, AcceptedMimeType.TIFF),
    ],
)
def test_detect_mime_type_accepts_whitelist(
    prefix: bytes, expected: AcceptedMimeType
) -> None:
    """Each whitelist format is recognized by its magic bytes."""
    assert detect_mime_type(prefix) is expected


@pytest.mark.parametrize(
    "prefix, expected",
    [
        (b"%PDF-1.4\n", "application/pdf"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 20, "image/png"),
        (b"\xff\xd8\xff" + b"\x00" * 20, "image/jpeg"),
        (b"II*\x00" + b"\x00" * 20, "image/tiff"),
        (b"MM\x00*" + b"\x00" * 20, "image/tiff"),
        (b"plain text", "application/octet-stream"),
    ],
)
def test_fallback_sniff_recognizes_magic_bytes(
    prefix: bytes, expected: str
) -> None:
    """The pure-Python fallback sniffer is used when libmagic is missing.

    The fallback recognizes the four whitelist formats by their documented
    magic bytes; everything else is ``application/octet-stream`` so the
    caller rejects.
    """
    from sidecar.ingest.mime import _fallback_sniff

    assert _fallback_sniff(prefix) == expected


def test_detect_mime_type_rejects_text() -> None:
    """A plain-text body is rejected with UploadMimeError(415)."""
    with pytest.raises(UploadMimeError) as info:
        detect_mime_type(b"this is not a clinical document\n")
    assert info.value.http_status == 415


def test_detect_mime_type_rejects_empty() -> None:
    """An empty body is rejected, not silently passed."""
    with pytest.raises(UploadMimeError):
        detect_mime_type(b"")


# ─── Virus scanner ────────────────────────────────────────────────────


def test_virus_scanner_catches_eicar() -> None:
    """The default StubScanner catches the EICAR test signature."""
    scanner = StubScanner()
    result = scanner.scan_bytes(b"prefix " + EICAR_TEST_BYTES + b" suffix")
    assert result.infected
    assert result.signature == "Eicar-Test-Signature"


def test_assert_clean_raises_typed_virus_error() -> None:
    """assert_clean raises UploadVirusError with the signature in detail."""
    scanner = StubScanner()
    with pytest.raises(UploadVirusError) as info:
        assert_clean(scanner, EICAR_TEST_BYTES)
    assert "Eicar-Test-Signature" in str(info.value)
    assert info.value.http_status == 422


def test_assert_clean_passes_for_clean_bytes() -> None:
    """A clean body does not raise."""
    scanner = StubScanner()
    assert_clean(scanner, b"clean clinical content")


# ─── PDF sanitizer ────────────────────────────────────────────────────


def test_sanitize_pdf_strips_javascript_action() -> None:
    """A PDF with /OpenAction /S /JavaScript loses the action on rewrite."""
    sanitized = sanitize_pdf(_malicious_pdf_bytes())
    assert b"/JavaScript" not in sanitized, (
        "sanitizer left a /JavaScript token in the rewritten PDF"
    )
    # And the rewritten bytes are still a valid PDF (start with %PDF-).
    assert sanitized.startswith(b"%PDF-")


def test_sanitize_pdf_keeps_clean_pdf_parseable() -> None:
    """A clean PDF survives the sanitizer with the same byte structure."""
    clean = _minimal_pdf_bytes()
    rewritten = sanitize_pdf(clean)
    assert rewritten.startswith(b"%PDF-")
    # The rewrite is allowed to renumber objects; we only assert the
    # output is still a parseable PDF.
    from pypdf import PdfReader
    import io
    PdfReader(io.BytesIO(rewritten), strict=False)


def test_sanitize_pdf_rejects_empty() -> None:
    with pytest.raises(UploadPdfSanitizationError):
        sanitize_pdf(b"")


def test_sanitize_pdf_rejects_garbage() -> None:
    with pytest.raises(UploadPdfSanitizationError):
        sanitize_pdf(b"not a pdf at all")


# ─── Auth ─────────────────────────────────────────────────────────────


def test_assert_token_matches_patient_passes() -> None:
    ctx = UploadContext(
        patient_id="Patient/87413",
        user_id="dr.m@example.org",
        purpose_of_use="document_ingest",
    )
    assert_token_matches_patient(ctx, "Patient/87413")  # no raise


def test_assert_token_matches_patient_rejects_mismatch() -> None:
    ctx = UploadContext(
        patient_id="Patient/87413",
        user_id="dr.m@example.org",
        purpose_of_use="document_ingest",
    )
    with pytest.raises(UploadAuthError):
        assert_token_matches_patient(ctx, "Patient/99999")


def test_assert_token_matches_patient_requires_correct_purpose() -> None:
    ctx = UploadContext(
        patient_id="Patient/87413",
        user_id="dr.m@example.org",
        purpose_of_use="diagnostic_cross_check",
    )
    with pytest.raises(UploadAuthError):
        assert_token_matches_patient(ctx, "Patient/87413")


# ─── Capped read ──────────────────────────────────────────────────────


async def _ait(items: list[bytes]) -> AsyncIterator[bytes]:
    for item in items:
        yield item


async def test_read_capped_concatenates_chunks() -> None:
    chunks = [b"hello ", b"world"]
    result = await read_capped(_ait(chunks), cap_bytes=100)
    assert result == b"hello world"


async def test_read_capped_aborts_on_overshoot() -> None:
    chunks = [b"a" * 60, b"b" * 60]
    with pytest.raises(UploadSizeError):
        await read_capped(_ait(chunks), cap_bytes=100)


async def test_read_capped_rejects_zero_cap() -> None:
    with pytest.raises(ValueError):
        await read_capped(_ait([b"x"]), cap_bytes=0)


# ─── Upload handler ───────────────────────────────────────────────────


async def _run_upload(
    *,
    body: bytes,
    scanner: Scanner | None = None,
    fhir_client: StubFhirClient | None = None,
    queue_conn: FakeQueueConnection | None = None,
    doc_type: DocType = DocType.LAB_PDF,
    parameters: UploadParameters | None = None,
) -> tuple[Any, FakeQueueConnection, StubFhirClient]:
    scanner = scanner or StubScanner()
    fhir_client = fhir_client or StubFhirClient(document_id="docref-001")
    queue_conn = queue_conn or FakeQueueConnection()
    parameters = parameters or UploadParameters(max_upload_bytes=1024 * 1024, max_attempts=5)
    context = UploadContext(
        patient_id="Patient/87413",
        user_id="dr.m@example.org",
        purpose_of_use="document_ingest",
    )
    result = await handle_upload(
        context=context,
        body=body,
        doc_type=doc_type,
        source=UploadSource.UPLOAD,
        scanner=scanner,
        fhir_client=fhir_client,
        queue_conn=queue_conn,
        parameters=parameters,
    )
    return result, queue_conn, fhir_client


async def test_handle_upload_happy_path_pdf() -> None:
    """A clean PDF round-trips through every step."""
    result, queue, fhir_client = await _run_upload(body=_minimal_pdf_bytes())

    assert result.status is JobState.QUEUED
    assert result.document_id == "docref-001"
    assert len(result.sha256) == 64
    assert result.page_count >= 1
    assert isinstance(result.job_id, uuid.UUID)
    assert queue.is_committed
    assert len(fhir_client.requests) == 1
    sent = fhir_client.requests[0]
    assert sent.mime_type is AcceptedMimeType.PDF
    assert sent.patient_id == "Patient/87413"
    # The agent_jobs row exists and is queued.
    assert len(queue.rows) == 1
    row = next(iter(queue.rows.values()))
    assert row.state == "queued"
    assert row.document_id == "docref-001"


async def test_handle_upload_rejects_oversized() -> None:
    parameters = UploadParameters(max_upload_bytes=128, max_attempts=5)
    with pytest.raises(UploadSizeError):
        await _run_upload(body=_minimal_pdf_bytes() + b"x" * 1024, parameters=parameters)


async def test_handle_upload_rejects_empty() -> None:
    with pytest.raises(UploadSizeError):
        await _run_upload(body=b"")


async def test_handle_upload_rejects_wrong_mime() -> None:
    with pytest.raises(UploadMimeError):
        await _run_upload(body=b"plain text body, not a clinical document\n")


async def test_handle_upload_rejects_eicar() -> None:
    """An EICAR-tainted PDF is rejected by the virus scanner step."""
    body = _minimal_pdf_bytes() + b"\n" + EICAR_TEST_BYTES + b"\n"
    with pytest.raises(UploadVirusError):
        await _run_upload(body=body)


async def test_handle_upload_rolls_back_queue_when_fhir_fails() -> None:
    """A failed FHIR write must not leave a queued row behind."""

    class FailingFhir(StubFhirClient):
        async def create(self, request):  # type: ignore[override]
            raise UploadFhirWriteError("simulated 502")

    queue = FakeQueueConnection()
    with pytest.raises(UploadFhirWriteError):
        await _run_upload(
            body=_minimal_pdf_bytes(),
            fhir_client=FailingFhir(),
            queue_conn=queue,
        )
    # No commit happened, no row exists.
    assert queue.commits == 0
    assert queue.rows == {}


async def test_handle_upload_rolls_back_when_queue_fails() -> None:
    """If the queue insert fails the connection rolls back; FHIR row may exist
    but is reported via UploadQueueError so the operator can reconcile."""

    class BrokenQueue(FakeQueueConnection):
        def execute(self, sql, params=None):  # type: ignore[override]
            if "INSERT INTO agent_jobs" in sql:
                raise RuntimeError("simulated queue insert failure")
            return super().execute(sql, params)

    fhir = StubFhirClient(document_id="docref-orphan")
    with pytest.raises(UploadQueueError):
        await _run_upload(
            body=_minimal_pdf_bytes(),
            fhir_client=fhir,
            queue_conn=BrokenQueue(),
        )
    # FHIR was called (we need the doc id before the queue insert), but
    # the queue insert never landed.
    assert len(fhir.requests) == 1


# ─── Queue layer direct ───────────────────────────────────────────────


def test_compute_backoff_seconds_is_exponential_with_cap() -> None:
    assert compute_backoff_seconds(0) == 1
    assert compute_backoff_seconds(1) == 2
    assert compute_backoff_seconds(2) == 4
    assert compute_backoff_seconds(10) == 1024
    # Cap at 60 minutes.
    assert compute_backoff_seconds(40) == 60 * 60


def test_compute_backoff_rejects_negative() -> None:
    with pytest.raises(ValueError):
        compute_backoff_seconds(-1)


def test_enqueue_lease_complete_round_trip() -> None:
    queue = FakeQueueConnection()
    request = EnqueueRequest(
        document_id="docref-99",
        patient_id="Patient/87413",
        doc_type=DocType.LAB_PDF,
        source=UploadSource.UPLOAD,
        sha256_hex="a" * 64,
        byte_size=4096,
        max_attempts=3,
    )
    queued = enqueue_job(queue, request)
    assert queued.attempt_count == 0

    leased = lease_one_job(queue)
    assert leased is not None
    assert leased.document_id == "docref-99"
    assert leased.attempt_count == 1

    complete_job(queue, leased.job_id)
    row = queue.rows[str(leased.job_id)]
    assert row.state == "done"


def test_fail_job_schedules_retry_then_dead_letters() -> None:
    queue = FakeQueueConnection()
    request = EnqueueRequest(
        document_id="docref-d",
        patient_id="Patient/87413",
        doc_type=DocType.LAB_PDF,
        source=UploadSource.UPLOAD,
        sha256_hex="a" * 64,
        byte_size=10,
        max_attempts=2,
    )
    enqueue_job(queue, request)

    # First failure: queued for retry.
    leased = lease_one_job(queue)
    assert leased is not None
    state = fail_job(queue, leased.job_id, "test_failure", "first")
    assert state is JobState.QUEUED

    # Walk the row's next_attempt_at backwards so the next lease can fire.
    row = queue.rows[str(leased.job_id)]
    row.next_attempt_at = datetime.now(tz=timezone.utc) - timedelta(seconds=1)

    # Second failure: max_attempts reached, dead_letter.
    leased2 = lease_one_job(queue)
    assert leased2 is not None
    state = fail_job(queue, leased2.job_id, "test_failure", "second")
    assert state is JobState.DEAD_LETTER
    assert queue.rows[str(leased2.job_id)].state == "dead_letter"


# ─── Worker loop ──────────────────────────────────────────────────────


async def test_worker_runs_one_iteration_happy_path() -> None:
    queue = FakeQueueConnection()
    enqueue_job(
        queue,
        EnqueueRequest(
            document_id="docref-w1",
            patient_id="Patient/87413",
            doc_type=DocType.LAB_PDF,
            source=UploadSource.UPLOAD,
            sha256_hex="a" * 64,
            byte_size=42,
            max_attempts=3,
        ),
    )

    extracted = []

    async def extract(job):
        extracted.append(job.document_id)

    ran = await run_one_iteration(conn=queue, extract_fn=extract)
    assert ran is True
    assert extracted == ["docref-w1"]
    row = next(iter(queue.rows.values()))
    assert row.state == "done"


async def test_worker_returns_false_when_queue_empty() -> None:
    queue = FakeQueueConnection()

    async def extract(job):
        raise AssertionError("must not be called")

    ran = await run_one_iteration(conn=queue, extract_fn=extract)
    assert ran is False


async def test_worker_marks_failed_on_typed_ingest_error() -> None:
    queue = FakeQueueConnection()
    enqueue_job(
        queue,
        EnqueueRequest(
            document_id="docref-w2",
            patient_id="Patient/87413",
            doc_type=DocType.LAB_PDF,
            source=UploadSource.UPLOAD,
            sha256_hex="a" * 64,
            byte_size=42,
            max_attempts=2,
        ),
    )

    async def extract(job):
        raise UploadFhirWriteError("simulated transient")

    ran = await run_one_iteration(conn=queue, extract_fn=extract)
    assert ran is True
    # First failure leaves it queued for retry (not dead-letter).
    row = next(iter(queue.rows.values()))
    assert row.state == "queued"
    assert "simulated transient" in (row.last_error or "")


async def test_worker_dead_letters_when_max_attempts_hit() -> None:
    queue = FakeQueueConnection()
    enqueue_job(
        queue,
        EnqueueRequest(
            document_id="docref-w3",
            patient_id="Patient/87413",
            doc_type=DocType.LAB_PDF,
            source=UploadSource.UPLOAD,
            sha256_hex="a" * 64,
            byte_size=42,
            max_attempts=1,
        ),
    )

    async def extract(job):
        raise UploadFhirWriteError("permanent")

    ran = await run_one_iteration(conn=queue, extract_fn=extract)
    assert ran is True
    row = next(iter(queue.rows.values()))
    assert row.state == "dead_letter"


# ─── Idempotency ──────────────────────────────────────────────────────


async def test_re_running_extract_on_done_row_does_not_create_duplicates() -> None:
    """A worker that re-claims a finished row never gets to run extract.

    The lease query filters on ``state='queued'``; a ``done`` row is
    invisible to ``lease_one_job`` so an idempotent extractor is never
    re-invoked against work the system already considers complete.
    """
    queue = FakeQueueConnection()
    enqueue_job(
        queue,
        EnqueueRequest(
            document_id="docref-i1",
            patient_id="Patient/87413",
            doc_type=DocType.LAB_PDF,
            source=UploadSource.UPLOAD,
            sha256_hex="a" * 64,
            byte_size=42,
            max_attempts=3,
        ),
    )

    invocations = 0

    async def extract(job):
        nonlocal invocations
        invocations += 1

    await run_one_iteration(conn=queue, extract_fn=extract)
    await run_one_iteration(conn=queue, extract_fn=extract)
    await run_one_iteration(conn=queue, extract_fn=extract)
    assert invocations == 1


# ─── Hypothesis property test ─────────────────────────────────────────

try:
    from hypothesis import HealthCheck, given, settings as hypothesis_settings, strategies as st
    _hypothesis_available = True
except ImportError:  # pragma: no cover — hypothesis is optional
    _hypothesis_available = False


if _hypothesis_available:

    @given(blob=st.binary(min_size=0, max_size=2048))
    @hypothesis_settings(
        max_examples=50,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
        deadline=None,
    )
    def test_handle_upload_never_crashes_on_random_bytes(blob: bytes) -> None:
        """Random input must always either succeed (vanishingly rare) or
        raise a typed IngestError. Crashing with a non-IngestError is a bug.

        We exercise the full handler with a stub scanner and stub FHIR. The
        contract is: any failure path raises an IngestError subclass, never
        a bare KeyError, IndexError, AttributeError, etc.
        """

        async def run():
            try:
                await _run_upload(body=blob)
            except IngestError:
                return  # acceptable
            except (RecursionError, MemoryError):
                raise

        asyncio.run(run())
