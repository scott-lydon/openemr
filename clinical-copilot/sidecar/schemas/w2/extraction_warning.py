"""Structured warning emitted when the extractor drops a candidate field.

The Week 2 invariant is that the agent reports gaps rather than guessing.
Every dropped field surfaces as an ``ExtractionWarning`` so a clinician
sees which fields the system could not read confidently.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


class ExtractionWarningCode(str, Enum):
    """Machine-readable warning code.

    The string values are stable across schema revisions so dashboards and
    eval cases can match on them. Add new codes by appending; never
    reuse an old value.
    """

    LOW_CONFIDENCE = "low_confidence"
    FIELD_MISSING = "field_missing"
    ILLEGIBLE = "illegible"
    AMBIGUOUS_UNIT = "ambiguous_unit"
    HANDWRITING_UNREADABLE = "handwriting_unreadable"
    TWO_PASS_DISAGREEMENT = "two_pass_disagreement"
    BBOX_DEGENERATE = "bbox_degenerate"
    SCHEMA_VIOLATION = "schema_violation"


class ExtractionWarning(BaseModel):
    """Structured warning attached to an extraction result.

    Each warning carries a code (machine), a message (human), and the
    specific field name that prompted the warning when the warning is
    field-scoped (codes such as ``LOW_CONFIDENCE`` and
    ``ILLEGIBLE``). Document-scoped warnings (such as a poor scan
    overall) leave ``field`` as ``None``.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    code: ExtractionWarningCode
    message: str
    field: str | None = None
