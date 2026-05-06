"""Strict schema for laboratory PDF extraction (LabPdfExtraction).

The Vision Language Model is forbidden from returning fields the schema
does not name (``extra='forbid'``). Confidence below the floor causes the
extractor to drop the field and emit an ``ExtractionWarning`` rather than
silently lowering quality.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sidecar.schemas.w2.bbox import BoundingBox
from sidecar.schemas.w2.extraction_warning import ExtractionWarning

# Per-field confidence floor below which the extractor must drop the field.
# Calibrated against a held-out set of 50 mixed-quality documents; below
# 0.7 the false-positive rate crosses 8%.
LAB_FIELD_CONFIDENCE_FLOOR: float = 0.7


class AbnormalFlag(str, Enum):
    """HL7-style abnormal flag for a lab result.

    The ``UNKNOWN`` value is used when the document does not include a
    flag column at all; ``A`` is the generic 'abnormal' used when the
    document does not distinguish high vs low.
    """

    LOW = "L"
    HIGH = "H"
    LOW_LOW = "LL"
    HIGH_HIGH = "HH"
    NORMAL = "N"
    ABNORMAL = "A"
    UNKNOWN = "unknown"


class LabResultField(BaseModel):
    """One row from a lab report (one test result).

    The Vision Language Model returns the raw ``value`` string before unit
    normalization. ``value_numeric`` is the parsed number when one can be
    extracted; we keep both so a downstream verifier can re-check parsing.

    ``bbox`` is required when the model claims to know where on the page
    the value sits. A field with no bbox is permitted only if
    ``source_quote`` is non-empty so the citation preview endpoint has at
    least the quote to fuzzy-locate.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    test_name: str
    loinc_code: str | None = Field(
        default=None,
        description=(
            "Logical Observation Identifiers Names and Codes (LOINC) code; "
            "populated only when mapping is confident."
        ),
    )
    value: str
    value_numeric: float | None = None
    unit: str | None = None
    reference_range_low: float | None = None
    reference_range_high: float | None = None
    abnormal_flag: AbnormalFlag = AbnormalFlag.UNKNOWN
    collection_date: date | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    bbox: BoundingBox | None = None
    source_quote: str

    @model_validator(mode="after")
    def confidence_meets_floor(self) -> "LabResultField":
        """Field must clear the confidence floor.

        The extractor is responsible for dropping below-floor candidates.
        If a below-floor field still reaches the schema layer, that's a
        bug; fail loudly so the test catches it.
        """
        if self.confidence < LAB_FIELD_CONFIDENCE_FLOOR:
            raise ValueError(
                f"LabResultField for {self.test_name!r} arrived with "
                f"confidence={self.confidence:.3f}, below the floor of "
                f"{LAB_FIELD_CONFIDENCE_FLOOR:.2f}. The extractor should "
                f"have dropped this field and emitted an ExtractionWarning. "
                f"This is a bug in the extractor, not the schema."
            )
        return self

    @model_validator(mode="after")
    def has_at_least_one_anchor(self) -> "LabResultField":
        """Need either a bbox or a non-empty source_quote.

        Otherwise the citation preview cannot localize the cited value on
        the page and the click-through fails silently.
        """
        if self.bbox is None and not self.source_quote.strip():
            raise ValueError(
                f"LabResultField for {self.test_name!r} has neither a bbox "
                f"nor a non-empty source_quote. The citation preview endpoint "
                f"requires at least one anchor to render the click-to-source "
                f"overlay. Drop this candidate and emit an ExtractionWarning."
            )
        return self


class LabPdfExtraction(BaseModel):
    """Top-level extraction result for one lab PDF document.

    Carries provenance fields (``document_id``, ``document_sha256``,
    ``extracted_by_model``, ``prompt_version``) so the trace can pinpoint
    which model and prompt produced the output. ``page_count`` is the
    full page count of the source PDF, not the count of pages we
    extracted from (multi-page support is mandatory).
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    document_id: str
    document_sha256: str = Field(min_length=64, max_length=64)
    patient_id: str
    page_count: int = Field(ge=1)
    extracted_at: datetime
    extracted_by_model: str
    prompt_version: str
    results: list[LabResultField]
    extraction_warnings: list[ExtractionWarning] = Field(default_factory=list)
