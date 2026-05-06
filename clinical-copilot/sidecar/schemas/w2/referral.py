"""Strict schema for referral-fax extraction (Phase 11 extension).

Mirrors ``lab.py`` and ``intake.py`` for consistency. The persistence
path (Phase 11) writes ``ServiceRequest`` plus ``Communication``
resources for each extraction.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sidecar.schemas.w2.bbox import BoundingBox
from sidecar.schemas.w2.extraction_warning import ExtractionWarning


REFERRAL_FIELD_CONFIDENCE_FLOOR: float = 0.7


class ReferralFaxExtraction(BaseModel):
    """One referral fax, extracted into its structured fields.

    Free-form fields are kept as text; structured fields are typed.
    Per-field source quotes anchor the citation contract.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    document_id: str
    document_sha256: str = Field(min_length=64, max_length=64)
    patient_id: str
    page_count: int = Field(ge=1)
    extracted_at: datetime
    extracted_by_model: str
    prompt_version: str

    referring_provider: str | None = None
    referring_provider_npi: str | None = None
    reason_for_referral: str
    requested_service: str
    prior_authorization_indicated: bool = False
    attached_documents: list[str] = Field(default_factory=list)
    appointment_by: date | None = None

    confidence: float = Field(ge=0.0, le=1.0)
    bbox: BoundingBox | None = None
    source_quote: str = ""

    extraction_warnings: list[ExtractionWarning] = Field(default_factory=list)

    @model_validator(mode="after")
    def confidence_meets_floor(self) -> "ReferralFaxExtraction":
        if self.confidence < REFERRAL_FIELD_CONFIDENCE_FLOOR:
            raise ValueError(
                f"ReferralFaxExtraction arrived with confidence "
                f"{self.confidence:.3f}, below floor "
                f"{REFERRAL_FIELD_CONFIDENCE_FLOOR:.2f}; the extractor "
                "should drop and emit an ExtractionWarning."
            )
        return self


__all__ = ["REFERRAL_FIELD_CONFIDENCE_FLOOR", "ReferralFaxExtraction"]
