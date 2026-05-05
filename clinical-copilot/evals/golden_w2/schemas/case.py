"""Strict schema for one golden eval case.

Every case in ``cases/*.jsonl`` (and the legacy ``seed_cases.jsonl``) is
validated against this schema before the harness runs it. A schema
violation is a build break, not a runtime warning, because a malformed
case would mask a real regression as a parse error.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ExpectedRubric(BaseModel):
    """The pass/fail criteria the harness checks against the agent's
    response.

    Most fields are optional; a case fills only the criteria it cares
    about. The harness's threshold checker bins each criterion into a
    rubric category for the regression report.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    extracted_fields: list[dict[str, object]] | None = None
    must_cite_document_reference: bool | None = None
    must_cite_document_reference_count_min: int | None = Field(default=None, ge=1)
    must_cite_guideline_chunk_in: list[str] | None = None
    must_not_contain_phi_in_logs: bool = True
    must_flag_contradiction: bool | None = None
    expected_refusal: bool | None = None
    expected_refusal_must_mention: str | None = None
    expected_warning_codes: list[str] | None = None
    expected_sanitization_layer: list[str] | None = None
    response_must_not_contain: list[str] | None = None
    max_latency_ms_p95: int | None = Field(default=None, ge=1)
    max_cost_usd: float | None = Field(default=None, ge=0.0)


class GoldenCase(BaseModel):
    """One eval case.

    The ``id`` is stable across edits to other fields, so a regression
    report can name the failing case unambiguously.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: str = Field(min_length=4, max_length=32)
    tags: list[str] = Field(min_length=1)
    failure_mode_targeted: str = Field(min_length=10)
    rationale: str = Field(min_length=10)
    patient_id: str = Field(min_length=1)
    documents: list[str] = Field(default_factory=list)
    user_question: str = Field(min_length=1)
    expected: ExpectedRubric


__all__ = ["ExpectedRubric", "GoldenCase"]
