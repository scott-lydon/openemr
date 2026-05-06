"""create citations table

Revision ID: 20260504_0002
Revises: 20260504_0001
Create Date: 2026-05-04

The citations table stores every machine-readable citation surfaced by
the agent. The verifier writes rows here when it accepts a claim; the
preview endpoint (``GET /agent-api/v1/citations/{citation_id}/preview.png``)
reads rows back when the user interface clicks through.

Storing citations in a row, not just the response packet:

- A citation is referenced by a stable URL, so the row needs to outlive
  the response. Inlined-only-in-response storage forces the user
  interface to keep the response object alive forever.
- Audit needs the citation rows for the FHIR Provenance write.
- Re-extraction can compare current citations to historical citations
  to detect a drift between extractor versions.

The ``bbox_json`` column is JSONB rather than five separate float
columns because:

- The bbox shape (page + 4 floats) is exactly one ``BoundingBox`` value.
  Splitting the value across columns reintroduces the assemble-from-pieces
  problem at every read.
- Future Vision Language Model (VLM) outputs may carry per-token bboxes,
  not per-field bboxes. JSONB lets the schema evolve without a migration.

Encounter scoping:

- ``encounter_id`` is the OpenEMR encounter the citation belongs to. The
  preview endpoint scope-checks the caller's task token against this row;
  cross-patient access is rejected with 403.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "20260504_0002"
down_revision: Union[str, None] = "20260504_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE citations (
            citation_id        UUID PRIMARY KEY,
            encounter_id       TEXT NOT NULL,
            patient_id         TEXT NOT NULL,
            source_type        TEXT NOT NULL CHECK (source_type IN (
                'DocumentReference','FhirResource','Guideline'
            )),
            source_id          TEXT NOT NULL,
            page               INT,
            section            TEXT,
            field_or_chunk_id  TEXT NOT NULL,
            quote_or_value     TEXT NOT NULL,
            bbox_json          JSONB,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            -- Document citations must carry either a bbox or a non-empty
            -- quote so the preview endpoint always has an anchor. The
            -- Pydantic schema enforces this at parse time; the database
            -- check is the second line of defense.
            CONSTRAINT citations_document_anchor_present CHECK (
                source_type <> 'DocumentReference'
                OR bbox_json IS NOT NULL
                OR length(trim(quote_or_value)) > 0
            )
        );
        """
    )
    op.execute(
        """
        CREATE INDEX citations_encounter_idx
            ON citations (encounter_id, created_at DESC);
        """
    )
    op.execute(
        """
        CREATE INDEX citations_patient_idx
            ON citations (patient_id, created_at DESC);
        """
    )
    op.execute(
        """
        CREATE INDEX citations_source_idx
            ON citations (source_type, source_id);
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS citations_source_idx;")
    op.execute("DROP INDEX IF EXISTS citations_patient_idx;")
    op.execute("DROP INDEX IF EXISTS citations_encounter_idx;")
    op.execute("DROP TABLE IF EXISTS citations;")
