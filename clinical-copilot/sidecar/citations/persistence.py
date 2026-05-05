"""Postgres operations against the ``citations`` table.

The table was landed in Phase 2's migration ``20260504_0002``. This
module provides CRUD wrappers the verifier and the preview endpoint
call.

Each operation is one short Structured Query Language (SQL) statement;
inlining them across the codebase would scatter the citation contract.
A single module owns the SQL surface so a future schema migration is a
single-file change.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Protocol

from sidecar.schemas.w2.bbox import BoundingBox
from sidecar.schemas.w2.citation import Citation, CitationSourceType


logger = logging.getLogger(__name__)


class CitationRowNotFound(Exception):
    """The citation_id does not exist in the table."""


class CitationsConnection(Protocol):
    """Subset of a database connection used by this module."""

    def execute(self, sql: str, params: tuple[object, ...] | None = ...) -> "CitationsCursor":
        ...

    def commit(self) -> None:
        ...


class CitationsCursor(Protocol):
    def fetchone(self) -> tuple[object, ...] | None:
        ...


@dataclass(frozen=True)
class CitationRow:
    """One row from the ``citations`` table.

    Mirrors the schema. Exposed because the preview endpoint needs the
    bbox + quote anchors AND the patient/encounter ids for scope check.
    """

    citation_id: uuid.UUID
    encounter_id: str
    patient_id: str
    source_type: CitationSourceType
    source_id: str
    page: int | None
    section: str | None
    field_or_chunk_id: str
    quote_or_value: str
    bbox: BoundingBox | None


def insert_citation(
    conn: CitationsConnection,
    *,
    encounter_id: str,
    patient_id: str,
    citation: Citation,
) -> uuid.UUID:
    """Insert one citation row. Returns the assigned ``citation_id``.

    Caller commits the transaction afterwards. Raising leaves the
    transaction in the rolled-back state for the caller to handle.
    """
    citation_id = uuid.uuid4()
    bbox_json: str | None = None
    page_value: int | None = None
    section_value: str | None = None
    if isinstance(citation.page_or_section, int):
        page_value = citation.page_or_section
    else:
        section_value = str(citation.page_or_section)

    if citation.bbox is not None:
        bbox_json = json.dumps(citation.bbox.model_dump(mode="json"))
        if page_value is None:
            page_value = citation.bbox.page

    conn.execute(
        """
        INSERT INTO citations (
            citation_id, encounter_id, patient_id, source_type, source_id,
            page, section, field_or_chunk_id, quote_or_value, bbox_json
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb);
        """,
        (
            str(citation_id),
            encounter_id,
            patient_id,
            citation.source_type.value,
            citation.source_id,
            page_value,
            section_value,
            citation.field_or_chunk_id,
            citation.quote_or_value,
            bbox_json,
        ),
    )
    return citation_id


def get_citation(
    conn: CitationsConnection,
    *,
    citation_id: uuid.UUID,
) -> CitationRow:
    """Fetch one row by id; raise ``CitationRowNotFound`` when absent."""
    cur = conn.execute(
        """
        SELECT citation_id, encounter_id, patient_id, source_type, source_id,
               page, section, field_or_chunk_id, quote_or_value, bbox_json
        FROM citations WHERE citation_id = %s;
        """,
        (str(citation_id),),
    )
    row = cur.fetchone()
    if row is None:
        raise CitationRowNotFound(
            f"citation_id={citation_id} not found"
        )

    bbox: BoundingBox | None = None
    if row[9] is not None:
        bbox_payload = row[9] if isinstance(row[9], dict) else json.loads(str(row[9]))
        bbox = BoundingBox.model_validate(bbox_payload, strict=False)

    return CitationRow(
        citation_id=uuid.UUID(str(row[0])),
        encounter_id=str(row[1]),
        patient_id=str(row[2]),
        source_type=CitationSourceType(str(row[3])),
        source_id=str(row[4]),
        page=int(row[5]) if row[5] is not None else None,  # type: ignore[arg-type]
        section=str(row[6]) if row[6] is not None else None,
        field_or_chunk_id=str(row[7]),
        quote_or_value=str(row[8]),
        bbox=bbox,
    )


__all__ = [
    "CitationRow",
    "CitationRowNotFound",
    "CitationsConnection",
    "CitationsCursor",
    "get_citation",
    "insert_citation",
]
