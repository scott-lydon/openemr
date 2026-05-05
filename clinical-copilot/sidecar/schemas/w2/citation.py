"""Machine-readable citation contract.

Every clinical claim emitted by the agent carries a ``Citation`` so the
verifier and the user interface can resolve back to the source. The
contract is stricter than the rubric's minimum: a ``DocumentReference``
citation must carry either a bounding box or a non-empty ``quote_or_value``
so the citation preview endpoint always has something to highlight.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, model_validator

from sidecar.schemas.w2.bbox import BoundingBox


class CitationSourceType(str, Enum):
    """Where the cited claim came from.

    ``DOCUMENT_REFERENCE`` — uploaded clinical document (lab PDF, intake
    form, faxed referral). The citation usually carries a bounding box;
    if it does not, the preview endpoint falls back to fuzzy quote search.

    ``FHIR_RESOURCE`` — a chart-resident row (Observation,
    AllergyIntolerance, Condition, MedicationRequest, etc.). The citation
    carries the resource identifier; the user interface highlights the
    matching row in the chart panel.

    ``GUIDELINE`` — a chunk from the Retrieval Augmented Generation (RAG)
    corpus. The citation carries a chunk identifier and a deep link to
    the public source page.
    """

    DOCUMENT_REFERENCE = "DocumentReference"
    FHIR_RESOURCE = "FhirResource"
    GUIDELINE = "Guideline"


class Citation(BaseModel):
    """One citation attached to one clinical claim.

    The minimum-shape contract from the rubric is
    ``{source_type, source_id, page_or_section, field_or_chunk_id,
    quote_or_value}``. We extend it with an optional ``BoundingBox``
    because the visual click-through is the rubric's required user
    interface affordance.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    source_type: CitationSourceType
    source_id: str
    page_or_section: str | int
    field_or_chunk_id: str
    quote_or_value: str
    bbox: BoundingBox | None = None

    @model_validator(mode="after")
    def document_citations_have_an_anchor(self) -> "Citation":
        """A document citation must have either a bbox or a non-empty quote.

        Without one of those the citation preview endpoint has nothing to
        highlight, and the click-through fails. Reject the citation at
        parse time so the verifier never has to handle it.
        """
        if self.source_type is CitationSourceType.DOCUMENT_REFERENCE:
            if self.bbox is None and not self.quote_or_value.strip():
                raise ValueError(
                    f"DocumentReference citation has neither a bounding box "
                    f"nor a quote_or_value. The Vision Language Model returned "
                    f"a citation that cannot be previewed; either reject the "
                    f"underlying claim or add at least one anchor. "
                    f"source_id={self.source_id!r}, page_or_section={self.page_or_section!r}."
                )
        return self
