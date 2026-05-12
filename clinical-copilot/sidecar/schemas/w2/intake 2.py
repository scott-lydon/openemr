"""Strict schema for intake-form extraction (IntakeFormExtraction).

Intake forms in primary care are often handwritten on top of a printed
template. The Vision Language Model handles the mixed input in one pass.
We keep the schema narrow so hallucinated fields cause Pydantic
ValidationErrors rather than reaching the chart.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sidecar.schemas.w2.bbox import BoundingBox
from sidecar.schemas.w2.extraction_warning import ExtractionWarning

# Confidence floor for intake fields. Slightly stricter than labs because
# intake fields drive medication and allergy lists, and a wrong allergy can
# kill someone. Calibrated against handwritten intake fixtures.
INTAKE_FIELD_CONFIDENCE_FLOOR: float = 0.75


class SexCode(str, Enum):
    """HL7 administrative gender code subset.

    We use the four codes the chart accepts. ``UNKNOWN`` covers the case
    where the form did not include a sex field at all.
    """

    MALE = "M"
    FEMALE = "F"
    OTHER = "X"
    UNKNOWN = "unknown"


class _AnchoredField(BaseModel):
    """Mixin shape for a field that needs at least one anchor.

    Subclasses define their own data fields plus ``confidence``,
    ``source_quote``, and optionally ``bbox``. The validator here enforces
    the bbox-or-quote invariant once for every subclass.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    confidence: float = Field(ge=0.0, le=1.0)
    source_quote: str
    bbox: BoundingBox | None = None

    @model_validator(mode="after")
    def has_at_least_one_anchor(self) -> "_AnchoredField":
        if self.bbox is None and not self.source_quote.strip():
            raise ValueError(
                f"{type(self).__name__} arrived with neither a bbox nor a "
                f"source_quote. The citation contract requires at least one "
                f"anchor; the extractor should drop this candidate and emit "
                f"an ExtractionWarning instead."
            )
        return self

    @model_validator(mode="after")
    def confidence_meets_floor(self) -> "_AnchoredField":
        if self.confidence < INTAKE_FIELD_CONFIDENCE_FLOOR:
            raise ValueError(
                f"{type(self).__name__} arrived with confidence "
                f"{self.confidence:.3f}, below the floor of "
                f"{INTAKE_FIELD_CONFIDENCE_FLOOR:.2f}. The extractor should "
                f"have dropped this field; this is a bug in the extractor."
            )
        return self


class DemographicsBlock(BaseModel):
    """Patient demographics extracted from an intake form."""

    model_config = ConfigDict(extra="forbid", strict=True)

    full_name: str | None = None
    dob: date | None = None
    sex: SexCode = SexCode.UNKNOWN
    confidence: float = Field(ge=0.0, le=1.0)
    bbox: BoundingBox | None = None
    source_quote: str = ""


class MedicationField(_AnchoredField):
    """One current medication from the intake form."""

    name: str
    dose: str | None = None
    frequency: str | None = None
    rxnorm_code: str | None = None


class AllergyField(_AnchoredField):
    """One allergy entry from the intake form.

    ``reaction`` is free text: hives, anaphylaxis, GI upset, etc. We
    intentionally do not enum-restrict; the verifier downstream maps to
    the FHIR ``AllergyIntolerance.reaction.manifestation`` code system.
    """

    substance: str
    reaction: str | None = None
    severity: str | None = None


class FamilyHistoryField(_AnchoredField):
    """One family-history entry."""

    relation: str
    condition: str
    age_at_diagnosis: int | None = Field(default=None, ge=0, le=120)


class IntakeFormExtraction(BaseModel):
    """Top-level extraction result for one intake form.

    Demographic fields are aggregated into a single ``DemographicsBlock``.
    Lists capture multi-row sections. ``chief_concern`` is free text and
    is the most common channel for prompt-injection attacks; LLM Guard
    Layer 4 always scans this field before the planner sees it.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    document_id: str
    document_sha256: str = Field(min_length=64, max_length=64)
    patient_id: str
    page_count: int = Field(ge=1)
    extracted_at: datetime
    extracted_by_model: str
    prompt_version: str

    demographics: DemographicsBlock
    chief_concern: str | None = None
    current_medications: list[MedicationField] = Field(default_factory=list)
    allergies: list[AllergyField] = Field(default_factory=list)
    family_history: list[FamilyHistoryField] = Field(default_factory=list)

    extraction_warnings: list[ExtractionWarning] = Field(default_factory=list)
