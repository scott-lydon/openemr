"""create agent_jobs queue table

Revision ID: 20260504_0001
Revises:
Create Date: 2026-05-04

The Postgres queue underpinning every Week 2 ingest job.

Why Postgres rather than Redis:

- We already run Postgres for the Retrieval Augmented Generation (RAG)
  index, so the queue adds no new infrastructure dependency.
- ``SELECT ... FOR UPDATE SKIP LOCKED`` gives us atomic, contention-free
  job leasing without a separate lock service.
- Visibility timeouts and dead-letter behavior are expressed as ordinary
  SQL columns, which makes the queue auditable (``SELECT *`` from a
  database administrator's terminal is a complete state dump).
- The same transaction can both insert a queue row and write a
  correlation row, so the upload handler atomically commits (FHIR write,
  queue insert) and never leaks orphan DocumentReferences when the
  process crashes mid-upload.

State machine:

    queued ──► running ──► done
       │          │
       │          └────► failed (transient)
       │                     │
       │                     └────► requeued (next_attempt_at += backoff)
       │                                  │
       │                                  └────► dead_letter (max_attempts hit)
       │
       └────► cancelled (operator action)

The CHECK constraint enumerates every valid state so a typo in the
worker code raises at insert time rather than producing a row no other
worker can interpret.

Indexes:

- ``agent_jobs_ready_idx``: partial index on ``(state, next_attempt_at)``
  filtered to ``state='queued'``. The worker's leasing query is
  ``WHERE state='queued' AND next_attempt_at <= NOW()``; the partial
  index makes the planner pick a single index scan over a tiny tuple set
  even when the table has millions of finished rows.
- ``agent_jobs_patient_idx``: lookup by patient for the per-patient
  status panel; ordered descending by enqueue time because the panel
  shows newest first.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20260504_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE agent_jobs (
            job_id           UUID PRIMARY KEY,
            document_id      TEXT NOT NULL,
            patient_id       TEXT NOT NULL,
            doc_type         TEXT NOT NULL CHECK (doc_type IN (
                'lab_pdf','intake_form','referral_fax'
            )),
            source           TEXT NOT NULL CHECK (source IN (
                'upload','fax','portal'
            )),
            sha256           CHAR(64) NOT NULL,
            byte_size        BIGINT NOT NULL CHECK (byte_size > 0),
            state            TEXT NOT NULL CHECK (state IN (
                'queued','running','done','failed','dead_letter','cancelled'
            )),
            enqueued_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            started_at       TIMESTAMPTZ,
            finished_at      TIMESTAMPTZ,
            last_error       JSONB,
            attempt_count    INT  NOT NULL DEFAULT 0
                             CHECK (attempt_count >= 0),
            max_attempts     INT  NOT NULL DEFAULT 5
                             CHECK (max_attempts >= 1),
            next_attempt_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """
    )
    # Partial index: only the rows the worker leases (queued + ready).
    # Reduces the index size from O(N) to O(currently-queued) and stays
    # tiny even with millions of finished rows.
    op.execute(
        """
        CREATE INDEX agent_jobs_ready_idx
            ON agent_jobs (next_attempt_at)
            WHERE state = 'queued';
        """
    )
    op.execute(
        """
        CREATE INDEX agent_jobs_patient_idx
            ON agent_jobs (patient_id, enqueued_at DESC);
        """
    )
    # Operational filter: counting failures and dead letters per hour for
    # the dashboard. Without this index the count query would full-scan.
    op.execute(
        """
        CREATE INDEX agent_jobs_state_idx
            ON agent_jobs (state, finished_at DESC);
        """
    )


def downgrade() -> None:
    # Defensive ordering: drop indexes first, then the table. PostgreSQL
    # drops indexes implicitly on DROP TABLE, but listing them explicitly
    # documents the schema artifacts the migration owns.
    op.execute("DROP INDEX IF EXISTS agent_jobs_state_idx;")
    op.execute("DROP INDEX IF EXISTS agent_jobs_patient_idx;")
    op.execute("DROP INDEX IF EXISTS agent_jobs_ready_idx;")
    op.execute("DROP TABLE IF EXISTS agent_jobs;")
