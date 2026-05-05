"""Postgres-backed job queue for the document ingest pipeline.

Three operations the rest of the pipeline calls:

- ``enqueue_job`` — INSERT a row in state ``queued``. Called from the
  upload handler inside the same transaction as the FHIR DocumentReference
  write so we never leak orphan rows.
- ``lease_one_job`` — ``SELECT ... FOR UPDATE SKIP LOCKED`` one ready
  row, mark it ``running``, return it to the caller. Called from the
  worker loop.
- ``complete_job`` / ``fail_job`` / ``dead_letter_job`` — mark the row
  finished. Failures schedule the next attempt with exponential backoff;
  attempts at the cap transition to ``dead_letter`` and emit a span
  event.

Why a thin wrapper:

- Each operation is one short SQL statement. Inlining them across the
  pipeline would scatter the queue contract; a single module is easier
  to audit and exercise from tests.
- The wrapper hides the connection-pool concrete type so a future move
  from psycopg to asyncpg would be a one-file change.
- Backoff scheduling lives next to the SQL that uses it. Co-locating
  policy with mechanics keeps the retry rule findable.

Concurrency model:

- The queue is consumed by N worker processes. ``FOR UPDATE SKIP LOCKED``
  ensures two workers never see the same row.
- A worker that crashes mid-job leaves the row in ``running`` with the
  lock released. A reaper (not in this module) periodically transitions
  abandoned ``running`` rows back to ``queued`` after a configurable
  visibility timeout. The visibility timeout reaper is wired in Phase 10
  observability.
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Protocol

from sidecar.ingest.errors import UploadQueueError
from sidecar.ingest.types import DocType, JobState, QueuedJob, UploadSource


# Hard cap on backoff so a long-running outage does not push a job's
# next_attempt_at past a reasonable horizon. 60 minutes gives the
# operator time to fix a downstream issue without losing the retry.
MAX_BACKOFF_SECONDS = 60 * 60


def compute_backoff_seconds(attempt_count: int) -> int:
    """Exponential backoff: ``2 ** attempt`` seconds, capped.

    ``attempt_count`` is the count BEFORE the failed attempt is recorded
    (so the very first failure schedules a 1-second retry, the second a
    2-second retry, and so on). Capped at ``MAX_BACKOFF_SECONDS`` so we
    do not push the next attempt into a tomorrow-someone-else-debugs-it
    horizon.
    """
    if attempt_count < 0:
        raise ValueError(
            f"attempt_count must be >= 0, got {attempt_count}"
        )
    delay = 2 ** min(attempt_count, 30)
    return min(delay, MAX_BACKOFF_SECONDS)


class Connection(Protocol):
    """Subset of psycopg's connection we use.

    Tests inject a ``StubConnection`` that mirrors the same surface
    without a database.
    """

    def execute(self, sql: str, params: tuple[object, ...] | None = ...) -> "Cursor":
        ...

    def commit(self) -> None:
        ...

    def rollback(self) -> None:
        ...


class Cursor(Protocol):
    """Subset of psycopg's cursor we use."""

    def fetchone(self) -> tuple[object, ...] | None:
        ...

    def fetchall(self) -> list[tuple[object, ...]]:
        ...


@dataclass(frozen=True)
class EnqueueRequest:
    """Inputs needed to insert a queued row."""

    document_id: str
    patient_id: str
    doc_type: DocType
    source: UploadSource
    sha256_hex: str
    byte_size: int
    max_attempts: int = 5


@contextmanager
def open_connection(
    database_url: str | None = None,
) -> Iterator[Connection]:
    """Open a psycopg connection from a URL.

    The default URL comes from ``COPILOT_DATABASE_URL`` so the queue can
    be exercised by scripts without importing the settings object. Tests
    pass their own URL or substitute a stub directly.
    """
    try:
        import psycopg
    except ImportError as exc:
        raise UploadQueueError(
            "psycopg is not installed; install the postgres extra. "
            "Underlying ImportError: " + repr(exc)
        ) from exc

    url = database_url or os.environ.get("COPILOT_DATABASE_URL")
    if not url:
        raise UploadQueueError(
            "COPILOT_DATABASE_URL is not set; cannot open queue connection."
        )

    try:
        with psycopg.connect(url, autocommit=False) as conn:
            yield conn  # type: ignore[misc]
    except Exception as exc:
        raise UploadQueueError(
            f"failed to open Postgres connection: "
            f"{type(exc).__name__}: {exc!s}"
        ) from exc


def enqueue_job(
    conn: Connection,
    request: EnqueueRequest,
) -> QueuedJob:
    """Insert one queued row. Returns the parsed ``QueuedJob`` DTO.

    Does NOT commit. The upload handler commits after the FHIR write
    confirms, so this insert and the FHIR write succeed or fail together.
    """
    job_id = uuid.uuid4()
    enqueued_at = datetime.now(tz=timezone.utc)
    try:
        conn.execute(
            """
            INSERT INTO agent_jobs (
                job_id, document_id, patient_id, doc_type, source,
                sha256, byte_size, state, enqueued_at,
                attempt_count, max_attempts, next_attempt_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, 'queued', %s,
                0, %s, %s
            );
            """,
            (
                str(job_id),
                request.document_id,
                request.patient_id,
                request.doc_type.value,
                request.source.value,
                request.sha256_hex,
                request.byte_size,
                enqueued_at,
                request.max_attempts,
                enqueued_at,
            ),
        )
    except Exception as exc:
        raise UploadQueueError(
            f"INSERT INTO agent_jobs failed: "
            f"{type(exc).__name__}: {exc!s}"
        ) from exc

    return QueuedJob(
        job_id=job_id,
        document_id=request.document_id,
        patient_id=request.patient_id,
        doc_type=request.doc_type,
        source=request.source,
        sha256=request.sha256_hex,
        byte_size=request.byte_size,
        attempt_count=0,
        max_attempts=request.max_attempts,
        enqueued_at=enqueued_at,
    )


