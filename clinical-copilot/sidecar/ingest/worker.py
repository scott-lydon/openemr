"""Long-lived queue worker for the document ingest pipeline.

The worker leases one job at a time, runs the extraction pipeline against
it, and either marks it ``done`` (successful FHIR write-back) or
schedules a retry / dead letter.

Phase 2 wires the queue consumer skeleton with a pluggable
``ExtractFn`` so Phase 3 can drop in the Vision Language Model (VLM)
extractor without changing the worker loop. The skeleton is
deliberately minimal:

- Lease one row.
- Call ``extract_fn(job)``.
- On success: ``complete_job`` + commit.
- On typed ``IngestError``: ``fail_job`` (which decides queued vs
  dead_letter) + commit.
- Sleep ``poll_seconds`` if no row was ready.

Why a sleep rather than ``LISTEN``/``NOTIFY``: the queue is small enough
that polling at one-second intervals dominates nothing, and the polling
contract gives us a deterministic worst-case latency without requiring a
notification channel that adds an opportunity for a stuck listener to
silently miss work. Phase 10 may add a ``LISTEN``/``NOTIFY`` fast path
for sub-second latency without removing the polling fallback.

Idempotency:

- The Phase 3 extractor produces FHIR resource ids deterministically
  derived from ``sha256(document_id + page + field_id)``. Re-running an
  extractor against the same document produces the same resources, so
  a re-leased job after a partial completion does not duplicate.

Graceful shutdown:

- ``stop_event`` is set by a SIGTERM handler. The worker finishes the
  current job, commits, and exits. The handler is wired in Phase 12
  deployment hardening.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Final

from sidecar.ingest.errors import IngestError, UploadQueueError
from sidecar.ingest.queue import (
    Connection,
    complete_job,
    fail_job,
    lease_one_job,
)
from sidecar.ingest.types import JobState, QueuedJob


logger = logging.getLogger(__name__)


DEFAULT_POLL_SECONDS: Final[float] = 1.0


ExtractFn = Callable[[QueuedJob], Awaitable[None]]
"""The extractor signature.

The Phase 3 implementation will:

1. Read the FHIR ``DocumentReference`` for ``job.document_id``.
2. Render every page at 300 dpi and run the VLM extractor.
3. Persist the parsed fields back to FHIR.
4. Persist the citations table rows.

The function returns ``None`` on success or raises an ``IngestError``
subclass. Any other exception is wrapped in ``UploadQueueError`` so the
worker only ever has to handle one error family.
"""


async def run_one_iteration(
    *,
    conn: Connection,
    extract_fn: ExtractFn,
) -> bool:
    """Lease one job and process it. Return True if a job ran.

    Idempotent on transient failure: a failed lease/process is rolled
    back on the queue connection and the loop will pick the row up
    again on the next iteration (after the backoff).
    """
    try:
        job = lease_one_job(conn)
    except UploadQueueError:
        # Lease failure is a queue-layer issue (Postgres reachability,
        # transient lock conflict). Roll back the abandoned transaction
        # so the pool stays usable, and signal "no work" so the caller
        # sleeps before the next attempt.
        try:
            conn.rollback()
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("queue rollback after lease failure: %r", exc)
        raise

    if job is None:
        # No work available. Commit the empty transaction so we do not
        # hold an idle one open across the sleep window.
        try:
            conn.commit()
        except Exception:
            conn.rollback()
        return False

    try:
        await extract_fn(job)
    except IngestError as exc:
        logger.warning(
            "ingest job %s failed (attempt %d/%d): %s",
            job.job_id, job.attempt_count, job.max_attempts, exc,
        )
        try:
            new_state = fail_job(
                conn=conn,
                job_id=job.job_id,
                error_code=exc.code,
                error_message=str(exc),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        if new_state is JobState.DEAD_LETTER:
            logger.error(
                "ingest job %s landed in dead_letter after %d attempts",
                job.job_id, job.attempt_count,
            )
        return True
    except Exception as exc:
        # An unexpected exception (not a typed IngestError) is a worker
        # bug. We still mark the job failed so it is not held in
        # ``running`` forever; the operator inspects the trace and the
        # dead-letter row to root-cause.
        logger.exception("worker bug while processing job %s", job.job_id)
        try:
            fail_job(
                conn=conn,
                job_id=job.job_id,
                error_code="worker_internal_error",
                error_message=f"{type(exc).__name__}: {exc!s}"[:512],
            )
            conn.commit()
        except Exception:
            conn.rollback()
        return True

    try:
        complete_job(conn, job.job_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


async def run_worker_loop(
    *,
    conn: Connection,
    extract_fn: ExtractFn,
    stop_event: asyncio.Event,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> None:
    """Run until ``stop_event`` is set.

    Each iteration leases at most one job. The loop keeps a single
    connection because psycopg connections are cheap to hold open and
    the queue's leasing semantics depend on transaction state.
    """
    if poll_seconds <= 0:
        raise ValueError(
            f"poll_seconds must be positive, got {poll_seconds}"
        )

    while not stop_event.is_set():
        try:
            ran = await run_one_iteration(conn=conn, extract_fn=extract_fn)
        except UploadQueueError:
            # Queue-layer fault: sleep slightly longer to avoid hammering
            # a Postgres outage.
            await _sleep_or_stop(stop_event, poll_seconds * 5)
            continue
        if not ran:
            await _sleep_or_stop(stop_event, poll_seconds)


async def _sleep_or_stop(stop_event: asyncio.Event, seconds: float) -> None:
    """Sleep up to ``seconds``, waking early if the stop event fires.

    Lets the worker exit promptly on SIGTERM rather than wait out the
    poll interval after a shutdown signal arrives.
    """
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return


__all__ = [
    "DEFAULT_POLL_SECONDS",
    "ExtractFn",
    "run_one_iteration",
    "run_worker_loop",
]