def lease_one_job(conn: Connection) -> QueuedJob | None:
    """Claim one ready job for processing, or return ``None`` if none ready.

    Uses ``FOR UPDATE SKIP LOCKED`` so concurrent workers each see a
    different row. The transaction must commit AFTER the job's work is
    finished (and ``complete_job`` / ``fail_job`` has been called); the
    lock is held for the duration of the lease.
    """
    try:
        cur = conn.execute(
            """
            UPDATE agent_jobs SET
                state='running',
                started_at=NOW(),
                attempt_count=attempt_count + 1
            WHERE job_id = (
                SELECT job_id FROM agent_jobs
                WHERE state='queued' AND next_attempt_at <= NOW()
                ORDER BY next_attempt_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING
                job_id, document_id, patient_id, doc_type, source,
                sha256, byte_size, attempt_count, max_attempts,
                enqueued_at;
            """
        )
    except Exception as exc:
        raise UploadQueueError(
            f"lease query failed: {type(exc).__name__}: {exc!s}"
        ) from exc

    row = cur.fetchone()
    if row is None:
        return None
    return QueuedJob(
        job_id=uuid.UUID(str(row[0])),
        document_id=str(row[1]),
        patient_id=str(row[2]),
        doc_type=DocType(str(row[3])),
        source=UploadSource(str(row[4])),
        sha256=str(row[5]),
        byte_size=int(row[6]),  # type: ignore[arg-type]
        attempt_count=int(row[7]),  # type: ignore[arg-type]
        max_attempts=int(row[8]),  # type: ignore[arg-type]
        enqueued_at=_coerce_datetime(row[9]),
    )


def complete_job(conn: Connection, job_id: uuid.UUID) -> None:
    """Mark a job ``done``. Caller commits the transaction afterwards."""
    try:
        conn.execute(
            """
            UPDATE agent_jobs
                SET state='done', finished_at=NOW(), last_error=NULL
            WHERE job_id=%s AND state='running';
            """,
            (str(job_id),),
        )
    except Exception as exc:
        raise UploadQueueError(
            f"complete_job UPDATE failed: "
            f"{type(exc).__name__}: {exc!s}"
        ) from exc


def fail_job(
    conn: Connection,
    job_id: uuid.UUID,
    error_code: str,
    error_message: str,
) -> JobState:
    """Mark the job ``failed`` (transient) or ``dead_letter`` (permanent).

    Returns the resulting state so the caller can emit the right span
    event without re-querying.
    """
    error_payload = json.dumps({"code": error_code, "message": error_message[:512]})

    try:
        # First check: have we hit max_attempts?
        cur = conn.execute(
            """
            SELECT attempt_count, max_attempts
            FROM agent_jobs
            WHERE job_id=%s AND state='running'
            FOR UPDATE;
            """,
            (str(job_id),),
        )
        row = cur.fetchone()
        if row is None:
            raise UploadQueueError(
                f"fail_job: job_id={job_id} not found in state='running'."
            )
        attempt_count = int(row[0])  # type: ignore[arg-type]
        max_attempts = int(row[1])  # type: ignore[arg-type]
        if attempt_count >= max_attempts:
            conn.execute(
                """
                UPDATE agent_jobs
                    SET state='dead_letter',
                        finished_at=NOW(),
                        last_error=%s::jsonb
                WHERE job_id=%s;
                """,
                (error_payload, str(job_id)),
            )
            return JobState.DEAD_LETTER

        backoff = compute_backoff_seconds(attempt_count)
        conn.execute(
            f"""
            UPDATE agent_jobs SET
                state='queued',
                last_error=%s::jsonb,
                next_attempt_at=NOW() + INTERVAL '{backoff} seconds'
            WHERE job_id=%s;
            """,
            (error_payload, str(job_id)),
        )
        return JobState.QUEUED
    except UploadQueueError:
        raise
    except Exception as exc:
        raise UploadQueueError(
            f"fail_job UPDATE failed: {type(exc).__name__}: {exc!s}"
        ) from exc


def queue_depth(conn: Connection) -> dict[str, int]:
    """Snapshot of ``state -> count`` for the dashboard.

    Cheap to call: the partial index on ``state='queued'`` and the
    ``agent_jobs_state_idx`` cover the dominant categories. The full
    aggregation runs in milliseconds even at million-row scale.
    """
    try:
        cur = conn.execute(
            "SELECT state, COUNT(*) FROM agent_jobs GROUP BY state;"
        )
    except Exception as exc:
        raise UploadQueueError(
            f"queue_depth aggregation failed: "
            f"{type(exc).__name__}: {exc!s}"
        ) from exc
    rows = cur.fetchall()
    counts: dict[str, int] = {state.value: 0 for state in JobState}
    for state, count in rows:
        counts[str(state)] = int(count)  # type: ignore[arg-type]
    return counts


def _coerce_datetime(value: object) -> datetime:
    """Coerce a Postgres timestamp value into a timezone-aware datetime.

    psycopg returns a ``datetime`` already, but the stub layer returns
    whatever the test wired up. Coerce defensively so a stub bug surfaces
    here, not three frames later.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    raise UploadQueueError(
        f"queue lease returned non-datetime value for enqueued_at: "
        f"type={type(value).__name__} value={value!r}"
    )


__all__ = [
    "Connection",
    "Cursor",
    "EnqueueRequest",
    "MAX_BACKOFF_SECONDS",
    "complete_job",
    "compute_backoff_seconds",
    "enqueue_job",
    "fail_job",
    "lease_one_job",
    "open_connection",
    "queue_depth",
]
